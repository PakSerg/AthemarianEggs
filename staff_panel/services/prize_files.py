"""
Файлы электронных призов: загрузка партиями, выдача победителю, освобождение.

Правила, на которых всё держится:

* склад файлов — это пара (вид приза, тип розыгрыша), а не приз конкретной
  недели: «сертификат Ozon на 4 000 ₽» — девять строк Prize (по одной на неделю),
  но один склад на все девять недель. При этом еженедельный и ежемесячный
  розыгрыши одного вида — разные склады: под них закупают разные партии;
* тип розыгрыша при загрузке выбирается только из тех, в которых вид приза
  реально разыгрывается, — загрузить файл «в никуда» нельзя;
* имя файла — уникальный идентификатор на всю акцию; повторное имя не загружается
  и не переименовывается молча — вся партия отклоняется с понятной ошибкой;
* один файл выдаётся не более чем одному победителю (гонки двух менеджеров
  отсекаются блокировкой строки в транзакции);
* удаление итога розыгрыша файл не удаляет — только освобождает (см.
  promotion.signals.release_prize_files_on_draw_result_delete).
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from promotion.models import Prize, PrizeFile, PrizeKind

MAX_FILES_PER_UPLOAD = 300
# Ограничение одно: суммарный размер партии за одну загрузку. Лимита на
# отдельный файл нет — он только путал (файлы призов это сертификаты и
# промокоды, каждый заметно меньше партии целиком).
MAX_UPLOAD_BATCH_SIZE = 25 * 1024 * 1024  # 25 МБ суммарно за одну загрузку

DRAW_TYPE_LABELS = dict(PrizeFile.DrawType.choices)
# Порядок, в котором типы розыгрыша показываются везде — от частого к редкому.
DRAW_TYPE_ORDER = (
    PrizeFile.DrawType.INSTANT,
    PrizeFile.DrawType.WEEKLY,
    PrizeFile.DrawType.MONTHLY,
    PrizeFile.DrawType.MAIN,
)

PRIZE_FILE_SORT_FIELDS = {
    'name': 'name',
    'prize': 'kind__name',
    'draw_type': 'draw_type',
    'uploaded_at': 'uploaded_at',
}


class PrizeFileError(Exception):
    """Ошибка, текст которой можно показать менеджеру как есть."""


@dataclass
class UploadResult:
    created: list[PrizeFile]
    kind: PrizeKind
    draw_type: str


# ─────────────────── в каких розыгрышах разыгрывается вид ───────────────────

def _prize_draw_type(is_main: bool, draw_period: str) -> str:
    if is_main:
        return PrizeFile.DrawType.MAIN
    if draw_period == Prize.DrawPeriod.MONTHLY:
        return PrizeFile.DrawType.MONTHLY
    if draw_period == Prize.DrawPeriod.INSTANT:
        # Под моментальные призы закупают отдельную партию сертификатов —
        # смешивать её с недельной нельзя, это разные склады.
        return PrizeFile.DrawType.INSTANT
    return PrizeFile.DrawType.WEEKLY


def draw_types_by_kind(kind_ids) -> dict[int, list[str]]:
    """
    Типы розыгрышей, в которых вид приза реально разыгрывается, — по одному
    запросу на всю страницу. Именно из них менеджер выбирает при загрузке:
    предложить тип, в котором приз не разыгрывается, значит дать залить файлы,
    которые потом никому не подберутся.
    """
    kind_ids = list(kind_ids)
    if not kind_ids:
        return {}

    found: dict[int, set[str]] = defaultdict(set)
    rows = Prize.objects.filter(kind_id__in=kind_ids).values_list('kind_id', 'is_main', 'draw_period')
    for kind_id, is_main, draw_period in rows:
        found[kind_id].add(_prize_draw_type(bool(is_main), draw_period))

    return {
        kind_id: [t for t in DRAW_TYPE_ORDER if t in types]
        for kind_id, types in found.items()
    }


def draw_types_for_kind(kind: PrizeKind) -> list[str]:
    return draw_types_by_kind([kind.pk]).get(kind.pk, [])


# ─────────────────────────── имена файлов ───────────────────────────

def normalize_name(filename: str) -> str:
    """Имя файла без пути и лишних пробелов — оно же идентификатор."""
    name = os.path.basename(str(filename or '')).strip()
    return name


def find_duplicates(names: list[str]) -> dict[str, str]:
    """
    Имена из партии, которые уже заняты в базе (регистр не важен):
    имя из партии → уже загруженное имя.
    """
    if not names:
        return {}

    condition = Q()
    for name in names:
        condition |= Q(name__iexact=name)
    taken = PrizeFile.objects.filter(condition).values_list('name', flat=True)

    lowered = {name.lower(): name for name in names}
    return {
        lowered[name.lower()]: name
        for name in taken
        if name.lower() in lowered
    }


# ─────────────────────────── загрузка ───────────────────────────

def upload_prize_files(*, kind_id: str, draw_type: str, files: list, user) -> UploadResult:
    """
    Загружает партию файлов одной транзакцией: либо все, либо ни одного.

    Бросает PrizeFileError с человекочитаемым текстом при любой проблеме —
    неизвестный приз, пустая партия, слишком большая партия, неуникальное имя,
    тип розыгрыша, в котором этот приз не разыгрывается.
    """
    if not str(kind_id or '').isdigit():
        raise PrizeFileError('Выберите приз.')
    kind = PrizeKind.objects.filter(pk=int(kind_id)).first()
    if kind is None:
        raise PrizeFileError('Приз не найден.')
    if not kind.prizes.filter(is_electronic=True).exists():
        raise PrizeFileError(
            f'Приз «{kind.name}» не электронный — файлы призов для него не загружаются. '
            'Если это сертификат или промокод, отметьте приз как электронный на вкладке «Призы».'
        )

    available = draw_types_for_kind(kind)
    if draw_type not in DRAW_TYPE_LABELS:
        raise PrizeFileError('Выберите тип розыгрыша.')
    if draw_type not in available:
        listed = ', '.join(DRAW_TYPE_LABELS[t].lower() for t in available) or '—'
        raise PrizeFileError(
            f'Приз «{kind.name}» не разыгрывается в розыгрышах типа '
            f'«{DRAW_TYPE_LABELS[draw_type].lower()}» — такие файлы никому не подберутся. '
            f'Доступные типы: {listed}.'
        )

    files = [f for f in files if f]
    if not files:
        raise PrizeFileError('Выберите хотя бы один файл.')
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise PrizeFileError(f'За один раз можно загрузить не больше {MAX_FILES_PER_UPLOAD} файлов.')

    names = []
    total_size = 0
    for upload in files:
        name = normalize_name(upload.name)
        if not name:
            raise PrizeFileError('У одного из файлов пустое имя.')
        names.append(name)
        total_size += upload.size or 0

    if total_size > MAX_UPLOAD_BATCH_SIZE:
        raise PrizeFileError(
            f'Максимальный совокупный размер загружаемых за раз файлов — '
            f'{MAX_UPLOAD_BATCH_SIZE // (1024 * 1024)} МБ, а выбрано '
            f'{total_size / (1024 * 1024):.1f} МБ. Загрузите файлы меньшими партиями.'
        )

    # дубли внутри самой партии
    seen: dict[str, str] = {}
    inner_duplicates = []
    for name in names:
        key = name.lower()
        if key in seen:
            inner_duplicates.append(name)
        seen[key] = name
    if inner_duplicates:
        raise PrizeFileError(
            'В выбранной партии есть файлы с одинаковыми именами: '
            + ', '.join(sorted(set(inner_duplicates)))
            + '. Имена файлов должны быть уникальными — ничего не загружено.'
        )

    # дубли с уже загруженными
    duplicates = find_duplicates(names)
    if duplicates:
        listed = ', '.join(f'«{name}»' for name in sorted(duplicates))
        raise PrizeFileError(
            f'Такие файлы уже загружены: {listed}. Имена файлов должны быть уникальными — '
            'ничего не загружено.'
        )

    created: list[PrizeFile] = []
    try:
        with transaction.atomic():
            for upload, name in zip(files, names):
                created.append(PrizeFile.objects.create(
                    kind=kind,
                    draw_type=draw_type,
                    name=name,
                    file=upload,
                    size=upload.size or 0,
                    uploaded_by=user if getattr(user, 'pk', None) else None,
                ))
    except IntegrityError:
        raise PrizeFileError(
            'Один из файлов с таким именем успел появиться в базе. Ничего не загружено — '
            'обновите страницу и попробуйте ещё раз.'
        )

    return UploadResult(created=created, kind=kind, draw_type=draw_type)


# ─────────────────────────── выдача победителю ───────────────────────────

def row_kind_id(row) -> int | None:
    """Вид приза победителя — вместе с типом розыгрыша задаёт нужный склад."""
    return row.prize.kind_id if row.prize else None


def row_draw_type(row) -> str:
    """Тип розыгрыша строки победителя — вторая половина ключа склада."""
    if row.kind == 'main':
        return PrizeFile.DrawType.MAIN
    if getattr(row, 'month_num', None) is not None:
        return PrizeFile.DrawType.MONTHLY
    return PrizeFile.DrawType.WEEKLY


def row_pool_key(row) -> tuple[int | None, str]:
    return row_kind_id(row), row_draw_type(row)


def row_supports_prize_file(row) -> bool:
    """Файл приза выдаётся только победителю электронного приза (сертификат, промокод)."""
    return bool(row.prize and row.prize.is_electronic)


def _owner_filter(row) -> dict:
    if row.kind == 'main':
        return {'main_draw_result_id': row.pk}
    return {'draw_result_id': row.pk}


def attached_file(row) -> PrizeFile | None:
    return PrizeFile.objects.filter(**_owner_filter(row)).select_related('kind').first()


def attached_files_map(rows) -> dict[tuple[str, int], PrizeFile]:
    """Один запрос на всю страницу списка вместо запроса на строку."""
    weekly_ids = [r.pk for r in rows if r.kind != 'main']
    main_ids = [r.pk for r in rows if r.kind == 'main']
    qs = PrizeFile.objects.filter(
        Q(draw_result_id__in=weekly_ids) | Q(main_draw_result_id__in=main_ids)
    ).select_related('kind')
    result: dict[tuple[str, int], PrizeFile] = {}
    for prize_file in qs:
        if prize_file.draw_result_id:
            result[('weekly', prize_file.draw_result_id)] = prize_file
        elif prize_file.main_draw_result_id:
            result[('main', prize_file.main_draw_result_id)] = prize_file
    return result


def free_files_for_row(row, *, query: str = '', limit: int = 50) -> list[dict]:
    """
    Свободные (ещё не выданные) файлы для выпадающего списка.

    Сначала — файлы из склада этого победителя: тот же вид приза и тот же тип
    розыгрыша (неважно, под какую неделю их грузили — недели склад не делят).
    Затем остальные свободные: менеджер видит подходящие сразу, но не заперт,
    если файл залили в соседний склад.
    """
    qs = PrizeFile.objects.filter(
        draw_result__isnull=True, main_draw_result__isnull=True,
    ).select_related('kind')

    query = (query or '').strip()
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(kind__name__icontains=query))

    kind_id, draw_type = row_pool_key(row)

    options = []
    for prize_file in qs.order_by('name')[: limit * 4]:
        same_kind = kind_id is not None and prize_file.kind_id == kind_id
        same_draw_type = prize_file.draw_type == draw_type
        matches = same_kind and same_draw_type
        if matches:
            match_label = 'этот приз'
        elif same_kind:
            match_label = 'другой розыгрыш'
        else:
            match_label = 'другой приз'
        options.append({
            'id': prize_file.pk,
            'name': prize_file.name,
            'prize': prize_file.kind.name or '',
            'draw_type': prize_file.draw_type,
            'draw_type_label': DRAW_TYPE_LABELS.get(prize_file.draw_type, prize_file.draw_type),
            'matches': matches,
            'match_label': match_label,
        })

    options.sort(key=lambda o: (not o['matches'], o['name'].lower()))
    return options[:limit]


def assign_file_to_row(row, file_id) -> PrizeFile:
    """
    Выдаёт свободный файл победителю. Бросает PrizeFileError, если файл уже
    занят, не найден или у победителя уже есть выданный файл.
    """
    if not str(file_id or '').isdigit():
        raise PrizeFileError('Файл приза не выбран.')
    if not row_supports_prize_file(row):
        raise PrizeFileError(
            'Приз этого победителя не электронный — файл приза ему не выдаётся.'
        )

    owner = _owner_filter(row)
    with transaction.atomic():
        try:
            prize_file = PrizeFile.objects.select_for_update().get(pk=int(file_id))
        except PrizeFile.DoesNotExist:
            raise PrizeFileError('Файл приза не найден.')

        if prize_file.is_assigned:
            if all(getattr(prize_file, key) == value for key, value in owner.items()):
                return prize_file
            raise PrizeFileError('Этот файл уже выдан другому победителю — обновите страницу.')

        # уже выданный этому победителю файл сначала освобождаем
        PrizeFile.objects.filter(**owner).update(
            **{key: None for key in owner}, assigned_at=None,
        )

        for key, value in owner.items():
            setattr(prize_file, key, value)
        prize_file.assigned_at = timezone.now()
        try:
            prize_file.save(update_fields=[*owner.keys(), 'assigned_at'])
        except IntegrityError:
            raise PrizeFileError('Этот файл уже выдан другому победителю — обновите страницу.')

    return prize_file


def clear_file_for_row(row) -> None:
    """Отвязывает файл от победителя — файл остаётся и снова свободен."""
    owner = _owner_filter(row)
    PrizeFile.objects.filter(**owner).update(
        **{key: None for key in owner}, assigned_at=None,
    )


DRAW_KIND_LABELS = {
    'weekly': 'Еженедельный розыгрыш',
    'monthly': 'Ежемесячный розыгрыш',
    'main': 'Главный розыгрыш',
}


@dataclass
class DistributionItem:
    row: object
    file: PrizeFile


@dataclass
class AutoDistributionPreview:
    kind: str
    kind_label: str
    period_label: str
    matched: list[DistributionItem] = field(default_factory=list)
    unmatched: list = field(default_factory=list)
    already_assigned: int = 0
    non_electronic: int = 0

    @property
    def total_in_scope(self) -> int:
        return len(self.matched) + len(self.unmatched) + self.already_assigned + self.non_electronic


def preview_auto_distribution(kind: str, period: int | None) -> AutoDistributionPreview | None:
    """
    Показывает, что случится при авто-раздаче файлов победителям конкретного розыгрыша,
    не трогая базу: кому файл уже выдан / у кого приз не электронный / кому какой
    свободный файл подберётся / кому свободного файла не хватило.

    Раздача идёт по одному розыгрышу и черпает из склада «вид приза + тип
    розыгрыша»: файл, загруженный когда угодно под этот же вид и этот же тип,
    подходит победителю любой недели. Партии соседнего типа розыгрыша не
    трогаются — их при необходимости выдают вручную, с подтверждением.
    """
    from . import winners as winners_services

    if kind not in DRAW_KIND_LABELS:
        return None

    scope = winners_services.draw_scope(kind, period)
    if scope is None:
        return None
    period_label, rows = scope

    already_assigned = 0
    non_electronic = 0
    needing = []
    for row in rows:
        if not row_supports_prize_file(row):
            non_electronic += 1
        elif row.prize_file:
            already_assigned += 1
        else:
            needing.append(row)

    kind_ids = {row_kind_id(row) for row in needing} - {None}
    draw_types = {row_draw_type(row) for row in needing}
    free_by_pool: dict[tuple[int, str], list[PrizeFile]] = defaultdict(list)
    for prize_file in PrizeFile.objects.filter(
        kind_id__in=kind_ids, draw_type__in=draw_types,
        draw_result__isnull=True, main_draw_result__isnull=True,
    ).order_by('uploaded_at', 'pk'):
        free_by_pool[(prize_file.kind_id, prize_file.draw_type)].append(prize_file)

    matched: list[DistributionItem] = []
    unmatched = []
    for row in sorted(needing, key=lambda r: r.created_at):
        pool = free_by_pool.get(row_pool_key(row)) or []
        if pool:
            matched.append(DistributionItem(row=row, file=pool.pop(0)))
        else:
            unmatched.append(row)

    return AutoDistributionPreview(
        kind=kind, kind_label=DRAW_KIND_LABELS[kind], period_label=period_label,
        matched=matched, unmatched=unmatched,
        already_assigned=already_assigned, non_electronic=non_electronic,
    )


@dataclass
class AutoDistributionResult:
    assigned: int
    unmatched_count: int
    kind_label: str
    period_label: str


def apply_auto_distribution(kind: str, period: int | None) -> AutoDistributionResult:
    """
    Раздаёт подобранные в превью файлы. Превью строится заново — на случай, если между
    открытием модалки и сабмитом кто-то успел выдать или загрузить файлы.
    """
    preview = preview_auto_distribution(kind, period)
    if preview is None:
        raise PrizeFileError('Такой розыгрыш не найден — обновите страницу и попробуйте снова.')

    assigned = 0
    for item in preview.matched:
        try:
            assign_file_to_row(item.row, item.file.pk)
            assigned += 1
        except PrizeFileError:
            # файл успели перехватить между превью и раздачей — просто пропускаем эту пару
            continue

    return AutoDistributionResult(
        assigned=assigned,
        unmatched_count=len(preview.unmatched),
        kind_label=preview.kind_label,
        period_label=preview.period_label,
    )


def delete_prize_file(prize_file: PrizeFile) -> None:
    """Полностью удаляет файл — разрешено только пока он никому не выдан."""
    if prize_file.is_assigned:
        raise PrizeFileError(
            'Файл выдан победителю. Сначала снимите его с победителя, потом удаляйте.'
        )
    prize_file.file.delete(save=False)
    prize_file.delete()


# ─────────────────────────── сводка по призам ───────────────────────────

def prize_file_stats() -> list[dict]:
    """
    Сводка «сколько файлов загружено / свободно / выдано»: одна карточка на вид
    приза, внутри — строка на каждый тип розыгрыша, то есть на каждый склад.
    Разбивки по неделям нет и быть не может — недели склад не делят.
    """
    rows = (
        PrizeFile.objects
        .values('kind_id', 'kind__name', 'draw_type')
        .annotate(
            total=Count('pk'),
            free=Count('pk', filter=Q(draw_result__isnull=True, main_draw_result__isnull=True)),
        )
        .order_by('kind__name', 'draw_type')
    )

    by_kind: dict = {}
    for row in rows:
        key = row['kind_id']
        card = by_kind.setdefault(key, {
            'kind_id': key,
            'prize': row['kind__name'] or '—',
            'total': 0,
            'free': 0,
            'assigned': 0,
            'rows': [],
        })
        assigned = row['total'] - row['free']
        card['total'] += row['total']
        card['free'] += row['free']
        card['assigned'] += assigned
        card['rows'].append({
            'draw_type': row['draw_type'],
            'draw_type_label': DRAW_TYPE_LABELS.get(row['draw_type'], row['draw_type']),
            'total': row['total'],
            'free': row['free'],
            'assigned': assigned,
        })
    return list(by_kind.values())


def prize_file_totals() -> dict:
    """Итог по всем файлам призов — шапка сводки на странице «Файлы призов»."""
    rows = PrizeFile.objects.aggregate(
        total=Count('pk'),
        free=Count('pk', filter=Q(draw_result__isnull=True, main_draw_result__isnull=True)),
    )
    total = rows['total'] or 0
    free = rows['free'] or 0
    return {'total': total, 'free': free, 'assigned': total - free}


def upload_kind_choices() -> list[PrizeKind]:
    """
    Виды призов, под которые вообще можно загружать файлы, — у которых есть хотя
    бы один электронный приз. Один пункт на вид, а не на каждую неделю.
    """
    return list(PrizeKind.objects.filter(prizes__is_electronic=True).distinct().order_by('name'))


def kinds_with_files(electronic_kinds: list[PrizeKind]) -> list[PrizeKind]:
    """
    Виды для фильтра на странице файлов: электронные плюс те, у которых файлы
    уже есть (флаг «электронный» могли снять позже — фильтр не должен «терять»
    такие файлы).
    """
    known = {k.pk: k for k in electronic_kinds}
    extra_ids = set(
        PrizeFile.objects.exclude(kind_id__in=known).values_list('kind_id', flat=True)
    )
    for kind in PrizeKind.objects.filter(pk__in=extra_ids):
        known[kind.pk] = kind
    return sorted(known.values(), key=lambda k: (k.name or '').lower())
