"""Сервис автоматической выплаты гарантированного приза 50₽ через СБП (Cyclops).

Флоу одной выплаты: create_deal → генерация персонального документа-основания
(cyclops_documents) → upload_document/deal → execute_deal, далее статус
опрашивается задачей poll_prize_deals. Идемпотентность обеспечивается одной
записью GuaranteedPrizePayout на участника и атомарным захватом статуса.
См. docs/cyclops-integration-plan.md
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import GuaranteedPrizePayout, SbpBank
from . import cyclops_documents
from .cyclops import CyclopsAPIClient
from .notify_bot import notify as notify_bot

logger = logging.getLogger(__name__)

# Поля профиля, обязательные для выплаты (bank_bik — источник bank_sbp_id)
REQUIRED_PROFILE_FIELDS = ('first_name', 'last_name', 'phone', 'bank_bik')

# Коды ошибок Cyclops, которые Точка возвращает при постоянных, а не временных
# проблемах — повторять выплату при них бессмысленно (одна и та же причина
# сохранится и на следующей попытке). Все прочие коды считаем временными.
# 4436 — An error occurred while requesting compliance (риск по 115-ФЗ);
# 4945 — Terminal not found (проблема конфигурации площадки, не участника).
PERMANENT_ERROR_CODES = {4436, 4945}


class PayoutError(Exception):
    """Ошибка выплаты. retryable=True — временная (можно повторить автоматически)."""

    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _raise_from_response(resp, action, retryable=True):
    """Разобрать ошибку JSON-RPC-ответа Cyclops и бросить PayoutError с учётом
    того, постоянная это ошибка или временная (см. PERMANENT_ERROR_CODES)."""
    error = resp.get('error') or {}
    code = error.get('code')
    message = error.get('message', resp)
    if code in PERMANENT_ERROR_CODES:
        retryable = False
    raise PayoutError(f'Ошибка {action} (code={code}): {message}', retryable=retryable)



# Ключевое слово, по которому текст отказа банка ("error_reason" в recipient
# сделки) распознаётся как проблема именно с ФИО получателя, а не временный
# сбой на стороне Точки/НСПК или нехватка средств. Официального перечня причин
# отказа СБП Точка не публикует, а собственный запасной текст ("Платёж
# отклонён банком получателя", см. ниже) подставляется нами же, когда банк
# вообще не прислал причину — поэтому матчим по узкому, специфичному слову
# «ФИО», а не по общим словам вроде «получатель». Если у Точки встретится
# другая формулировка причины несовпадения имени — список стоит дополнить.
_FIO_ERROR_KEYWORDS = ('фио',)


def _is_fio_related_error(reason):
    """Похож ли текст отказа банка на ошибку в ФИО получателя (а не на
    временный сбой, недостаток средств и т.п.)."""
    text = (reason or '').lower()
    return any(keyword in text for keyword in _FIO_ERROR_KEYWORDS)


def normalize_phone(phone):
    """Привести телефон к формату СБП: 11 цифр, начинается с «7». None — если некорректный."""
    digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if len(digits) == 11 and digits[0] in ('7', '8'):
        return '7' + digits[1:]
    if len(digits) == 10:
        return '7' + digits
    return None


def _profile_complete(participant):
    return all((getattr(participant, f, '') or '').strip() for f in REQUIRED_PROFILE_FIELDS)


def _has_confirmed_receipt(participant):
    """Право на гарантированный приз (п. 4.9/6.2 Правил) возникает только после
    регистрации хотя бы одного Чека, успешно прошедшего модерацию — заполненного
    профиля недостаточно."""
    from ..models import Receipt

    return Receipt.objects.filter(
        participant_id=participant.pk, status=Receipt.Status.CONFIRMED,
    ).exists()


def payout_blocker(participant):
    """Причина, по которой выплату этому участнику отправлять бессмысленно,
    или None, если данных достаточно.

    Проверяет только то, что зависит от данных участника и справочника СБП, —
    то есть то, что можно узнать ДО обращения в Точку. Используется дважды:
    плановой задачей, чтобы не вносить в Реестр выплат людей, которым всё равно
    ничего не уйдёт, и самой выплатой перед созданием сделки.
    """
    if not _profile_complete(participant):
        return 'Профиль заполнен не полностью'
    if not normalize_phone(participant.phone):
        return f'Некорректный номер телефона: {participant.phone!r}'
    if not SbpBank.resolve_sbp_id(participant.bank_bik):
        return f'Банк с БИК {participant.bank_bik} не найден среди участников СБП'
    return None


def _payout_config():
    """Вернуть (virtual_account_id, beneficiary_id) плательщика призового фонда.

    Плательщик берётся из БД-синглтона CyclopsSettings. Бросает
    PayoutError(retryable), если плательщик ещё не настроен — как только
    настройки заполнят в панели персонала, ближайший авто-ретрай пройдёт успешно.
    """
    from ..models import CyclopsSettings

    cs = CyclopsSettings.load()
    if not cs.is_payout_configured:
        raise PayoutError(
            'Плательщик Cyclops не настроен: укажите бенефициара и виртуальный счёт в настройках',
            retryable=True,
        )
    return cs.payout_virtual_account.virtual_account_id, cs.payout_beneficiary.beneficiary_id


def _ensure_document_uploaded(payout, client, beneficiary_id):
    """Сгенерировать (если ещё не создан) персональный документ-основание для
    выплаты и загрузить его на сделку, если ещё не загружен. Идемпотентно —
    безопасно вызывать повторно при ретраях."""
    document = cyclops_documents.generate_service_agreement_document(payout)
    if document.uploaded:
        return document

    payout.log(f'upload_document/deal: {document.file.name}')
    resp = client.upload_document_for_deal(document.file.path, beneficiary_id, payout.deal_id)
    payout.log(f'upload_document OK: {resp}')
    payout.save(update_fields=['api_log', 'updated_at'])
    document.document_id = resp.get('document_id') if isinstance(resp, dict) else None
    document.uploaded = True
    document.save(update_fields=['document_id', 'uploaded'])
    return document


# ---------------------------------------------------------------------------- #
# Точка входа из ProfileView — постановка выплаты в очередь
# ---------------------------------------------------------------------------- #
def ensure_guaranteed_prize_payout(participant):
    """Создать запись выплаты (если нужно), когда профиль заполнен И у участника
    есть хотя бы один подтверждённый чек (п. 4.9/6.2 Правил).

    Вызывается из двух мест: при сохранении профиля (ProfileView.post) и при
    подтверждении чека — автоматическом (сигнал post_save на Receipt, см.
    promotion/signals.py) и ручном (админка/дашборд, тот же сигнал, т.к. оба
    пути сохраняют чек через .save()). Порядок событий (профиль/чек) не важен —
    какое бы условие ни выполнилось последним, тогда и создастся запись.

    Мгновенная автоматическая выплата отключена: выплата будет запускаться позже,
    согласно официальным правилам акции (вручную из админки/панели или плановой
    задачей). Эта функция только резервирует запись со статусом STATUS_HOLD и
    ничего не ставит в очередь Celery. Безопасно при повторных вызовах.
    """
    if not _profile_complete(participant):
        return
    if not _has_confirmed_receipt(participant):
        return

    payout, created = GuaranteedPrizePayout.objects.get_or_create(
        participant=participant,
        defaults={
            'amount': Decimal(str(settings.CYCLOPS_CONFIG['PRIZE_AMOUNT'])),
            'status': GuaranteedPrizePayout.STATUS_HOLD,
        },
    )
    if created or payout.status not in GuaranteedPrizePayout.MANUAL_ONLY_STATUSES:
        return

    # Участник исправил профиль после финальной ошибки (или после того, как
    # выплата исчерпала лимит авто-повторов) — вернём запись в режим ожидания
    # планового запуска (а не сразу в очередь на отправку) и дадим ей заново
    # полный запас автоматических попыток.
    payout.status = GuaranteedPrizePayout.STATUS_HOLD
    payout.error_reason = None
    payout.cnt_retry = 0
    payout.save(update_fields=['status', 'error_reason', 'cnt_retry', 'updated_at'])


# ---------------------------------------------------------------------------- #
# Выполнение выплаты (вызывается из Celery-задачи)
# ---------------------------------------------------------------------------- #
def perform_payout(participant_id):
    """Выполнить одну выплату. Атомарно захватывает запись, чтобы исключить дубли."""
    with transaction.atomic():
        payout = (
            GuaranteedPrizePayout.objects
            .select_for_update()
            .filter(participant_id=participant_id)
            .first()
        )
        if payout is None:
            return
        if payout.status in GuaranteedPrizePayout.IN_FLIGHT_STATUSES:
            # Уже выплачено или прямо сейчас обрабатывается/отправлено
            return
        if payout.participant.guaranteed_prize_opt_out:
            # Участник отказался от гарантированного приза. Единая точка входа
            # всех выплат (плановая задача, авто-ретраи, кнопка «Оплатить»),
            # поэтому одной проверки здесь достаточно, чтобы деньги не ушли ни
            # по одному из путей. Статус намеренно НЕ меняем: запись остаётся
            # там, где была, и выплата продолжится сама, если галочку снимут.
            logger.info('perform_payout: участник отказался от выплаты, пропуск participant_id=%s',
                        participant_id)
            return
        payout.status = GuaranteedPrizePayout.STATUS_PROCESSING
        payout.cnt_retry += 1
        payout.save(update_fields=['status', 'cnt_retry', 'updated_at'])

    participant = payout.participant
    try:
        _do_payout(payout, participant)
    except PayoutError as e:
        _mark_error(payout, str(e), retryable=e.retryable)
    except Exception as e:  # noqa: BLE001 — любая непредвиденная ошибка → повторяемая
        logger.exception('perform_payout: непредвиденная ошибка participant_id=%s', participant_id)
        _mark_error(payout, f'Непредвиденная ошибка: {e}', retryable=True)


def _do_payout(payout, participant):
    from ..models import CyclopsSettings

    cfg = settings.CYCLOPS_CONFIG

    # Главный выключатель — проверяем на каждой попытке (а не только при
    # постановке в очередь), чтобы отключение в настройках останавливало и уже
    # запланированные ретраи, и ручной перезапуск из админки.
    if not CyclopsSettings.load().payout_enabled:
        raise PayoutError(
            'Выплаты гарантированного приза временно отключены в настройках Cyclops',
            retryable=True,
        )

    client = CyclopsAPIClient()

    # Если сделка уже создавалась ранее (ретрай после падения на execute) —
    # не создаём новую, а продолжаем по существующей.
    if payout.deal_id:
        if _resume_existing_deal(payout, client):
            return

    # --- валидация данных участника ---
    blocker = payout_blocker(participant)
    if blocker:
        raise PayoutError(blocker, retryable=False)

    phone = normalize_phone(participant.phone)
    sbp_id = SbpBank.resolve_sbp_id(participant.bank_bik)

    virtual_account, beneficiary_id = _payout_config()
    amount = float(payout.amount)

    # --- проверка баланса призового фонда ---
    payout.log(f'get_virtual_account: {virtual_account}')
    va = client.get_virtual_account(virtual_account)
    payout.log(f'get_virtual_account OK: {va}')
    payout.save(update_fields=['api_log', 'updated_at'])
    cash = (va.get('virtual_account') or {}).get('cash')
    if cash is None or float(cash) < amount:
        raise PayoutError('Недостаточно средств на виртуальном счёте призового фонда', retryable=True)

    # --- получатель payment_contract_by_sbp ---
    recipient = {
        'number': 1,
        'type': 'payment_contract_by_sbp',
        'amount': amount,
        'first_name': participant.first_name.strip(),
        'last_name': participant.last_name.strip(),
        'phone_number': phone,
        'bank_sbp_id': sbp_id,
        'purpose': cfg['PRIZE_PURPOSE'],
    }
    middle_name = (participant.middle_name or '').strip()
    if middle_name:
        recipient['middle_name'] = middle_name

    payers = [{'virtual_account': virtual_account, 'amount': amount}]

    # --- создание сделки ---
    payout.log(f'create_deal: {recipient}')
    resp = client.create_deal(amount, payers, [recipient])
    if 'result' not in resp:
        payout.log(f'create_deal ОШИБКА: {resp}')
        payout.save(update_fields=['api_log', 'updated_at'])
        _raise_from_response(resp, 'создания сделки')

    deal_id = resp['result']['deal_id']
    payout.log(f'create_deal OK: deal_id={deal_id}')
    payout.deal_id = deal_id
    payout.bank_sbp_id = sbp_id
    payout.phone_number = phone
    payout.save(update_fields=['deal_id', 'bank_sbp_id', 'phone_number', 'api_log', 'updated_at'])

    # --- генерация персонального документа-основания и загрузка на сделку ---
    # (обязателен перед исполнением — иначе execute_deal вернёт 4406/4408)
    _ensure_document_uploaded(payout, client, beneficiary_id)

    # --- исполнение сделки ---
    payout.log(f'execute_deal: {deal_id}')
    exec_resp = client.execute_deal(deal_id)
    if 'result' not in exec_resp:
        payout.log(f'execute_deal ОШИБКА: {exec_resp}')
        payout.save(update_fields=['api_log', 'updated_at'])
        _raise_from_response(exec_resp, 'исполнения сделки')
    payout.log(f'execute_deal OK: {exec_resp.get("result")}')
    payout.save(update_fields=['api_log', 'updated_at'])

    _mark_executing(payout)
    logger.info('Выплата отправлена: participant_id=%s deal_id=%s', participant.pk, deal_id)


def _mark_executing(payout):
    """Пометить сделку отправленной в банк.

    Письмо «Гарантированный приз отправлен» НЕ отправляется автоматически —
    по умолчанию оно отключено и отправляется только вручную со страницы
    конкретной выплаты в админке (см. guaranteed_prize_email.send_guaranteed_prize_paid_email
    и GuaranteedPrizePayoutAdmin)."""
    payout.status = GuaranteedPrizePayout.STATUS_EXECUTING
    payout.sent_at = timezone.now()
    payout.error_reason = None
    payout.save(update_fields=['status', 'sent_at', 'error_reason', 'updated_at'])


def _resume_existing_deal(payout, client):
    """Продолжить по уже созданной сделке (идемпотентность ретраев).

    Возвращает True, если статус разобран и дальнейшее создание сделки не нужно.
    """
    payout.log(f'get_deal (ретрай): {payout.deal_id}')
    resp = client.get_deal(payout.deal_id)
    payout.log(f'get_deal OK: {resp}')
    payout.save(update_fields=['api_log', 'updated_at'])
    deal = (resp.get('result') or {}).get('deal')
    if not deal:
        # Сделку не нашли — сбросим deal_id и документ (он привязан к старой
        # сделке и не подойдёт новой) и позволим создать всё заново
        payout.deal_id = None
        payout.document = None
        payout.save(update_fields=['deal_id', 'document', 'updated_at'])
        return False

    deal_status = deal.get('status')
    if deal_status == 'new':
        # Сделка создана, но не исполнена — доисполним
        _, beneficiary_id = _payout_config()
        _ensure_document_uploaded(payout, client, beneficiary_id)
        payout.log(f'execute_deal (ретрай): {payout.deal_id}')
        exec_resp = client.execute_deal(payout.deal_id)
        if 'result' not in exec_resp:
            payout.log(f'execute_deal ОШИБКА: {exec_resp}')
            payout.save(update_fields=['api_log', 'updated_at'])
            _raise_from_response(exec_resp, 'исполнения сделки')
        payout.log(f'execute_deal OK: {exec_resp.get("result")}')
        payout.save(update_fields=['api_log', 'updated_at'])
        _mark_executing(payout)
        return True

    # in_process / partial / closed / rejected / correction — разберём по статусу выплаты
    apply_deal_status(payout, deal, client=client)
    return True


# Код ошибки, которым Cyclops отвечает на rejected_deal, если сделка уже
# ушла в статус correction (получатель СБП отклонил перевод после
# execute_deal) — этот статус rejected_deal не отменяет, нужен отдельный
# метод cancel_deal_with_executed_recipients (см. официальный гайд Точки,
# раздел «Сделки» — иначе сумма навсегда останется висеть в blocked_cash).
_DEAL_IN_CORRECTION_ERROR_CODE = 4418


def _release_deal_block(payout, client, deal_id):
    """Отменить отклонённую банком, но ни разу не исполненную сделку —
    иначе сумма так и останется висеть в blocked_cash виртуального счёта
    навсегда: get_deal её не освобождает сам.

    Вызывается только когда получатель сделки НЕ исполнен (`executed=False`
    в recipient — деньги реально никуда не уходили, просто зарезервированы).
    Если сделка уже исполнена, а деньги вернулись отдельным платежом —
    это другой сценарий (см. cyclops_manager.identify_returned_payment) и
    им нужно заниматься руками.

    На практике реальный СБП-отказ (get_deal вернул recipient.status=reject)
    почти всегда переводит саму сделку в статус correction — rejected_deal
    такую сделку не берёт (код 4418), нужен cancel_deal_with_executed_recipients
    (проверено вручную на 6 реальных отклонённых сделках — 300₽ blocked_cash
    освободились именно им). rejected_deal пробуем первым на случай, если
    сделка почему-то осталась в статусе new.
    """
    try:
        resp = client.rejected_deal(deal_id)
        payout.log(f'rejected_deal (снятие блокировки): {resp}')
        if 'result' in resp:
            return
        error_code = (resp.get('error') or {}).get('code')
        if error_code != _DEAL_IN_CORRECTION_ERROR_CODE:
            logger.warning('_release_deal_block: rejected_deal не снял блокировку deal_id=%s: %s',
                           deal_id, resp)
            return
    except Exception as e:  # noqa: BLE001 — не должно ронять разбор статуса выплаты
        payout.log(f'rejected_deal ОШИБКА: {e}')
        logger.exception('_release_deal_block: ошибка rejected_deal deal_id=%s', deal_id)
        return

    try:
        resp = client.cancel_deal_with_executed_recipients(deal_id)
        payout.log(f'cancel_deal_with_executed_recipients (снятие блокировки): {resp}')
        if 'result' not in resp:
            logger.warning('_release_deal_block: cancel_deal_with_executed_recipients '
                           'не снял блокировку deal_id=%s: %s', deal_id, resp)
    except Exception as e:  # noqa: BLE001
        payout.log(f'cancel_deal_with_executed_recipients ОШИБКА: {e}')
        logger.exception('_release_deal_block: ошибка cancel_deal_with_executed_recipients deal_id=%s', deal_id)


# ---------------------------------------------------------------------------- #
# Разбор статуса сделки (используется poll_prize_deals и _resume_existing_deal)
# ---------------------------------------------------------------------------- #
def apply_deal_status(payout, deal, client=None):
    """Обновить статус выплаты по данным сделки из get_deal."""
    recipients = deal.get('recipients') or []
    rec = recipients[0] if recipients else {}
    rstatus = rec.get('status')
    payout.log(f'get_deal: сделка={deal.get("status")}, получатель={rstatus}, '
               f'error_reason={rec.get("error_reason")}')

    if rstatus == 'success':
        payout.status = GuaranteedPrizePayout.STATUS_PAID
        payout.paid_at = timezone.now()
        payout.error_reason = None
        payout.save(update_fields=['status', 'paid_at', 'error_reason', 'api_log', 'updated_at'])
        logger.info('Выплата подтверждена: participant_id=%s deal_id=%s', payout.participant_id, payout.deal_id)

        # Автописьмо — только для выплат плановой фоновой задачи. Выплаты,
        # запущенные вручную кнопкой «Провести N выплат» (sent_manually),
        # письма не получают сами — их шлют вручную со страницы выплаты.
        if not payout.sent_manually:
            from .guaranteed_prize_email import send_guaranteed_prize_paid_email
            send_guaranteed_prize_paid_email(payout.participant, payout)
    elif rstatus == 'reject':
        reason = rec.get('error_reason') or 'Платёж отклонён банком получателя'
        old_deal_id = payout.deal_id
        executed = bool(rec.get('executed'))

        if executed:
            # Получатель отмечен как executed=True — деньги фактически
            # уходили (а потом банк всё равно вернул reject), значит это уже
            # не «сделка заблокировала сумму», а отдельный возвращённый
            # платёж (см. cyclops_manager.identify_returned_payment). Заводить
            # новую сделку/ретрай здесь опасно — можно заплатить дважды, если
            # разберутся и старый платёж всё же дойдёт. Останавливаемся и
            # зовём на помощь человека, ничего не автоматизируем дальше.
            payout.status = GuaranteedPrizePayout.STATUS_FAILED
            payout.error_reason = f'{reason} (executed=True — требует ручной проверки)'
            payout.save(update_fields=['status', 'error_reason', 'api_log', 'updated_at'])
            logger.warning('Выплата отклонена, но executed=True — нужна ручная проверка: '
                           'participant_id=%s deal_id=%s reason=%s',
                           payout.participant_id, old_deal_id, reason)
            notify_bot(
                f'🛑 Cyclops: отклонённая сделка {old_deal_id} помечена executed=True — '
                f'деньги, возможно, фактически ушли. НЕ ретраится автоматически, нужна '
                f'ручная проверка (cyclops_manager.identify_returned_payment)\n'
                f'participant_id={payout.participant_id} · {payout.participant.email}\n'
                f'причина: {reason}'
            )
            return

        # get_deal сам не снимает блокировку суммы на виртуальном счёте —
        # без явной отмены сделки деньги так и останутся в blocked_cash
        # навсегда, хотя получателю ничего не ушло (executed=False).
        if old_deal_id:
            _release_deal_block(payout, client or CyclopsAPIClient(), old_deal_id)

        if _is_fio_related_error(reason):
            # Похоже на ФИО/реквизиты — не финальный отказ: участник мог
            # исправить профиль (уточнить ФИО/банк) к следующей пятнице.
            # Возвращаем в «Отложена» (а не в retryable) — так запись заново
            # подхватит именно плановая пятничная задача (_claim_hold_payouts),
            # а не 15-минутный retry_failed_prizes, который бы дёргал её сразу,
            # не дожидаясь пятницы. Письмо шлём один раз — error_email_sent
            # не даёт продублировать его на следующих попытках.
            payout.status = GuaranteedPrizePayout.STATUS_HOLD
            payout.error_reason = reason
            payout.deal_id = None
            payout.document = None
            payout.save(update_fields=['status', 'error_reason', 'deal_id', 'document',
                                        'api_log', 'updated_at'])
            logger.warning('Выплата отклонена (ФИО, уйдёт в повтор в следующую пятницу): '
                           'participant_id=%s deal_id=%s reason=%s',
                           payout.participant_id, old_deal_id, reason)

            if not payout.sent_manually:
                from .guaranteed_prize_email import send_guaranteed_prize_error_email
                send_guaranteed_prize_error_email(payout.participant, payout)
        else:
            # Любая другая причина отказа банка (нехватка средств у получателя,
            # закрытый счёт и т.п.) — не считаем окончательной: сбрасываем
            # сделку и возвращаем в «Отложена» (а не retryable!) — повтор
            # должен произойти в рамках следующей плановой пятничной задачи,
            # а не через 15-минутный retry_failed_prizes: тот держит запись
            # STATUS_RETRYABLE до CYCLOPS_MAX_PAYOUT_RETRIES попыток каждые 15
            # минут — с одной и той же (скорее всего непреходящей) причиной
            # это просто раз за разом пересоздаёт и тут же вновь отклоняет
            # сделку, шлёт повторные уведомления в Telegram и оплачивает Точке
            # комиссию за каждую попытку. Уведомляем в Telegram один раз —
            # именно в момент отказа, не при каждой последующей попытке. Письмо
            # участнику (send_guaranteed_prize_bank_error_email) шлём тоже
            # один раз — error_bank_email_sent не даёт продублировать его на
            # следующих попытках.
            payout.status = GuaranteedPrizePayout.STATUS_HOLD
            payout.error_reason = reason
            payout.deal_id = None
            payout.document = None
            payout.save(update_fields=['status', 'error_reason', 'deal_id', 'document',
                                        'api_log', 'updated_at'])
            logger.warning('Выплата отклонена (не ФИО, уйдёт в повтор в следующую пятницу): '
                           'participant_id=%s deal_id=%s reason=%s',
                           payout.participant_id, old_deal_id, reason)
            notify_bot(
                f'⚠️ Cyclops: банк отклонил выплату гарантированного приза — уйдёт в повтор в следующую пятницу\n'
                f'participant_id={payout.participant_id} · {payout.participant.email}\n'
                f'причина: {reason}'
            )

            if not payout.sent_manually:
                from .guaranteed_prize_email import send_guaranteed_prize_bank_error_email
                send_guaranteed_prize_bank_error_email(payout.participant, payout)
    else:
        # new / in_process / old — ещё в работе, оставляем executing
        if payout.status != GuaranteedPrizePayout.STATUS_EXECUTING:
            payout.status = GuaranteedPrizePayout.STATUS_EXECUTING
        payout.save(update_fields=['status', 'api_log', 'updated_at'])


def max_payout_retries():
    """Сколько раз подряд система пробует выплату сама (CYCLOPS_MAX_PAYOUT_RETRIES)."""
    return settings.CYCLOPS_CONFIG['MAX_PAYOUT_RETRIES']


def _mark_error(payout, message, retryable):
    # Лимит авто-повторов: cnt_retry уже увеличен на текущую попытку (см.
    # perform_payout), поэтому >= означает «эта попытка была последней».
    limit_reached = retryable and payout.cnt_retry >= max_payout_retries()
    if limit_reached:
        # Ошибка временная, но повторять её автоматически больше не будем —
        # запись ждёт человека (кнопка «Повторить выплату», которая обнуляет
        # счётчик попыток).
        payout.status = GuaranteedPrizePayout.STATUS_RETRY_LIMIT
        message = f'{message} (исчерпан лимит авто-повторов: {payout.cnt_retry})'
    elif retryable:
        payout.status = GuaranteedPrizePayout.STATUS_RETRYABLE
    else:
        payout.status = GuaranteedPrizePayout.STATUS_FAILED
    payout.error_reason = message
    payout.save(update_fields=['status', 'error_reason', 'updated_at'])
    logger.warning('Выплата не выполнена (retryable=%s, попытка %s/%s): participant_id=%s — %s',
                   retryable and not limit_reached, payout.cnt_retry, max_payout_retries(),
                   payout.participant_id, message)
    if limit_reached:
        repeat = 'нет, исчерпан лимит'
    elif retryable:
        repeat = 'да'
    else:
        repeat = 'нет'
    notify_bot(
        f'⚠️ Cyclops: ошибка выплаты гарантированного приза\n'
        f'participant_id={payout.participant_id} · попытка {payout.cnt_retry}/{max_payout_retries()} · повтор={repeat}\n'
        f'{message}'
    )


# ---------------------------------------------------------------------------- #
# Периодические операции (вызываются из Celery-задач)
# ---------------------------------------------------------------------------- #
def poll_prize_deals():
    """Опросить статусы отправленных сделок и финализировать paid/failed."""
    client = CyclopsAPIClient()
    qs = GuaranteedPrizePayout.objects.filter(
        status=GuaranteedPrizePayout.STATUS_EXECUTING,
    ).exclude(deal_id__isnull=True).exclude(deal_id='')

    errors = []
    for payout in qs:
        try:
            resp = client.get_deal(payout.deal_id)
            deal = (resp.get('result') or {}).get('deal')
            if deal:
                apply_deal_status(payout, deal, client=client)
            else:
                payout.log(f'poll_prize_deals: get_deal — сделка не найдена, ответ {resp}')
                payout.save(update_fields=['api_log', 'updated_at'])
        except Exception as e:
            logger.exception('poll_prize_deals: ошибка по deal_id=%s', payout.deal_id)
            errors.append(f'deal_id={payout.deal_id}: {e}')

    if errors:
        notify_bot(
            f'⚠️ Cyclops: ошибки опроса статуса сделок ({len(errors)})\n'
            + '\n'.join(errors[:10])
        )


def retry_failed_prizes():
    """Перезапустить выплаты, которые должны быть отправлены, но ещё не отправлены."""
    from ..tasks import pay_guaranteed_prize

    max_retries = max_payout_retries()
    # Записи, которые уже израсходовали лимит (например, попали в retryable до
    # того, как лимит понизили), переводим в терминальный статус, чтобы они не
    # висели в «авто-повторе» вечно и были видны людям как требующие внимания.
    GuaranteedPrizePayout.objects.filter(
        status=GuaranteedPrizePayout.STATUS_RETRYABLE,
        cnt_retry__gte=max_retries,
    ).update(status=GuaranteedPrizePayout.STATUS_RETRY_LIMIT, updated_at=timezone.now())

    qs = GuaranteedPrizePayout.objects.filter(
        status__in=GuaranteedPrizePayout.RETRIABLE_STATUSES,
        cnt_retry__lt=max_retries,
    ).exclude(participant__guaranteed_prize_opt_out=True)
    for payout in qs:
        pay_guaranteed_prize.delay(payout.participant_id)


def _claim_hold_payouts():
    """Атомарно забрать все накопившиеся выплаты «Отложена» в работу.

    Захват и смена статуса происходят в одной транзакции под `select_for_update`,
    поэтому второй одновременный запуск плановой задачи (двойное срабатывание
    beat, ручной запуск поверх планового) не увидит эти записи в HOLD и не
    сформирует на тех же участников второй реестр.

    Возвращает список захваченных выплат — уже в статусе STATUS_RETRYABLE.

    Участники с БИК банка из CYCLOPS_CONFIG['BLOCKED_BANK_BIKS'] не
    захватываются вовсе и остаются в «Отложена» — временное ручное исключение
    (например, банк не резолвится в справочнике СБП и решение по нему ещё не
    принято с Заказчиком), не путать с payout_blocker, который помечает
    финальной ошибкой.

    Точно так же пропускаются участники с `guaranteed_prize_opt_out` — те, кто
    добровольно отказался от приза (галочка в карточке участника). Они не
    попадают ни в Реестр выплат, ни в очередь Celery.
    """
    blocked_biks = settings.CYCLOPS_CONFIG.get('BLOCKED_BANK_BIKS') or set()
    with transaction.atomic():
        qs = (
            GuaranteedPrizePayout.objects
            .select_for_update()
            .filter(status=GuaranteedPrizePayout.STATUS_HOLD)
            .exclude(participant__guaranteed_prize_opt_out=True)
        )
        if blocked_biks:
            qs = qs.exclude(participant__bank_bik__in=blocked_biks)
        payouts = list(qs.order_by('pk'))
        if not payouts:
            return []
        # cnt_retry обнуляем: начинается новый цикл отправки (плановая пятница),
        # и на него выплате полагается полный запас авто-повторов — иначе записи,
        # которые банк отклонял неделя за неделей, упирались бы в лимит с первой
        # же технической ошибки.
        GuaranteedPrizePayout.objects.filter(
            pk__in=[p.pk for p in payouts],
        ).update(status=GuaranteedPrizePayout.STATUS_RETRYABLE, cnt_retry=0, updated_at=timezone.now())

    for payout in payouts:
        payout.status = GuaranteedPrizePayout.STATUS_RETRYABLE
        payout.cnt_retry = 0
    return payouts


def process_scheduled_guaranteed_prize_payouts():
    """Плановая пакетная выплата (раз в неделю, пятница).

    Собирает всех участников, накопившихся в статусе «Отложена» (их создаёт
    `ensure_guaranteed_prize_payout` при заполнении профиля), формирует на них
    Реестр выплат — один Excel на всю партию, он же прикладывается к сделке
    каждого участника из партии — и ставит каждую выплату в очередь Celery.

    Защита от повторной выплаты выстроена в три слоя:
    1. `_claim_hold_payouts` атомарно уводит записи из HOLD, так что два
       параллельных запуска не разберут одних и тех же участников;
    2. `GuaranteedPrizePayout.participant` — OneToOneField, в БД физически не
       может быть двух выплат на участника;
    3. `perform_payout` захватывает запись через `select_for_update` и
       немедленно выходит, если статус уже paid/processing/executing.

    Запускается только при CYCLOPS_CONFIG['SCHEDULED_PAYOUTS_ENABLED'] — см.
    задачу promotion.tasks.process_scheduled_guaranteed_prize_payouts.
    """
    from ..tasks import pay_guaranteed_prize
    from . import cyclops_registry

    claimed = _claim_hold_payouts()
    if not claimed:
        logger.info('process_scheduled_guaranteed_prize_payouts: нет накопившихся выплат, пропуск')
        return []

    # Реестр выплат — юридический документ («Реестр перечисления денежных
    # средств Победителям Акции»), поэтому в него попадают только те, кому
    # выплата действительно будет отправлена. Участников с заведомо непригодными
    # данными (нет банка в СБП, битый телефон, неполный профиль) отсеиваем
    # заранее и помечаем финальной ошибкой — иначе они окажутся в подписанном
    # реестре, не получив денег.
    payouts, blocked = [], []
    for payout in claimed:
        reason = payout_blocker(payout.participant)
        if reason:
            blocked.append((payout, reason))
        else:
            payouts.append(payout)

    for payout, reason in blocked:
        _mark_error(payout, reason, retryable=False)
    if blocked:
        logger.warning('process_scheduled_guaranteed_prize_payouts: %s участников исключены из реестра',
                       len(blocked))

    if not payouts:
        logger.info('process_scheduled_guaranteed_prize_payouts: пригодных к выплате участников нет')
        return []

    # Шаблон реестра рассчитан на ограниченное число строк за раз — бьём на
    # партии, каждая партия получает свой реестр.
    step = cyclops_registry.TEMPLATE_DATA_ROWS
    batches = [payouts[i:i + step] for i in range(0, len(payouts), step)]

    # Отправки разносятся по времени (countdown), а не летят в Точку все разом —
    # иначе пакет из десятков/сотен выплат, поставленных в очередь одновременно,
    # выглядит как DDOS для банковского API. Шаг паузы — CYCLOPS_PAYOUT_THROTTLE_SECONDS.
    throttle = settings.CYCLOPS_CONFIG.get('PAYOUT_THROTTLE_SECONDS', 5)

    registries = []
    delay_index = 0
    for batch in batches:
        registry = cyclops_registry.create_registry(batch)
        registries.append(registry)
        for payout in batch:
            pay_guaranteed_prize.apply_async(
                args=[payout.participant_id], countdown=delay_index * throttle,
            )
            delay_index += 1
        logger.info('process_scheduled_guaranteed_prize_payouts: реестр №%s, %s участников поставлено в очередь',
                    registry.registry_number, len(batch))

    message = (
        f'📋 Cyclops: плановая выплата гарантированного приза\n'
        f'участников: {len(payouts)} · реестров: {len(registries)} '
        f'(№{", №".join(r.registry_number for r in registries)})'
    )
    if blocked:
        message += f'\nисключены из реестра (данные непригодны): {len(blocked)}'
    notify_bot(message)
    return registries


def run_next_payouts(limit=10):
    """Ручной тестовый запуск из админки: «Провести следующие N выплат».

    В отличие от `process_scheduled_guaranteed_prize_payouts` (плановая задача
    на весь накопившийся объём, выполняется в Celery), эта функция берёт
    ограниченную партию и выполняет выплаты СИНХРОННО — прямо в текущем
    запросе, — чтобы сразу увидеть результат на экране, не дожидаясь воркера.
    Использует ту же защиту от повторного захвата (select_for_update), что и
    плановая задача, и тот же Реестр выплат на партию.

    В партию берутся только участники с полным ФИО (заполненным отчеством) —
    остальные остаются в «Отложена» до тех пор, пока отчество не появится в
    профиле, независимо от того, сколько выплат запрошено кнопкой. Отказавшиеся
    от приза (`guaranteed_prize_opt_out`) не берутся вовсе.
    """
    from . import cyclops_registry

    with transaction.atomic():
        ids = list(
            GuaranteedPrizePayout.objects
            .filter(status=GuaranteedPrizePayout.STATUS_HOLD)
            .exclude(participant__guaranteed_prize_opt_out=True)
            .exclude(participant__middle_name__isnull=True)
            .exclude(participant__middle_name='')
            .order_by('pk')
            .values_list('pk', flat=True)[:limit]
        )
        if not ids:
            return {'processed': [], 'blocked': [], 'registry': None}

        payouts = list(
            GuaranteedPrizePayout.objects.select_for_update().filter(pk__in=ids).order_by('pk')
        )
        # sent_manually — чтобы apply_deal_status не слал письма об итоге сама:
        # для выплат, запущенных этой кнопкой, письмо об успехе/ошибке ФИО
        # отправляется только вручную со страницы выплаты (см. requirements).
        # cnt_retry=0 — запуск руками начинает новый цикл отправки с полным
        # запасом авто-повторов (см. _claim_hold_payouts).
        GuaranteedPrizePayout.objects.filter(pk__in=ids).update(
            status=GuaranteedPrizePayout.STATUS_RETRYABLE, sent_manually=True,
            cnt_retry=0, updated_at=timezone.now(),
        )

    eligible, blocked = [], []
    for payout in payouts:
        payout.status = GuaranteedPrizePayout.STATUS_RETRYABLE
        payout.sent_manually = True
        payout.cnt_retry = 0
        reason = payout_blocker(payout.participant)
        if reason:
            blocked.append((payout, reason))
        else:
            eligible.append(payout)

    for payout, reason in blocked:
        _mark_error(payout, reason, retryable=False)

    registry = None
    if eligible:
        registry = cyclops_registry.create_registry(eligible)
        for payout in eligible:
            perform_payout(payout.participant_id)
            payout.refresh_from_db()

    return {'processed': eligible, 'blocked': blocked, 'registry': registry}
