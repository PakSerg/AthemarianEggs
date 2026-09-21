"""
Розыгрыш призов: еженедельный (чек) и главный (билеты).

Публичный результат — dict из DrawResult.to_dict():
  status, draw_type, winners_count, has_errors, message, run_id, ok
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Callable

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone

from ..models import (
    Prize,
    PromotionDrawResult,
    PromotionDrawResultMainRaffle,
    Receipt,
    User,
)
from . import prize_limits
from .notify_bot import notify as notify_bot
from .promo_calendar import month_num_on_date, week_num_on_date

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    'success': '✅ Успешно',
    'partial': '⚠️ Частично',
    'failed': '❌ Не удалось',
    'empty': '⭕ Пустой пул',
    'error': '💥 Ошибка',
}

_DRAW_TYPE_LABELS = {
    'weekly': 'Еженедельный розыгрыш',
    'monthly': 'Ежемесячный розыгрыш',
    'main': 'Главный розыгрыш',
}

_STEP_LABELS = {
    'award': 'сохранение',
    'email': 'email',
    'oki_doki': 'OkiDoki',
    'fatal': 'критическая',
}


def _notify_draw_report(result: DrawResult) -> None:
    """Краткий отчёт о розыгрыше через forward-signal."""
    data = result.to_dict()
    status_line = _STATUS_LABELS.get(data['status'], data['status'])
    title = _DRAW_TYPE_LABELS.get(data['draw_type'], data['draw_type'])

    lines = [
        title,
        f'Run ID: {data["run_id"]}',
        f'Статус: {status_line}',
        f'Победителей: {data["winners_count"]}',
        f'Есть ошибки: {"да" if data["has_errors"] else "нет"}',
        data['message'] or '',
    ]

    if data['draw_type'] in ('weekly', 'monthly'):
        lines.append(f'Пул: призов {data.get("prizes_pool", "—")}, чеков {data.get("receipts_pool", "—")}')
    else:
        lines.append(f'Пул: призов {data.get("prizes_pool", "—")}, билетов {data.get("participants_pool", "—")}')

    errors = data.get('failures') or []
    if errors:
        lines.append('')
        lines.append(f'Ошибки ({len(errors)}):')
        for idx, err in enumerate(errors[:10], start=1):
            step = _STEP_LABELS.get(err.get('step'), err.get('step'))
            lines.append(
                f'{idx}. {step} · p={err.get("participant_id")} · '
                f'prize={err.get("prize_id")} · receipt={err.get("receipt_id")}'
            )
            lines.append(f'   {err.get("error") or err.get("message")}')
        if len(errors) > 10:
            lines.append(f'…ещё {len(errors) - 10}, см. логи run_id={data["run_id"]}')

    notify_bot('\n'.join(lines))

# Минимум зарегистрированных чеков для участия в розыгрыше. В этой Акции
# ограничений нет (бриф, п. 5: «Лимит чеков на участника — нет»), поэтому
# достаточно одного подтверждённого чека. Значения оставлены отдельными
# константами: если Заказчик введёт порог, менять нужно ровно здесь.
MIN_RECEIPTS_FOR_MONTHLY = 1
MIN_RECEIPTS_FOR_MAIN = 1

# Бриф, п. 8: на каждого победителя определяются два резервных.
RESERVE_WINNERS_PER_PRIZE = 2


class DrawType(str, Enum):
    WEEKLY = 'weekly'
    MONTHLY = 'monthly'
    MAIN = 'main'


class DrawStatus(str, Enum):
    SUCCESS = 'success'   # все победители сохранены
    PARTIAL = 'partial'   # победители есть, но были сбои при назначении
    FAILED = 'failed'     # ни одного победителя не сохранено
    EMPTY = 'empty'       # нечего разыгрывать
    ERROR = 'error'       # падение всего процесса


@dataclass
class DrawError:
    step: str
    message: str
    participant_id: int | None = None
    prize_id: int | None = None
    receipt_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'step': self.step,
            'error': self.message,
            'participant_id': self.participant_id,
            'prize_id': self.prize_id,
            'receipt_id': self.receipt_id,
        }


@dataclass
class DrawResult:
    status: DrawStatus
    draw_type: DrawType
    winners_count: int
    has_errors: bool
    message: str
    run_id: str
    pool: dict[str, int] = field(default_factory=dict)
    errors: list[DrawError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in (DrawStatus.SUCCESS, DrawStatus.PARTIAL) and self.winners_count > 0

    def to_dict(self) -> dict[str, Any]:
        """Совместимость с tasks, API и логами."""
        return {
            'status': self.status.value,
            'draw_type': self.draw_type.value,
            'winners_count': self.winners_count,
            'has_errors': self.has_errors,
            'message': self.message,
            'run_id': self.run_id,
            'ok': self.ok,
            'success_count': self.winners_count,
            'failed_count': len(self.errors),
            'failures': [e.to_dict() for e in self.errors],
            'prizes_pool': self.pool.get('prizes'),
            'receipts_pool': self.pool.get('receipts'),
            'participants_pool': self.pool.get('participants'),
            'raffle_id': self.pool.get('raffle_id'),
        }

    @classmethod
    def empty(cls, draw_type: DrawType, run_id: str, *, pool: dict, reason: str) -> DrawResult:
        return cls(
            status=DrawStatus.EMPTY,
            draw_type=draw_type,
            winners_count=0,
            has_errors=False,
            message=reason,
            run_id=run_id,
            pool=pool,
        )

    @classmethod
    def fatal(cls, draw_type: DrawType, run_id: str, exc: Exception, *, pool: dict | None = None) -> DrawResult:
        return cls(
            status=DrawStatus.ERROR,
            draw_type=draw_type,
            winners_count=0,
            has_errors=True,
            message=str(exc),
            run_id=run_id,
            pool=pool or {},
            errors=[DrawError(step='fatal', message=str(exc))],
        )

    @classmethod
    def from_run(
        cls,
        draw_type: DrawType,
        run_id: str,
        *,
        pool: dict,
        winners_count: int,
        errors: list[DrawError],
        empty_reason: str,
    ) -> DrawResult:
        if winners_count <= 0:
            msg = empty_reason if not errors else 'Не удалось сохранить ни одного победителя'
            status = DrawStatus.EMPTY if not errors else DrawStatus.FAILED
            return cls(
                status=status,
                draw_type=draw_type,
                winners_count=0,
                has_errors=bool(errors),
                message=msg,
                run_id=run_id,
                pool=pool,
                errors=errors,
            )

        has_errors = bool(errors)
        if not has_errors:
            status = DrawStatus.SUCCESS
            message = (
                'Розыгрыш выполнен: победители назначены. '
                'Опубликуйте их в разделе «Итоги розыгрышей». '
                'OkiDoki и письма — вручную в админке.'
            )
        else:
            status = DrawStatus.PARTIAL
            message = f'Розыгрыш завершён: победителей {winners_count}, ошибок {len(errors)}'

        return cls(
            status=status,
            draw_type=draw_type,
            winners_count=winners_count,
            has_errors=has_errors,
            message=message,
            run_id=run_id,
            pool=pool,
            errors=errors,
        )


def get_week_num_raffle(raffle) -> int:
    """
    Номер текущей недели Акции. Недели — календарные (пн–вс), см. promo_calendar:
    еженедельный розыгрыш разыгрывает предыдущую неделю (week_num - 1).
    """
    return week_num_on_date(raffle, timezone.localtime().date())


def get_month_num_raffle(raffle) -> int:
    """
    Номер месяца акции от даты старта (1 — календарный месяц старта акции,
    независимо от того, каким числом месяца акция стартовала).
    """
    return month_num_on_date(raffle, timezone.localtime().date())


def _new_run_id() -> str:
    return str(uuid.uuid4())[:8]


def _shuffle(items: list) -> list:
    shuffled = list(items)
    secrets.SystemRandom().shuffle(shuffled)
    return shuffled


def _participant_label(participant) -> str:
    return participant.email or participant.phone or f'id:{participant.pk}'


def _finalize(result: DrawResult) -> dict[str, Any]:
    print(
        'draw finished type=%s status=%s run_id=%s winners=%s has_errors=%s',
        result.draw_type.value,
        result.status.value,
        result.run_id,
        result.winners_count,
        result.has_errors,
    )
    _notify_draw_report(result)
    return result.to_dict()


def _min_receipt_date_for_draw() -> datetime:
    min_date = getattr(settings, 'RAFFLE_RECEIPT_MIN_DATE', date(2026, 10, 1))
    if isinstance(min_date, datetime):
        if timezone.is_aware(min_date):
            return timezone.localtime(min_date)
        return timezone.make_aware(min_date, timezone.get_current_timezone())
    return timezone.make_aware(
        datetime.combine(min_date, time.min),
        timezone.get_current_timezone(),
    )


def _used_receipt_ids() -> set[int]:
    """
    Чеки, которые уже принесли приз (не резервный) в любом из розыгрышей —
    моментальном, еженедельном, ежемесячном или главном: один чек — один приз.

    Моментальные призы сюда входят намеренно: выигравший в лотке чек переводится
    в статус «Победный» и дальше везёт договор и приз победителя (см.
    instant_prizes._award_instant_prize). Оставить его и в пуле еженедельного
    розыгрыша значило бы повесить на один чек два договора.
    """
    weekly_monthly = PromotionDrawResult.objects.filter(
        is_reserve=False, receipt_id__isnull=False,
    ).values_list('receipt_id', flat=True)
    main = PromotionDrawResultMainRaffle.objects.filter(
        is_reserve=False, receipt_id__isnull=False,
    ).values_list('receipt_id', flat=True)
    return set(weekly_monthly) | set(main)


def _weekly_pools(raffle) -> tuple[list, list]:
    """Пул призов и чеков ИМЕННО прошедшей недели розыгрыша (неделя = week_num - 1).

    Ограничение этой Акции (см. prize_limits): участник может выиграть не более
    одного приза в еженедельных и ежемесячных розыгрышах ВМЕСТЕ ВЗЯТЫХ за всю
    Акцию. Поэтому из пула сразу исключаются все, кто уже выигрывал — и на
    прошлых неделях, и в ежемесячном розыгрыше. Моментальный приз в этот счёт
    не входит: он живёт по своему, отдельному лимиту.
    """
    week_num = get_week_num_raffle(raffle)
    target_week = week_num - 1
    print(
        'weekly draw pools: raffle_id=%s start_date=%s -> current week_num=%s target_week=%s',
        raffle.pk, raffle.start_date, week_num, target_week,
    )

    prizes_qs = Prize.objects.filter(
        is_active=True,
        is_main=False,
        draw_period=Prize.DrawPeriod.WEEKLY,
        week=target_week,
    )
    prizes = []
    for prize in prizes_qs:
        prizes.extend([prize] * (prize.count or 0))
    print(
        'weekly draw pools: week=%s -> %s типов призов, %s призовых слотов всего: %s',
        target_week, prizes_qs.count(), len(prizes),
        [(p.pk, p.name if hasattr(p, "name") else p.pk, p.count) for p in prizes_qs],
    )

    used_ids = _used_receipt_ids()
    already_won_ids = prize_limits.regular_winner_ids()

    receipts_list = []
    set_participants = set()
    receipts_qs = Receipt.objects.select_related('participant').filter(
        status=Receipt.Status.CONFIRMED,
        week=target_week,
    ).exclude(participant_id__in=already_won_ids)

    raw_count = 0
    for receipt in receipts_qs:
        raw_count += 1
        if receipt.id in used_ids:
            print(
                'weekly draw pools: чек id=%s уже принёс приз ранее (п. 7.1 Правил), пропущен',
                receipt.pk,
            )
            continue
        if receipt.participant.id not in set_participants:
            set_participants.add(receipt.participant.id)
            receipts_list.append(receipt)
        else:
            print(
                'weekly draw pools: participant id=%s уже есть в пуле, '
                'чек id=%s (week=%s) пропущен (1 участник = 1 билет в неделю)',
                receipt.participant.id, receipt.pk, receipt.week,
            )

    print(
        'weekly draw pools: week=%s -> найдено %s подтверждённых чеков, '
        '%s уникальных участников после дедупликации',
        target_week, raw_count, len(receipts_list),
    )

    return prizes, receipts_list


def _monthly_pools(raffle) -> tuple[list, list]:
    """Пул призов и чеков ИМЕННО прошедшего месяца розыгрыша (месяц = month_num - 1).

    Как и в еженедельном розыгрыше, из пула исключаются участники, уже
    выигравшие любой еженедельный или ежемесячный приз (см. prize_limits).
    """
    month_num = get_month_num_raffle(raffle)
    target_month = month_num - 1
    print(
        'monthly draw pools: raffle_id=%s start_date=%s -> current month_num=%s target_month=%s',
        raffle.pk, raffle.start_date, month_num, target_month,
    )

    prizes_qs = Prize.objects.filter(
        is_active=True,
        is_main=False,
        draw_period=Prize.DrawPeriod.MONTHLY,
        month=target_month,
    )
    prizes = []
    for prize in prizes_qs:
        prizes.extend([prize] * (prize.count or 0))
    print(
        'monthly draw pools: month=%s -> %s типов призов, %s призовых слотов всего: %s',
        target_month, prizes_qs.count(), len(prizes),
        [(p.pk, p.name if hasattr(p, "name") else p.pk, p.count) for p in prizes_qs],
    )

    used_ids = _used_receipt_ids()

    already_won_ids = prize_limits.regular_winner_ids()

    receipts_by_participant: dict[int, list] = defaultdict(list)
    receipts_qs = Receipt.objects.select_related('participant').filter(
        status=Receipt.Status.CONFIRMED,
        month=target_month,
    ).exclude(participant_id__in=already_won_ids)
    for receipt in receipts_qs:
        receipts_by_participant[receipt.participant_id].append(receipt)

    receipts_list = []
    for participant_id, receipts in receipts_by_participant.items():
        if len(receipts) < MIN_RECEIPTS_FOR_MONTHLY:
            print(
                'monthly draw pools: participant id=%s зарегистрировал только %s чек(ов) '
                'в месяце %s (нужно %s+), пропущен',
                participant_id, len(receipts), target_month, MIN_RECEIPTS_FOR_MONTHLY,
            )
            continue
        candidate = next((r for r in receipts if r.id not in used_ids), None)
        if candidate is None:
            print(
                'monthly draw pools: participant id=%s — все чеки месяца %s уже принесли приз, пропущен',
                participant_id, target_month,
            )
            continue
        receipts_list.append(candidate)

    print(
        'monthly draw pools: month=%s -> %s участников с %s+ чеками, %s билетов в пуле',
        target_month, len(receipts_by_participant), MIN_RECEIPTS_FOR_MONTHLY, len(receipts_list),
    )

    return prizes, receipts_list


def _main_pools() -> tuple[list, list]:
    """Пул призов и чеков Главного розыгрыша.

    Билетами являются ВСЕ подтверждённые чеки участника за весь срок, кроме
    чеков, по которым уже вручён приз, — то есть больше чеков увеличивает шансы
    на победу.

    Главный розыгрыш по умолчанию считается отдельной корзиной призов: победа в
    еженедельном розыгрыше холодильник не отменяет. Это поведение переключается
    одной константой в prize_limits (MAIN_DRAW_SHARES_REGULAR_LIMIT), и пул
    подстроится сам — он спрашивает именно prize_limits, а не повторяет условие.
    """
    # Главных призов в Акции пять (холодильники), и разыгрываются они одним
    # прогоном: призовых слотов ровно столько, сколько единиц приза заведено —
    # так же, как в еженедельном розыгрыше.
    prizes = []
    for prize in Prize.objects.filter(is_active=True, is_main=True):
        prizes.extend([prize] * (prize.count or 0))

    used_ids = _used_receipt_ids()

    total_by_participant = dict(
        Receipt.objects.filter(status=Receipt.Status.CONFIRMED)
        .values('participant_id')
        .annotate(total=Count('id'))
        .values_list('participant_id', 'total')
    )

    eligible_participant_ids = {
        participant_id
        for participant_id, total in total_by_participant.items()
        if total >= MIN_RECEIPTS_FOR_MAIN and prize_limits.can_win_main(participant_id)
    }

    tickets = list(
        Receipt.objects.select_related('participant').filter(
            status=Receipt.Status.CONFIRMED,
            participant_id__in=eligible_participant_ids,
        ).exclude(pk__in=used_ids)
    )

    print(
        'main draw pools: %s участников с %s+ чеками, %s билетов (неиспользованных чеков) в пуле',
        len(eligible_participant_ids), MIN_RECEIPTS_FOR_MAIN, len(tickets),
    )

    return prizes, tickets


def _award_weekly(prize, receipt, *, raffle=None) -> tuple[PromotionDrawResult, Any, Prize, Receipt]:
    with transaction.atomic():
        receipt = (
            Receipt.objects.select_for_update()
            .select_related('participant')
            .get(pk=receipt.pk)
        )
        participant = receipt.participant

        if receipt.status != Receipt.Status.CONFIRMED:
            raise ValueError(f'Чек {receipt.pk} не подтверждён')
        if PromotionDrawResult.objects.filter(receipt=receipt, is_reserve=False).exists():
            raise ValueError(f'Чек {receipt.pk} уже приносил приз ранее (п. 7.1 Правил)')
        if not prize_limits.can_win_regular(participant.pk):
            raise ValueError(
                f'Участник {participant.pk} уже выигрывал приз в еженедельном или '
                f'ежемесячном розыгрыше — второй такой приз Правилами не предусмотрен'
            )

        draw_result = PromotionDrawResult.objects.create(
            participant=participant,
            prize=prize,
            receipt=receipt,
            week_num=receipt.week,
            is_published=False,
        )
        receipt.is_participation = True
        receipt.is_month_participation = True
        receipt.save(update_fields=['is_participation', 'is_month_participation', 'updated_at'])

    return draw_result, participant, prize, receipt


def _award_monthly(prize, receipt, *, raffle=None) -> tuple[PromotionDrawResult, Any, Prize, Receipt]:
    with transaction.atomic():
        receipt = (
            Receipt.objects.select_for_update()
            .select_related('participant')
            .get(pk=receipt.pk)
        )
        participant = receipt.participant

        if receipt.status != Receipt.Status.CONFIRMED:
            raise ValueError(f'Чек {receipt.pk} не подтверждён')
        if PromotionDrawResult.objects.filter(receipt=receipt, is_reserve=False).exists():
            raise ValueError(f'Чек {receipt.pk} уже приносил приз ранее (п. 7.1 Правил)')
        if not prize_limits.can_win_regular(participant.pk):
            raise ValueError(
                f'Участник {participant.pk} уже выигрывал приз в еженедельном или '
                f'ежемесячном розыгрыше — второй такой приз Правилами не предусмотрен'
            )

        draw_result = PromotionDrawResult.objects.create(
            participant=participant,
            prize=prize,
            receipt=receipt,
            month_num=receipt.month,
            is_published=False,
        )
        receipt.is_participation = True
        receipt.is_month_participation = True
        receipt.save(update_fields=['is_participation', 'is_month_participation', 'updated_at'])

    return draw_result, participant, prize, receipt


def _award_main(prize, receipt) -> tuple[PromotionDrawResultMainRaffle, User, Prize]:
    with transaction.atomic():
        receipt = (
            Receipt.objects.select_for_update()
            .select_related('participant')
            .get(pk=receipt.pk)
        )
        participant = receipt.participant

        if receipt.status != Receipt.Status.CONFIRMED:
            raise ValueError(f'Чек {receipt.pk} не подтверждён')
        if not prize_limits.can_win_main(participant.pk):
            raise ValueError(f'Участник {participant.pk} уже выигрывал Главный приз')
        if PromotionDrawResult.objects.filter(receipt=receipt, is_reserve=False).exists():
            raise ValueError(f'Чек {receipt.pk} уже приносил приз ранее (п. 7.1 Правил)')
        if PromotionDrawResultMainRaffle.objects.filter(receipt=receipt, is_reserve=False).exists():
            raise ValueError(f'Чек {receipt.pk} уже приносил приз ранее (п. 7.1 Правил)')

        draw_result = PromotionDrawResultMainRaffle.objects.create(
            participant=participant,
            prize=prize,
            receipt=receipt,
        )

    return draw_result, participant, prize


def _award_weekly_reserve(prize, receipt, rank: int) -> PromotionDrawResult:
    return PromotionDrawResult.objects.create(
        participant=receipt.participant,
        prize=prize,
        receipt=receipt,
        week_num=receipt.week,
        is_published=False,
        is_reserve=True,
        reserve_rank=rank,
    )


def _award_monthly_reserve(prize, receipt, rank: int) -> PromotionDrawResult:
    return PromotionDrawResult.objects.create(
        participant=receipt.participant,
        prize=prize,
        receipt=receipt,
        month_num=receipt.month,
        is_published=False,
        is_reserve=True,
        reserve_rank=rank,
    )


def _award_main_reserve(prize, receipt, rank: int) -> PromotionDrawResultMainRaffle:
    return PromotionDrawResultMainRaffle.objects.create(
        participant=receipt.participant,
        prize=prize,
        receipt=receipt,
        is_reserve=True,
        reserve_rank=rank,
    )


def _build_reserve_groups(count: int, leftover_receipts: list) -> list[list[tuple[int, Any]]]:
    """
    Распределяет оставшиеся (не выигравшие) билеты по резервным местам для каждого
    призового слота — по кругу, сначала все первые резервы, затем все вторые и т.д.
    (Правила, п. 8.4: резервные победители определяются одновременно с основными.)
    """
    groups: list[list[tuple[int, Any]]] = [[] for _ in range(count)]
    if count <= 0:
        return groups
    idx = 0
    for rank in range(1, RESERVE_WINNERS_PER_PRIZE + 1):
        for slot in range(count):
            if idx >= len(leftover_receipts):
                return groups
            groups[slot].append((rank, leftover_receipts[idx]))
            idx += 1
    return groups


def _run_draw(
    draw_type: DrawType,
    raffle,
    *,
    load_pools: Callable[[], tuple[list, list]],
    process_pair: Callable[[Any, Any, str, Any], list[DrawError]],
    process_reserve: Callable[[Any, Any, int], None],
    empty_message: str,
    pool_keys: tuple[str, str],
) -> dict[str, Any]:
    run_id = _new_run_id()
    pool: dict[str, Any] = {'raffle_id': getattr(raffle, 'pk', None)}

    try:
        pool_a, pool_b = load_pools()
        pool[pool_keys[0]] = len(pool_a)
        pool[pool_keys[1]] = len(pool_b)

        count = min(len(pool_a), len(pool_b))
        if count <= 0:
            return _finalize(DrawResult.empty(draw_type, run_id, pool=pool, reason=empty_message))

        shuffled_prizes = _shuffle(pool_a)
        shuffled_tickets = _shuffle(pool_b)

        # Один участник получает не больше одного приза за прогон. В недельном и
        # месячном пулах участник и так встречается один раз, а вот в Главном
        # розыгрыше у него столько билетов, сколько чеков (больше чеков — больше
        # шансов), и без этой проверки пять холодильников могли бы уехать втроём.
        pairs = []
        taken_participant_ids = set()
        for ticket in shuffled_tickets:
            if len(pairs) >= count:
                break
            participant_id = getattr(ticket, 'participant_id', None)
            if participant_id is not None and participant_id in taken_participant_ids:
                continue
            taken_participant_ids.add(participant_id)
            pairs.append((shuffled_prizes[len(pairs)], ticket))
        count = len(pairs)
        if count <= 0:
            return _finalize(DrawResult.empty(draw_type, run_id, pool=pool, reason=empty_message))

        # Билет уже выигравшего участника не может быть его же резервом (актуально для
        # Главного розыгрыша, где один участник может держать несколько билетов).
        # Участники, выигравшие в предыдущих розыгрышах, в пул не попали вовсе —
        # см. _weekly_pools/_monthly_pools/_main_pools.
        winner_participant_ids = {
            getattr(ticket, 'participant_id', None) for _, ticket in pairs
        }
        reserve_candidates = [
            ticket for ticket in shuffled_tickets
            if getattr(ticket, 'participant_id', None) not in winner_participant_ids
        ]
        reserve_groups = _build_reserve_groups(count, reserve_candidates)

        winners_count = 0
        reserve_count = 0
        all_errors: list[DrawError] = []

        for slot, (left, right) in enumerate(pairs):
            try:
                pair_errors = process_pair(left, right, run_id, raffle)
                winners_count += 1
                all_errors.extend(pair_errors)
            except Exception as exc:
                print('draw %s run_id=%s: award failed', draw_type.value, run_id)
                participant = getattr(right, 'participant', right)
                receipt = right
                all_errors.append(DrawError(
                    step='award',
                    message=str(exc),
                    participant_id=getattr(participant, 'pk', None),
                    prize_id=getattr(left, 'pk', None),
                    receipt_id=getattr(receipt, 'pk', None) if receipt else None,
                ))
                continue

            for rank, reserve_ticket in reserve_groups[slot]:
                try:
                    process_reserve(left, reserve_ticket, rank)
                    reserve_count += 1
                except Exception:
                    logger.exception(
                        'draw %s run_id=%s: не удалось сохранить резервного победителя (slot=%s rank=%s)',
                        draw_type.value, run_id, slot, rank,
                    )

        result = DrawResult.from_run(
            draw_type,
            run_id,
            pool=pool,
            winners_count=winners_count,
            errors=all_errors,
            empty_reason=empty_message,
        )
        result.pool['reserves'] = reserve_count
        return _finalize(result)

    except Exception as exc:
        print('draw %s fatal run_id=%s', draw_type.value, run_id)
        return _finalize(DrawResult.fatal(draw_type, run_id, exc, pool=pool))


def _process_weekly_pair(prize, receipt, run_id: str, raffle=None) -> list[DrawError]:
    _award_weekly(prize, receipt, raffle=raffle)
    return []


def _process_monthly_pair(prize, receipt, run_id: str, raffle=None) -> list[DrawError]:
    _award_monthly(prize, receipt, raffle=raffle)
    return []


def _process_main_pair(prize, receipt, run_id: str, raffle=None) -> list[DrawError]:
    _award_main(prize, receipt)
    return []


def _execute_draw_result(raffle=None) -> dict[str, Any]:
    """Еженедельный розыгрыш по подтверждённым чекам ТЕКУЩЕЙ недели."""
    if raffle is None:
        raise ValueError('Для еженедельного розыгрыша обязателен объект raffle (нужен для week_num)')

    week_num = get_week_num_raffle(raffle)
    print('=== старт еженедельного розыгрыша: raffle_id=%s, week_num=%s ===', raffle.pk, week_num)

    return _run_draw(
        DrawType.WEEKLY,
        raffle,
        load_pools=lambda: _weekly_pools(raffle),
        process_pair=_process_weekly_pair,
        process_reserve=lambda prize, receipt, rank: _award_weekly_reserve(prize, receipt, rank),
        empty_message=f'Недостаточно призов или чеков для розыгрыша на неделе {week_num}',
        pool_keys=('prizes', 'receipts'),
    )



def _execute_draw_result_monthly(raffle=None) -> dict[str, Any]:
    """Ежемесячный розыгрыш по подтверждённым чекам ЗАВЕРШИВШЕГОСЯ месяца."""
    if raffle is None:
        raise ValueError('Для ежемесячного розыгрыша обязателен объект raffle (нужен для month_num)')

    month_num = get_month_num_raffle(raffle)
    print('=== старт ежемесячного розыгрыша: raffle_id=%s, month_num=%s ===', raffle.pk, month_num)

    return _run_draw(
        DrawType.MONTHLY,
        raffle,
        load_pools=lambda: _monthly_pools(raffle),
        process_pair=_process_monthly_pair,
        process_reserve=lambda prize, receipt, rank: _award_monthly_reserve(prize, receipt, rank),
        empty_message=f'Недостаточно призов или чеков для розыгрыша в месяце {month_num}',
        pool_keys=('prizes', 'receipts'),
    )


def _execute_draw_result_main_raffle(raffle=None) -> dict[str, Any]:
    """Главный розыгрыш по билетам (билет = неиспользованный чек участника с 3+ чеками)."""
    print('=== старт главного розыгрыша: raffle_id=%s ===', getattr(raffle, 'pk', None))
    return _run_draw(
        DrawType.MAIN,
        raffle,
        load_pools=_main_pools,
        process_pair=_process_main_pair,
        process_reserve=lambda prize, receipt, rank: _award_main_reserve(prize, receipt, rank),
        empty_message='Недостаточно участников или призов для главного розыгрыша',
        pool_keys=('prizes', 'participants'),
    )


def _execute_draw_result_response(raffle=None):
    result = _execute_draw_result(raffle)
    if result['ok']:
        return JsonResponse({
            'status': result['status'],
            'winners_count': result['winners_count'],
            'has_errors': result['has_errors'],
            'message': result['message'],
            'run_id': result['run_id'],
        }, status=200)
    status = 400 if result['status'] == DrawStatus.EMPTY.value else 500
    return JsonResponse({
        'status': result['status'],
        'error': result['message'],
        'has_errors': result['has_errors'],
        'run_id': result['run_id'],
    }, status=status)


def _execute_draw_result_monthly_response(raffle=None):
    result = _execute_draw_result_monthly(raffle)
    if result['ok']:
        return JsonResponse({
            'status': result['status'],
            'winners_count': result['winners_count'],
            'has_errors': result['has_errors'],
            'message': result['message'],
            'run_id': result['run_id'],
        }, status=200)
    status = 400 if result['status'] == DrawStatus.EMPTY.value else 500
    return JsonResponse({
        'status': result['status'],
        'error': result['message'],
        'has_errors': result['has_errors'],
        'run_id': result['run_id'],
    }, status=status)


def _execute_draw_result_main_raffle_response(raffle=None):
    result = _execute_draw_result_main_raffle(raffle)
    if result['ok']:
        return JsonResponse({
            'status': result['status'],
            'winners_count': result['winners_count'],
            'has_errors': result['has_errors'],
            'message': result['message'],
            'run_id': result['run_id'],
        }, status=200)
    status = 400 if result['status'] == DrawStatus.EMPTY.value else 500
    return JsonResponse({
        'status': result['status'],
        'error': result['message'],
        'has_errors': result['has_errors'],
        'run_id': result['run_id'],
    }, status=status)


DRAW_WEEKLY = DrawType.WEEKLY.value
DRAW_MONTHLY = DrawType.MONTHLY.value
DRAW_MAIN = DrawType.MAIN.value
