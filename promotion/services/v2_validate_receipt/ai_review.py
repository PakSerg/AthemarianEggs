"""
Проверка чека нейросетью и решение по её ответу.

Нейросеть включается последней — после всех первичных проверок (ФНС, дата, дубль,
ключевые слова). Она может только ПОДТВЕРДИТЬ чек; отклонить чек она не может ни при
каком ответе. Если модель сомневается, предлагает отклонить или недоступна — чек
остаётся «На проверке» и уходит к живому модератору, статус не меняется.

Ключевые слова пополняются при любом вердикте, но только теми позициями, в которых
модель уверена полностью (sure = true).

Разбор всегда складывается в поля чека ai_recommendation / ai_review_note /
ai_reviewed_at. В теневом режиме (settings.OPENROUTER_SHADOW_MODE) это её
единственный след: статус, сообщение участнику и ключевые слова остаются нетронутыми.
"""
import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from promotion.models import KeywordProduct, Receipt
from promotion.services.validate_receipt import _normalize_receipt_items

from .module_ai import ask_receipt_ai, is_enabled
from .validate import (
    promo_min_sum_kopecks,
    promo_items_total_kopecks,
    receipt_total_kopecks as receipt_total,
)

logger = logging.getLogger(__name__)

# Что нейросеть рекомендует сделать с чеком. Отклонить чек система всё равно не
# может по итогам её ответа — это именно рекомендация, для глаз модератора.
RECOMMENDATION_ACCEPT = Receipt.AIRecommendation.ACCEPT
RECOMMENDATION_REVIEW = Receipt.AIRecommendation.REVIEW
RECOMMENDATION_REJECT = Receipt.AIRecommendation.REJECT
RECOMMENDATION_NO_ANSWER = Receipt.AIRecommendation.NO_ANSWER


@dataclass
class AIReview:
    """Результат проверки чека нейросетью."""

    accepted: bool                       # можно ли подтвердить чек автоматически
    system_message: str                  # текст для «Системного сообщения» в панели
    promo_items: list[dict] = field(default_factory=list)   # позиции, в которых модель уверена
    promo_sum_kopecks: int = 0
    verdict: str | None = None           # match / doubt / reject / None (ответа не было)
    added_keywords: list[str] = field(default_factory=list)
    # Что модель рекомендовала бы сделать с чеком. В теневом режиме это единственный
    # её след: accepted там всегда False, а ключевые слова не добавляются.
    recommendation: str | None = None
    suggested_keywords: list[str] = field(default_factory=list)


def rubles(kopecks: int) -> str:
    return f'{kopecks / 100:.2f}'.replace('.', ',')


def _sure_items(items: list[dict], answer: dict) -> tuple[list[dict], list[dict]]:
    """
    Раскладывает позиции, отмеченные моделью, на «уверенные» и «сомнительные».

    Модель возвращает индексы позиций переданного списка — так не нужно сопоставлять
    строки обратно и невозможно «придумать» товар, которого в чеке не было.
    """
    sure, unsure = [], []
    for entry in answer.get('promo_items') or []:
        if not isinstance(entry, dict):
            continue
        index = entry.get('index')
        if not isinstance(index, int) or not (0 <= index < len(items)):
            logger.warning('ai_review: модель вернула индекс вне чека: %r', index)
            continue
        item = items[index]
        if entry.get('sure') is True:
            sure.append(item)
        else:
            unsure.append(item)
    return sure, unsure


def _new_keyword_candidates(items: list[dict]) -> list[str]:
    """Наименования позиций, которых в ключевых словах ещё нет."""
    candidates = []
    for item in items:
        name = (item.get('name') or '').strip()
        if not name or name in candidates:
            continue
        if KeywordProduct.objects.filter(keyword__iexact=name).exists():
            continue
        candidates.append(name)
    return candidates


def _add_keywords(names: list[str]) -> list[str]:
    """
    Заносит наименования в ключевые слова, чтобы в следующий раз такой товар
    определялся автоматически, без нейросети. Вызывается только для позиций,
    в которых модель уверена полностью (sure = true), и только в боевом режиме.
    """
    for name in names:
        KeywordProduct.objects.create(keyword=name)
    return names


def _names(items: list[dict] | list[str]) -> str:
    parts = [
        (item.get('name') if isinstance(item, dict) else item) or ''
        for item in items
    ]
    return ', '.join(f'«{part.strip()}»' for part in parts if part.strip()) or '—'


def review_receipt(receipt) -> AIReview:
    """
    Спрашивает нейросеть по чеку и возвращает решение.

    Сам чек не сохраняет и статус не меняет — это делает вызывающий код
    (receipt_fns._update_receipt_from_fns).
    """
    if not is_enabled():
        # Нейросеть выключена — чек разбирается как до её подключения, а системное
        # сообщение не должно намекать на сбой: сбоя нет.
        return AIReview(
            accepted=False,
            system_message='Ключевых слов недостаточно — требуется ручная модерация.',
        )

    items = _normalize_receipt_items(receipt.items)
    if not items:
        return AIReview(
            accepted=False,
            system_message='В чеке нет позиций — требуется ручная модерация.',
        )

    answer = ask_receipt_ai(items)

    if answer is None:
        logger.warning('ai_review: receipt_id=%s — ответа от нейросети нет', receipt.pk)
        _remember(receipt, RECOMMENDATION_NO_ANSWER,
                  'Нейросеть не ответила: сбой запроса к OpenRouter. Подробности в логе ошибок.')
        return AIReview(
            accepted=False,
            system_message='Нейросеть не ответила — требуется ручная модерация.',
            recommendation=RECOMMENDATION_NO_ANSWER,
        )

    verdict = answer.get('verdict')
    comment = (answer.get('comment') or '').strip()
    sure, unsure = _sure_items(items, answer)
    promo_sum = promo_items_total_kopecks(sure)
    suggested_keywords = _new_keyword_candidates(sure)

    would_accept = verdict == 'match' and bool(sure) and promo_sum >= promo_min_sum_kopecks()
    if would_accept:
        recommendation = RECOMMENDATION_ACCEPT
    elif verdict == 'reject':
        recommendation = RECOMMENDATION_REJECT
    else:
        recommendation = RECOMMENDATION_REVIEW

    note = _build_note(receipt, items, answer, sure, unsure, promo_sum, recommendation,
                       suggested_keywords, comment, shadow=settings.OPENROUTER_SHADOW_MODE)
    _remember(receipt, recommendation, note)

    if settings.OPENROUTER_SHADOW_MODE:
        # Теневой режим: модель отработала, но на чек не влияет ничем — ни статусом,
        # ни системным сообщением, ни ключевыми словами. Остаётся только разбор
        # в полях ai_* — по ним и оценивается её работа перед боевым включением.
        return AIReview(
            accepted=False,
            system_message='Ключевых слов недостаточно — требуется ручная модерация.',
            verdict=verdict,
            promo_sum_kopecks=promo_sum,
            recommendation=recommendation,
            suggested_keywords=suggested_keywords,
        )

    # Ключевые слова пополняем при любом вердикте, но только по уверенным позициям.
    added_keywords = _add_keywords(suggested_keywords)

    logger.info(
        'ai_review: receipt_id=%s verdict=%s sure=%s unsure=%s sum=%s added_keywords=%s usage=%s',
        receipt.pk, verdict, len(sure), len(unsure), promo_sum, len(added_keywords),
        answer.get('_usage'),
    )

    accepted = would_accept

    if accepted:
        system_message = (
            f'Чек принят нейросетью. Акционные товары: {_names(sure)} '
            f'на сумму {rubles(promo_sum)} ₽.'
        )
    elif verdict == 'match':
        # Товары нашлись, но их сумма не дотягивает до порога — отклонять чек по итогам
        # проверки нейросетью нельзя, поэтому отправляем к модератору.
        system_message = (
            f'Нейросеть нашла акционные товары {_names(sure)} на сумму {rubles(promo_sum)} ₽ — '
            f'меньше {rubles(promo_min_sum_kopecks())} ₽. Требуется ручная модерация.'
        )
    elif verdict == 'reject':
        system_message = 'Нейросеть предлагает отклонить чек: акционные товары не найдены.'
        if comment:
            system_message = f'Нейросеть предлагает отклонить чек: {comment}'
        system_message += ' Чек оставлен на ручную модерацию.'
    else:
        system_message = 'У нейросети есть сомнения'
        if comment:
            system_message += f': {comment}'
        if unsure:
            system_message += f' Похожие позиции: {_names(unsure)}.'
        system_message += ' Требуется ручная модерация.'

    if added_keywords:
        system_message += f' Добавлены ключевые слова: {_names(added_keywords)}.'

    return AIReview(
        accepted=accepted,
        system_message=system_message,
        promo_items=sure,
        promo_sum_kopecks=promo_sum,
        verdict=verdict,
        added_keywords=added_keywords,
        recommendation=recommendation,
        suggested_keywords=suggested_keywords,
    )


def _build_note(receipt, items, answer, sure, unsure, promo_sum,
                recommendation, suggested_keywords, comment, shadow: bool) -> str:
    """
    Человекочитаемый разбор для поля «Разбор чека нейросетью».

    Пишется так, чтобы модератор понял решение, не открывая ни логов, ни чека:
    что признано акционным, на какую сумму, чего не хватило и что модель
    предлагает добавить в ключевые слова.
    """
    def money(item):
        value = item.get('sum')
        if value is None:
            value = (item.get('price') or 0) * (item.get('quantity') or 0)
        try:
            return rubles(round(float(value)))
        except (TypeError, ValueError):
            return '?'

    label = dict(Receipt.AIRecommendation.choices).get(recommendation, recommendation)
    lines = [
        f'Рекомендация: {label}.',
        f'Позиций в чеке: {len(items)}, сумма чека: {rubles(receipt_total(receipt))} ₽.',
        f'Вердикт модели: {answer.get("verdict")}; акционных товаров на {rubles(promo_sum)} ₽ '
        f'(порог {rubles(promo_min_sum_kopecks())} ₽).',
    ]
    if sure:
        lines.append('Уверенно акционные:')
        lines += [f'  • {money(i)} ₽ — {(i.get("name") or "").strip()}' for i in sure]
    if unsure:
        lines.append('Под вопросом:')
        lines += [f'  • {money(i)} ₽ — {(i.get("name") or "").strip()}' for i in unsure]
    if suggested_keywords:
        prefix = 'Предлагает добавить в ключевые слова'
        lines.append(f'{prefix} (теневой режим — НЕ добавлены):' if shadow else f'{prefix}:')
        lines += [f'  • {name}' for name in suggested_keywords]
    else:
        lines.append('Новых ключевых слов не предлагает.')
    if comment:
        lines.append(f'Комментарий модели: {comment}')
    usage = answer.get('_usage') or {}
    if usage:
        lines.append(
            f'Токены: вход {usage.get("prompt_tokens")}, выход {usage.get("completion_tokens")}, '
            f'стоимость ${usage.get("cost", 0):.6f}. Модель: {settings.OPENROUTER_MODEL}.'
        )
    if shadow:
        lines.append('Теневой режим: на решение по чеку этот разбор не повлиял.')

    return '\n'.join(lines)


def _remember(receipt, recommendation: str, note: str) -> None:
    """
    Кладёт разбор в поля чека. Не сохраняет — сохранит вызывающий код вместе с
    остальными полями (см. fields_to_update в receipt_fns).
    """
    receipt.ai_recommendation = recommendation
    receipt.ai_review_note = note
    receipt.ai_reviewed_at = timezone.now()
