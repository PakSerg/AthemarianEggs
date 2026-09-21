"""
Моментальные призы — «лоток яиц».

Что видит участник
------------------
Чек приняли — участнику начислилась попытка. При следующем заходе в кабинет
открывается модалка с лотком из десяти яиц: он выбирает одно, яйцо открывается,
внутри либо приз, либо пусто. Остались ещё попытки — кнопка «Попробовать ещё
раз» даёт новый лоток. Выиграть моментальный приз можно ровно один раз за всю
Акцию (см. prize_limits).

Как определяется выигрыш
------------------------
Не броском монетки на каждый клик, а расписанием ПРИЗОВЫХ МОМЕНТОВ. На каждую
единицу моментального приза заранее создаётся строка InstantMoment с точным
временем. Открыл яйцо — сервер берёт самый ранний ненаступивший... точнее,
самый ранний УЖЕ наступивший и ещё никем не пойманный момент. Есть такой —
участник выиграл приз этого момента; нет — яйцо пустое.

Почему именно так:

* Призы уходят ровно по графику. Расписание строится так, что на каждый день
  Акции приходится примерно одинаковое количество моментов (требование
  Заказчика), а внутри дня они размазаны по игровому окну.
* Фонд не может ни закончиться раньше срока, ни остаться неразыгранным:
  моментов ровно столько, сколько единиц приза заведено.
* Результат не зависит от трафика. В тихий час момент просто ждёт первого
  игрока, а в час пик несколько подряд открытых яиц не «съедят» недельный запас.

Moment, который никто не поймал, по умолчанию не сгорает (moment_ttl_hours = 0):
он достаётся первому же участнику, открывшему яйцо после его наступления. Если
Заказчик захочет, чтобы просроченные моменты сгорали, достаточно выставить срок
жизни в настройках — тогда приз из такого момента не будет вручён никому.

Генерация расписания
--------------------
``generate_moments`` читает призы с периодичностью «Моментальный» (у каждого
указаны неделя Акции и количество) и раскладывает их количество:

1. по дням недельного периода — поровну; остаток (count % дней) раздаётся не
   подряд, а равномерно разнесёнными днями, чтобы «лишние» призы не собирались
   в начале недели;
2. внутри дня — по игровому окну: день режется на равные слоты по числу
   моментов, момент ставится в случайную точку своего слота. Слоты гарантируют
   равномерность, случайная точка внутри слота — непредсказуемость.

Генерация идемпотентна: повторный запуск не плодит дубли, уже разыгранные
моменты не трогает, а для недель с изменившимся количеством добавляет или
убирает только неразыгранные моменты.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.db import connection, transaction
from django.utils import timezone

from ..models import (
    InstantAttempt,
    InstantMoment,
    InstantPrizeSettings,
    Prize,
    PromotionDrawResult,
    Raffle,
    Receipt,
)
from . import prize_limits
from .promo_calendar import last_week_num, week_bounds

logger = logging.getLogger(__name__)

_random = secrets.SystemRandom()


def _locked(queryset):
    """Заблокировать строки на время транзакции — там, где СУБД это умеет.

    На бою это PostgreSQL, и блокировка обязательна: она не даёт двум
    участникам, открывшим яйцо в одну и ту же секунду, забрать один призовой
    момент. SQLite (локальная разработка и тесты) SELECT ... FOR UPDATE не
    поддерживает вовсе и пишет в одну транзакцию за раз, поэтому там мы просто
    возвращаем исходный queryset, а не падаем с NotSupportedError.
    """
    features = connection.features
    if not features.has_select_for_update:
        return queryset
    if features.has_select_for_update_skip_locked:
        return queryset.select_for_update(skip_locked=True)
    return queryset.select_for_update()


# --------------------------------------------------------------------------- #
# Попытки
# --------------------------------------------------------------------------- #
def grant_attempts_for_receipt(receipt: Receipt) -> int:
    """
    Начислить попытки за принятый чек. Возвращает число НОВЫХ попыток.

    Вызывается в момент подтверждения чека — и из автоматической проверки
    (receipt_fns), и из панели модератора (staff_panel.services.receipts), —
    поэтому обязана быть идемпотентной: чек могут подтвердить, отклонить и
    подтвердить снова, и попытки от этого не должны размножаться.
    """
    if receipt.status != Receipt.Status.CONFIRMED:
        return 0

    config = InstantPrizeSettings.load()
    target = config.attempts_per_receipt
    if target <= 0:
        return 0

    created = 0
    for sequence in range(1, target + 1):
        _, is_new = InstantAttempt.objects.get_or_create(
            receipt=receipt,
            sequence=sequence,
            defaults={'participant_id': receipt.participant_id},
        )
        if is_new:
            created += 1
    return created


def attempts_queryset(participant):
    return InstantAttempt.objects.filter(participant=participant)


def available_attempts(participant) -> int:
    """Сколько попыток у участника не сыграно."""
    return attempts_queryset(participant).filter(played_at__isnull=True).count()


@dataclass(frozen=True)
class GameState:
    """Состояние лотка для кабинета и модалки."""

    is_enabled: bool
    eggs: int
    attempts_left: int
    attempts_total: int
    attempts_played: int
    already_won: bool
    won_prize_name: str = ''

    @property
    def can_play(self) -> bool:
        return self.is_enabled and self.attempts_left > 0

    @property
    def egg_range(self) -> range:
        """Номера яиц для шаблона: 1..eggs."""
        return range(1, self.eggs + 1)

    @property
    def should_show_modal(self) -> bool:
        """Показывать ли модалку при заходе в кабинет.

        Показываем, пока есть чем играть. Участнику, который уже выиграл
        моментальный приз, лоток всё ещё доступен — но выиграть второй раз он
        не может, поэтому звать его в модалку незачем.
        """
        return self.can_play and not self.already_won


def game_state(participant) -> GameState:
    config = InstantPrizeSettings.load()
    attempts = attempts_queryset(participant)
    total = attempts.count()
    played = attempts.filter(played_at__isnull=False).count()

    won = (
        PromotionDrawResult.objects
        .filter(participant=participant, is_instant=True, is_reserve=False)
        .select_related('prize')
        .first()
    )

    return GameState(
        is_enabled=config.is_enabled,
        eggs=config.eggs_per_tray,
        attempts_left=total - played,
        attempts_total=total,
        attempts_played=played,
        already_won=won is not None,
        won_prize_name=(won.prize.name if won and won.prize else ''),
    )


# --------------------------------------------------------------------------- #
# Игра
# --------------------------------------------------------------------------- #
class InstantPlayError(Exception):
    """Сыграть нельзя: механика выключена, попыток нет или яйцо выбрано неверно."""


@dataclass(frozen=True)
class PlayResult:
    is_win: bool
    egg: int
    attempts_left: int
    prize: object | None = None
    draw_result: object | None = None


def play(participant, egg: int) -> PlayResult:
    """
    Открыть яйцо: потратить одну попытку и узнать исход.

    Выбор яйца на исход не влияет и сохраняется только для отчётности: решение
    принимается сервером здесь и сейчас, поэтому «подсмотреть» выигрышную
    ячейку в ответе сервера или в разметке невозможно.
    """
    config = InstantPrizeSettings.load()

    if not config.is_enabled:
        raise InstantPlayError('Моментальные призы сейчас недоступны.')
    if not (1 <= int(egg) <= config.eggs_per_tray):
        raise InstantPlayError('Такого яйца в лотке нет.')

    with transaction.atomic():
        attempt = (
            _locked(InstantAttempt.objects.filter(
                participant=participant, played_at__isnull=True,
            ))
            .order_by('created_at', 'pk')
            .first()
        )
        if attempt is None:
            raise InstantPlayError('У вас не осталось попыток. Зарегистрируйте новый чек.')

        moment = None
        # Второй моментальный приз одному участнику не достаётся (бриф, п. 5).
        # Попытку при этом всё равно тратим: иначе участник копил бы их вечно.
        if prize_limits.can_win_instant(participant.pk):
            moment = _claim_moment(config)

        attempt.played_at = timezone.now()
        attempt.chosen_egg = int(egg)
        attempt.is_win = moment is not None
        attempt.moment = moment
        attempt.save(update_fields=['played_at', 'chosen_egg', 'is_win', 'moment'])

        draw_result = None
        if moment is not None:
            draw_result = _award_instant_prize(participant, moment, attempt)

    attempts_left = available_attempts(participant)
    return PlayResult(
        is_win=moment is not None,
        egg=int(egg),
        attempts_left=attempts_left,
        prize=(moment.prize if moment else None),
        draw_result=draw_result,
    )


def _claim_moment(config: InstantPrizeSettings) -> InstantMoment | None:
    """Забрать самый ранний наступивший и ещё не разыгранный призовой момент.

    Строки берутся с блокировкой (см. ``_locked``) — это защита от гонки: два
    участника, открывшие яйцо одновременно, не получат один и тот же момент,
    второй просто возьмёт следующий (или уйдёт ни с чем).
    """
    now = timezone.now()
    queryset = _locked(InstantMoment.objects.filter(
        is_claimed=False, scheduled_at__lte=now,
    ))
    if config.moment_ttl_hours:
        queryset = queryset.filter(
            scheduled_at__gte=now - timedelta(hours=config.moment_ttl_hours),
        )

    moment = queryset.select_related('prize').order_by('scheduled_at', 'pk').first()
    if moment is None:
        return None

    moment.is_claimed = True
    moment.claimed_at = now
    moment.save(update_fields=['is_claimed', 'claimed_at'])
    return moment


def _award_instant_prize(participant, moment: InstantMoment, attempt: InstantAttempt):
    """Создать итог розыгрыша по моментальному призу.

    Итог создаётся сразу опубликованным: участник узнаёт о победе в момент
    игры, держать её неопубликованной нечего. Дальше запись живёт по общему
    маршруту победителя — вкладка «Победители» в панели, договор OkiDoki,
    письмо, файл электронного приза.

    К итогу привязывается чек, который дал попытку: весь маршрут победителя
    (договор, письмо, статус) в еженедельных призах завязан на чек, и без
    привязки моментальный победитель выпал бы из него. Побочный эффект тот же,
    что и в еженедельном розыгрыше: чек становится «победным» и больше не
    участвует в других розыгрышах — один чек приносит один приз.
    """
    from ..messages import ReceiptMessage

    moment.participant = participant
    moment.save(update_fields=['participant'])

    receipt = attempt.receipt
    draw_result = PromotionDrawResult.objects.create(
        participant=participant,
        prize=moment.prize,
        receipt=receipt,
        is_instant=True,
        instant_moment=moment,
        is_published=True,
        published_at=timezone.now(),
    )

    if receipt is not None:
        receipt.status = Receipt.Status.WINNER
        receipt.message = ReceiptMessage.get(
            ReceiptMessage.WINNER,
            prize_name=(moment.prize.name if moment.prize else 'приз'),
        )
        receipt.date_result_raffle = timezone.localdate()
        receipt.is_participation = True
        receipt.save(update_fields=[
            'status', 'message', 'date_result_raffle', 'is_participation', 'updated_at',
        ])

    return draw_result


# --------------------------------------------------------------------------- #
# Расписание призовых моментов
# --------------------------------------------------------------------------- #
def instant_prizes_queryset(week: int | None = None):
    queryset = Prize.objects.filter(
        is_active=True, is_main=False, draw_period=Prize.DrawPeriod.INSTANT,
    )
    if week is not None:
        queryset = queryset.filter(week=week)
    return queryset.order_by('week', 'pk')


def spread_over_days(count: int, days: int) -> list[int]:
    """Разложить `count` призов по `days` дням как можно ровнее.

    Остаток от деления раздаётся не первым дням подряд, а равномерно
    разнесённым — иначе «лишние» призы копились бы в начале каждой недели, и
    понедельник систематически оказывался бы щедрее воскресенья.
    """
    if days <= 0 or count <= 0:
        return [0] * max(days, 0)

    base, remainder = divmod(count, days)
    result = [base] * days
    for index in range(remainder):
        # Позиции остатка: 0, days/remainder, 2*days/remainder, ...
        result[(index * days) // remainder] += 1
    return result


def _day_moments(day: date, count: int, config: InstantPrizeSettings) -> list[datetime]:
    """Времена `count` моментов внутри одного дня, по равным слотам игрового окна."""
    if count <= 0:
        return []

    tz = timezone.get_current_timezone()
    start_hour = min(config.day_start_hour, 23)
    end_hour = max(config.day_end_hour, start_hour + 1)

    window_start = timezone.make_aware(datetime.combine(day, time(start_hour, 0)), tz)
    window_end = timezone.make_aware(
        datetime.combine(day, time(0, 0)) + timedelta(hours=min(end_hour, 24)), tz,
    )
    window_seconds = max(int((window_end - window_start).total_seconds()), 1)
    slot_seconds = window_seconds / count

    moments = []
    for index in range(count):
        slot_start = index * slot_seconds
        offset = slot_start + _random.uniform(0, slot_seconds)
        moments.append(window_start + timedelta(seconds=offset))
    return moments


@transaction.atomic
def generate_moments(raffle=None, *, week: int | None = None) -> dict:
    """
    Построить (или досоздать) расписание призовых моментов.

    Идемпотентна: уже разыгранные моменты не трогаются никогда, неразыгранные
    приводятся в соответствие с текущим количеством у приза. Поэтому её можно
    запускать хоть каждый день — например, после того как менеджер поправил
    количество моментальных призов на неделю.

    :return: сводка {'created': ..., 'removed': ..., 'kept': ..., 'by_week': {...}}
    """
    if raffle is None:
        raffle = Raffle.objects.filter(is_active=True).first() or Raffle.objects.first()
    if raffle is None:
        return {'created': 0, 'removed': 0, 'kept': 0, 'by_week': {}, 'error': 'Не заведён розыгрыш'}

    config = InstantPrizeSettings.load()
    promo_start = timezone.localtime(raffle.start_date).date()
    promo_end = timezone.localtime(raffle.end_date).date()
    last_week = last_week_num(raffle)

    created = removed = kept = 0
    by_week: dict[int, int] = {}

    for prize in instant_prizes_queryset(week):
        prize_week = prize.week or 1
        if prize_week > last_week:
            logger.warning(
                'generate_moments: приз %s заведён на неделю %s, а в Акции недель %s — пропущен',
                prize.pk, prize_week, last_week,
            )
            continue

        target = prize.count or 0
        existing = InstantMoment.objects.filter(prize=prize, week=prize_week)
        claimed = existing.filter(is_claimed=True).count()
        unclaimed = list(existing.filter(is_claimed=False).order_by('scheduled_at', 'pk'))

        needed = max(target - claimed, 0)

        if len(unclaimed) > needed:
            # Количество приза уменьшили — снимаем лишние ещё не разыгранные моменты,
            # начиная с самых поздних: ближайшие к «сейчас» трогать не хочется.
            extra = unclaimed[needed:]
            InstantMoment.objects.filter(pk__in=[m.pk for m in extra]).delete()
            removed += len(extra)
            unclaimed = unclaimed[:needed]

        kept += len(unclaimed)
        to_create = needed - len(unclaimed)
        if to_create <= 0:
            by_week[prize_week] = by_week.get(prize_week, 0) + claimed + len(unclaimed)
            continue

        week_start, week_end = week_bounds(raffle, prize_week)
        week_start = max(week_start, promo_start)
        week_end = min(week_end, promo_end)
        days = [week_start + timedelta(days=i) for i in range((week_end - week_start).days + 1)]
        if not days:
            logger.warning('generate_moments: у недели %s нет дней внутри Акции', prize_week)
            continue

        per_day = spread_over_days(to_create, len(days))
        schedule: list[datetime] = []
        for day, day_count in zip(days, per_day):
            schedule.extend(_day_moments(day, day_count, config))
        schedule.sort()

        InstantMoment.objects.bulk_create([
            InstantMoment(prize=prize, week=prize_week, scheduled_at=moment_at)
            for moment_at in schedule
        ])
        created += len(schedule)
        by_week[prize_week] = by_week.get(prize_week, 0) + claimed + len(unclaimed) + len(schedule)

    return {'created': created, 'removed': removed, 'kept': kept, 'by_week': by_week}


def schedule_summary(limit_days: int = 0) -> list[dict]:
    """Сводка расписания по дням — для панели: сколько призов на день и сколько уже ушло."""
    from django.db.models import Count, Q
    from django.db.models.functions import TruncDate

    rows = (
        InstantMoment.objects
        .annotate(day=TruncDate('scheduled_at'))
        .values('day')
        .annotate(
            total=Count('pk'),
            claimed=Count('pk', filter=Q(is_claimed=True)),
        )
        .order_by('day')
    )
    result = [
        {
            'day': row['day'],
            'total': row['total'],
            'claimed': row['claimed'],
            'left': row['total'] - row['claimed'],
        }
        for row in rows
    ]
    return result[-limit_days:] if limit_days else result
