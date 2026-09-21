import json
import logging

import requests
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from ..models import Receipt, PromotionDrawResultMainRaffle

logger = logging.getLogger(__name__)

OKI_API_BASE = 'https://api.doki.online'
OKI_CONTRACT_URL = f'{OKI_API_BASE}/external/contract'
OKI_CONTRACTS_LIST_URL = f'{OKI_API_BASE}/external/contracts'
OKI_CONTRACT_CANCEL_URL = f'{OKI_API_BASE}/external/contract/{{contract_id}}/cancel'

TEMPLATE_SMALL_PRIZE = '69897f66bb27c395d9bb0bae'
TEMPLATE_LARGE_PRIZE = '69786d7ade2e38aef528a40b'


def _api_key() -> str:
    return settings.OKI_DOKI_API_KEY


def _callback_url() -> str:
    """Адрес, на который OkiDoki шлёт смену статуса договора.

    Значение вшивается в каждый договор при создании, поэтому ошибка здесь не
    видна сразу — она проявляется только тем, что статусы перестают обновляться.
    Фолбэк собирается из SITE_URL этого же проекта, чтобы даже при отсутствии
    настройки callback не ушёл на чужой домен (именно так и случилось до 09.2026,
    когда здесь был захардкожен домен другого проекта).
    """
    configured = (getattr(settings, 'OKI_DOKI_CALLBACK_URL', '') or '').strip()
    if configured:
        return configured
    return f"{settings.SITE_URL.rstrip('/')}/oki-doki/callback/"


def _template_catalog() -> dict:
    return getattr(settings, 'OKI_DOKI_TEMPLATES', {
        TEMPLATE_SMALL_PRIZE: {
            'label': 'Приз до 4000 ₽ (ежемесячный)',
            'url': f'https://doki.online/templates/{TEMPLATE_SMALL_PRIZE}',
        },
        TEMPLATE_LARGE_PRIZE: {
            'label': 'Приз свыше 4000 ₽ (с НДФЛ)',
            'url': f'https://doki.online/templates/{TEMPLATE_LARGE_PRIZE}',
        },
    })


def _prize_cost(prize) -> float:
    if not prize or prize.cost is None:
        return 0
    try:
        return float(prize.cost)
    except (TypeError, ValueError):
        return 0


def resolve_oki_template(prize) -> dict:
    """Больше не используется. Какой шаблон OkiDoki будет использован для приза."""
    template_id = TEMPLATE_SMALL_PRIZE if _prize_cost(prize) <= 4000 else TEMPLATE_LARGE_PRIZE
    catalog = _template_catalog()
    meta = catalog.get(template_id, {})
    return {
        'template_id': template_id,
        'template_label': meta.get('label', template_id),
        'template_url': meta.get('url', ''),
    }


def _split_kopecks(amount) -> tuple[str, str]:
    """Return (rubles_str, kopecks_label) for an amount like 1500.50."""
    if not amount:
        return '', '00 копеек'
    from decimal import Decimal, ROUND_DOWN
    d = Decimal(str(amount))
    rubles = int(d.to_integral_value(rounding=ROUND_DOWN))
    kopecks = int((d - rubles) * 100)
    kopecks_label = f'{kopecks:02d} копеек' if kopecks else '00 копеек'
    return str(rubles), kopecks_label


def _build_entities(prize) -> list[dict]:
    prize_name = prize.name if prize else ''
    prize_price = prize.cost if prize else ''
    ndfl_amount = prize.ndfl if prize else ''

    rubles, kopecks_label = _split_kopecks(prize_price)
    ndfl_rubles, _ = _split_kopecks(ndfl_amount)

    if prize_price and prize_price <= 4000:
        return [
            {'keyword': 'Наименование приза', 'value': prize_name},
            {'keyword': 'Стоимость приза', 'value': rubles},
            {'keyword': 'копейки', 'value': kopecks_label},
        ]

    return [
        {'keyword': 'Наименование приза', 'value': prize_name},
        {'keyword': 'Стоимость приза', 'value': rubles},
        {'keyword': 'копейки', 'value': kopecks_label},
        {'keyword': 'НДФЛ', 'value': ndfl_rubles},
    ]


def build_contract_payload(winner, *, oki_template) -> dict:
    """
    oki_template — экземпляр OkiDokiTemplate (поля: pk, name, url).
    """
    prize = winner.prize
    participant = winner.participant
    payload = {
        'api_key': _api_key(),
        'external_id': '',
        'template_id': oki_template.oki_template_id,
        'system_entities': [
            {'keyword': 'client_first_name', 'value': participant.first_name or ''},
            {'keyword': 'client_last_name', 'value': participant.last_name or ''},
            {'keyword': 'client_phone_number', 'value': participant.phone or ''},
        ],
        'entities': _build_entities(prize),
        'callback_url': _callback_url(),
    }
    print(payload)
    if prize.is_main:
        payload['external_id'] = str(winner.public_id)
    else:
        payload['external_id'] = str(winner.receipt.public_id)
    return payload



def preview_oki_contract(winner) -> dict:
    """Превью договора для админки — без вызова API."""
    prize = winner.prize
    participant = winner.participant
    template = resolve_oki_template(prize)
    payload = build_contract_payload(winner)

    return {
        'template': template,
        'external_id': payload['external_id'],
        'participant': {
            'name': participant.get_full_name() if hasattr(participant, 'get_full_name') else '',
            'email': participant.email or '',
            'phone': participant.phone or '',
        },
        'prize': {
            'name': prize.name if prize else '',
            'cost': prize.cost,
            'ndfl': prize.ndfl,
        },
        'system_entities': payload['system_entities'],
        'entities': payload['entities'],
        'already_issued': bool(
            getattr(winner, 'link_oki_document', None)
            or getattr(winner, 'link_oki_document_admin', None)
        ),
        'status_oki_document': getattr(winner, 'status_oki_document', None) or '',
    }


def send_link_oki_doki(winner, *, oki_template):
    """Создаёт договор в OkiDoki."""
    if getattr(settings, 'OKIDOKI_DISABLED', False):
        logger.info('OKIDOKI_DISABLED: договор не создан (локальная среда)')
        return {'contract_link': '', 'status_oki_document': 'Отключено (локальная среда)', 'for_admin': True}

    try:
        contract_data = build_contract_payload(winner, oki_template=oki_template)
        response = requests.post(
            OKI_CONTRACT_URL,
            json=contract_data,
            timeout=60,
        )

        if response.status_code == 201:
            data = response.json()
            status = data.get('status') or {}
            internal_id = status.get('internal_id')
            if internal_id == 0:
                return {
                    'contract_link': data.get('link'),
                    'status_oki_document': 'Черновик',
                    'for_admin': True,
                }
            if internal_id == 1:
                return {
                    'contract_link': data.get('link'),
                    'status_oki_document': 'Выставлен',
                    'for_admin': False,
                }
            return {
                'contract_link': data.get('link'),
                'status_oki_document': status.get('name') or '',
                'for_admin': False,
            }

        logger.error('OkiDoki contract failed status=%s body=%s', response.status_code, response.text[:500])
        return {
            'contract_link': '',
            'status_oki_document': 'Ошибка на стороне сервиса',
            'for_admin': True,
            '_api_error': response.text[:500],
        }
    except Exception as exc:
        logger.exception('OkiDoki contract exception')
        return {
            'contract_link': '',
            'status_oki_document': 'Ошибка на стороне сервиса',
            'for_admin': True,
            '_exception': exc,
        }


NDFL_THRESHOLD = 4000


def check_prize_ndfl_consistency(prize) -> str | None:
    """
    Сверяет стоимость приза и заполненность НДФЛ (порог совпадает с тем, что
    решает, какой шаблон OkiDoki используется — см. _build_entities выше).
    Возвращает текст проблемы, если данные не согласованы, иначе None.
    """
    if not prize or prize.cost is None:
        return None

    try:
        cost = float(prize.cost)
    except (TypeError, ValueError):
        return None
    ndfl = float(prize.ndfl) if prize.ndfl else 0.0

    if cost < NDFL_THRESHOLD and ndfl:
        return (
            f'Стоимость приза «{prize.name}» — {cost:.0f} ₽ (менее {NDFL_THRESHOLD} ₽), '
            f'но в договор передан ненулевой НДФЛ: {ndfl:.0f} ₽.'
        )
    if cost > NDFL_THRESHOLD and not ndfl:
        return (
            f'Стоимость приза «{prize.name}» — {cost:.0f} ₽ (более {NDFL_THRESHOLD} ₽), '
            f'но НДФЛ не заполнен (пусто или 0).'
        )
    return None


def check_email_send_blocked(*, prize, participant, status_oki_document) -> str | None:
    """
    Причина, по которой нельзя отправлять победителю письмо с договором OkiDoki,
    либо None, если можно. Используется и для проверки при самой отправке
    (send_winner_email_for_receipt/for_main), и для отрисовки кнопки в дашборде
    (staff_panel/services/winner_fulfillment.py) — не дублируем условия в двух
    местах.
    """
    missing = []
    if not participant or not (participant.first_name or '').strip():
        missing.append('имя участника')
    if not participant or not (participant.last_name or '').strip():
        missing.append('фамилия участника')
    if not participant or not (participant.phone or '').strip():
        missing.append('телефон участника')
    if not prize or not (prize.name or '').strip():
        missing.append('название приза')
    if not prize or prize.cost is None:
        missing.append('стоимость приза')
    if missing:
        return 'В OkiDoki не переданы обязательные поля: ' + ', '.join(missing) + '.'

    ndfl_issue = check_prize_ndfl_consistency(prize)
    if ndfl_issue:
        return ndfl_issue

    status = (status_oki_document or '').strip()
    if status != 'Выставлен':
        return f'Договор не в статусе «Выставлен» (текущий статус: {status or "договор ещё не создан"}).'

    return None


def fetch_contract_state(external_id) -> dict:
    """Актуальное состояние договора в OkiDoki по нашему external_id.

    Возвращает один из трёх вариантов:
      {'ok': True, 'found': True, 'status': 'Подписан', 'internal_id': 2, 'link': ..., 'signed_at': ...}
      {'ok': True, 'found': False}   — договора с таким external_id в OkiDoki нет
      {'ok': False, 'error': '...'}  — API недоступен или ответил ошибкой

    «Договора нет» и «не смогли спросить» намеренно разведены: при ошибке сверка
    (oki_status_sync) не должна делать никаких выводов о состоянии договора.
    """
    try:
        response = requests.get(
            OKI_CONTRACTS_LIST_URL,
            params={
                'api_key': _api_key(),
                'external_id': external_id,
            },
            timeout=60,
        )
    except Exception as exc:
        logger.exception('OkiDoki get contracts failed external_id=%s', external_id)
        return {'ok': False, 'error': str(exc)}

    # На этот GET OkiDoki отвечает кодом 201, а не 200 — 200 принимаем на случай,
    # если в API это когда-нибудь поправят.
    if response.status_code not in (200, 201):
        logger.error(
            'OkiDoki get contracts failed external_id=%s status=%s body=%s',
            external_id, response.status_code, response.text[:500],
        )
        return {'ok': False, 'error': f'HTTP {response.status_code}'}

    try:
        contracts = response.json().get('contracts') or []
    except ValueError:
        logger.error('OkiDoki get contracts: некорректный JSON external_id=%s', external_id)
        return {'ok': False, 'error': 'некорректный JSON в ответе'}

    if not contracts:
        return {'ok': True, 'found': False}

    contract = next((c for c in contracts if c.get('link')), contracts[0])
    status = contract.get('status') or {}
    return {
        'ok': True,
        'found': True,
        'status': (status.get('name') or '').strip(),
        'internal_id': status.get('internal_id'),
        'link': (contract.get('link') or '').strip(),
        'signed_at': contract.get('signed_at') or '',
    }


def get_contracts_by_external_id(external_id):
    """Ссылка на договор победителя — пустая строка, если её не удалось получить."""
    state = fetch_contract_state(external_id)
    if not state.get('ok') or not state.get('found'):
        return ''
    return state.get('link') or ''


def _extract_contract_id(link: str) -> str:
    """ID договора — последний сегмент ссылки на него (например
    'https://desktop.doki.online/contract/6a2a29df57cee39070c35e5b' → '6a2a29df57cee39070c35e5b').
    Отдельного поля с ID у нас не хранится — ссылка на договор есть всегда."""
    link = (link or '').strip().rstrip('/')
    if not link:
        return ''
    return link.rsplit('/', 1)[-1]


def cancel_oki_contract(record, *, reason: str = '') -> dict:
    """
    Отменяет/расторгает договор прежнего победителя в OkiDoki при замене
    победителя (см. staff_panel.services.winner_replacement.replace_winner).

    Поведение на стороне OkiDoki зависит от статуса договора: черновик — мягко
    удаляется, ожидающий подписи — отменяется, подписанный — расторгается.
    Если контракта не было (ссылок нет) — ничего не делаем. Ошибку самой замены
    победителя не блокируем: если OkiDoki недоступен, локальные поля договора
    всё равно очищаются (см. вызывающий код), а проблему видно в логах.
    """
    link = (getattr(record, 'link_oki_document', None) or getattr(record, 'link_oki_document_admin', None) or '').strip()
    contract_id = _extract_contract_id(link)
    if not contract_id:
        return {'ok': True, 'skipped': True}

    if getattr(settings, 'OKIDOKI_DISABLED', False):
        logger.info('OKIDOKI_DISABLED: договор не отменён (локальная среда) contract_id=%s', contract_id)
        return {'ok': True, 'skipped': True}

    try:
        response = requests.put(
            OKI_CONTRACT_CANCEL_URL.format(contract_id=contract_id),
            json={
                'api_key': _api_key(),
                'cancellation_text': (reason or '').strip() or 'Победитель заменён на другого участника акции',
                'should_send_agreement': False,
            },
            timeout=60,
        )

        if response.status_code == 200:
            data = response.json()
            return {
                'ok': True,
                'action': data.get('action'),
                'status': (data.get('status') or {}).get('name'),
            }
        if response.status_code == 400:
            # Договор уже отменён/расторгнут — для нас это не ошибка: он всё равно недействителен.
            logger.info('OkiDoki contract already cancelled/terminated contract_id=%s', contract_id)
            return {'ok': True, 'already_cancelled': True}

        logger.error(
            'OkiDoki cancel failed contract_id=%s status=%s body=%s',
            contract_id, response.status_code, response.text[:500],
        )
        return {'ok': False, 'error': response.text[:500]}
    except Exception as exc:
        logger.exception('OkiDoki cancel exception contract_id=%s', contract_id)
        return {'ok': False, 'error': str(exc)}


@csrf_exempt
def callback_oki_doki(request):
    try:
        data = json.loads(request.body)

        external_id = data.get('external_id')

        winner = Receipt.objects.filter(public_id=external_id, status=Receipt.Status.WINNER).first()

        if winner:
            if external_id and data.get('status').get('internal_id') == 1 and winner.status_oki_document == 'Черновик':
                link_oki_document = get_contracts_by_external_id(external_id)

                winner.link_oki_document = link_oki_document
                winner.link_oki_document_admin = ''

            winner.status_oki_document = data.get('status').get('name')
            winner.save()
        else:
            winner = PromotionDrawResultMainRaffle.objects.filter(public_id=external_id).first()

            if winner:
                if external_id and data.get('status').get('internal_id') == 1 and winner.status_oki_document == 'Черновик':
                    link_oki_document = get_contracts_by_external_id(external_id)

                    winner.link_oki_document = link_oki_document
                    winner.link_oki_document_admin = ''

                winner.status_oki_document = data.get('status').get('name')
                winner.save()

        return JsonResponse({'success': True})
    except Exception:
        logger.exception('OkiDoki callback failed')
        return JsonResponse({'success': False})
