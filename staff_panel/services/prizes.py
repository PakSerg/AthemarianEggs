from __future__ import annotations

from django.db.models import Q, QuerySet

from promotion.models import Prize

from .filters import values_for

PRIZE_SORT_FIELDS = {
    'name': 'name',
    'type_prize': 'type_prize',
    'draw_period': 'draw_period',
    'count': 'count',
    'cost': 'cost',
    'is_active': 'is_active',
    'created_at': 'created_at',
}


def get_prizes_queryset() -> QuerySet[Prize]:
    return Prize.objects.all()


def filter_prizes(queryset: QuerySet[Prize], request) -> QuerySet[Prize]:
    search = request.GET.get('q', '').strip()
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search) | Q(description__icontains=search) | Q(type_prize__icontains=search),
        )

    is_main = [v for v in values_for(request, 'is_main') if v in ('1', '0')]
    if len(is_main) == 1:
        queryset = queryset.filter(is_main=(is_main[0] == '1'))

    is_active = [v for v in values_for(request, 'is_active') if v in ('1', '0')]
    if len(is_active) == 1:
        queryset = queryset.filter(is_active=(is_active[0] == '1'))

    draw_periods = [
        v for v in values_for(request, 'draw_period')
        if v in (Prize.DrawPeriod.WEEKLY, Prize.DrawPeriod.MONTHLY)
    ]
    if draw_periods:
        queryset = queryset.filter(draw_period__in=draw_periods)

    return queryset
