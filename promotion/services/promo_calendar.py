"""
Календарь Акции: номер недельного и месячного периода по дате.

Периоды регистрации чеков — это календарные недели (понедельник — воскресенье),
а не «каждые 7 дней от даты старта». Первый период — исключение: он тянется от
даты старта Акции до даты, указанной в ``Raffle.first_week_end_date``, и может
быть как короче, так и длиннее календарной недели.

Для Акции «Купи и выиграй с Атемарской» (старт — четверг 01.10.2026, первый
розыгрыш — среда 14.10.2026) это даёт ровно 14 недельных периодов из брифа:

    неделя 1  — 01.10.2026 – 11.10.2026 (первый период, розыгрыш 14.10)
    неделя 2  — 12.10.2026 – 18.10.2026 (розыгрыш 21.10)
    неделя 3  — 19.10.2026 – 25.10.2026 (розыгрыш 28.10)
    ...
    неделя 14 — 04.01.2027 – 10.01.2027 (розыгрыш 13.01, он же главный)

Без склейки первых четырёх дней (01.10 – 04.10, четверг — воскресенье) в
отдельный период получилось бы 15 периодов на 14 дат розыгрышей, и календарь
разъехался бы с брифом начиная с первой же недели.

Старая формула ``(target - start).days // 7 + 1`` отсчитывала недели семидневками
от даты старта и из-за неполной первой недели давала сдвиг на один день: чек,
зарегистрированный в понедельник, попадал в предыдущий недельный период и
участвовал в уже прошедшем розыгрыше. Здесь номер недели считается по границам
календарных недель, поэтому сдвига нет.

Период чека определяется ДАТОЙ ЕГО РЕГИСТРАЦИИ участником (``Receipt.created_at``),
а не датой покупки и не датой модерации: в Правилах у каждого розыгрыша указан
именно «Период регистрации чеков». Модерация может занять сутки и больше —
привязка к ней переносила чек в чужой период в обе стороны.

Из этого правила есть одно исключение: если модерация чека завершилась уже
ПОСЛЕ определения Победителей его недельного периода, чек переносится в
следующий Еженедельный розыгрыш (а для последнего, четырнадцатого, периода —
в еженедельных розыгрышах не участвует вовсе). На Ежемесячный и
Главный розыгрыши это не влияет: там чек всегда учитывается по месяцу
регистрации, поэтому ``month`` переносу не подлежит.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.db.models import Max
from django.utils import timezone


def _as_local_date(value) -> date:
    """Дату/датувремя (в любой таймзоне) приводим к календарной дате в таймзоне проекта."""
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            return timezone.localtime(value).date()
        return value.date()
    return value


def _monday_of(value: date) -> date:
    return value - timedelta(days=value.weekday())


def first_week_end(raffle) -> date:
    """Дата окончания первого недельного периода Акции.

    Задаётся полем ``Raffle.first_week_end_date``. Если оно не заполнено, первый
    период заканчивается ближайшим воскресеньем после старта — поведение по
    умолчанию для акций, которым склейка не нужна.
    """
    explicit = getattr(raffle, 'first_week_end_date', None)
    if explicit:
        return _as_local_date(explicit)
    start = _as_local_date(raffle.start_date)
    return start + timedelta(days=6 - start.weekday())


def week_num_on_date(raffle, target) -> int:
    """Номер недельного периода Акции, в который попадает дата ``target`` (нумерация с 1)."""
    target = _as_local_date(target)
    boundary = first_week_end(raffle)
    if target <= boundary:
        return 1
    # Со второго периода недели снова календарные: считаем от понедельника,
    # следующего за концом первого периода.
    second_monday = _monday_of(boundary + timedelta(days=1))
    return (_monday_of(target) - second_monday).days // 7 + 2


def month_num_on_date(raffle, target) -> int:
    """Номер месячного периода Акции (месяцы календарные, нумерация с 1)."""
    start = _as_local_date(raffle.start_date)
    target = _as_local_date(target)
    return (target.year - start.year) * 12 + (target.month - start.month) + 1


def week_bounds(raffle, week_num: int) -> tuple[date, date]:
    """Границы недельного периода (включительно).

    Первый период — от даты старта Акции до ``first_week_end``; остальные —
    обычные календарные недели пн–вс.
    """
    start = _as_local_date(raffle.start_date)
    boundary = first_week_end(raffle)
    if week_num <= 1:
        return start, boundary
    second_monday = _monday_of(boundary + timedelta(days=1))
    week_start = second_monday + timedelta(days=7 * (week_num - 2))
    return week_start, week_start + timedelta(days=6)


def weekly_draw_date(raffle, week_num: int) -> date:
    """
    Плановая дата определения Победителей недельного периода — ближайший день
    розыгрыша (``Raffle.week_day``) ПОСЛЕ окончания периода. Для старта 01.10.2026,
    первого периода до 11.10.2026 и розыгрышей по средам даёт ровно даты из брифа:
    14.10, 21.10, 28.10, ..., 13.01.2027.
    """
    _, end = week_bounds(raffle, week_num)
    return end + timedelta(days=((raffle.week_day - end.weekday()) % 7) or 7)


def last_week_num(raffle) -> int:
    """
    Номер последнего недельного периода. Берётся из призов, заведённых на недели
    (в Акции их 14 — по числу еженедельных розыгрышей из брифа), с запасным
    вариантом по календарю, если призы ещё не заведены.
    """
    from ..models import Prize
    last = Prize.objects.filter(
        is_active=True, is_main=False, draw_period=Prize.DrawPeriod.WEEKLY, week__isnull=False,
    ).aggregate(last=Max('week'))['last']
    return last or week_num_on_date(raffle, raffle.end_date)


def weekly_draw_calendar(raffle) -> dict[int, datetime | None]:
    """
    По каждому недельному периоду — момент, когда его розыгрыш фактически прошёл
    (``None``, если ещё не проводился).

    Берётся из уже сохранённых итогов розыгрыша, а не из плановой даты: розыгрыш
    может пройти позже плановой даты (ручной перезапуск), и тогда чеки, успевшие
    пройти модерацию до него, по п. 4.5 всё ещё участвуют. Если итогов нет, но
    плановая дата давно прошла (например, пул был пуст), период считается
    закрытым по плановой дате — иначе чеки застревали бы в нём навсегда.
    """
    from ..models import PromotionDrawResult

    held: dict[int, datetime] = {}
    rows = (
        PromotionDrawResult.objects
        .filter(week_num__isnull=False)
        .values('week_num')
        .annotate(held_at=Max('created_at'))
    )
    for row in rows:
        held[row['week_num']] = row['held_at']

    today = timezone.localtime().date()
    calendar: dict[int, datetime | None] = {}
    for week in range(1, last_week_num(raffle) + 1):
        if week in held:
            calendar[week] = held[week]
            continue
        scheduled = weekly_draw_date(raffle, week)
        calendar[week] = (
            timezone.make_aware(
                datetime.combine(scheduled, time.max), timezone.get_current_timezone(),
            )
            if today > scheduled else None
        )
    return calendar


def registration_date(receipt) -> date:
    """Дата регистрации чека участником — точка отсчёта периода Акции для этого чека."""
    return _as_local_date(receipt.created_at or timezone.now())


def moderation_moment(receipt) -> datetime:
    """
    Момент завершения модерации чека — от него зависит перенос по п. 4.5.

    У чеков, подтверждённых до того, как панель модератора научилась проставлять
    ``moderated_at``, его нет. Для них берётся ``updated_at`` (последнее сохранение
    чека — практически момент решения модератора), а не «сейчас»: иначе при
    пересчёте все такие чеки разом уехали бы в ближайший непроведённый розыгрыш.
    """
    return receipt.moderated_at or receipt.updated_at or receipt.created_at or timezone.now()


def weekly_period_for_receipt(raffle, receipt, draw_calendar=None) -> int | None:
    """
    Недельный розыгрыш, в котором участвует чек (п. 4.5 Правил).

    Базово — период регистрации чека. Если модерация завершилась уже после
    определения Победителей этого периода, чек переносится в следующий
    Еженедельный розыгрыш; если непроведённых периодов не осталось — чек в
    еженедельных розыгрышах не участвует (``None``), но остаётся в Ежемесячном
    и Главном.
    """
    if draw_calendar is None:
        draw_calendar = weekly_draw_calendar(raffle)

    week = week_num_on_date(raffle, registration_date(receipt))
    moderated_at = moderation_moment(receipt)
    last = last_week_num(raffle)

    while week <= last:
        held_at = draw_calendar.get(week)
        if held_at is None or moderated_at <= held_at:
            return week
        week += 1
    return None


def assign_promo_period(receipt, raffle=None, draw_calendar=None) -> list[str]:
    """
    Проставляет чеку ``week`` и ``month``: месяц — по дате регистрации, неделя —
    по дате регистрации с учётом переноса из п. 4.5 Правил.

    Вызывать ПОСЛЕ того, как выставлен ``receipt.moderated_at`` — от него зависит
    перенос. Возвращает список изменённых полей: его удобно добавить в
    ``update_fields``. Идемпотентна: повторный вызов ничего не меняет.
    """
    if raffle is None:
        from ..models import Raffle
        raffle = Raffle.objects.filter(is_active=True).first() or Raffle.objects.first()
    if raffle is None:
        return []

    week = weekly_period_for_receipt(raffle, receipt, draw_calendar=draw_calendar)
    month = month_num_on_date(raffle, registration_date(receipt))

    changed = []
    if receipt.week != week:
        receipt.week = week
        changed.append('week')
    if receipt.month != month:
        receipt.month = month
        changed.append('month')
    return changed
