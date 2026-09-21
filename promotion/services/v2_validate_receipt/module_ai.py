"""
Запрос к нейросети (OpenRouter) для проверки чека по перечню акционных товаров.

Модуль отвечает только за разговор с моделью: собрать промпт из перечня Товаров
(Дополнение № 1 к Правилам — модель main.ActionProducts) и позиций чека, получить
разобранный JSON. Решение о статусе чека принимает ai_review.review_receipt —
здесь оно намеренно не принимается.

Настройки — в settings.OPENROUTER_* (ключ, модель, таймаут).
"""
import json
import logging
import time

import httpx
from django.conf import settings
from json_repair import repair_json

logger = logging.getLogger(__name__)

OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
RETRY_DELAY_SECONDS = 1.5

SYSTEM_PROMPT = """
Ты — помощник модератора акции «Купи и выиграй с Атемарской». Ты НЕ принимаешь и НЕ отклоняешь
чеки: ты только сопоставляешь позиции кассового чека с перечнем акционных Товаров.
Окончательное решение принимает программа, а спорные чеки уходят к живому модератору.

## Что известно
- В Акции участвуют только Товары торговой марки «Атемарская Ферма» из перечня, который
  тебе передадут (Дополнение № 1 к Правилам Акции).
- В чеке кроме акционных Товаров обычно есть посторонние товары (молоко, хлеб, пакет
  и т. п.) — их нужно игнорировать.
- Наименования в чеке почти всегда сокращены и искажены: «Колб.вар.Докторская ГОСТ 350г»,
  «СОС.МОЛОЧНЫЕ ОБ 380», «Фарш Сибирский с/м 700». Это нормально.
- Идентификация Товара — по совокупности признаков: вид продукции, наименование и вес.

## Твоя задача
Для каждой позиции чека решить, соответствует ли она какому-либо Товару из перечня,
и насколько ты в этом уверен.

Поле "sure" — это полная уверенность, а не «похоже»:
- sure = true, если наименование однозначно соответствует ОДНОМУ Товару из перечня:
  совпадает вид продукции и собственное наименование (а если вес указан — и вес),
  и нет признаков другого производителя. Явное указание «Атемарская» / «Атемарская
  Ферма» / «АФ» в названии позиции — дополнительный довод за.
- sure = false, если название общее и без признаков марки, так что такой товар мог бы
  выпустить любой производитель («Сосиски молочные», «Фарш домашний», «Котлеты 400г»),
  либо позиция похожа сразу на несколько Товаров, либо не совпадает вес, либо ты
  сомневаешься по любой другой причине.
При малейшем сомнении ставь sure = false. Лучше отправить чек на ручную проверку,
чем ошибочно засчитать посторонний товар.

## Итоговый вердикт
- "match" — в чеке есть позиции с sure = true, и ты уверен, что чек акционный.
- "doubt" — есть похожие позиции, но полной уверенности нет; пусть посмотрит модератор.
- "reject" — акционных Товаров в чеке не видно совсем.

## Формат ответа
Только JSON, без markdown и пояснений вокруг:
{
  "verdict": "match" | "doubt" | "reject",
  "comment": "одно-два предложения по-русски: что нашёл и в чём сомневаешься",
  "promo_items": [
    {"index": <номер позиции чека из списка>, "product": "<наименование из перечня>", "sure": true|false}
  ]
}
В promo_items перечисляй только позиции, похожие на Товары Акции. Посторонние товары
не включай. Поле index бери ровно из переданного списка позиций.
comment пиши для живого модератора: коротко и по делу, без воды.
"""

RESPONSE_FORMAT = {
    'type': 'json_schema',
    'json_schema': {
        'name': 'receipt_check',
        'strict': True,
        'schema': {
            'type': 'object',
            'properties': {
                'verdict': {'type': 'string', 'enum': ['match', 'doubt', 'reject']},
                'comment': {'type': 'string'},
                'promo_items': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'index': {'type': 'integer'},
                            'product': {'type': 'string'},
                            'sure': {'type': 'boolean'},
                        },
                        'required': ['index', 'product', 'sure'],
                        'additionalProperties': False,
                    },
                },
            },
            'required': ['verdict', 'comment', 'promo_items'],
            'additionalProperties': False,
        },
    },
}


def is_enabled() -> bool:
    """
    Пустой OPENROUTER_API_KEY — штатный выключатель проверки нейросетью, а не сбой.
    Чеки в этом случае разбираются как до её подключения: ключевые слова и модератор.
    """
    return bool(settings.OPENROUTER_API_KEY)


def load_action_products() -> list[str]:
    """Перечень акционных Товаров (Дополнение № 1) — из админки, main.ActionProducts."""
    from main.models import ActionProducts

    return [
        name.strip()
        for name in ActionProducts.objects.values_list('name', flat=True)
        if name and name.strip()
    ]


def _format_price(item: dict) -> str:
    """Сумма позиции в рублях. ФНС отдаёт копейки."""
    value = item.get('sum')
    if value is None:
        try:
            value = (item.get('price') or 0) * (item.get('quantity') or 0)
        except TypeError:
            value = 0
    try:
        return f'{round(float(value)) / 100:.2f} руб.'
    except (TypeError, ValueError):
        return 'сумма не указана'


def build_user_prompt(items: list[dict], products: list[str]) -> str:
    products_block = '\n'.join(f'- {name}' for name in products)
    items_block = '\n'.join(
        f'[{i}] {(item.get("name") or "").strip()} — {_format_price(item)}'
        for i, item in enumerate(items)
    )
    return (
        'Перечень Товаров Акции (торговая марка «Атемарская Ферма»):\n'
        f'{products_block}\n\n'
        'Позиции кассового чека:\n'
        f'{items_block}\n\n'
        'Верни JSON по заданной схеме.'
    )


def _post(payload: dict) -> httpx.Response:
    with httpx.Client() as client:
        return client.post(
            OPENROUTER_URL,
            headers={
                'Authorization': f'Bearer {settings.OPENROUTER_API_KEY}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=settings.OPENROUTER_TIMEOUT,
        )


def _ask_once(payload: dict) -> tuple[dict | None, bool]:
    """
    Одна попытка спросить модель.

    Возвращает (разобранный ответ, стоит ли повторить). Повторять имеет смысл на
    разовых сбоях: сеть, 5xx, пустой или нечитаемый ответ. На 402 (кончились
    кредиты), 429 (лимит) и ошибках запроса повтор бессмысленен.
    """
    try:
        resp = _post(payload)
    except Exception:
        logger.exception('ask_receipt_ai: запрос к OpenRouter не удался (model=%s)', settings.OPENROUTER_MODEL)
        return None, True

    if resp.status_code == 400 and 'response_format' in resp.text and 'response_format' in payload:
        # Не все модели умеют строгую JSON-схему (в частности, часть бесплатных).
        # Тогда просим тот же JSON словами — формат описан в системном промпте.
        logger.info(
            'ask_receipt_ai: модель %s не поддерживает json_schema, повторяем без неё',
            settings.OPENROUTER_MODEL,
        )
        payload.pop('response_format')
        return _ask_once(payload)

    if resp.status_code != 200:
        # 402 (кончились кредиты) и 429 (упёрлись в лимит модели) — ожидаемые состояния.
        # Это warning, иначе каждый чек после исчерпания лимита слал бы сообщение
        # в Telegram-бот (см. LOGGING в settings: ERROR уходит туда).
        expected = resp.status_code in (402, 429)
        level = logger.warning if expected else logger.error
        level(
            'ask_receipt_ai: OpenRouter ответил %s (model=%s): %s',
            resp.status_code, settings.OPENROUTER_MODEL, resp.text[:500],
        )
        return None, resp.status_code >= 500

    try:
        body = resp.json()
        raw = (body['choices'][0]['message'].get('content') or '').strip()
    except Exception:
        logger.exception('ask_receipt_ai: неожиданный ответ OpenRouter: %s', resp.text[:500])
        return None, True

    if not raw:
        logger.warning('ask_receipt_ai: модель вернула пустой ответ (model=%s)', settings.OPENROUTER_MODEL)
        return None, True

    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
        raw = raw.strip()

    try:
        result = json.loads(repair_json(raw))
    except Exception:
        logger.warning('ask_receipt_ai: не разобрали JSON модели: %s', raw[:500])
        return None, True

    result['_usage'] = body.get('usage') or {}
    return result, False


def ask_receipt_ai(items: list[dict], products: list[str] | None = None) -> dict | None:
    """
    Спрашивает модель по позициям чека.

    Возвращает разобранный ответ модели либо None, если спросить не удалось
    (нет ключа, нет перечня товаров, ошибка сети, нечитаемый ответ). None — это
    «ответа нет», а не «не подходит»: отличать их важно, иначе сбой OpenRouter
    выглядел бы как решение модели.
    """
    if not is_enabled():
        # Именно info: выключенная нейросеть — это настройка, а не происшествие.
        # На ERROR каждый чек слал бы сообщение в Telegram-бот (см. LOGGING в settings).
        logger.info('ask_receipt_ai: OPENROUTER_API_KEY не задан — проверка нейросетью выключена')
        return None

    if not items:
        return None

    if products is None:
        products = load_action_products()
    if not products:
        logger.error('ask_receipt_ai: перечень акционных товаров пуст (main.ActionProducts) — нечего сопоставлять')
        return None

    payload = {
        'model': settings.OPENROUTER_MODEL,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': build_user_prompt(items, products)},
        ],
        'temperature': 0,
        'max_tokens': 1500,
        # Без этого Qwen тратит весь лимит токенов на размышления и возвращает
        # пустой content — проверено на qwen3.7-flash и qwen3.8-flash.
        'reasoning': {'enabled': False},
        'response_format': RESPONSE_FORMAT,
    }

    # Разовые сбои у OpenRouter случаются: на прогоне 30 боевых чеков два ответа
    # потерялись, а на повторе оба пришли нормально. Одна повторная попытка.
    result = None
    for attempt in (1, 2):
        result, retryable = _ask_once(payload)
        if result is not None or not retryable:
            break
        logger.warning(
            'ask_receipt_ai: попытка %s не удалась (model=%s), пробуем ещё раз',
            attempt, settings.OPENROUTER_MODEL,
        )
        time.sleep(RETRY_DELAY_SECONDS)

    if result is None:
        return None

    if not isinstance(result, dict):
        logger.error('ask_receipt_ai: модель вернула не объект: %r', result)
        return None

    return result
