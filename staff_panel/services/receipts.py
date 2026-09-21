from __future__ import annotations

from django.db.models import Q, QuerySet
from django.utils import timezone

from promotion.models import Receipt, ReceiptMessageTemplate
from promotion.services.instant_prizes import grant_attempts_for_receipt
from promotion.services.promo_calendar import assign_promo_period
from promotion.services.validate_receipt import _normalize_receipt_items, sync_promo_keywords_from_receipt

from .filters import values_for
from .phone_search import annotate_phone_digits, looks_like_phone_query, phone_search_q

RECEIPT_SORT_FIELDS = {
    'created_at': 'created_at',
    'participant': 'participant__last_name',
    'status': 'status',
    'amount': 'amount',
    'store': 'store',
    'date': 'date',
}


def get_receipts_queryset() -> QuerySet[Receipt]:
    return Receipt.objects.select_related('participant').all()


def filter_receipts(queryset: QuerySet[Receipt], request) -> QuerySet[Receipt]:
    statuses = [s for s in values_for(request, 'status') if s in Receipt.Status.values]
    if statuses:
        queryset = queryset.filter(status__in=statuses)

    search = request.GET.get('q', '').strip()
    if search:
        # См. комментарий в participants.filter_participants: без классификации
        # запроса цифры внутри email/store давали случайные совпадения по
        # телефону/ИНН и захламляли выдачу.
        if '@' in search:
            queryset = queryset.filter(participant__email__icontains=search)
        elif looks_like_phone_query(search):
            queryset = annotate_phone_digits(queryset, 'participant__phone', 'phone_digits')
            queryset = queryset.filter(
                phone_search_q('phone_digits', search) | Q(inn__icontains=search)
            )
        else:
            queryset = queryset.filter(
                Q(participant__email__icontains=search)
                | Q(participant__first_name__icontains=search)
                | Q(participant__last_name__icontains=search)
                | Q(store__icontains=search)
                | Q(public_id__icontains=search)
            )

    cities = values_for(request, 'city')
    if cities:
        queryset = queryset.filter(participant__city__in=cities)

    input_methods = [
        m for m in values_for(request, 'input_method') if m in Receipt.InputMethod.values
    ]
    if input_methods:
        queryset = queryset.filter(input_method__in=input_methods)

    amount_from = request.GET.get('amount_from', '').strip()
    amount_to = request.GET.get('amount_to', '').strip()
    if amount_from:
        queryset = queryset.filter(amount__gte=amount_from)
    if amount_to:
        queryset = queryset.filter(amount__lte=amount_to)

    return queryset


def apply_date_range(queryset: QuerySet[Receipt], *, field: str, date_from, date_to) -> QuerySet[Receipt]:
    if date_from:
        queryset = queryset.filter(**{f'{field}__date__gte': date_from})
    if date_to:
        queryset = queryset.filter(**{f'{field}__date__lte': date_to})
    return queryset


def get_copyable_texts():
    """Шаблоны сообщений для выпадающего списка «Тексты для копирования» в карточке чека."""
    return list(ReceiptMessageTemplate.objects.all().order_by('code'))


def validate_status_promo_total(status: str, promo_total: float, promo_threshold: float) -> str | None:
    """
    Проверяет согласованность статуса чека и суммы отмеченных акционных товаров.
    Возвращает предупреждающий текст, если статус конфликтует с суммой, иначе None.

    Сохранение чека никогда не блокируется этой проверкой — карточку чека нужно
    иметь возможность редактировать всегда, в том числе уже отклонённый или принятый
    чек. Модератор подтверждает решение осознанно через диалог «Да/Нет» в карточке
    чека (см. initReceiptPromoValidation в panel.js); здесь текст только для
    messages.warning на сервере.
    """
    if status == Receipt.Status.REJECTED and promo_total >= promo_threshold:
        return (
            f'Внимание: чек отклонён, хотя сумма акционных товаров {promo_total:.2f} ₽ '
            f'соответствует условию (≥ {promo_threshold:.0f} ₽).'
        )
    return None


def apply_status_change(receipt: Receipt, previous_status: str) -> None:
    """
    Побочные эффекты смены статуса чека из панели — те же, что и в админке:
    при подтверждении чеку проставляется период Акции (неделя/месяц по дате
    регистрации, см. promo_calendar), при любом решении модератора — дата
    модерации. Без периода подтверждённый чек не попадал бы ни в один
    еженедельный розыгрыш.
    """
    if receipt.status == previous_status:
        return

    update_fields = []
    if receipt.status in (Receipt.Status.CONFIRMED, Receipt.Status.REJECTED):
        receipt.moderated_at = timezone.now()
        update_fields.append('moderated_at')
    # Период считается после moderated_at: от него зависит перенос по п. 4.5 Правил.
    if receipt.status == Receipt.Status.CONFIRMED:
        update_fields += assign_promo_period(receipt)

    if update_fields:
        receipt.save(update_fields=[*update_fields, 'updated_at'])

    if receipt.status == Receipt.Status.CONFIRMED:
        sync_promo_keywords_from_receipt(receipt)
        # Принятый вручную чек даёт те же попытки в лотке яиц, что и принятый
        # автоматически: решение модератора и решение робота для участника
        # неразличимы. Вызов идемпотентен — повторное подтверждение чека
        # попыток не удваивает.
        grant_attempts_for_receipt(receipt)


def set_status(receipt: Receipt, status: str) -> None:
    previous_status = receipt.status
    receipt.status = status
    receipt.save()
    apply_status_change(receipt, previous_status)


def save_promo_items(receipt: Receipt, promo_items: list) -> int:
    cleaned = [
        item for item in promo_items
        if isinstance(item, dict) and item.get('name')
    ]
    receipt.promo_items = cleaned
    receipt.save(update_fields=['promo_items', 'updated_at'])
    # Чек мог быть подтверждён раньше, чем отмечены акционные товары (два
    # независимых сабмита формы в модалке) — досинхронизируем ключевые слова,
    # чтобы порядок действий модератора не влиял на результат.
    if receipt.status == Receipt.Status.CONFIRMED:
        sync_promo_keywords_from_receipt(receipt)
    return len(cleaned)


def items_with_promo_flags(receipt: Receipt) -> list[dict]:
    """Список товаров чека с индексом и флагом «отмечен как акционный» — для модалки панели."""
    items = _normalize_receipt_items(receipt.items)
    promo_names = {
        (item.get('name') or '').strip().lower()
        for item in (receipt.promo_items or [])
        if isinstance(item, dict) and item.get('name')
    }
    result = []
    for idx, item in enumerate(items):
        result.append({
            'idx': idx,
            'name': item.get('name', ''),
            'quantity': item.get('quantity', 0),
            'price': (item.get('price', 0) or 0) / 100,
            'sum': (item.get('sum', 0) or 0) / 100,
            'is_promo': (item.get('name') or '').strip().lower() in promo_names,
        })
    return result
