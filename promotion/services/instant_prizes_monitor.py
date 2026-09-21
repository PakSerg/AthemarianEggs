"""
Ежедневная сводка по моментальным призам в Telegram.

Зачем: лоток яиц — единственная механика Акции, которая может тихо
«сломаться» без единой ошибки в логах. Расписание призовых моментов забыли
сгенерировать — участники играют, но выиграть не могут. Трафик ниже
ожидаемого — моменты накапливаются непойманными, и к концу Акции призовой фонд
останется неразыгранным. И то и другое видно только по цифрам, поэтому цифры
приходят сами, а не ждут, пока кто-то откроет вкладку в панели.

Сводка уходит раз в сутки; тревожные случаи помечаются отдельной строкой.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from ..models import InstantMoment, InstantPrizeSettings
from .notify_bot import notify as notify_bot

logger = logging.getLogger(__name__)

# Доля непойманных наступивших моментов, после которой фонд считается
# отстающим от графика. 20% — заметное отставание, но ещё не катастрофа:
# сигнал подумать о промо-поддержке, а не чинить систему.
OVERDUE_ALERT_SHARE = 0.2


def collect_state() -> dict:
    now = timezone.now()
    total = InstantMoment.objects.count()
    claimed = InstantMoment.objects.filter(is_claimed=True).count()
    overdue = InstantMoment.objects.filter(is_claimed=False, scheduled_at__lte=now).count()
    today = InstantMoment.objects.filter(scheduled_at__date=timezone.localdate()).count()
    arrived = claimed + overdue

    return {
        'enabled': InstantPrizeSettings.load().is_enabled,
        'total': total,
        'claimed': claimed,
        'left': total - claimed,
        'overdue': overdue,
        'today': today,
        'overdue_share': (overdue / arrived) if arrived else 0.0,
    }


def process_instant_prizes_report() -> dict:
    """Собрать сводку и отправить её в Telegram. Возвращает саму сводку."""
    state = collect_state()

    lines = ['🥚 Моментальные призы — сводка за сутки']
    if not state['enabled']:
        lines.append('⚠️ Механика ВЫКЛЮЧЕНА в настройках — участники не могут играть.')

    if state['total'] == 0:
        lines.append(
            '❌ Расписание призовых моментов не сгенерировано: лоток всегда пустой. '
            'Заведите моментальные призы и нажмите «Пересобрать расписание» в панели.'
        )
        notify_bot('\n'.join(lines))
        return state

    lines += [
        f'Всего призовых моментов: {state["total"]}',
        f'Разыграно: {state["claimed"]}',
        f'Осталось в фонде: {state["left"]}',
        f'Запланировано на сегодня: {state["today"]}',
        f'Наступили, но не пойманы: {state["overdue"]}',
    ]

    if state['overdue_share'] >= OVERDUE_ALERT_SHARE:
        lines.append(
            f'⚠️ Непойманных моментов {state["overdue_share"] * 100:.0f}% от наступивших — '
            f'призы уходят медленнее графика. Так к концу Акции фонд может остаться '
            f'неразыгранным.'
        )

    notify_bot('\n'.join(lines))
    return state
