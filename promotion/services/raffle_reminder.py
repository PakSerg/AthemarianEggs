"""
Напоминания в Telegram за сутки до розыгрыша (еженедельный и главный).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.utils import timezone

from ..models import Prize, Raffle
from .notify_bot import notify as notify_bot
from .promo_calendar import month_num_on_date, week_num_on_date

logger = logging.getLogger(__name__)

_DRAW_TYPE_LABELS = {
    'weekly': 'Еженедельный розыгрыш',
    'monthly': 'Ежемесячный розыгрыш',
    'main': 'Главный розыгрыш',
}


def _notify_draw_reminder(*, draw_type: str, draw_date, prize_lines: list[str], extra: str = '') -> None:
    """Напоминание за сутки до розыгрыша через forward-signal."""
    title = _DRAW_TYPE_LABELS.get(draw_type, draw_type)
    date_str = draw_date.strftime('%d.%m.%Y')

    lines = [
        'Напоминание о розыгрыше',
        f'Завтра, {date_str}, состоится {title}.',
    ]
    if extra:
        lines.append(extra)
    lines.append('')
    lines.append('Призы:')
    lines.extend(prize_lines[:30])
    if len(prize_lines) > 30:
        lines.append(f'…ещё {len(prize_lines) - 30} призов')

    notify_bot('\n'.join(lines))


WEEKDAY_LABELS = {
    0: 'понедельник',
    1: 'вторник',
    2: 'среда',
    3: 'четверг',
    4: 'пятница',
    5: 'суббота',
    6: 'воскресенье',
}


def _tomorrow() -> date:
    return timezone.localtime().date() + timedelta(days=1)


# Канонические реализации живут в promo_calendar — здесь только псевдонимы,
# чтобы не ломать существующие импорты.
_week_num_on_date = week_num_on_date
_month_num_on_date = month_num_on_date


def _is_in_campaign(raffle: Raffle, target: date) -> bool:
    start = timezone.localtime(raffle.start_date).date()
    end = timezone.localtime(raffle.end_date).date()
    return start <= target <= end


def _weekly_draw_tomorrow(raffle: Raffle, tomorrow: date) -> bool:
    """Условия как в process_raffle, но на завтра."""
    if not _is_in_campaign(raffle, tomorrow):
        return False

    end = timezone.localtime(raffle.end_date).date()
    if tomorrow == end:
        return True

    if tomorrow.weekday() != raffle.week_day:
        return False

    return _week_num_on_date(raffle, tomorrow) != 1


def _monthly_draw_tomorrow(raffle: Raffle, tomorrow: date) -> bool:
    """Ежемесячный розыгрыш завтра, если у Raffle задан month_day и он совпадает с числом месяца."""
    if not raffle.month_day:
        return False
    if not _is_in_campaign(raffle, tomorrow):
        return False
    return tomorrow.day == raffle.month_day


def _main_draw_tomorrow(raffle: Raffle, tomorrow: date) -> bool:
    main_date = timezone.localtime(raffle.main_raffle_date).date()
    return tomorrow == main_date and _is_in_campaign(raffle, tomorrow)


def _prize_lines(*, is_main: bool, draw_period: str | None = None) -> list[str]:
    prizes = Prize.objects.filter(is_active=True, is_main=is_main)
    if draw_period:
        prizes = prizes.filter(draw_period=draw_period)
    prizes = prizes.order_by('name')
    lines = []
    for prize in prizes:
        if is_main:
            lines.append(f'• {_format_prize(prize)}')
            continue
        count = prize.count or 0
        if count > 0:
            lines.append(f'• {prize.name} — {count} шт.')
        elif prize.name:
            lines.append(f'• {prize.name}')
    return lines or ['• призы не заданы в админке']


def _format_prize(prize) -> str:
    name = prize.name or 'Приз'
    if prize.cost:
        return f'{name} ({int(prize.cost)} ₽)'
    return name


def _reminder_key(draw_type: str, draw_date: date) -> str:
    return f'{draw_type}_{draw_date.isoformat()}'


def process_upcoming_raffle_reminders() -> dict:
    """
    Проверяет активный Raffle и шлёт в TG напоминания за сутки.
    Возвращает краткий отчёт для логов/Celery.
    """
    raffle = Raffle.objects.filter(is_active=True).first()
    if not raffle:
        logger.info('raffle_reminder: no active raffle')
        return {'sent': [], 'skipped': 'no_active_raffle'}

    tomorrow = _tomorrow()
    sent = []
    update_fields = []

    if _weekly_draw_tomorrow(raffle, tomorrow):
        key = _reminder_key('weekly', tomorrow)
        if raffle.key_reminder_weekly != key:
            weekday = WEEKDAY_LABELS.get(tomorrow.weekday(), '')
            _notify_draw_reminder(
                draw_type='weekly',
                draw_date=tomorrow,
                prize_lines=_prize_lines(is_main=False, draw_period='weekly'),
                extra=f'День розыгрыша: {weekday}',
            )
            raffle.key_reminder_weekly = key
            update_fields.append('key_reminder_weekly')
            sent.append('weekly')
            logger.info('raffle_reminder: sent weekly for %s', tomorrow)
        else:
            logger.debug('raffle_reminder: weekly already sent key=%s', key)

    if _monthly_draw_tomorrow(raffle, tomorrow):
        key = _reminder_key('monthly', tomorrow)
        if raffle.key_reminder_monthly != key:
            _notify_draw_reminder(
                draw_type='monthly',
                draw_date=tomorrow,
                prize_lines=_prize_lines(is_main=False, draw_period='monthly'),
            )
            raffle.key_reminder_monthly = key
            update_fields.append('key_reminder_monthly')
            sent.append('monthly')
            logger.info('raffle_reminder: sent monthly for %s', tomorrow)
        else:
            logger.debug('raffle_reminder: monthly already sent key=%s', key)

    if _main_draw_tomorrow(raffle, tomorrow):
        key = _reminder_key('main', tomorrow)
        if raffle.key_reminder_main != key:
            _notify_draw_reminder(
                draw_type='main',
                draw_date=tomorrow,
                prize_lines=_prize_lines(is_main=True),
            )
            raffle.key_reminder_main = key
            update_fields.append('key_reminder_main')
            sent.append('main')
            logger.info('raffle_reminder: sent main for %s', tomorrow)
        else:
            logger.debug('raffle_reminder: main already sent key=%s', key)

    if update_fields:
        raffle.save(update_fields=update_fields)

    return {
        'sent': sent,
        'tomorrow': tomorrow.isoformat(),
        'raffle_id': raffle.pk,
    }
