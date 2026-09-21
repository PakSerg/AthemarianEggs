"""
Данные вкладки «Моментальные призы» в панели персонала.

Менеджеру нужно видеть три вещи:

1. **Состояние фонда** — сколько призовых моментов заведено, сколько уже
   разыграно, сколько ждёт своего часа. Если расписание не сгенерировано, лоток
   будет всегда пустым, и это должно бросаться в глаза.
2. **Равномерность** — график по дням: сколько призов приходится на каждый день
   Акции и сколько из них уже ушло. Именно на этот график смотрят, когда
   спрашивают «призы точно уходят равномерно?».
3. **Просроченные моменты** — наступившие, но не пойманные. Их много, когда
   трафик ниже ожидаемого; это сигнал, что фонд не успевает разыгрываться.
"""

from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone

from promotion.models import InstantAttempt, InstantMoment, Prize, PromotionDrawResult
from promotion.services.instant_prizes import schedule_summary


def fund_summary() -> dict:
    """Сводка по призовому фонду моментальных призов."""
    now = timezone.now()
    moments = InstantMoment.objects.aggregate(
        total=Count('pk'),
        claimed=Count('pk', filter=Q(is_claimed=True)),
        overdue=Count('pk', filter=Q(is_claimed=False, scheduled_at__lte=now)),
    )
    attempts = InstantAttempt.objects.aggregate(
        total=Count('pk'),
        played=Count('pk', filter=Q(played_at__isnull=False)),
        won=Count('pk', filter=Q(is_win=True)),
    )
    return {
        'moments_total': moments['total'],
        'moments_claimed': moments['claimed'],
        'moments_left': moments['total'] - moments['claimed'],
        'moments_overdue': moments['overdue'],
        'attempts_total': attempts['total'],
        'attempts_played': attempts['played'],
        'attempts_left': attempts['total'] - attempts['played'],
        'attempts_won': attempts['won'],
        'winners_total': PromotionDrawResult.objects.filter(
            is_instant=True, is_reserve=False,
        ).count(),
    }


def prizes_summary() -> list[dict]:
    """По каждому моментальному призу: заведено единиц, сгенерировано, разыграно."""
    rows = []
    prizes = Prize.objects.filter(
        is_active=True, is_main=False, draw_period=Prize.DrawPeriod.INSTANT,
    ).order_by('week', 'name')

    counts = {
        row['prize_id']: row
        for row in InstantMoment.objects.values('prize_id').annotate(
            generated=Count('pk'),
            claimed=Count('pk', filter=Q(is_claimed=True)),
        )
    }

    for prize in prizes:
        stats = counts.get(prize.pk, {'generated': 0, 'claimed': 0})
        rows.append({
            'prize': prize,
            'week': prize.week,
            'planned': prize.count or 0,
            'generated': stats['generated'],
            'claimed': stats['claimed'],
            'left': stats['generated'] - stats['claimed'],
            # Расписание не совпадает с планом — значит, количество приза меняли
            # после генерации и расписание нужно пересобрать.
            'needs_regeneration': stats['generated'] != (prize.count or 0),
        })
    return rows


def daily_schedule(limit_days: int = 0) -> list[dict]:
    """График «сколько призов на день» — та самая проверка равномерности."""
    rows = schedule_summary(limit_days=limit_days)
    peak = max((row['total'] for row in rows), default=0) or 1
    today = timezone.localtime().date()
    for row in rows:
        row['share'] = round(row['total'] * 100 / peak)
        row['is_today'] = row['day'] == today
        row['is_past'] = row['day'] < today
    return rows


def recent_wins(limit: int = 30):
    """Последние выигранные моментальные призы — для быстрой проверки «работает ли»."""
    return (
        PromotionDrawResult.objects
        .filter(is_instant=True, is_reserve=False)
        .select_related('participant', 'prize', 'receipt')
        .order_by('-created_at')[:limit]
    )
