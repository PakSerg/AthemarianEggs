import logging
from decimal import Decimal

from django.conf import settings

from promotion.models import Receipt
from promotion.services.validate_receipt import (
    load_keywords,
    load_excluding_keywords,
    item_matches_keyword,
    _normalize_receipt_items,
)
from .module_rapidfuzz import valid_rapidfuzz
from .module_ai import ask_receipt_ai

logger = logging.getLogger(__name__)

def promo_min_sum_kopecks() -> int:
    """Минимальная совокупная стоимость акционных товаров в чеке, копейки.

    В этой Акции ограничения по сумме нет (бриф, п. 4: «Минимальная сумма чека —
    без ограничений»), поэтому по умолчанию порог равен нулю: чек принимается,
    если в нём вообще нашлись акционные позиции. Порог включается настройкой
    PROMO_MIN_SUM_RUB, без правки кода.

    Значение читается на каждый вызов, а не один раз при импорте: иначе
    override_settings в тестах и правка .env на стенде не подхватывались бы
    до перезапуска процесса.
    """
    return int(round(float(getattr(settings, 'PROMO_MIN_SUM_RUB', 0)) * 100))


def promo_items_total_kopecks(items: list[dict]) -> int:
    """Сумма позиций чека (в копейках), уже прошедших проверку по ключевым словам."""
    total = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        value = item.get('sum')
        if value is None:
            price = item.get('price') or 0
            quantity = item.get('quantity') or 0
            try:
                value = price * quantity
            except TypeError:
                value = 0
        try:
            total += round(float(value))
        except (TypeError, ValueError):
            continue
    return total


def receipt_total_kopecks(receipt: Receipt) -> int:
    """
    Сумма всего чека в копейках — включая неакционные товары.

    Берём сумму из данных ФНС, а если её нет — складываем позиции. Нужна для
    первичной отсечки: чек дешевле порога не может содержать акционных товаров
    на нужную сумму, и разбирать его дальше (в том числе нейросетью) незачем.
    """
    amount = getattr(receipt, 'amount', None)
    if amount is not None:
        try:
            return int(round(Decimal(amount) * 100))
        except (TypeError, ValueError, ArithmeticError):
            pass
    return promo_items_total_kopecks(_normalize_receipt_items(receipt.items))


def validate_keywords(receipt: Receipt) -> tuple[bool, list[dict]]:
    """
    Ветвление 1: точное совпадение по ключевым словам.
    Возвращает (matched, список подошедших items).
    """
    items = _normalize_receipt_items(receipt.items)
    if not items:
        return False, []

    keywords = load_keywords()
    excluding_keywords = load_excluding_keywords()

    matched_items = []
    for item in items:
        item_name = (item.get('name') or '').lower()
        if not item_name:
            continue
        matched, _ = item_matches_keyword(item_name, keywords, excluding_keywords)
        if matched:
            matched_items.append(item)

    return bool(matched_items), matched_items


def validate_rapidfuzz(receipt: Receipt) -> tuple[bool, list[dict]]:
    """
    Ветвление 2: нечёткое совпадение через rapidfuzz.
    Возвращает (matched, список подошедших items).
    """
    items = _normalize_receipt_items(receipt.items)
    if not items:
        return False, []

    keywords = load_keywords()

    matched_items = []
    for item in items:
        item_name = (item.get('name') or '').lower()
        if not item_name:
            continue
        result = valid_rapidfuzz([item], keywords)
        if result.get('status'):
            item_label = (result.get('product') or '').strip()
            if len(item_label.split()) > 1:
                matched_items.append(item)

    return bool(matched_items), matched_items


def validate_ai(receipt: Receipt) -> bool:
    """
    Ветвление 3: проверка через AI. Не возвращает конкретные позиции.

    Боевой путь проверки нейросетью — ai_review.review_receipt; здесь остаётся
    только грубый ответ «да/нет» для отладочного прогона всех трёх ветвлений.
    """
    items = _normalize_receipt_items(receipt.items)
    if not items:
        return False

    try:
        answer = ask_receipt_ai(items)
        return bool(answer) and answer.get('verdict') == 'match'
    except Exception:
        logger.exception('validate_ai failed receipt_id=%s', receipt.pk)
        return False


class ValidationResult:
    """Результат трёх ветвлений валидации."""

    def __init__(
        self,
        keywords: bool | None,
        rapidfuzz: bool | None,
        ai: bool | None,
        matched_items: list[dict] | None = None,
    ):
        self.keywords = keywords
        self.rapidfuzz = rapidfuzz
        self.ai = ai
        # Акционные позиции: дедуплицируем по name
        self.matched_items: list[dict] = _dedup_items(matched_items or [])

    @property
    def all_true(self) -> bool:
        return all(x is True for x in [self.keywords, self.rapidfuzz, self.ai] if x is not None)

    @property
    def all_false(self) -> bool:
        return all(x is False for x in [self.keywords, self.rapidfuzz, self.ai] if x is not None)

    @property
    def needs_manual_review(self) -> bool:
        return not self.all_true and not self.all_false

    def __repr__(self):
        return (
            f'ValidationResult('
            f'keywords={self.keywords}, '
            f'rapidfuzz={self.rapidfuzz}, '
            f'ai={self.ai}, '
            f'matched_items={len(self.matched_items)})'
        )


def _dedup_items(items: list[dict]) -> list[dict]:
    """Убирает дубли по полю name (одна позиция может совпасть и по keywords, и по rapidfuzz)."""
    seen: set[str] = set()
    result = []
    for item in items:
        key = (item.get('name') or '').strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def validate(
    receipt: Receipt,
    keywords_mode: bool = True,
    rapidfuzz_mode: bool = True,
    ai_mode: bool = False,
) -> ValidationResult:
    """
    Запускает все три ветвления и возвращает ValidationResult.
    Исключение внутри одного из валидаторов не роняет остальные.
    """
    kw = None
    kw_items: list[dict] = []
    if keywords_mode:
        try:
            kw, kw_items = validate_keywords(receipt)
        except Exception:
            logger.exception('validate_keywords failed receipt_id=%s', receipt.pk)
            kw = False

    rf = None
    rf_items: list[dict] = []
    if rapidfuzz_mode:
        try:
            rf, rf_items = validate_rapidfuzz(receipt)
        except Exception:
            logger.exception('validate_rapidfuzz failed receipt_id=%s', receipt.pk)
            rf = False

    ai = None
    if ai_mode:
        try:
            ai = validate_ai(receipt)
        except Exception:
            logger.exception('validate_ai failed receipt_id=%s', receipt.pk)
            ai = False

    # Объединяем подошедшие позиции из keywords и rapidfuzz
    all_matched = kw_items + rf_items

    logger.info(
        'validate: receipt_id=%s keywords=%s rapidfuzz=%s ai=%s matched_items=%s',
        receipt.pk, kw, rf, ai, len(all_matched),
    )
    return ValidationResult(keywords=kw, rapidfuzz=rf, ai=ai, matched_items=all_matched)