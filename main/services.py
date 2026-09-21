
from collections import OrderedDict

from django.utils import timezone

from promotion.models import PromotionDrawResult, Raffle
from promotion.services.promo_calendar import week_bounds, week_num_on_date

MONTH_NAMES_RU_GENITIVE = {
    1: 'января', 2: 'февраля', 3: 'марта', 4: 'апреля',
    5: 'мая', 6: 'июня', 7: 'июля', 8: 'августа',
    9: 'сентября', 10: 'октября', 11: 'ноября', 12: 'декабря',
}


def _week_label(week_start, week_end, *, show_year) -> str:
    """«с 1 по 6 сентября» — либо с двумя месяцами/годом, если неделя их пересекает."""
    if week_start.month == week_end.month:
        label = f'с {week_start.day} по {week_end.day} {MONTH_NAMES_RU_GENITIVE[week_end.month]}'
    else:
        label = (
            f'с {week_start.day} {MONTH_NAMES_RU_GENITIVE[week_start.month]} '
            f'по {week_end.day} {MONTH_NAMES_RU_GENITIVE[week_end.month]}'
        )
    if show_year:
        label += f' {week_end.year}'
    return label


def _mask_participant(participant) -> dict:
    """Замаскированные данные победителя для публикации (бриф, п. 8)."""
    phone = (participant.phone or '').strip()
    tail = phone[-4:] if len(phone) >= 4 else ''
    return {
        'name': f'{participant.first_name} {(participant.last_name or "")[:1]}.'.strip(),
        'phone': f'+7 (***) ***-{tail[:2]}-{tail[2:]}' if tail else '+7 (***) ***-**-**',
        'city': participant.city or '',
    }


def _published_results(**filters):
    return (
        PromotionDrawResult.objects
        .filter(is_published=True, **filters)
        .select_related('participant', 'public_participant', 'prize')
        .order_by('created_at', 'participant__first_name')
    )


def _row(result) -> dict:
    # Замена победителя в дашборде (например, если победитель не выходит на
    # связь) не должна менять уже опубликованные итоги: public_participant —
    # это тот, кто был объявлен победителем изначально. Пока замен не было,
    # поле пустое и показывается текущий победитель.
    participant = result.public_participant or result.participant
    return {**_mask_participant(participant), 'prize': result.prize.name}


def get_winners_by_months() -> list:
    """Опубликованные итоги еженедельных и ежемесячных розыгрышей, сгруппированные
    по недельному периоду, ЗА который они разыгрывались (`PromotionDrawResult.week_num`),
    а не по дате, когда физически прошёл розыгрыш (он проходит на 1–3 дня позже
    закрытия периода регистрации чеков).

    Границы периодов берутся из календаря Акции (promo_calendar.week_bounds), а не
    пересчитываются здесь: первый недельный период Акции длиннее календарной недели
    (01.10 – 11.10.2026), и собственная арифметика «семидневками от старта» давала бы
    подписи, разъехавшиеся с розыгрышем.

    Моментальные призы сюда не попадают — у них свой блок (см.
    get_instant_winners_by_days): они разыгрываются не в дату розыгрыша, а
    непрерывно, и в таблице недельных итогов выглядели бы чужеродно.

    Если реальных опубликованных победителей ещё нет, возвращает пустой список,
    и блок на лендинге не отображается.
    """

    raffle = Raffle.objects.filter(is_active=True).first() or Raffle.objects.first()
    if not raffle:
        return []

    if raffle.start_date and timezone.now() <= raffle.start_date:
        return []

    campaign_end = timezone.localtime(raffle.end_date).date() if raffle.end_date else None

    winners_by_week = OrderedDict()
    for result in _published_results(is_instant=False):
        if result.week_num:
            week_num = result.week_num
        else:
            # Ежемесячный розыгрыш: показываем его в периоде, на который пришлась
            # дата самого розыгрыша.
            week_num = max(1, week_num_on_date(raffle, timezone.localtime(result.created_at).date()))
        winners_by_week.setdefault(week_num, []).append(_row(result))

    if not winners_by_week:
        return []

    bounds = {}
    for week_num in winners_by_week:
        week_start, week_end = week_bounds(raffle, week_num)
        if campaign_end and week_end > campaign_end:
            week_end = campaign_end
        bounds[week_num] = (week_start, week_end)

    years_present = {d.year for pair in bounds.values() for d in pair}
    show_year = len(years_present) > 1

    return [
        {
            'key': str(week_num),
            'label': _week_label(*bounds[week_num], show_year=show_year),
            'rows': rows,
        }
        for week_num, rows in sorted(winners_by_week.items(), key=lambda item: item[0], reverse=True)
    ]


def get_instant_winners_by_days(limit_days: int = 14) -> list:
    """Опубликованные победители моментальных призов, сгруппированные по дням.

    Моментальный приз достаётся участнику в момент игры, а не в дату розыгрыша,
    поэтому недельная группировка для него бессмысленна: группируем по дню
    выигрыша и показываем последние `limit_days` дней.
    """
    winners_by_day = OrderedDict()
    for result in _published_results(is_instant=True).order_by('-created_at'):
        day = timezone.localtime(result.created_at).date()
        winners_by_day.setdefault(day, []).append(_row(result))

    years_present = {day.year for day in winners_by_day}
    show_year = len(years_present) > 1

    return [
        {
            'key': day.isoformat(),
            'label': (
                f'{day.day} {MONTH_NAMES_RU_GENITIVE[day.month]}'
                + (f' {day.year}' if show_year else '')
            ),
            'rows': rows,
        }
        for day, rows in list(winners_by_day.items())[:limit_days]
    ]


MOCK_WINNERS_DATA = [
    {
        'key': '1',
        'label': 'с 1 по 11 октября',
        'rows': [
            {'name': 'Светлана С.', 'phone': '+7 (***) ***-59-95', 'prize': 'Сертификат Ozon 500 ₽'},
            {'name': 'Евгения Ч.', 'phone': '+7 (***) ***-33-90', 'prize': 'Сертификат Ozon 500 ₽'},
            {'name': 'Надежда Я.', 'phone': '+7 (***) ***-88-39', 'prize': 'Сертификат Ozon 1000 ₽'},
            {'name': 'Татьяна К.', 'phone': '+7 (***) ***-08-89', 'prize': 'Сертификат Ozon 1000 ₽'},
            {'name': 'Виктория К.', 'phone': '+7 (***) ***-67-81', 'prize': 'Аэрогриль'},
        ],
    },
]


def get_mock_winners() -> list:
    """Тестовые данные для блока "Победители" — включаются флагом MOCK_WINNERS в .env,
    когда в БД ещё нет реальных опубликованных итогов розыгрыша."""

    return MOCK_WINNERS_DATA
