"""
Замена победителя в уже разыгранном призовом слоте.

Зачем: победитель может не выйти на связь и не подписать договор — приз в этом
случае передаётся другому участнику (п. 8.4 Правил). Слот розыгрыша при этом
остаётся прежним (тот же приз, та же неделя/месяц), меняется только участник.

Два режима:

* автоматический — система сама выбирает участника, подходящего ПОД ТЕ ЖЕ
  условия, что и прежний победитель (чек нужного периода, нужное количество
  чеков, чек ещё не приносил приз, участник ещё не выигрывал в этом периоде);
  сначала берутся резервные победители этого же периода по очереди резерва,
  затем — случайный участник из подходящих;
* ручной — менеджер выбирает участника сам: в списке сначала идут подходящие
  под условия, затем любые другие участники акции.

Вся история замен хранится в promotion.models.WinnerReplacement — итог розыгрыша
можно заменять сколько угодно раз, цепочка сохраняется целиком.

Важно: замена НИКОГДА не меняет то, что видно на лендинге. Первоначальный
победитель фиксируется в PromotionDrawResult.public_participant при первой же
замене, и блок «Победители» на сайте показывает именно его
(см. main.services.get_winners_by_months).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from promotion.messages import ReceiptMessage
from promotion.models import (
    PromotionDrawResult,
    PromotionDrawResultMainRaffle,
    Receipt,
    User,
    WinnerReplacement,
    WinnerReplacementSettings,
)
from promotion.services.draw_result import MIN_RECEIPTS_FOR_MAIN, MIN_RECEIPTS_FOR_MONTHLY
from promotion.services.oki_doki import cancel_oki_contract

CANDIDATE_LIMIT = 40

DRAW_KIND_INSTANT = 'instant'
DRAW_KIND_WEEKLY = 'weekly'
DRAW_KIND_MONTHLY = 'monthly'
DRAW_KIND_MAIN = 'main'


class WinnerReplacementError(Exception):
    """Ошибка, текст которой можно показать менеджеру как есть."""


# ─────────────────────────── общие помощники ───────────────────────────

def participant_label(participant) -> str:
    if participant is None:
        return '—'
    full_name = f'{participant.last_name or ""} {participant.first_name or ""}'.strip()
    email = (participant.email or '').strip()
    if full_name and email:
        return f'{full_name} ({email})'
    return full_name or email or f'id:{participant.pk}'


def receipt_label(receipt) -> str:
    if receipt is None:
        return '—'
    parts = [f'Чек #{receipt.pk}']
    if receipt.date:
        parts.append(timezone.localtime(receipt.date).strftime('%d.%m.%Y'))
    if receipt.amount is not None:
        parts.append(f'{receipt.amount} ₽')
    return ', '.join(parts)


def draw_kind(row) -> str:
    """Тип розыгрыша строки победителя — от него зависят условия отбора."""
    if row.kind == 'main':
        return DRAW_KIND_MAIN
    if getattr(row, 'is_instant', False):
        return DRAW_KIND_INSTANT
    if getattr(row, 'month_num', None) is not None:
        return DRAW_KIND_MONTHLY
    return DRAW_KIND_WEEKLY


def sign_deadline_days() -> int:
    """Срок на подпись договора (дней) — глобальная настройка дашборда. 0 — без ограничения."""
    return WinnerReplacementSettings.load().sign_deadline_days


def _days_since(dt) -> int:
    return (timezone.now() - dt).days


def deadline_status(row) -> dict | None:
    """
    Статус срока на подпись для этого победителя, либо None, если срок не
    действует (письмо ещё не отправлено или ограничение выключено).

    Срок отсчитывается от отправки письма с договором, а не от создания
    договора: пока победитель не увидел договор, ждать нечего.
    """
    deadline = sign_deadline_days()
    if not deadline or not row.is_send_email or not row.email_sent_at:
        return None
    elapsed = _days_since(row.email_sent_at)
    return {
        'deadline_days': deadline,
        'elapsed_days': elapsed,
        'remaining_days': max(deadline - elapsed, 0),
        'expired': elapsed >= deadline,
    }


def can_replace(row) -> bool:
    """
    Заменить можно, пока победитель не подписал договор (после подписи приз за
    ним закреплён юридически) и пока не истёк срок на подпись после отправки
    письма с договором (см. deadline_status). Если письмо ещё не отправлено,
    срок не действует — заменить можно в любой момент.
    """
    if row.contract_status == 'signed':
        return False
    status = deadline_status(row)
    return status is None or status['expired']


def replace_block_reason(row) -> str:
    if row.contract_status == 'signed':
        return 'Договор уже подписан — заменить победителя нельзя.'
    status = deadline_status(row)
    if status and not status['expired']:
        sent_label = timezone.localtime(row.email_sent_at).strftime('%d.%m.%Y %H:%M')
        return (
            f'Письмо с договором отправлено {sent_label} — победителю даётся '
            f'{status["deadline_days"]} дн. на подпись, срок ещё не истёк '
            f'(осталось {status["remaining_days"]} дн.). Заменить победителя можно будет по истечении срока.'
        )
    return ''


# ─────────────────────────── условия отбора ───────────────────────────

def _used_receipt_ids(exclude_row=None) -> set[int]:
    """
    Чеки, которые уже принесли приз (не резервный) в любом розыгрыше —
    п. 7.1 Правил «один чек — один приз». Чек самого заменяемого итога
    из множества исключается: он освобождается этой же заменой.
    """
    weekly = PromotionDrawResult.objects.filter(is_reserve=False, receipt_id__isnull=False)
    main = PromotionDrawResultMainRaffle.objects.filter(is_reserve=False, receipt_id__isnull=False)
    if exclude_row is not None:
        if exclude_row.kind == 'main':
            main = main.exclude(pk=exclude_row.pk)
        else:
            weekly = weekly.exclude(pk=exclude_row.pk)
    return set(weekly.values_list('receipt_id', flat=True)) | set(main.values_list('receipt_id', flat=True))


def _busy_participant_ids(row) -> set[int]:
    """
    Участники, которые уже выиграли в ЭТОМ же периоде розыгрыша: п. 7.2/7.3
    Правил — не больше одного приза в рамках одного еженедельного и одного
    ежемесячного розыгрыша. Сам заменяемый победитель в множество не попадает.
    """
    kind = draw_kind(row)
    if kind == DRAW_KIND_MAIN:
        qs = PromotionDrawResultMainRaffle.objects.filter(is_reserve=False).exclude(pk=row.pk)
        return set(qs.values_list('participant_id', flat=True))

    qs = PromotionDrawResult.objects.filter(is_reserve=False).exclude(pk=row.pk)
    if kind == DRAW_KIND_INSTANT:
        # Моментальный приз — один на участника за всю Акцию, периода у него нет.
        qs = qs.filter(is_instant=True)
    elif kind == DRAW_KIND_MONTHLY:
        qs = qs.filter(month_num=row.month_num)
    else:
        qs = qs.filter(week_num=row.week_num)
    return set(qs.values_list('participant_id', flat=True))


def _reserve_ranks(row) -> dict[int, int]:
    """Резервные победители этого же слота: participant_id → очередь резерва."""
    if row.kind == 'main':
        qs = PromotionDrawResultMainRaffle.objects.filter(is_reserve=True)
    else:
        qs = PromotionDrawResult.objects.filter(is_reserve=True)
        kind = draw_kind(row)
        if kind == DRAW_KIND_INSTANT:
            # У моментального приза резервных победителей не бывает: он выдаётся
            # в момент игры, а не по расписанию, и «очереди» за ним нет.
            qs = qs.none()
        elif kind == DRAW_KIND_MONTHLY:
            qs = qs.filter(month_num=row.month_num)
        else:
            qs = qs.filter(week_num=row.week_num)
        if row.prize is not None:
            qs = qs.filter(prize_id=row.prize.pk)

    ranks: dict[int, int] = {}
    for participant_id, rank in qs.values_list('participant_id', 'reserve_rank'):
        rank = rank or 99
        if participant_id not in ranks or rank < ranks[participant_id]:
            ranks[participant_id] = rank
    return ranks


@dataclass
class Candidate:
    participant: object
    receipt: object | None
    is_eligible: bool
    note: str
    reserve_rank: int | None = None
    receipts_in_period: int = 0

    @property
    def participant_id(self) -> int:
        return self.participant.pk

    @property
    def sort_key(self):
        return (
            self.reserve_rank if self.reserve_rank is not None else 99,
            (self.participant.last_name or '').lower(),
            (self.participant.email or '').lower(),
        )

    def as_option(self) -> dict:
        return {
            'participant_id': self.participant.pk,
            'receipt_id': self.receipt.pk if self.receipt else None,
            'label': participant_label(self.participant),
            'email': (self.participant.email or '').strip(),
            'phone': (self.participant.phone or '').strip(),
            'receipt_label': receipt_label(self.receipt),
            'is_eligible': self.is_eligible,
            'note': self.note,
            'reserve_rank': self.reserve_rank,
        }


@dataclass
class EligibilityCheck:
    ok: bool
    note: str
    receipt: object | None = None
    receipts_in_period: int = 0


@dataclass
class ConditionsSummary:
    kind: str
    label: str
    rules: list[str] = field(default_factory=list)


def conditions_summary(row) -> ConditionsSummary:
    """Человекочитаемые условия, которым должен удовлетворять новый победитель."""
    kind = draw_kind(row)
    if kind == DRAW_KIND_MAIN:
        return ConditionsSummary(
            kind=kind,
            label='Главный розыгрыш',
            rules=[
                f'{MIN_RECEIPTS_FOR_MAIN}+ подтверждённых чека за всю акцию (п. 8.2 Правил)',
                'чек ещё не приносил приз (п. 7.1 Правил)',
                'участник ещё не выигрывал главный приз',
            ],
        )
    if kind == DRAW_KIND_INSTANT:
        return ConditionsSummary(
            kind=kind,
            label='Моментальный приз',
            rules=[
                'подтверждённый чек за любой период акции',
                'чек ещё не приносил приз',
                'участник ещё не выигрывал моментальный приз',
            ],
        )
    if kind == DRAW_KIND_MONTHLY:
        return ConditionsSummary(
            kind=kind,
            label=f'Ежемесячный розыгрыш, месяц {row.month_num}',
            rules=[
                f'{MIN_RECEIPTS_FOR_MONTHLY}+ подтверждённых чека в месяце {row.month_num} (п. 8.2 Правил)',
                f'чек относится к месяцу {row.month_num}',
                'чек ещё не приносил приз (п. 7.1 Правил)',
                f'участник ещё не выигрывал в ежемесячном розыгрыше месяца {row.month_num} (п. 7.3 Правил)',
            ],
        )
    return ConditionsSummary(
        kind=kind,
        label=f'Еженедельный розыгрыш, неделя {row.week_num}',
        rules=[
            f'подтверждённый чек недели {row.week_num}',
            'чек ещё не приносил приз (п. 7.1 Правил)',
            f'участник ещё не выигрывал в еженедельном розыгрыше недели {row.week_num} (п. 7.2 Правил)',
        ],
    )


def _period_receipts(participant_ids, row) -> dict[int, list]:
    """Подтверждённые чеки нужного периода по участникам — одним запросом."""
    kind = draw_kind(row)
    qs = Receipt.objects.filter(status=Receipt.Status.CONFIRMED)
    if participant_ids is not None:
        qs = qs.filter(participant_id__in=participant_ids)
    if kind == DRAW_KIND_WEEKLY:
        qs = qs.filter(week=row.week_num)
    elif kind == DRAW_KIND_MONTHLY:
        qs = qs.filter(month=row.month_num)
    # DRAW_KIND_INSTANT и DRAW_KIND_MAIN периодом не ограничены: подойдёт любой
    # подтверждённый чек за всю Акцию.

    grouped: dict[int, list] = {}
    for receipt in qs.select_related('participant').order_by('pk'):
        grouped.setdefault(receipt.participant_id, []).append(receipt)
    return grouped


def check_participant(participant, row, *, used_ids=None, busy_ids=None, period_receipts=None) -> EligibilityCheck:
    """
    Проверяет одного участника по условиям розыгрыша заменяемого итога и
    подбирает чек, который станет его билетом.
    """
    kind = draw_kind(row)
    used_ids = _used_receipt_ids(row) if used_ids is None else used_ids
    busy_ids = _busy_participant_ids(row) if busy_ids is None else busy_ids
    if period_receipts is None:
        period_receipts = _period_receipts([participant.pk], row)

    receipts = period_receipts.get(participant.pk, [])
    free_receipts = [r for r in receipts if r.pk not in used_ids]
    fallback = free_receipts[0] if free_receipts else (receipts[0] if receipts else None)

    if row.participant and participant.pk == row.participant.pk:
        return EligibilityCheck(False, 'Это текущий победитель', fallback, len(receipts))

    if participant.pk in busy_ids:
        if kind == DRAW_KIND_MAIN:
            note = 'Уже выигрывал главный приз'
        elif kind == DRAW_KIND_INSTANT:
            note = 'Уже выигрывал моментальный приз'
        elif kind == DRAW_KIND_MONTHLY:
            note = f'Уже выигрывал в месяце {row.month_num} (п. 7.3 Правил)'
        else:
            note = f'Уже выигрывал на неделе {row.week_num} (п. 7.2 Правил)'
        return EligibilityCheck(False, note, fallback, len(receipts))

    if kind == DRAW_KIND_MAIN:
        if len(receipts) < MIN_RECEIPTS_FOR_MAIN:
            return EligibilityCheck(
                False,
                f'Подтверждённых чеков: {len(receipts)} из {MIN_RECEIPTS_FOR_MAIN} нужных (п. 8.2 Правил)',
                fallback,
                len(receipts),
            )
        if not free_receipts:
            return EligibilityCheck(False, 'Все чеки уже принесли приз (п. 7.1 Правил)', fallback, len(receipts))
        return EligibilityCheck(
            True, f'Подтверждённых чеков за акцию: {len(receipts)}', free_receipts[0], len(receipts),
        )

    if kind == DRAW_KIND_INSTANT:
        if not receipts:
            return EligibilityCheck(False, 'Нет подтверждённых чеков', None, 0)
        if not free_receipts:
            return EligibilityCheck(
                False, 'Все чеки уже принесли приз', fallback, len(receipts),
            )
        return EligibilityCheck(
            True, f'Подтверждённых чеков за акцию: {len(receipts)}', free_receipts[0], len(receipts),
        )

    if kind == DRAW_KIND_MONTHLY:
        if len(receipts) < MIN_RECEIPTS_FOR_MONTHLY:
            return EligibilityCheck(
                False,
                f'Чеков в месяце {row.month_num}: {len(receipts)} из {MIN_RECEIPTS_FOR_MONTHLY} нужных (п. 8.2 Правил)',
                fallback,
                len(receipts),
            )
        if not free_receipts:
            return EligibilityCheck(
                False, f'Все чеки месяца {row.month_num} уже принесли приз (п. 7.1 Правил)', fallback, len(receipts),
            )
        return EligibilityCheck(
            True, f'Чеков в месяце {row.month_num}: {len(receipts)}', free_receipts[0], len(receipts),
        )

    if not receipts:
        return EligibilityCheck(False, f'Нет подтверждённого чека недели {row.week_num}', None, 0)
    if not free_receipts:
        return EligibilityCheck(
            False, f'Чек недели {row.week_num} уже принёс приз (п. 7.1 Правил)', fallback, len(receipts),
        )
    return EligibilityCheck(
        True, f'Подтверждённый чек недели {row.week_num}', free_receipts[0], len(receipts),
    )


def eligible_candidates(row) -> list[Candidate]:
    """
    Все участники, подходящие под условия заменяемого розыгрыша.
    Резервные победители этого же слота идут первыми — по очереди резерва.
    """
    period_receipts = _period_receipts(None, row)
    if not period_receipts:
        return []

    used_ids = _used_receipt_ids(row)
    busy_ids = _busy_participant_ids(row)
    ranks = _reserve_ranks(row)

    candidates: list[Candidate] = []
    for participant_id, receipts in period_receipts.items():
        participant = receipts[0].participant
        check = check_participant(
            participant, row, used_ids=used_ids, busy_ids=busy_ids, period_receipts=period_receipts,
        )
        if not check.ok:
            continue
        candidates.append(Candidate(
            participant=participant,
            receipt=check.receipt,
            is_eligible=True,
            note=check.note,
            reserve_rank=ranks.get(participant_id),
            receipts_in_period=check.receipts_in_period,
        ))

    candidates.sort(key=lambda c: c.sort_key)
    return candidates


def candidate_options(row, *, query: str = '', limit: int = CANDIDATE_LIMIT) -> dict:
    """
    Список для выпадающего списка ручного выбора: сначала подходящие под
    условия, затем — любые другие участники акции (их выбрать тоже можно).
    """
    query = (query or '').strip()

    eligible = eligible_candidates(row)
    if query:
        lowered = query.lower()

        def matches(candidate: Candidate) -> bool:
            haystack = ' '.join(filter(None, [
                candidate.participant.email,
                candidate.participant.first_name,
                candidate.participant.last_name,
                candidate.participant.phone,
            ])).lower()
            return lowered in haystack

        eligible = [c for c in eligible if matches(c)]

    eligible_ids = {c.participant_id for c in eligible}
    eligible = eligible[:limit]

    others_limit = max(limit - len(eligible), 10)
    others = _other_participants(row, query=query, exclude_ids=eligible_ids, limit=others_limit)

    return {
        'eligible': [c.as_option() for c in eligible],
        'others': [c.as_option() for c in others],
        'eligible_total': len(eligible_ids),
        'conditions': conditions_summary(row).__dict__,
    }


def _other_participants(row, *, query: str, exclude_ids: set[int], limit: int) -> list[Candidate]:
    """Участники, НЕ подходящие под условия, — выбрать их всё равно можно."""
    qs = User.objects.all() if settings.DEBUG else User.objects.filter(is_staff=False)
    qs = qs.exclude(pk__in=exclude_ids)
    if query:
        qs = qs.filter(
            Q(email__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(phone__icontains=query)
        )
    participants = list(qs.order_by('last_name', 'email')[:limit])
    if not participants:
        return []

    ids = [p.pk for p in participants]
    used_ids = _used_receipt_ids(row)
    busy_ids = _busy_participant_ids(row)
    period_receipts = _period_receipts(ids, row)
    latest_receipts = _latest_confirmed_receipts(ids)

    candidates = []
    for participant in participants:
        check = check_participant(
            participant, row, used_ids=used_ids, busy_ids=busy_ids, period_receipts=period_receipts,
        )
        candidates.append(Candidate(
            participant=participant,
            receipt=check.receipt or latest_receipts.get(participant.pk),
            is_eligible=check.ok,
            note=check.note,
            receipts_in_period=check.receipts_in_period,
        ))
    return candidates


def _latest_confirmed_receipts(participant_ids) -> dict[int, object]:
    """Последний подтверждённый чек участника — запасной билет для «любого» победителя."""
    latest: dict[int, object] = {}
    for receipt in Receipt.objects.filter(
        participant_id__in=participant_ids, status=Receipt.Status.CONFIRMED,
    ).order_by('participant_id', '-pk'):
        latest.setdefault(receipt.participant_id, receipt)
    return latest


def pick_auto_candidate(row) -> Candidate:
    """
    Кандидат для автоматической замены: сначала резервный победитель этого же
    слота с наименьшей очередью резерва (п. 8.4 Правил), иначе — случайный из
    подходящих под условия.
    """
    candidates = eligible_candidates(row)
    if not candidates:
        raise WinnerReplacementError(
            'Не нашлось ни одного участника, подходящего под условия этого розыгрыша. '
            'Выберите нового победителя вручную.'
        )

    reserves = [c for c in candidates if c.reserve_rank is not None]
    if reserves:
        best_rank = min(c.reserve_rank for c in reserves)
        pool = [c for c in reserves if c.reserve_rank == best_rank]
    else:
        pool = candidates

    return secrets.SystemRandom().choice(pool)


# ─────────────────────────── сама замена ───────────────────────────

def _release_old_receipt(receipt, *, reason: str = '') -> None:
    """
    Прежний победитель теряет приз: его чек перестаёт быть победным и
    возвращается в обычный подтверждённый статус, договор и отметка об
    отправленном письме снимаются — они относились к прежнему победителю.

    Если в OkiDoki уже был выставлен договор, он сначала аннулируется/
    расторгается на стороне сервиса (см. oki_doki.cancel_oki_contract) — без
    этого у прежнего победителя остался бы «висящий» договор, а по чеку
    нельзя было бы создать новый (OkiDoki не разрешает выставить второй
    договор с тем же external_id, пока прежний активен).
    """
    if receipt is None:
        return
    if receipt.link_oki_document or receipt.link_oki_document_admin:
        cancel_oki_contract(receipt, reason=reason)
    fields = []
    if receipt.status == Receipt.Status.WINNER:
        receipt.status = Receipt.Status.CONFIRMED
        receipt.message = None
        receipt.date_result_raffle = None
        fields += ['status', 'message', 'date_result_raffle']
    if receipt.is_send_email:
        receipt.is_send_email = False
        receipt.email_sent_at = None
        fields += ['is_send_email', 'email_sent_at']
    if receipt.link_oki_document or receipt.status_oki_document or receipt.link_oki_document_admin:
        receipt.link_oki_document = None
        receipt.status_oki_document = None
        receipt.link_oki_document_admin = None
        receipt.oki_document_issued_at = None
        fields += [
            'link_oki_document', 'status_oki_document', 'link_oki_document_admin',
            'oki_document_issued_at',
        ]
    if fields:
        receipt.save(update_fields=[*fields, 'updated_at'])


def _promote_new_receipt(receipt, *, prize, published: bool, date_result_raffle) -> None:
    """
    Чек нового победителя. Если итог розыгрыша уже опубликован, чек сразу
    становится победным — ровно так же, как это делает публикация итогов
    (promotion.services.publish_winners). Если ещё не опубликован, чек остаётся
    подтверждённым: победным он станет на шаге «Публикация».
    """
    if receipt is None:
        return
    fields = []
    if receipt.is_participation is False:
        receipt.is_participation = True
        fields.append('is_participation')
    if published and receipt.status != Receipt.Status.WINNER:
        receipt.status = Receipt.Status.WINNER
        receipt.message = ReceiptMessage.get(
            ReceiptMessage.WINNER, prize_name=prize.name if prize else 'приз',
        )
        receipt.date_result_raffle = date_result_raffle or timezone.localdate()
        fields += ['status', 'message', 'date_result_raffle']
    if fields:
        receipt.save(update_fields=[*fields, 'updated_at'])


@transaction.atomic
def replace_winner(row, *, new_participant_id, new_receipt_id=None, mode: str, reason: str = '', user=None) -> WinnerReplacement:
    """
    Переносит призовой слот на нового победителя и записывает замену в историю.

    Слот (приз, неделя/месяц, публикация, выданный файл приза) остаётся прежним —
    меняются участник и его чек. Опубликованные на лендинге итоги не меняются
    никогда: первый победитель фиксируется в public_participant.
    """
    if not can_replace(row):
        raise WinnerReplacementError(replace_block_reason(row))

    if not str(new_participant_id or '').isdigit():
        raise WinnerReplacementError('Новый победитель не выбран.')
    if mode not in WinnerReplacement.Mode.values:
        raise WinnerReplacementError('Неизвестный способ замены.')

    new_participant = User.objects.filter(pk=int(new_participant_id)).first()
    if new_participant is None:
        raise WinnerReplacementError('Участник не найден.')

    model = PromotionDrawResultMainRaffle if row.kind == 'main' else PromotionDrawResult
    draw_result = model.objects.select_for_update().select_related('participant', 'prize').get(pk=row.pk)

    old_participant = draw_result.participant
    if new_participant.pk == old_participant.pk:
        raise WinnerReplacementError('Этот участник уже является победителем.')

    check = check_participant(new_participant, row)

    new_receipt = None
    if new_receipt_id and str(new_receipt_id).isdigit():
        new_receipt = Receipt.objects.filter(
            pk=int(new_receipt_id), participant_id=new_participant.pk,
        ).first()
        if new_receipt is None:
            raise WinnerReplacementError('Выбранный чек не принадлежит этому участнику.')
    else:
        new_receipt = check.receipt

    old_receipt = draw_result.receipt
    if new_receipt is not None and old_receipt is not None and new_receipt.pk == old_receipt.pk:
        raise WinnerReplacementError('Этот чек уже закреплён за текущим победителем.')

    published = bool(getattr(draw_result, 'is_published', False))
    # Был ли этот победитель объявлен публично. У главного розыгрыша отдельного
    # флага публикации нет (в PromotionDrawResultMainRaffle его просто не
    # существует) — его итог считается объявленным сразу, поэтому там поведение
    # прежнее: прежний победитель фиксируется при первой же замене.
    announced = True if row.kind == 'main' else published
    date_result_raffle = old_receipt.date_result_raffle if old_receipt else None

    update_fields = ['participant', 'receipt']
    if announced and draw_result.public_participant_id is None:
        # Первая замена объявленного итога: лендинг навсегда запоминает того,
        # кто был объявлен победителем изначально, — публикация итогов не должна
        # «переигрываться».
        # Неопубликованный итог объявлять ещё не успели, поэтому фиксировать
        # прежнего победителя нельзя: иначе при публикации на сайт уйдёт он, а не
        # тот, кто по итогу замены стал победителем (например, когда прежний был
        # снят до публикации как не имеющий права участвовать).
        draw_result.public_participant = old_participant
        update_fields.append('public_participant')

    draw_result.participant = new_participant
    draw_result.receipt = new_receipt

    if row.kind == 'main':
        # Для главного приза договор и письмо живут на самом итоге розыгрыша —
        # у нового победителя они должны быть пустыми. Если договор уже был
        # выставлен в OkiDoki, сначала аннулируем его там же (см. комментарий
        # в _release_old_receipt) — иначе по тому же external_id нельзя будет
        # выставить договор новому победителю.
        if draw_result.link_oki_document or draw_result.link_oki_document_admin:
            cancel_oki_contract(draw_result, reason=reason)
        draw_result.link_oki_document = None
        draw_result.status_oki_document = None
        draw_result.link_oki_document_admin = None
        draw_result.oki_document_issued_at = None
        draw_result.is_send_email = False
        draw_result.email_sent_at = None
        update_fields += [
            'link_oki_document', 'status_oki_document', 'link_oki_document_admin',
            'oki_document_issued_at', 'is_send_email', 'email_sent_at',
        ]

    draw_result.save(update_fields=update_fields)

    if row.kind != 'main':
        _release_old_receipt(old_receipt, reason=reason)
        _promote_new_receipt(
            new_receipt,
            prize=draw_result.prize,
            published=published,
            date_result_raffle=date_result_raffle,
        )

    owner_field = 'main_draw_result' if row.kind == 'main' else 'draw_result'
    previous = WinnerReplacement.objects.filter(**{owner_field: draw_result}).count()

    return WinnerReplacement.objects.create(
        **{owner_field: draw_result},
        order=previous + 1,
        mode=mode,
        old_participant=old_participant,
        old_receipt=old_receipt,
        old_participant_label=participant_label(old_participant),
        new_participant=new_participant,
        new_receipt=new_receipt,
        new_participant_label=participant_label(new_participant),
        was_eligible=check.ok,
        eligibility_note=check.note[:500],
        reason=(reason or '').strip(),
        created_by=user if getattr(user, 'pk', None) else None,
    )


# ─────────────────────────── история ───────────────────────────

def history_for_row(row) -> list[WinnerReplacement]:
    owner_field = 'main_draw_result_id' if row.kind == 'main' else 'draw_result_id'
    return list(
        WinnerReplacement.objects
        .filter(**{owner_field: row.pk})
        .select_related('old_participant', 'new_participant', 'created_by', 'old_receipt', 'new_receipt')
        .order_by('order', 'pk')
    )


def replacement_counts(rows) -> dict[tuple[str, int], int]:
    """Сколько раз заменяли победителя в каждом слоте — один запрос на всю страницу."""
    weekly_ids = [r.pk for r in rows if r.kind != 'main']
    main_ids = [r.pk for r in rows if r.kind == 'main']
    counts: dict[tuple[str, int], int] = {}
    grouped = (
        WinnerReplacement.objects
        .filter(Q(draw_result_id__in=weekly_ids) | Q(main_draw_result_id__in=main_ids))
        .values('draw_result_id', 'main_draw_result_id')
        .annotate(total=Count('pk'))
    )
    for item in grouped:
        if item['draw_result_id']:
            counts[('weekly', item['draw_result_id'])] = item['total']
        elif item['main_draw_result_id']:
            counts[('main', item['main_draw_result_id'])] = item['total']
    return counts
