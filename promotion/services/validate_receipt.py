import logging
from datetime import date, datetime
import re

from django.utils import timezone
from idna import check_label

from promotion.messages import ReceiptMessage
from promotion.models import Raffle, Receipt, Store
from promotion.models import ExcludingKeywordProduct, KeywordProduct

logger = logging.getLogger(__name__)

RECEIPT_DATE_TOO_EARLY_MESSAGE = (
    'Дата чека раньше начала акции. Чек не может участвовать в розыгрыше.'
)
RECEIPT_DATE_TOO_LATE_MESSAGE = (
    'Дата чека позже окончания акции. Чек не может участвовать в розыгрыше.'
)


def _normalize_receipt_items(items):
    if not items:
        return []
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    if isinstance(items, dict):
        if 'items' in items and isinstance(items['items'], list):
            return [item for item in items['items'] if isinstance(item, dict)]
        return [items]
    return []


def load_keywords() -> list[str]:
    return [kw.keyword.lower() for kw in KeywordProduct.objects.all() if kw.keyword]


def load_excluding_keywords() -> list[str]:
    return [kw.keyword.lower() for kw in ExcludingKeywordProduct.objects.all() if kw.keyword]


def item_matches_keyword(
    item_name: str,
    keywords: list[str],
    excluding_keywords: list[str] | None = None,
) -> tuple[bool, str | None]:
    """Возвращает (matched, keyword). Учитывает исключающие ключевые слова."""
    if excluding_keywords:
        for kw in excluding_keywords:
            if kw in item_name:
                return False, None

    for kw in keywords:
        if kw in item_name:
            return True, kw

    return False, None


def sync_promo_keywords_from_receipt(receipt: Receipt) -> int:
    """
    Названия отмеченных акционных товаров чека добавляются в ключевые слова —
    чтобы при следующей проверке такие товары принимались автоматически.
    Возвращает количество добавленных новых ключевых слов.
    """
    names = {
        (item.get('name') or '').strip()
        for item in (receipt.promo_items or [])
        if isinstance(item, dict) and item.get('name')
    }
    added = 0
    for name in names:
        if not name:
            continue
        if not KeywordProduct.objects.filter(keyword__iexact=name).exists():
            KeywordProduct.objects.create(keyword=name)
            added += 1
    return added


# Валидация даты 

def _receipt_check_date(receipt) -> date | None:
    dt = receipt.date
    if not dt:
        return None
    if isinstance(dt, datetime):
        if timezone.is_aware(dt):
            return timezone.localtime(dt).date()
        return dt.date()
    return dt


def validate_receipt_start_date(receipt: Receipt) -> str | None:
    """Возвращает текст ошибки, если дата чека раньше старта акции."""
    check_date = _receipt_check_date(receipt)
    print(f'check_date: {check_date}')
    if check_date is None:
        return None
    receipt_start_date = Raffle.objects.first().start_date.date()
    receipt_end_date = Raffle.objects.first().end_date.date()
    print(receipt_start_date, receipt_end_date)
    if check_date < receipt_start_date or check_date > receipt_end_date:
        return ReceiptMessage.get(ReceiptMessage.REJECTED_DATE_INVALID)
    return None


class StoreValidationResult:
    """Результат валидации магазина"""

    def __init__(self, address: bool | None = None, inn: bool | None = None, store: bool | None = None):
        if all(arg is None for arg in (address, inn, store)):
            raise Exception('Не передано ни одного аргумента для валидации')
        self.address = address
        self.inn = inn
        self.store = store

    @property
    def all_true(self) -> bool:
        return all(x is True for x in [self.address, self.inn, self.store] if x is not None)

    @property
    def all_false(self) -> bool:
        return all(x is False for x in [self.address, self.inn, self.store] if x is not None)

    def __repr__(self):
        return (
            f'StoreValidationResult('
            f'address={self.address}, '
            f'inn={self.inn}, '
            f'store={self.store})'
        )


def validate_receipt_store(receipt: Receipt) -> 'StoreValidationResult':
    """Возвращает, найден ли ИНН магазина, в котором был куплен чек, в базе данных"""

    def clean_string(text: str) -> str:
        if not text:
            return ""
        # Удаление всех символов, кроме цифр, букв, пробелов и дефисов
        return re.sub(r'[^\w-]', '', text).lower()

    stores = Store.objects.all()

    db_inns = set(clean_string(store.inn) for store in stores if store.inn)

    clean_receipt_inn = clean_string(receipt.inn)

    right_inn = clean_receipt_inn in db_inns

    return StoreValidationResult(inn=right_inn)


# Валидация чека (ключевые слова)

def validate_check(receipt: Receipt) -> tuple[bool, bool]:
    """
    Возвращает (valid, has_product).
    Только ключевые слова — без rapidfuzz и AI.
    """
    try:
        if validate_receipt_start_date(receipt):
            return False, False

        items = _normalize_receipt_items(receipt.items)
        if not items:
            return False, False

        keywords = load_keywords()
        excluding_keywords = load_excluding_keywords()

        for item in items:
            item_name = (item.get('name') or '').lower()
            if not item_name:
                continue

            matched, _ = item_matches_keyword(item_name, keywords, excluding_keywords)
            if matched:
                return True, True

        return False, False

    except Exception:
        logger.exception('validate_check failed receipt_id=%s', receipt.pk)
        return False, False