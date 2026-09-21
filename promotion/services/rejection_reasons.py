"""
Единая классификация причин отклонения чеков — используется и дашбордом
staff_panel (интерфейс + выгрузка в Excel), и Django admin (выгрузка в Excel),
чтобы обе стороны показывали одну и ту же картину, а не расходились со временем.

У Receipt нет отдельного поля с кодом причины отклонения — есть только
свободный текст в message/system_message (см. Receipt.message,
ReceiptMessageTemplate). Поэтому причина определяется сопоставлением текста
сообщения с текущим текстом соответствующего шаблона (или с известной
статической строкой для причин, которые шаблонами не оформлены).
"""
from __future__ import annotations

from django.db.models import Count, Q, QuerySet

from ..models import Receipt, ReceiptMessageTemplate

# Причины, оформленные как ReceiptMessageTemplate — сопоставляем по ТЕКУЩЕМУ
# тексту шаблона. Если текст шаблона изменят в админке, уже отклонённые до
# этого чеки со старым текстом попадут в категорию «Другое» — это ожидаемое
# ограничение подхода без отдельного поля-кода на Receipt.
_TEMPLATE_REASONS: tuple[tuple[str, str], ...] = (
    (ReceiptMessageTemplate.Code.REJECTED_PROMO_SUM_TOO_LOW, 'Сумма акционных товаров ниже порога'),
    (ReceiptMessageTemplate.Code.REJECTED_DATE_INVALID, 'Дата покупки вне периода акции'),
    (ReceiptMessageTemplate.Code.REJECTED_QR_DECODE_FAILED, 'Не удалось распознать QR-код на фото'),
    (ReceiptMessageTemplate.Code.REJECTED_FNS_NOT_CONFIRMED, 'ФНС не подтвердил существование чека'),
    (ReceiptMessageTemplate.Code.REJECTED_DUPLICATE, 'Чек уже зарегистрирован (дубликат)'),
    (ReceiptMessageTemplate.Code.REJECTED_ITEMS_MISMATCH, 'Товары не соответствуют условиям акции'),
    (ReceiptMessageTemplate.Code.REJECTED_STORE_NOT_FOUND, 'Магазин не участвует в акции'),
    (ReceiptMessageTemplate.Code.INSUFFICIENT_DATA, 'Недостаточно данных для проверки'),
)

# Причины, которые код проставляет напрямую строкой, минуя ReceiptMessageTemplate
# (см. promotion/services/receipt_fns.py: _handle_fns_error, QRData.from_receipt,
# promotion/tasks.py: process_participant_receipt).
_RAW_REASONS: tuple[tuple[str, str, Q], ...] = (
    (
        'invalid_qr_format',
        'Не удалось распознать QR-код (неверный формат данных)',
        Q(message__icontains='Неверный формат QR') | Q(system_message__icontains='Неверный формат QR'),
    ),
    (
        'fns_retry_exhausted',
        'Чек не прошёл проверку ФНС',
        Q(system_message__icontains='превышено количество попыток')
        | Q(message__icontains='Ошибка получения данных чека из ФНС'),
    ),
    (
        'processing_error',
        'Техническая ошибка при обработке чека',
        Q(message__icontains='Ошибка при проверке чека'),
    ),
)

OTHER_KEY = 'other'
OTHER_LABEL = 'Другое / отклонено вручную без стандартной причины'


def _reason_definitions() -> list[tuple[str, str, Q]]:
    """[(ключ, подпись, Q), ...] в порядке вывода, читает актуальные тексты шаблонов из БД."""
    template_texts = dict(
        ReceiptMessageTemplate.objects.filter(
            code__in=[code for code, _label in _TEMPLATE_REASONS],
        ).values_list('code', 'text')
    )
    reasons = []
    for code, label in _TEMPLATE_REASONS:
        text = template_texts.get(code)
        if code == ReceiptMessageTemplate.Code.REJECTED_PROMO_SUM_TOO_LOW:
            # Чек мог быть отклонён вручную с произвольной формулировкой (не точным
            # текстом шаблона) — но раз в ней упоминается порог по сумме акционных
            # товаров, это та же причина, а не «Другое». Порог менялся (250 → 248 ₽),
            # поэтому матчим оба значения, чтобы не терять старые отклонённые чеки.
            q = Q(message__icontains='250') | Q(message__icontains='248')
            if text:
                q |= Q(message=text)
            reasons.append((code, label, q))
            continue
        if text:
            reasons.append((code, label, Q(message=text)))
    reasons.extend(_RAW_REASONS)
    return reasons


def rejection_reasons_breakdown(receipts_qs: QuerySet[Receipt] | None = None) -> list[dict]:
    """
    Разбивка отклонённых чеков по причинам.

    :param receipts_qs: любой queryset Receipt для ограничения выборки (например,
        по дате регистрации) — фильтр по status=REJECTED накладывается здесь же.
        По умолчанию — все чеки.
    :return: список {key, label, count, percent}, отсортированный по убыванию count.
        Последняя строка (если есть) — «Другое»: чеки, не подошедшие ни под одну
        известную причину (например, отклонённые вручную с произвольным текстом).
        Сумма count по всем строкам равна общему числу отклонённых чеков в выборке.
    """
    base_qs = receipts_qs if receipts_qs is not None else Receipt.objects.all()
    rejected_qs = base_qs.filter(status=Receipt.Status.REJECTED)

    total = rejected_qs.count()
    if total == 0:
        return []

    reasons = _reason_definitions()
    annotate_kwargs = {key: Count('id', filter=q) for key, _label, q in reasons}

    known_q = Q()
    for _key, _label, q in reasons:
        known_q |= q
    annotate_kwargs[OTHER_KEY] = Count('id', filter=~known_q)

    row = rejected_qs.aggregate(**annotate_kwargs)

    results = []
    for key, label, _q in reasons:
        count = row.get(key, 0)
        if count:
            results.append({'key': key, 'label': label, 'count': count})

    other_count = row.get(OTHER_KEY, 0)
    if other_count:
        results.append({'key': OTHER_KEY, 'label': OTHER_LABEL, 'count': other_count})

    results.sort(key=lambda r: r['count'], reverse=True)
    for r in results:
        r['percent'] = round(r['count'] / total * 100, 1)

    return results
