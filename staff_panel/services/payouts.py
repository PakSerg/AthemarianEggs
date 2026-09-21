"""Человеческие «этапы» выплат гарантированного приза для панели персонала.

Технические статусы `GuaranteedPrizePayout.status` рассчитаны на движок выплат,
а не на человека, и в дашборде читаются плохо:

* `hold` («В очереди на выплату») — это сразу ДВА разных состояния: выплату ещё
  ни разу не пробовали отправить, и выплату уже отправляли, но банк её отклонил
  (и она вернётся в работу в следующую пятницу, см. `apply_deal_status`). Из-за
  этого на странице не было ни одной записи в «Ошибка (повторим)», хотя по сути
  десятки записей — именно ошибки с повтором;
* `retryable` — это НЕ «банк отказал», а техническая ошибка обращения к Точке,
  которую авто-ретрай перезапускает каждые 15 минут;
* `failed` — терминальный статус: дальше без человека ничего не произойдёт;
* `retry_limit` — тоже терминальный: ошибка была временной, но авто-повторы
  израсходованы (CYCLOPS_MAX_PAYOUT_RETRIES подряд), и система сама больше
  не пробует;
* `new` в проде практически не встречается (остаток от мгновенных выплат).

Здесь эти состояния раскладываются на этапы, по которым и фильтрует дашборд.
Раскладка делается на стороне БД (`annotate_stage`), чтобы одинаково работали
и фильтр, и счётчики по этапам, и сортировка, и выгрузка в Excel.
"""

from __future__ import annotations

from django.db.models import Case, CharField, Count, IntegerField, Q, Sum, Value, When

from promotion.models import GuaranteedPrizePayout

from . import phone_search

# Те же ключевые слова, по которым движок отличает отказ банка из-за ФИО от
# любого другого отказа (см. guaranteed_prize_payout._FIO_ERROR_KEYWORDS).
# Дублируются здесь в виде icontains-условия: то же правило, но применимое
# в SQL, а не к одной строке в Python.
FIO_ERROR_KEYWORDS = ('фио',)

STAGE_PAID = 'paid'
STAGE_SENDING = 'sending'
STAGE_QUEUED = 'queued'
STAGE_RETRY_FIO = 'retry_fio'
STAGE_RETRY_BANK = 'retry_bank'
STAGE_RETRY_AUTO = 'retry_auto'
STAGE_RETRY_LIMIT = 'retry_limit'
STAGE_ATTENTION = 'attention'
STAGE_OPTED_OUT = 'opted_out'

# tone → цвет плашки в шаблоне (ok / wait / no / mute)
STAGES = [
    {
        'value': STAGE_PAID,
        'label': 'Выплачена',
        'short': 'Выплачено',
        'tone': 'ok',
        'hint': 'Банк подтвердил зачисление 50 ₽ участнику. Финальное состояние.',
    },
    {
        'value': STAGE_SENDING,
        'label': 'Отправлена в банк',
        'short': 'В пути',
        'tone': 'wait',
        'hint': 'Сделка создана и передана в Точку, ждём подтверждения от банка '
                'получателя. Обычно занимает минуты, статус обновляется сам.',
    },
    {
        'value': STAGE_QUEUED,
        'label': 'Ждёт выплаты',
        'short': 'Ждут выплаты',
        'tone': 'mute',
        'hint': 'Участник выполнил условия, выплату ещё ни разу не отправляли. '
                'Уйдёт в ближайшую плановую выплату (пятница).',
    },
    {
        'value': STAGE_RETRY_FIO,
        'label': 'Ошибка в ФИО — повтор в пятницу',
        'short': 'Ошибка ФИО',
        'tone': 'no',
        'hint': 'Банк не принял платёж из-за несовпадения ФИО получателя. '
                'Участнику отправлено письмо с просьбой поправить профиль; '
                'выплата будет повторена автоматически в следующую пятницу.',
    },
    {
        'value': STAGE_RETRY_BANK,
        'label': 'Банк отклонил — повтор в пятницу',
        'short': 'Отклонено банком',
        'tone': 'no',
        'hint': 'Банк получателя отклонил платёж не из-за ФИО (закрытый счёт, '
                'лимиты, СБП-подключение и т.п.). Выплата будет повторена '
                'автоматически в следующую пятницу.',
    },
    {
        'value': STAGE_RETRY_AUTO,
        'label': 'Техническая ошибка — авто-повтор',
        'short': 'Тех. ошибка',
        'tone': 'wait',
        'hint': 'Сбой обращения к Точке (сеть, недоступный сервис, плательщик '
                'не настроен). Повторяется автоматически каждые 15 минут, пока '
                'не исчерпан лимит попыток.',
    },
    {
        'value': STAGE_RETRY_LIMIT,
        'label': 'Исчерпан лимит авто-повторов',
        'short': 'Лимит повторов',
        'tone': 'no',
        'hint': 'Система пробовала отправить выплату несколько раз подряд и '
                'каждый раз получала техническую ошибку. Сама она больше не '
                'пробует — нужно разобраться с причиной и нажать «Повторить»: '
                'запас автоматических попыток начнётся заново.',
    },
    {
        'value': STAGE_ATTENTION,
        'label': 'Не выплачена — нужна проверка',
        'short': 'Нужна проверка',
        'tone': 'no',
        'hint': 'Сама система дальше ничего не сделает: данные участника '
                'непригодны (банк не в СБП, битый телефон, неполный профиль) '
                'либо платёж требует ручного разбора. Нужно действие человека.',
    },
    {
        'value': STAGE_OPTED_OUT,
        'label': 'Отказ участника',
        'short': 'Отказались',
        'tone': 'mute',
        'hint': 'В карточке участника стоит галочка «Отказ от гарантированного '
                'приза». Выплата ему не отправляется — ни планово, ни вручную.',
    },
]

STAGE_LABELS = {stage['value']: stage['label'] for stage in STAGES}
STAGE_VALUES = [stage['value'] for stage in STAGES]

# Этапы, по которым выплата ещё «живая» — деньги участнику не ушли, но система
# (или человек) к ней ещё вернётся. Используется для сводки «в работе».
OPEN_STAGES = (STAGE_SENDING, STAGE_QUEUED, STAGE_RETRY_FIO, STAGE_RETRY_BANK, STAGE_RETRY_AUTO)
# Этапы, требующие вмешательства человека.
ACTION_STAGES = (STAGE_ATTENTION, STAGE_RETRY_LIMIT)


def _has_error_reason():
    return Q(error_reason__isnull=False) & ~Q(error_reason='')


def _looks_like_fio_error():
    condition = Q()
    for keyword in FIO_ERROR_KEYWORDS:
        condition |= Q(error_reason__icontains=keyword)
    return condition


def annotate_stage(queryset):
    """Добавить в выборку поле `stage` — человеческий этап выплаты.

    Порядок веток важен: каждая следующая разбирает только то, что не подошло
    предыдущим. Поэтому к веткам с разбором `error_reason` доходят уже только
    записи в hold/new, то есть ровно те, где непустая причина ошибки означает
    «пробовали, не вышло, повторим», а не «ошибка прямо сейчас».
    """
    branches = [
        (Q(status=GuaranteedPrizePayout.STATUS_PAID), STAGE_PAID),
        (Q(participant__guaranteed_prize_opt_out=True), STAGE_OPTED_OUT),
        (Q(status=GuaranteedPrizePayout.STATUS_FAILED), STAGE_ATTENTION),
        (
            Q(status__in=(
                GuaranteedPrizePayout.STATUS_PROCESSING,
                GuaranteedPrizePayout.STATUS_EXECUTING,
            )),
            STAGE_SENDING,
        ),
        (Q(status=GuaranteedPrizePayout.STATUS_RETRY_LIMIT), STAGE_RETRY_LIMIT),
        (Q(status=GuaranteedPrizePayout.STATUS_RETRYABLE), STAGE_RETRY_AUTO),
        (_has_error_reason() & _looks_like_fio_error(), STAGE_RETRY_FIO),
        (_has_error_reason(), STAGE_RETRY_BANK),
    ]
    order = {stage['value']: index for index, stage in enumerate(STAGES)}
    return queryset.annotate(
        stage=Case(
            *[When(condition, then=Value(value)) for condition, value in branches],
            default=Value(STAGE_QUEUED),
            output_field=CharField(),
        ),
        # Отдельное числовое поле — чтобы сортировка по колонке «Состояние»
        # шла в осмысленном порядке STAGES (выплачено → в пути → … → отказ),
        # а не по алфавиту технических ключей этапа.
        stage_order=Case(
            *[When(condition, then=Value(order[value])) for condition, value in branches],
            default=Value(order[STAGE_QUEUED]),
            output_field=IntegerField(),
        ),
    )


def stage_breakdown(queryset):
    """Количество и сумма по каждому этапу — в порядке STAGES, включая нули.

    Нулевые этапы намеренно не выбрасываются: «0 выплат нужны проверки» —
    это тоже полезная информация, ради которой не нужно менять фильтр.
    """
    rows = {
        row['stage']: row
        for row in queryset.values('stage').annotate(count=Count('pk'), amount=Sum('amount'))
    }
    result = []
    for stage in STAGES:
        row = rows.get(stage['value'])
        result.append({
            **stage,
            'count': row['count'] if row else 0,
            'amount': row['amount'] if row and row['amount'] else 0,
        })
    return result


def summarize(breakdown):
    """Верхнеуровневая сводка по разбивке: выплачено / в работе / требует действий."""
    by_value = {row['value']: row for row in breakdown}

    def _sum(values, key):
        return sum(by_value[value][key] for value in values if value in by_value)

    return {
        'total_count': sum(row['count'] for row in breakdown),
        'total_amount': sum(row['amount'] for row in breakdown),
        'paid_count': _sum((STAGE_PAID,), 'count'),
        'paid_amount': _sum((STAGE_PAID,), 'amount'),
        'open_count': _sum(OPEN_STAGES, 'count'),
        'open_amount': _sum(OPEN_STAGES, 'amount'),
        'action_count': _sum(ACTION_STAGES, 'count'),
        'action_amount': _sum(ACTION_STAGES, 'amount'),
        'opted_out_count': _sum((STAGE_OPTED_OUT,), 'count'),
    }


def apply_search(queryset, term):
    """Поиск по строке: email, ФИО, телефон (в любом формате) или ID сделки.

    Как и в списке участников, строка сначала классифицируется по виду и ищется
    только в подходящих полях — иначе цифры из email случайно совпадали бы с
    чужими телефонами (см. participants.filter_participants).
    """
    term = (term or '').strip()
    if not term:
        return queryset
    if '@' in term:
        return queryset.filter(participant__email__icontains=term)
    if phone_search.looks_like_phone_query(term):
        # Телефон в профиле хранится как его ввёл участник («+7 (999) …»),
        # а в самой выплате — уже нормализованным; ищем по обоим.
        queryset = phone_search.annotate_phone_digits(
            queryset, 'participant__phone', 'participant_phone_digits',
        )
        return queryset.filter(
            phone_search.phone_search_q('participant_phone_digits', term)
            | phone_search.phone_search_q('phone_number', term),
        )
    return queryset.filter(
        Q(participant__email__icontains=term)
        | Q(participant__first_name__icontains=term)
        | Q(participant__last_name__icontains=term)
        | Q(participant__middle_name__icontains=term)
        | Q(deal_id__icontains=term),
    )
