from __future__ import annotations

import re
from dataclasses import dataclass

from django.core.paginator import Paginator
from django.db.models import Q

from promotion.models import PrizeFile, PromotionDrawResult, PromotionDrawResultMainRaffle
from promotion.services.winner_workflow import shipping_soon_email_sent

WINNER_SORT_FIELDS = {
    'created_at': 'created_at',
    'draw_type': 'draw_type',
    'period': 'period',
    'participant': 'participant',
    'prize': 'prize',
    'stage': 'stage',
    'status': 'status_oki_document',
    'delivery_status': 'delivery_status',
    'prize_file': 'prize_file',
    'email': 'email',
}

# Этапы обработки победителя по возрастанию: чем дальше в списке, тем дальше
# продвинут итог розыгрыша. Всё, кроме «Приз отправлен», читается из самой
# записи итога (публикация, договор, письмо), последний шаг — из поля
# «Доставлено».
WINNER_STAGES = (
    ('not_published', 'Не опубликован', 'gray'),
    ('published', 'Опубликован', 'blue'),
    ('contract_created', 'Договор создан', 'blue'),
    ('contract_sent', 'Договор отправлен', 'yellow'),
    ('contract_signed', 'Договор подписан', 'green'),
    ('prize_sent', 'Приз отправлен', 'green'),
)
WINNER_STAGE_ORDER = {key: index for index, (key, _, _) in enumerate(WINNER_STAGES)}
WINNER_STAGE_LABELS = {key: label for key, label, _ in WINNER_STAGES}
WINNER_STAGE_COLORS = {key: color for key, _, color in WINNER_STAGES}
WINNER_STAGE_CHOICES = tuple((key, label) for key, label, _ in WINNER_STAGES)

DELIVERY_STATUS_COLORS = {
    'да': 'green',
    'нет': 'red',
    'оформлен': 'blue',
}

DELIVERY_STATUS_CHOICES = (
    ('Да', 'green'),
    ('Нет', 'red'),
    ('Оформлен', 'blue'),
)

DRAW_TYPE_LABELS = {
    'instant': 'Моментальный',
    'weekly': 'Еженедельный',
    'monthly': 'Ежемесячный',
    'main': 'Главный',
}

# Коротко — для таблиц и Excel: полное «Еженедельный» раздувает колонку.
DRAW_TYPE_SHORT_LABELS = {
    'instant': 'Момент.',
    'weekly': 'Еженед.',
    'monthly': 'Ежемес.',
    'main': 'Главный',
}

DRAW_TYPE_CHOICES = tuple(
    (value, DRAW_TYPE_LABELS[value]) for value in ('instant', 'weekly', 'monthly', 'main')
)

CONTRACT_STATUS_CHOICES = (
    ('none', 'Договор не создан'),
    ('signed', 'Подписан'),
    ('other', 'В процессе'),
)


def get_prize_choices():
    """Список призов, реально фигурирующих среди победителей — для фильтра на вкладке."""
    from promotion.models import Prize

    prize_ids = set(
        PromotionDrawResult.objects.exclude(prize__isnull=True).values_list('prize_id', flat=True)
    ) | set(
        PromotionDrawResultMainRaffle.objects.exclude(prize__isnull=True).values_list('prize_id', flat=True)
    )
    return list(Prize.objects.filter(pk__in=prize_ids).order_by('name'))


def _format_fio(participant) -> str:
    parts = [participant.last_name, participant.first_name]
    full_name = ' '.join(part for part in parts if part).strip()
    return full_name or participant.email


def _format_phone(phone: str) -> str:
    raw = str(phone).strip()
    if not raw:
        return raw
    if raw.startswith('+7'):
        return '8' + raw[2:]
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 11 and digits.startswith('7'):
        return '8' + digits[1:]
    return raw


def _format_city_address(participant) -> str:
    parts = []
    if participant.city:
        parts.append(str(participant.city).strip())
    if participant.address:
        parts.append(str(participant.address).strip())
    return ', '.join(parts)


@dataclass
class WinnerRow:
    kind: str  # 'weekly' | 'main' (маршрутизация: 'weekly' покрывает и еженедельные, и ежемесячные призы)
    kind_label: str
    obj: object  # PromotionDrawResult | PromotionDrawResultMainRaffle
    participant: object
    prize: object
    receipt: object | None
    week_num: int | None
    is_published: bool | None
    status_oki_document: str | None
    created_at: object
    month_num: int | None = None
    contract_link: str = ''
    is_send_email: bool = False
    email_sent_at: object = None
    shipping_soon_sent: bool = False
    date_result_raffle: object = None
    delivery_status: str = ''
    contract_issued_at: object = None
    prize_file: object = None  # promotion.models.PrizeFile | None — проставляется во вьюхе
    replacements_count: int = 0  # сколько раз победителя в этом слоте уже заменяли
    is_instant: bool = False  # приз выигран в лотке яиц, а не в розыгрыше по расписанию

    @property
    def pk(self):
        return self.obj.pk

    @property
    def public_participant(self):
        """Победитель, который показан на лендинге, — если его уже заменяли."""
        return getattr(self.obj, 'public_participant', None)

    @property
    def can_replace(self) -> bool:
        """Заменить победителя можно, пока договор не подписан и не истекает срок на подпись."""
        from .winner_replacement import can_replace
        return can_replace(self)

    @property
    def replace_block_reason(self) -> str:
        """Почему победителя нельзя заменить прямо сейчас — пусто, если можно."""
        from .winner_replacement import replace_block_reason
        return replace_block_reason(self)

    @property
    def delivery_color(self) -> str:
        key = (self.delivery_status or '').strip().lower()
        return DELIVERY_STATUS_COLORS.get(key, 'gray')

    @property
    def prize_file_name(self) -> str:
        return self.prize_file.name if self.prize_file else ''

    @property
    def is_contract_issued(self) -> bool:
        return bool((self.status_oki_document or '').strip() or (self.contract_link or '').strip())

    @property
    def is_prize_delivered(self) -> bool:
        return (self.delivery_status or '').strip().lower() == 'да'

    @property
    def stage(self) -> str:
        """Самый дальний из уже пройденных этапов обработки победителя."""
        if self.is_prize_delivered:
            return 'prize_sent'
        if (self.status_oki_document or '').strip() == 'Подписан':
            return 'contract_signed'
        if self.is_send_email:
            return 'contract_sent'
        if self.is_contract_issued:
            return 'contract_created'
        # У главного и моментального призов шага публикации нет — итог существует
        # сразу опубликованным.
        if self.kind == 'main' or self.is_instant or self.is_published:
            return 'published'
        return 'not_published'

    @property
    def stage_label(self) -> str:
        return WINNER_STAGE_LABELS[self.stage]

    @property
    def stage_color(self) -> str:
        return WINNER_STAGE_COLORS[self.stage]

    @property
    def stage_order(self) -> int:
        return WINNER_STAGE_ORDER[self.stage]

    @property
    def draw_type(self) -> str:
        """Тип розыгрыша строки — ключ, совпадающий со значениями фильтра."""
        if self.kind == 'main':
            return 'main'
        if self.is_instant:
            return 'instant'
        return 'monthly' if self.month_num is not None else 'weekly'

    @property
    def draw_type_short_label(self) -> str:
        return DRAW_TYPE_SHORT_LABELS[self.draw_type]

    @property
    def draw_type_label(self) -> str:
        return DRAW_TYPE_LABELS[self.draw_type]

    @property
    def contract_status(self) -> str:
        status = (self.status_oki_document or '').strip()
        if not status:
            return 'none'
        if status == 'Подписан':
            return 'signed'
        return 'other'

    @property
    def contract_status_class(self) -> str:
        return {
            'none': '',
            'other': 'pending',
            'signed': 'confirmed',
        }[self.contract_status]

    @property
    def fio(self) -> str:
        return _format_fio(self.participant)

    @property
    def phone(self) -> str:
        return _format_phone(self.participant.phone) if self.participant.phone else ''

    @property
    def email(self) -> str:
        return str(self.participant.email).strip() if self.participant.email else ''

    @property
    def city_address(self) -> str:
        return _format_city_address(self.participant)

    @property
    def period_label(self) -> str:
        if self.is_instant:
            # У моментального приза нет периода розыгрыша: он выигран в конкретный
            # момент, и этот момент — дата самой записи.
            return 'Моментальный'
        if self.month_num is not None:
            return f'Месяц {self.month_num}'
        if self.week_num is not None:
            return f'Неделя {self.week_num}'
        return '—'


def attach_prize_files(rows: list[WinnerRow]) -> list[WinnerRow]:
    """Проставляет выданный файл электронного приза одним запросом на все строки."""
    if not rows:
        return rows

    weekly_ids = [r.pk for r in rows if r.kind != 'main']
    main_ids = [r.pk for r in rows if r.kind == 'main']
    by_key: dict[tuple[str, int], object] = {}
    for prize_file in PrizeFile.objects.filter(
        Q(draw_result_id__in=weekly_ids) | Q(main_draw_result_id__in=main_ids)
    ).select_related('kind'):
        if prize_file.draw_result_id:
            by_key[('weekly', prize_file.draw_result_id)] = prize_file
        elif prize_file.main_draw_result_id:
            by_key[('main', prize_file.main_draw_result_id)] = prize_file

    for row in rows:
        row.prize_file = by_key.get((row.kind, row.pk))
    return rows


def attach_replacement_counts(rows: list[WinnerRow]) -> list[WinnerRow]:
    """Проставляет число замен победителя одним запросом на все строки."""
    if not rows:
        return rows

    from .winner_replacement import replacement_counts

    counts = replacement_counts(rows)
    for row in rows:
        row.replacements_count = counts.get((row.kind, row.pk), 0)
    return rows


def build_weekly_row(dr) -> WinnerRow:
    """Строка таблицы «Победители» для итога из PromotionDrawResult.

    Одна функция на весь модуль: раньше та же сборка была продублирована в
    collect_winner_rows и find_winner_row, и поля, добавленные в одном месте
    (например, признак моментального приза), во втором молча терялись.
    """
    if dr.is_instant:
        kind_label = 'Моментальный приз'
    elif dr.month_num is not None:
        kind_label = 'Ежемесячный приз'
    else:
        kind_label = 'Еженедельный приз'

    return WinnerRow(
        kind='weekly',
        kind_label=kind_label,
        is_instant=dr.is_instant,
        obj=dr,
        participant=dr.participant,
        prize=dr.prize,
        receipt=dr.receipt,
        week_num=dr.week_num,
        month_num=dr.month_num,
        is_published=dr.is_published,
        status_oki_document=dr.receipt.status_oki_document if dr.receipt else None,
        created_at=dr.created_at,
        contract_link=(dr.receipt.link_oki_document if dr.receipt else '') or '',
        is_send_email=bool(dr.receipt and dr.receipt.is_send_email),
        email_sent_at=dr.receipt.email_sent_at if dr.receipt else None,
        shipping_soon_sent=bool(dr.receipt and shipping_soon_email_sent(dr.receipt)),
        date_result_raffle=dr.receipt.date_result_raffle if dr.receipt else None,
        delivery_status=dr.delivery_status or '',
        contract_issued_at=dr.receipt.oki_document_issued_at if dr.receipt else None,
    )


def collect_winner_rows() -> list[WinnerRow]:
    rows: list[WinnerRow] = []

    for dr in PromotionDrawResult.objects.filter(is_reserve=False).select_related(
        'participant', 'prize', 'receipt', 'public_participant',
    ):
        rows.append(build_weekly_row(dr))

    for dr in PromotionDrawResultMainRaffle.objects.filter(is_reserve=False).select_related(
        'participant', 'prize', 'public_participant',
    ):
        rows.append(WinnerRow(
            kind='main',
            kind_label='Главный приз',
            obj=dr,
            participant=dr.participant,
            prize=dr.prize,
            receipt=None,
            week_num=None,
            is_published=None,
            status_oki_document=dr.status_oki_document,
            created_at=dr.created_at,
            contract_link=dr.link_oki_document or '',
            is_send_email=bool(dr.is_send_email),
            email_sent_at=dr.email_sent_at,
            shipping_soon_sent=False,
            date_result_raffle=None,
            delivery_status=dr.delivery_status or '',
            contract_issued_at=dr.oki_document_issued_at,
        ))

    return attach_replacement_counts(attach_prize_files(rows))


def _multi(request, key: str) -> list[str]:
    """
    Значения фильтра из URL. Панель фильтров (как на FarPostLogs) пишет
    множественный выбор одним параметром через запятую — `?draw_type=weekly,main`,
    но старые ссылки с повторяющимся параметром тоже должны работать.
    """
    values: list[str] = []
    for raw in request.GET.getlist(key):
        for part in str(raw).split(','):
            part = part.strip()
            if part and part not in values:
                values.append(part)
    return values


def filter_winner_rows(rows: list[WinnerRow], request) -> list[WinnerRow]:
    draw_types = _multi(request, 'draw_type')
    weeks = _multi(request, 'week')
    months = _multi(request, 'month')
    contract_statuses = _multi(request, 'contract_status')
    prize_ids = _multi(request, 'prize')
    stages = _multi(request, 'stage')
    email_sent = _multi(request, 'email_sent')
    delivery_statuses = _multi(request, 'delivery_status')
    prize_files = _multi(request, 'prize_file')
    search = request.GET.get('q', '').strip().lower()

    if draw_types:
        rows = [r for r in rows if r.draw_type in draw_types]

    if weeks:
        wanted = {int(w) for w in weeks if w.isdigit()}
        rows = [r for r in rows if r.week_num in wanted]

    if months:
        wanted = {int(m) for m in months if m.isdigit()}
        rows = [r for r in rows if r.month_num in wanted]

    if contract_statuses:
        rows = [r for r in rows if r.contract_status in contract_statuses]

    if prize_ids:
        wanted = {int(p) for p in prize_ids if p.isdigit()}
        rows = [r for r in rows if r.prize and r.prize.pk in wanted]

    if stages:
        rows = [r for r in rows if r.stage in stages]

    if email_sent:
        wanted = {v == '1' for v in email_sent if v in ('1', '0')}
        rows = [r for r in rows if r.is_send_email in wanted]

    if delivery_statuses:
        wanted = {v.lower() for v in delivery_statuses}
        rows = [
            r for r in rows
            if ((r.delivery_status or '').strip().lower() in wanted)
            or ('__empty__' in wanted and not (r.delivery_status or '').strip())
        ]

    if prize_files:
        wanted = {v == '1' for v in prize_files if v in ('1', '0')}
        rows = [r for r in rows if bool(r.prize_file) in wanted]

    if search:
        def matches(row: WinnerRow) -> bool:
            haystack = ' '.join(filter(None, [
                row.participant.email,
                row.participant.first_name,
                row.participant.last_name,
                row.prize.name if row.prize else '',
                row.prize_file_name,
            ])).lower()
            return search in haystack

        rows = [r for r in rows if matches(r)]

    return rows


_DRAW_TYPE_SORT_ORDER = {'weekly': 0, 'monthly': 1, 'main': 2}

WINNER_SORT_KEY_FUNCS = {
    'created_at': lambda r: r.created_at,
    'draw_type': lambda r: _DRAW_TYPE_SORT_ORDER.get(r.draw_type, 99),
    'period': lambda r: (
        _DRAW_TYPE_SORT_ORDER.get(r.draw_type, 99),
        r.month_num if r.month_num is not None else (r.week_num or 0),
    ),
    'participant': lambda r: (r.participant.last_name or r.participant.email or '').lower(),
    'prize': lambda r: (r.prize.name or '').lower() if r.prize else '',
    'stage': lambda r: r.stage_order,
    'status': lambda r: r.status_oki_document or '',
    'delivery_status': lambda r: (r.delivery_status or '').lower(),
    'prize_file': lambda r: r.prize_file_name.lower(),
    'email': lambda r: (1 if r.is_send_email else 0, r.email_sent_at.timestamp() if r.email_sent_at else 0),
}


def sort_winner_rows(rows: list[WinnerRow], *, sort: str, direction: str) -> list[WinnerRow]:
    key_func = WINNER_SORT_KEY_FUNCS.get(sort or '', WINNER_SORT_KEY_FUNCS['created_at'])
    return sorted(rows, key=key_func, reverse=(direction == 'desc'))


def paginate_winner_rows(rows: list[WinnerRow], page_number, per_page: int):
    paginator = Paginator(rows, per_page)
    return paginator.get_page(page_number), paginator


def get_available_weeks() -> list[int]:
    return sorted({
        w for w in PromotionDrawResult.objects.values_list('week_num', flat=True) if w is not None
    })


def get_available_months() -> list[int]:
    return sorted({
        m for m in PromotionDrawResult.objects.values_list('month_num', flat=True) if m is not None
    })


def available_draws() -> list[dict]:
    """
    Список уже проведённых розыгрышей для выбора в модалке авто-раздачи файлов —
    сначала последние (по убыванию номера), в конце главный приз (он один).
    """
    rows = collect_winner_rows()

    weeks = sorted(
        {r.week_num for r in rows if r.kind == 'weekly' and r.month_num is None},
        reverse=True,
    )
    months = sorted(
        {r.month_num for r in rows if r.kind == 'weekly' and r.month_num is not None},
        reverse=True,
    )
    has_main = any(r.kind == 'main' for r in rows)

    draws = [
        {'value': f'weekly:{week}', 'kind': 'weekly', 'period': week,
         'label': f'Еженедельный · Неделя {week}'}
        for week in weeks
    ]
    draws += [
        {'value': f'monthly:{month}', 'kind': 'monthly', 'period': month,
         'label': f'Ежемесячный · Месяц {month}'}
        for month in months
    ]
    if has_main:
        draws.append({'value': 'main:', 'kind': 'main', 'period': None, 'label': 'Главный приз'})
    return draws


def draw_scope(kind: str, period: int | None) -> tuple[str, list[WinnerRow]] | None:
    """Строки победителей конкретного розыгрыша + подпись периода, либо None, если такого розыгрыша не было."""
    rows = collect_winner_rows()

    if kind == 'main':
        group = [r for r in rows if r.kind == 'main']
        label = 'Главный приз'
    elif kind == 'monthly':
        group = [r for r in rows if r.kind == 'weekly' and r.month_num == period]
        label = f'Месяц {period}'
    elif kind == 'weekly':
        group = [r for r in rows if r.kind == 'weekly' and r.month_num is None and r.week_num == period]
        label = f'Неделя {period}'
    else:
        return None

    if not group:
        return None
    return label, group


def find_winner_row(kind: str, pk: int) -> WinnerRow | None:
    if kind == 'weekly':
        try:
            dr = PromotionDrawResult.objects.select_related(
                'participant', 'prize', 'receipt', 'public_participant',
            ).get(pk=pk)
        except PromotionDrawResult.DoesNotExist:
            return None
        return attach_replacement_counts(attach_prize_files([build_weekly_row(dr)]))[0]

    if kind == 'main':
        try:
            dr = PromotionDrawResultMainRaffle.objects.select_related(
                'participant', 'prize', 'public_participant',
            ).get(pk=pk)
        except PromotionDrawResultMainRaffle.DoesNotExist:
            return None
        return attach_replacement_counts(attach_prize_files([WinnerRow(
            kind='main',
            kind_label='Главный приз',
            obj=dr,
            participant=dr.participant,
            prize=dr.prize,
            receipt=None,
            week_num=None,
            is_published=None,
            status_oki_document=dr.status_oki_document,
            created_at=dr.created_at,
            contract_link=dr.link_oki_document or '',
            is_send_email=bool(dr.is_send_email),
            email_sent_at=dr.email_sent_at,
            shipping_soon_sent=False,
            date_result_raffle=None,
            delivery_status=dr.delivery_status or '',
            contract_issued_at=dr.oki_document_issued_at,
        )]))[0]

    return None
