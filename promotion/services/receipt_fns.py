from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import wraps
import sys
from typing import TypedDict, Any

from promotion.messages import ReceiptMessage
from promotion.services.instant_prizes import grant_attempts_for_receipt
from promotion.services.promo_calendar import assign_promo_period
from django.conf import settings
if sys.version_info >= (3, 11):
    from typing import NotRequired
else:
    from typing_extensions import NotRequired

from django.utils import timezone
from ..models import Receipt
from ..api.utils import create_api_client, decode_qr_fns
from .validate_receipt import validate_check, validate_receipt_start_date
from .notify_bot import notify as notify_bot
from ..debug_config import DEBUG_FNS_RESULT

logger = logging.getLogger(__name__)


# Константы
class ReceiptStatus:
    PENDING = 'pending'
    REJECTED = 'rejected'


class ErrorMessage:
    NOT_FOUND = 'not_found'
    DUPLICATE = 'duplicate'
    
    
FNS_MAX_RETRY_ATTEMPTS = 30


def is_fns_error_message(message: str | None) -> bool:
    """Ошибка, связанная с ФНС — чек остаётся на проверке и уходит в повтор."""
    if not message:
        return False
    return 'фнс' in message.lower()


# Типизированные словари для ответов
class ProcessingResult(TypedDict):
    ok: bool
    error: NotRequired[str]
    skipped: NotRequired[bool]
    status: NotRequired[str]
    message_id: NotRequired[str]


class FNSResponse(TypedDict):
    status: str
    message_id: str
    ticket_data: dict


# Dataclass для хранения QR данных
@dataclass
class QRData:
    raw: str
    fn: str
    fd: str
    fp: str
    t: str | None

    @classmethod
    def from_receipt(cls, receipt: Receipt) -> tuple['QRData | None', str | None]:
        """Извлекает QR данные из чека или возвращает ошибку"""
        try:
            qr_payload, error = _resolve_qr_payload(receipt)
            if error:
                return None, error

            client = create_api_client()
            params = client.parse_qr_data(qr_payload)

            if not isinstance(params, dict):
                return None, 'Неверный формат QR-кода'

            if not params.get('fn') or not params.get('i') or not params.get('fp') or not params.get('t'):
                return None, 'Неверный формат QR-кода'

            return cls(
                raw=qr_payload,
                fn=params.get('fn', ''),
                fd=str(params.get('i', '')),
                fp=params.get('fp', ''),
                t=params.get('t')
            ), None

        except Exception as e:
            return None, ReceiptMessage.get(ReceiptMessage.REJECTED_QR_DECODE_FAILED)


# Dataclass для данных из ФНС
@dataclass
class TicketData:
    fn: str
    fd: str
    fp: str
    amount: Decimal | None
    date: datetime | None
    store: str | None
    address: str | None 
    inn: str | None
    items: Any

    @classmethod
    def from_fns_response(cls, ticket: dict) -> 'TicketData':
        """Извлекает данные чека из ответа ФНС"""
        content = ticket.get('content', {})

        return cls(
            fn=content.get('fiscalDriveNumber', ''),
            fd=content.get('fiscalDocumentNumber', ''),
            fp=content.get('fiscalSign', ''),
            amount=_total_sum_to_decimal(content.get('totalSum')),
            date=_parse_ticket_datetime(content.get('dateTime')),
            store=content.get('user', '').strip() or None,
            address=content.get('retailPlaceAddress').strip(),
            inn=content.get('userInn').strip(),
            items=content.get('items', '')
        )


# Декоратор для обработки ошибок
def handle_errors(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Receipt.DoesNotExist:
            return {'ok': False, 'error': ErrorMessage.NOT_FOUND}
        except Exception as e:
            logger.exception(f"Unexpected error in {func.__name__}")
            return {'ok': False, 'error': str(e)}

    return wrapper


# ============= Вспомогательные функции =============

def _format_t_param(dt: datetime | None) -> str | None:
    """Форматирует дату для параметра t в QR коде"""
    if not dt:
        return None
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.strftime('%Y%m%dT%H%M')


def _build_fns_qr_string(receipt: Receipt) -> str | None:
    """Собирает строку QR кода из полей чека"""
    if receipt.qr_code_str and receipt.qr_code_str.strip():
        return receipt.qr_code_str.strip()

    fn = (receipt.fn or '').strip()
    fd = (receipt.fd or '').strip()
    fp = (receipt.fp or '').strip()

    if not (fn and fd and fp):
        return None

    if receipt.amount is None or receipt.date is None:
        return None

    t = _format_t_param(receipt.date)
    if not t:
        return None

    try:
        s_converted = float(receipt.amount)
    except (TypeError, ValueError, ArithmeticError):
        return None

    return f'fn={fn}&i={fd}&fp={fp}&t={t}&s={s_converted}'


def _resolve_qr_payload(receipt: Receipt) -> tuple[str | None, str | None]:
    """Получает QR payload из разных источников: строка, изображение или поля"""
    # Приоритет 1: прямая строка QR кода
    if receipt.qr_code_str and receipt.qr_code_str.strip():
        return receipt.qr_code_str.strip(), None

    # Приоритет 2: распознавание из изображения
    if receipt.receipt_image:
        try:
            path = receipt.receipt_image.path
            raw = decode_qr_fns(path)
            if raw:
                return raw, None
            return None, ReceiptMessage.get(ReceiptMessage.REJECTED_QR_DECODE_FAILED)
        except Exception as exc:
            logger.warning(f'QR decode from image failed: {exc}')
            return None, f'Ошибка распознавания изображения: {exc}'

    # Приоритет 3: сборка из отдельных полей
    built = _build_fns_qr_string(receipt)
    if built:
        return built, None

    return None, ReceiptMessage.get(ReceiptMessage.INSUFFICIENT_DATA)


def _persist_qr_raw_if_needed(receipt: Receipt, qr_data: QRData) -> None:
    """
    Сохраняет распознанную строку QR-кода сразу после успешного декодирования,
    не дожидаясь ответа ФНС. Без этого при ошибке/недоступности ФНС повторные
    попытки (см. repeat_process_receipts) заново распознают QR с фото при
    каждом ретрае, включая обращение к внешнему сервису распознавания
    (proverkacheka.com), у которого есть лимит запросов на один чек — из-за
    этого после нескольких ретраев внешний сервис начинает отказывать и чек
    перестаёт распознаваться вовсе.
    """
    if qr_data.raw and not (receipt.qr_code_str or '').strip():
        receipt.qr_code_str = qr_data.raw
        _save_receipt_with_fields(receipt, ['qr_code_str'])


def _find_duplicate(receipt: Receipt, fn: str, fd: str, fp: str) -> Receipt | None:
    """Ищет дубликат чека по ФН/ФД/ФП"""
    if not all([fn, fd, fp]):
        return None

    return Receipt.objects.filter(
        fn=fn,
        fd=fd,
        fp=fp,
    ).exclude(pk=receipt.pk).first()


def _parse_ticket_datetime(value: Any) -> datetime | None:
    """Парсит дату из ответа ФНС"""
    if not isinstance(value, str) or 'T' not in value:
        return None

    try:
        dt = datetime.strptime(value, "%Y%m%dT%H%M")
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt
    except ValueError:
        return None


def _total_sum_to_decimal(raw: Any) -> Decimal | None:
    """Конвертирует сумму из копеек в Decimal"""
    if raw is None:
        return None

    try:
        v = int(raw)
        return (Decimal(v) / Decimal(100)).quantize(Decimal('0.01'))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _copy_receipt_data(target: Receipt, source: Receipt, fields: list[str]) -> None:
    """Копирует данные из одного чека в другой"""
    for field in fields:
        setattr(target, field, getattr(source, field))


def _save_receipt_with_fields(receipt: Receipt, fields: list[str]) -> None:
    """Сохраняет чек только с указанными полями"""
    receipt.save(update_fields=[*fields, 'updated_at'])
    

def _send_pending_notification(receipt: Receipt):
    """Отправляет уведомление о необходимости ручной проверки, если оно ещё не отправлено для этого чека""" 
    if not receipt.review_notified:
        try:
            # notify_receipt_needs_review(receipt)
            receipt.review_notified = True
            logger.info('receipt_fns: review notification sent receipt_id=%s', receipt.pk)
        except Exception:
            logger.exception('receipt_fns: failed to notify about receipt %s', receipt.pk)
    else:
        logger.info(
            'receipt_fns: skip review notification receipt_id=%s — already notified',
            receipt.pk,
        )


# ============= Функции обновления статусов =============

def _skip_response(receipt: Receipt) -> ProcessingResult:
    """Возвращает ответ для пропущенного чека"""
    return {'ok': True, 'skipped': True, 'status': receipt.status}


def _reject_receipt(receipt: Receipt, error_message: str, **extra_fields) -> ProcessingResult:
    """Отклоняет чек с указанной ошибкой"""
    receipt.status = Receipt.Status.REJECTED
    receipt.message = error_message
    receipt.moderated_at = timezone.now()

    for field, value in extra_fields.items():
        if hasattr(receipt, field):
            setattr(receipt, field, value)

    _save_receipt_with_fields(receipt, ['status', 'message', 'moderated_at', *extra_fields.keys()])
    from .receipt_status_email import send_receipt_rejected_email
    send_receipt_rejected_email(receipt)
    return {'ok': False, 'error': error_message}


def _receipt_can_retry_fns(receipt: Receipt) -> bool:
    """Достаточно данных для повторного запроса в ФНС."""
    if (receipt.qr_code_str or '').strip():
        return True
    if receipt.receipt_image:
        return True
    if receipt.fn and receipt.fd and receipt.fp and receipt.amount is not None and receipt.date:
        return True
    return bool(_build_fns_qr_string(receipt))


def _max_fns_retry_attempts() -> int:
    return int(getattr(settings, 'FNS_RECEIPT_MAX_RETRY_ATTEMPTS', 30))

def _handle_fns_error(receipt: Receipt, error_message: str, message_id: str | None = None) -> ProcessingResult:
    """
    Ошибка ФНС: чек остаётся на проверке, повтор через периодическую задачу.
    После FNS_RECEIPT_MAX_RETRY_ATTEMPTS неудач — отклонение.
    """
    receipt.status = Receipt.Status.PENDING
    receipt.system_message = error_message
    receipt.message = "Чек на проверке"
    receipt.fns_retry_pending = True
    receipt.fns_retry_count = (receipt.fns_retry_count or 0) + 1

    if message_id:
        receipt.fns_message_id = message_id

    if receipt.fns_retry_count >= _max_fns_retry_attempts():
        receipt.status = Receipt.Status.REJECTED
        receipt.fns_retry_pending = False
        receipt.system_message = "Ошибка ФНС: превышено количество попыток получения данных чека из ФНС"
        receipt.message = "Ошибка получения данных чека из ФНС"

    _save_receipt_with_fields(
        receipt,
        ['status', 'message', 'system_message', 'fns_retry_pending', 'fns_retry_count', 'fns_message_id'],
    )
    return {'ok': False, 'error': error_message, 'status': receipt.status}


def _handle_duplicate(receipt: Receipt, original: Receipt) -> ProcessingResult:
    """Обрабатывает случай дубликата чека"""
    receipt.status = Receipt.Status.REJECTED
    receipt.message = ReceiptMessage.get(ReceiptMessage.REJECTED_DUPLICATE)

    # Копируем все данные из оригинального чека
    fields_to_copy = ['fn', 'fd', 'fp', 'amount', 'date', 'store', 'items']
    _copy_receipt_data(receipt, original, fields_to_copy)

    receipt.moderated_at = timezone.now()
    _save_receipt_with_fields(receipt, ['status', 'message', 'moderated_at', *fields_to_copy])
    from .receipt_status_email import send_receipt_rejected_email
    send_receipt_rejected_email(receipt)
    return {'ok': False, 'error': ErrorMessage.DUPLICATE}


def _update_receipt_from_fns(
        receipt: Receipt,
        qr_data: QRData,
        ticket_data: TicketData,
        message_id: str | None
) -> ProcessingResult:
    """Обновляет чек данными из ФНС"""
    logger.info(
        'receipt_fns: start updating receipt_id=%s participant_id=%s',
        receipt.pk,
        receipt.participant_id,
    )
    # Заполняем данными из QR
    if qr_data.fn:
        receipt.fn = qr_data.fn
    if qr_data.fd:
        receipt.fd = qr_data.fd
    if qr_data.fp:
        receipt.fp = qr_data.fp
    if qr_data.t:
        parsed_qr_date = _parse_ticket_datetime(qr_data.t)
        if parsed_qr_date:
            receipt.date = parsed_qr_date

    # Заполняем данными из ФНС
    if ticket_data.fn:
        receipt.fn = ticket_data.fn
    if ticket_data.fd:
        receipt.fd = ticket_data.fd
    if ticket_data.fp:
        receipt.fp = ticket_data.fp
    if ticket_data.amount is not None:
        receipt.amount = ticket_data.amount
    if ticket_data.date:
        receipt.date = ticket_data.date
    if ticket_data.store:
        receipt.store = ticket_data.store
    if ticket_data.items is not None:
        receipt.items = ticket_data.items
    if ticket_data.address: 
        receipt.address = ticket_data.address 
    if ticket_data.inn: 
        receipt.inn = ticket_data.inn

    if message_id:
        receipt.fns_message_id = message_id

    if qr_data.raw and not (receipt.qr_code_str or '').strip():
        receipt.qr_code_str = qr_data.raw

    date_error = validate_receipt_start_date(receipt)
    if date_error:
        logger.info(
            'receipt_fns: receipt_id=%s rejected — date before promotion start',
            receipt.pk,
        )
        return _reject_receipt(
            receipt,
            date_error,
            fn=receipt.fn,
            fd=receipt.fd,
            fp=receipt.fp,
            amount=receipt.amount,
            date=receipt.date,
            store=receipt.store,
            items=receipt.items,
            qr_code_str=receipt.qr_code_str,
            fns_message_id=receipt.fns_message_id,
        )
        
    # Проверка ИНН магазина по справочнику Store НЕ проводится: Правила Акции
    # Правила Акции (бриф, п. 3) не ограничивают участие конкретной
    # торговой сетью — Товар может быть куплен в любом магазине на территории РФ.
    # Справочник Store больше не используется для валидации чеков.

    # ── Проверка товаров чека ─────────────────────────────────────────────────
    # Порядок такой:
    #   1. сумма всего чека ниже порога — автоотказ, дальше не смотрим;
    #   2. сумма акционных товаров по ключевым словам достаточна — автоприём;
    #   3. всё остальное — вопрос к нейросети, и чек в любом случае остаётся
    #      как минимум на ручной проверке: отклонить чек нейросеть не может.
    from .v2_validate_receipt.validate import (
        promo_items_total_kopecks,
        promo_min_sum_kopecks,
        receipt_total_kopecks,
        validate_keywords,
        _dedup_items,
    )
    from .v2_validate_receipt.ai_review import review_receipt, rubles
    from .validate_receipt import sync_promo_keywords_from_receipt

    # Системное сообщение от нейросети живёт до самого сохранения: подтверждённый чек
    # ниже затирает system_message, а этот текст на нём нужно оставить.
    ai_system_message = ''

    min_sum = promo_min_sum_kopecks()

    receipt_total = receipt_total_kopecks(receipt)
    if min_sum and receipt_total < min_sum:
        # Весь чек дешевле порога — акционных товаров на нужную сумму в нём быть
        # не может, поэтому ни ключевые слова, ни нейросеть уже ничего не изменят.
        logger.info(
            'receipt_fns: receipt_id=%s rejected — receipt total below threshold (%s)',
            receipt.pk, receipt_total,
        )
        return _reject_receipt(
            receipt,
            ReceiptMessage.get(ReceiptMessage.REJECTED_PROMO_SUM_TOO_LOW),
            fn=receipt.fn,
            fd=receipt.fd,
            fp=receipt.fp,
            amount=receipt.amount,
            date=receipt.date,
            store=receipt.store,
            items=receipt.items,
            address=receipt.address,
            inn=receipt.inn,
            qr_code_str=receipt.qr_code_str,
            fns_message_id=receipt.fns_message_id,
        )

    matched, matched_items = validate_keywords(receipt)
    keywords_sum = promo_items_total_kopecks(matched_items) if matched else 0
    logger.info(
        'receipt_fns: receipt_id=%s keywords_matched=%s items=%s sum=%s total=%s',
        receipt.pk, matched, len(matched_items), keywords_sum, receipt_total,
    )
    if matched:
        receipt.promo_items = matched_items

    if not matched or keywords_sum < min_sum:
        # Ключевых слов не хватило (совсем не нашлись или нашлись на сумму меньше
        # порога) — последний шаг перед ручной модерацией: спрашиваем нейросеть.
        # Условие «not matched» обязательно отдельным слагаемым: при выключенном
        # пороге (min_sum = 0) сравнение суммы всегда ложно, и чек без единой
        # акционной позиции уезжал бы в автоприём.
        # Подтвердить чек она может, отклонить — никогда: любой её ответ, кроме
        # уверенного «подходит», оставляет чек на проверке.
        ai = review_receipt(receipt)

        if ai.accepted and not receipt.participant.is_blocked:
            receipt.promo_items = _dedup_items(matched_items + ai.promo_items)
            receipt.status = Receipt.Status.CONFIRMED
            # Период чека — по дате его регистрации участником (см. ветку с ключевыми
            # словами ниже), поэтому moderated_at выставляем до assign_promo_period.
            receipt.moderated_at = timezone.now()
            assign_promo_period(receipt)
            receipt.message = ReceiptMessage.get(ReceiptMessage.ACCEPTED, week_num=receipt.week)
            ai_system_message = ai.system_message
            logger.info(
                'receipt_fns: receipt_id=%s confirmed by AI — promo sum=%s',
                receipt.pk, ai.promo_sum_kopecks,
            )
            from .receipt_status_email import send_receipt_confirmed_email
            send_receipt_confirmed_email(receipt)
        else:
            if receipt.participant.is_blocked:
                receipt.status = Receipt.Status.FROZEN
                # У заблокированного участника чек не подтверждается автоматически
                # даже при уверенном «подходит» — решение остаётся за модератором.
                if ai.accepted:
                    receipt.system_message = (
                        f'Нейросеть определила акционные товары на сумму '
                        f'{rubles(ai.promo_sum_kopecks)} ₽, но участник заблокирован — '
                        f'чек заморожен.'
                    )
                else:
                    receipt.system_message = (
                        f'{ai.system_message} (участник заблокирован, чек заморожен)'
                    )
                logger.info('receipt_fns: receipt_id=%s frozen — participant is blocked', receipt.pk)
            else:
                receipt.status = Receipt.Status.PENDING
                receipt.system_message = ai.system_message
                logger.info(
                    'receipt_fns: receipt_id=%s pending — keywords sum=%s below threshold, ai verdict=%s',
                    receipt.pk, keywords_sum, ai.verdict,
                )
            receipt.message = ReceiptMessage.get(ReceiptMessage.PENDING)
            _send_pending_notification(receipt)
    else:
        # Ключевые слова набрали порог — чек принимается автоматически, без нейросети.
        receipt.status = Receipt.Status.CONFIRMED
        # Период чека — по дате его регистрации участником, а не по дате
        # модерации. Момент завершения модерации всё же нужен: от него зависит
        # перенос чека в следующий розыгрыш по п. 4.5 Правил, поэтому
        # moderated_at выставляем ДО assign_promo_period.
        receipt.moderated_at = timezone.now()
        assign_promo_period(receipt)
        receipt.message = ReceiptMessage.get(ReceiptMessage.ACCEPTED, week_num=receipt.week)
        logger.info('receipt_fns: receipt_id=%s confirmed — promo sum=%s', receipt.pk, keywords_sum)
        sync_promo_keywords_from_receipt(receipt)
        from .receipt_status_email import send_receipt_confirmed_email
        send_receipt_confirmed_email(receipt)

    if receipt.status == Receipt.Status.CONFIRMED:
        receipt.retry_count = 0
        # На подтверждённом чеке системное сообщение обнуляется — кроме случая, когда
        # чек принят нейросетью: по этому тексту в панели видно, кем принят чек.
        receipt.system_message = ai_system_message

    receipt.fns_retry_pending = False

    fields_to_update = [
        'fn', 'fd', 'fp', 'amount', 'date', 'store', 'items',
        'qr_code_str', 'fns_message_id', 'status', 'message',
        'review_notified', 'retry_count', 'system_message',
        'address', 'inn', 'week', 'month', 'promo_items',
        'moderated_at', 'fns_retry_pending',
        # Разбор нейросети: заполняется в review_receipt, сохраняется здесь.
        'ai_recommendation', 'ai_review_note', 'ai_reviewed_at',
    ]
    _save_receipt_with_fields(receipt, fields_to_update)
    # Попытки в лотке яиц начисляются ПОСЛЕ сохранения статуса: начислять их
    # чеку, который ещё не записан как принятый, нельзя — упадёт сохранение,
    # и у участника останутся попытки от несуществующего чека.
    grant_instant_attempts(receipt)
    logger.info(
        'receipt_fns: receipt_id=%s saved status=%s review_notified=%s',
        receipt.pk,
        receipt.status,
        receipt.review_notified,
    )

    return {'ok': True, 'status': receipt.status}


def _fetch_fns_data(qr_payload: str) -> tuple[dict | None, str | None]:
    """Запрашивает данные чека из ФНС"""
    if getattr(settings, 'FNS_DISABLED', False):
        logger.info('FNS_DISABLED: возвращаем тестовый ответ ФНС')
        return DEBUG_FNS_RESULT, None

    client = create_api_client()
    result = client.get_ticket(qr_payload)

    if not result or result.get('status') != 'success':
        return None, ReceiptMessage.get(ReceiptMessage.REJECTED_FNS_NOT_CONFIRMED)

    ticket = result.get('ticket_data')
    if not isinstance(ticket, dict):
        return None, ReceiptMessage.get(ReceiptMessage.REJECTED_FNS_NOT_CONFIRMED)

    return result, None


# ============= Главная функция =============

@handle_errors
def grant_instant_attempts(receipt: Receipt) -> None:
    """Начислить попытки лотка яиц за принятый чек.

    Ошибка в механике моментальных призов не должна ломать приём чека —
    чек уже сохранён, и его судьба от лотка не зависит.
    """
    if receipt.status != Receipt.Status.CONFIRMED:
        return
    try:
        granted = grant_attempts_for_receipt(receipt)
        if granted:
            logger.info(
                'receipt_fns: receipt_id=%s — начислено попыток лотка: %s',
                receipt.pk, granted,
            )
    except Exception:
        logger.exception('receipt_fns: не удалось начислить попытки receipt_id=%s', receipt.pk)


def process_receipt_by_id(receipt_id: int) -> ProcessingResult:
    """
    Обрабатывает чек по ID:
    1. Получает QR данные из разных источников
    2. Проверяет дубликаты
    3. Запрашивает данные из ФНС
    4. Обновляет чек
    """
    receipt = Receipt.objects.get(pk=receipt_id)

    # Если чек уже обработан - пропускаем
    if receipt.status != Receipt.Status.PENDING:
        return _skip_response(receipt)

    # Получаем QR данные
    qr_data, error = QRData.from_receipt(receipt)
    if error:
        return _reject_receipt(receipt, error)

    # Сохраняем распознанную строку QR сразу, чтобы повторные попытки (при
    # ошибке ФНС) не распознавали QR с фото заново
    _persist_qr_raw_if_needed(receipt, qr_data)

    # Проверяем дубликаты
    duplicate = _find_duplicate(receipt, qr_data.fn, qr_data.fd, qr_data.fp)
    if duplicate:
        return _handle_duplicate(receipt, duplicate)

    # Запрашиваем данные из ФНС
    fns_result, error = _fetch_fns_data(qr_data.raw)
    if error:
        message_id = (fns_result or {}).get('message_id') if fns_result else None
        return _handle_fns_error(receipt, error, message_id)

    # Извлекаем данные чека из ответа
    ticket_data = TicketData.from_fns_response(fns_result.get('ticket_data', {}))
    message_id = fns_result.get('message_id')

    # Обновляем чек
    return _update_receipt_from_fns(receipt, qr_data, ticket_data, message_id)

def repeat_process_receipts():
    """Повторная проверка чеков, не найденных в ФНС (celery beat, раз в 4 часа)."""
    receipts = Receipt.objects.filter(
        fns_retry_pending=True,
    ).select_related('participant')

    processed = 0
    for receipt in receipts:
        if not _receipt_can_retry_fns(receipt):
            logger.warning(
                'repeat_process_receipts: skip receipt_id=%s — недостаточно данных для повтора',
                receipt.pk,
            )
            continue

        try:
            logger.info(
                'repeat_process_receipts: retry receipt_id=%s attempt=%s',
                receipt.pk,
                receipt.fns_retry_count,
            )
            process_receipt_by_id(receipt.pk)
            processed += 1
        except Exception:
            logger.exception(
                'repeat_process_receipts: failed receipt_id=%s',
                receipt.pk,
            )

    logger.info('repeat_process_receipts: finished, processed=%s', processed)


def notify_pending_receipts_manual_check(receipts) -> None:
    """Ежедневная сводка pending-чеков для ручной проверки через forward-signal.

    receipts: iterable объектов Receipt (id обязателен).
    """
    count = len(receipts)
    if count == 0:
        return

    admin_base_url = settings.SITE_URL

    lines = [
        'Чеки на ручную проверку',
        f'Всего pending-чеков: {count}',
        '',
    ]
    for idx, receipt in enumerate(receipts[:80], start=1):
        lines.append(f'{idx}. {admin_base_url}/admin/promotion/receipt/{receipt.id}/change/')
    if count > 80:
        lines.append(f'…ещё {count - 80} чеков')

    notify_bot('\n'.join(lines))


