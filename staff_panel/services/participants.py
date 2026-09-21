from __future__ import annotations

from django.conf import settings
from django.db.models import Count, Exists, IntegerField, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce

from promotion.models import PromotionDrawResult, PromotionDrawResultMainRaffle, Receipt, User

from .filters import values_for
from .phone_search import annotate_phone_digits, looks_like_phone_query, phone_search_q

PARTICIPANT_SORT_FIELDS = {
    'last_name': 'last_name',
    'email': 'email',
    'phone': 'phone',
    'city': 'city',
    'receipts_count': 'receipts_count',
    'created_at': 'created_at',
}


def _receipt_count_subquery(status_filter=Q()):
    return Coalesce(
        Subquery(
            Receipt.objects
            .filter(participant=OuterRef('pk'))
            .filter(status_filter)
            .values('participant')
            .annotate(c=Count('*'))
            .values('c'),
            output_field=IntegerField(),
        ),
        Value(0),
    )


def get_participants_queryset() -> QuerySet[User]:
    queryset = User.objects.all() if settings.DEBUG else User.objects.filter(is_staff=False)
    return queryset.annotate(
        receipts_count=_receipt_count_subquery(),
        receipts_confirmed=_receipt_count_subquery(Q(status=Receipt.Status.CONFIRMED)),
        receipts_pending=_receipt_count_subquery(Q(status=Receipt.Status.PENDING)),
        receipts_rejected=_receipt_count_subquery(Q(status=Receipt.Status.REJECTED)),
        receipts_winner=_receipt_count_subquery(Q(status=Receipt.Status.WINNER)),
        receipts_frozen=_receipt_count_subquery(Q(status=Receipt.Status.FROZEN)),
        is_weekly_winner=Exists(
            PromotionDrawResult.objects.filter(participant=OuterRef('pk'), is_reserve=False),
        ),
        is_main_winner=Exists(
            PromotionDrawResultMainRaffle.objects.filter(participant=OuterRef('pk'), is_reserve=False),
        ),
    )


def filter_participants(queryset: QuerySet[User], request) -> QuerySet[User]:
    search = request.GET.get('q', '').strip()
    if search:
        # Поисковая строка сначала классифицируется по виду (email / телефон /
        # текст) и ищется только в подходящих полях — раньше запрос всегда искал
        # сразу везде, включая телефон по цифрам из строки, из-за чего, например,
        # цифры в email (день рождения и т.п.) случайно совпадали с чужими
        # номерами телефонов и засоряли выдачу нерелевантными результатами.
        if '@' in search:
            queryset = queryset.filter(email__icontains=search)
        elif looks_like_phone_query(search):
            queryset = annotate_phone_digits(queryset, 'phone', 'phone_digits')
            queryset = queryset.filter(phone_search_q('phone_digits', search))
        else:
            queryset = queryset.filter(
                Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(middle_name__icontains=search)
                | Q(city__icontains=search)
            )

    cities = values_for(request, 'city')
    if cities:
        queryset = queryset.filter(city__in=cities)

    count_from = request.GET.get('count_from', '').strip()
    count_to = request.GET.get('count_to', '').strip()
    if count_from.isdigit():
        queryset = queryset.filter(receipts_count__gte=int(count_from))
    if count_to.isdigit():
        queryset = queryset.filter(receipts_count__lte=int(count_to))

    win_types = set(values_for(request, 'win_type'))
    if 'weekly' in win_types:
        queryset = queryset.filter(is_weekly_winner=True)
    if 'main' in win_types:
        queryset = queryset.filter(is_main_winner=True)

    is_blocked = [v for v in values_for(request, 'is_blocked') if v in ('1', '0')]
    if len(is_blocked) == 1:
        queryset = queryset.filter(is_blocked=(is_blocked[0] == '1'))

    email_confirmed = [v for v in values_for(request, 'email_confirmed') if v in ('1', '0')]
    if len(email_confirmed) == 1:
        queryset = queryset.filter(is_active=(email_confirmed[0] == '1'))

    reg_from = request.GET.get('date_reg_from', '').strip()
    reg_to = request.GET.get('date_reg_to', '').strip()
    if reg_from:
        queryset = queryset.filter(created_at__date__gte=reg_from)
    if reg_to:
        queryset = queryset.filter(created_at__date__lte=reg_to)

    engagement_conditions = {
        'no_receipts': Q(receipts_count=0),
        'one_pending': Q(receipts_count=1, receipts_pending=1),
        'one_confirmed': Q(receipts_count=1, receipts_confirmed=1),
        'one_rejected': Q(receipts_count=1, receipts_rejected=1),
        'one_frozen': Q(receipts_count=1, receipts_frozen=1),
        'multiple': Q(receipts_count__gte=2),
    }
    engagement = [v for v in values_for(request, 'engagement') if v in engagement_conditions]
    if engagement:
        condition = Q()
        for value in engagement:
            condition |= engagement_conditions[value]
        queryset = queryset.filter(condition)

    return queryset


def get_city_choices():
    return list(
        User.objects.exclude(city__isnull=True).exclude(city='')
        .values_list('city', flat=True).distinct().order_by('city'),
    )


def get_receipt_stats(participant: User) -> dict:
    receipts = Receipt.objects.filter(participant=participant)
    return {
        'total': receipts.count(),
        'confirmed': receipts.filter(status=Receipt.Status.CONFIRMED).count(),
        'pending': receipts.filter(status=Receipt.Status.PENDING).count(),
        'rejected': receipts.filter(status=Receipt.Status.REJECTED).count(),
        'winner': receipts.filter(status=Receipt.Status.WINNER).count(),
        'frozen': receipts.filter(status=Receipt.Status.FROZEN).count(),
    }


def participant_status_label(participant: User) -> str:
    """
    Человекочитаемый статус участия — на замену неинформативному «Активен/не активен»
    (это поле означает лишь «подтвердил email», см. is_active). Считается по уже
    аннотированным receipts_count/receipts_confirmed/receipts_pending/receipts_rejected/
    receipts_frozen (см. get_participants_queryset), с фолбэком на прямой подсчёт, если
    участник получен не через неё (например, в модалке — participant, не annotated row).
    """
    total = getattr(participant, 'receipts_count', None)
    if total is None:
        stats = get_receipt_stats(participant)
        total = stats['total']
        confirmed, pending, rejected, frozen = (
            stats['confirmed'], stats['pending'], stats['rejected'], stats['frozen'],
        )
    else:
        confirmed = getattr(participant, 'receipts_confirmed', 0)
        pending = getattr(participant, 'receipts_pending', 0)
        rejected = getattr(participant, 'receipts_rejected', 0)
        frozen = getattr(participant, 'receipts_frozen', 0)

    if total == 0:
        return 'Зарегистрировался, но не загрузил ни одного чека'
    if total == 1:
        if pending == 1:
            return 'Зарегистрировался и загрузил один чек (на проверке)'
        if confirmed == 1:
            return 'Зарегистрировался и загрузил один чек (подтверждён)'
        if rejected == 1:
            return 'Зарегистрировался и загрузил один чек (отклонён)'
        if frozen == 1:
            return 'Зарегистрировался и загрузил один чек (заморожен)'
        return 'Зарегистрировался и загрузил один чек (победный)'
    return f'Зарегистрировался и загрузил чеков: {total}'


def guaranteed_prize_payout_info(participant):
    """Выплата гарантированного приза этого участника — для карточки участника.

    Возвращает саму запись с добавленными `stage_label`/`stage_hint` (тот же
    человеческий этап, что и в разделе Cyclops — см. services/payouts.py), либо
    None, если выплата ещё не заводилась.
    """
    from promotion.models import GuaranteedPrizePayout

    from . import payouts as payouts_services

    payout = payouts_services.annotate_stage(
        GuaranteedPrizePayout.objects.filter(participant=participant),
    ).first()
    if payout is None:
        return None

    meta = next((s for s in payouts_services.STAGES if s['value'] == payout.stage), None)
    payout.stage_label = meta['label'] if meta else payout.get_status_display()
    payout.stage_hint = meta['hint'] if meta else ''
    return payout
