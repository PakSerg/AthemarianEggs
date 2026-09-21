"""Клиент API Точка Банк Cyclops («Расчёты по номинальному счёту»).

Перенесён из референс-проекта Cyclops (cyclop_manager/services/cyclops_api.py)
и адаптирован под OmskBacon. Используется для автоматической выплаты
гарантированного приза через СБП. См. docs/cyclops-integration-plan.md

Транспорт: JSON-RPC 2.0, POST {BASE_URL}/v2/jsonrpc.
Аутентификация: заголовки sign-system / sign-thumbprint / sign-data,
где sign-data — подпись RSA PKCS#1 v1.5 + SHA-256 от компактного тела запроса
(base64). На pre-слое допускается значение '12345' (USE_TEST_KEY=True).
"""

import base64
import json
import logging
import mimetypes
import re
import uuid
from datetime import datetime

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from django.conf import settings

logger = logging.getLogger(__name__)

# Точка принимает в назначении платежа только символы из этого набора
# (ASCII-печатные, №, русские буквы и пробельные). Всё остальное —
# в первую очередь типографика: длинное тире, кавычки-ёлочки, знак рубля,
# многоточие — отклоняется с ошибкой 4002 «No valid data in request».
PURPOSE_ALLOWED_RE = re.compile(r'^[ -~№А-яёЁ\t\n\r]*$')

_PURPOSE_REPLACEMENTS = {
    '—': '-',   # — длинное тире
    '–': '-',   # – среднее тире
    '‒': '-',
    '−': '-',   # − минус
    '«': '"',   # «
    '»': '"',   # »
    '“': '"',
    '”': '"',
    '„': '"',
    '‘': "'",
    '’': "'",
    '…': '...',  # …
    '₽': 'руб.',  # ₽
    ' ': ' ',   # неразрывный пробел
}


def sanitize_purpose(text):
    """Привести назначение платежа к набору символов, который принимает Точка.

    Типографские символы заменяются на допустимые аналоги, всё остальное
    недопустимое — на пробел. Без этого безобидное «—» или «₽» в тексте
    роняет create_deal / refund_virtual_account с ошибкой 4002.
    """
    if not text:
        return text
    for src, dst in _PURPOSE_REPLACEMENTS.items():
        text = text.replace(src, dst)
    if not PURPOSE_ALLOWED_RE.match(text):
        cleaned = ''.join(ch if PURPOSE_ALLOWED_RE.match(ch) else ' ' for ch in text)
        logger.warning('Назначение платежа содержало недопустимые символы, очищено: %r -> %r',
                       text, cleaned)
        text = cleaned
    return text


class CyclopsAPIClient:
    """Клиент для работы с API Cyclops."""

    def __init__(self):
        cfg = settings.CYCLOPS_CONFIG
        self.base_url = cfg['BASE_URL']
        self.platform_id = cfg['PLATFORM_ID']
        self.cert_thumbprint = cfg['CERT_THUMBPRINT']
        self.private_key_path = cfg['PRIVATE_KEY_PATH']
        self.use_test_key = cfg.get('USE_TEST_KEY', True)
        self.nominal_account = cfg.get('NOMINAL_ACCOUNT', '')
        self.session = requests.Session()
        self.session.timeout = 60

    # ------------------------------------------------------------------ #
    # Подпись и низкоуровневый транспорт
    # ------------------------------------------------------------------ #
    def _load_private_key(self):
        """Загрузка приватного ключа."""
        try:
            with open(self.private_key_path, 'rb') as key_file:
                return serialization.load_pem_private_key(
                    key_file.read(),
                    password=None,
                )
        except Exception as e:
            logger.error("Ошибка загрузки приватного ключа: %s", e)
            raise

    def _sign_bytes(self, data: bytes) -> str:
        """Подпись байтов (RSA PKCS#1 v1.5 + SHA-256, base64)."""
        try:
            private_key = self._load_private_key()
            signature = private_key.sign(
                data,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return base64.b64encode(signature).decode('utf-8')
        except Exception as e:
            logger.error("Ошибка подписи: %s", e)
            raise

    def _sign_header(self, data: bytes) -> str:
        """Значение заголовка sign-data (или '12345' на pre-слое)."""
        if self.use_test_key:
            return '12345'
        return self._sign_bytes(data)

    def _make_jsonrpc_request(self, method: str, params: dict, version: str = 'v2') -> dict:
        """Отправка JSON-RPC запроса.

        Тело сериализуется компактно (без пробелов и переносов) — переносы
        строк в подписываемом теле приводят к ошибке 403.

        `version` выбирает эндпоинт: методы работы с бенефициарами-физлицами
        и их документами (create_beneficiary, *_beneficiary_documents_data)
        живут на /v3/jsonrpc, всё остальное — на /v2/jsonrpc.
        """
        data = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }
        data_str = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        body = data_str.encode('utf-8')

        request_url = f"{self.base_url.rstrip('/')}/{version}/jsonrpc"
        headers = {
            'sign-system': self.platform_id,
            'sign-thumbprint': self.cert_thumbprint,
            'sign-data': self._sign_header(body),
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

        try:
            response = self.session.post(
                request_url,
                headers=headers,
                data=body,
                timeout=60,
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error("Ошибка запроса к Cyclops (%s): %s", method, e)
            raise

    def _upload_document_request(self, endpoint: str, file_path: str, params: dict) -> dict:
        """Загрузка документа (бинарный PDF, отдельный эндпоинт, не JSON-RPC)."""
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            raise ValueError(f"Не удалось определить Content-Type для файла {file_path}")

        with open(file_path, 'rb') as f:
            file_body = f.read()

        headers = {
            'sign-system': self.platform_id,
            'sign-thumbprint': self.cert_thumbprint,
            'sign-data': self._sign_header(file_body),
            'Content-Type': mime_type,
        }

        resp = requests.post(
            url=f"{self.base_url.rstrip('/')}/{endpoint}",
            headers=headers,
            data=file_body,
            params=params,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # Служебные
    # ------------------------------------------------------------------ #
    def echo(self, text: str = "test") -> dict:
        """Тестовый метод для проверки связи."""
        return self._make_jsonrpc_request('echo', {"text": text})

    # ------------------------------------------------------------------ #
    # Бенефициары (разовая настройка держателя призового фонда)
    # ------------------------------------------------------------------ #
    def create_beneficiary_ul(
        self,
        name: str,
        inn: str,
        kpp: str = None,
        nominal_account_code: str = None,
        nominal_account_bic: str = None,
    ) -> dict:
        """Создание бенефициара — юридического лица."""
        if not kpp:
            raise ValueError("Для юридического лица необходимо указать КПП")
        params = {
            'inn': inn,
            'nominal_account_code': nominal_account_code,
            'nominal_account_bic': nominal_account_bic,
            'beneficiary_data': {
                'name': name,
                'kpp': kpp,
            },
        }
        return self._make_jsonrpc_request('create_beneficiary_ul', params)

    def create_beneficiary_ip(self, first_name: str, last_name: str, inn: str, middle_name: str = None) -> dict:
        """Создание бенефициара — ИП/физлица."""
        params = {
            "inn": inn,
            "beneficiary_data": {
                "first_name": first_name,
                "middle_name": middle_name,
                "last_name": last_name,
            },
        }
        return self._make_jsonrpc_request('create_beneficiary_ip', params)

    # --- v3: бенефициары-физлица (ИП, самозанятые) и их документы --------- #
    # Точка требует создавать ИП/самозанятых именно этими методами (v3), а не
    # устаревшим create_beneficiary_ip. Документы (ИНН и ДУЛ) по таким
    # бенефициарам передаются отдельно — данными, а не файлом.
    def create_beneficiary(
        self,
        inn: str,
        registration_address: str,
        legal_type: str = 'I',
        tax_resident: bool = True,
        nominal_account_code: str = None,
        nominal_account_bic: str = None,
    ) -> dict:
        """Создание бенефициара-физлица: legal_type 'I' — ИП, 'F' — физлицо/самозанятый."""
        if legal_type not in ('I', 'F'):
            raise ValueError("legal_type должен быть 'I' (ИП) или 'F' (физлицо/самозанятый)")
        params = {
            'legal_type': legal_type,
            'inn': inn,
            'beneficiary_data': {
                'registration_address': registration_address,
                'tax_resident': tax_resident,
            },
        }
        if nominal_account_code and nominal_account_bic:
            params['nominal_account'] = {
                'code': nominal_account_code,
                'bic': nominal_account_bic,
            }
        return self._make_jsonrpc_request('create_beneficiary', params, version='v3')

    def add_beneficiary_documents_data(self, beneficiary_id: str, documents: list) -> dict:
        """Добавить/обновить данные документов бенефициара-физлица.

        `documents` — список словарей; ожидаются два типа:
        - {'type': 'internal_passport', 'series', 'number', 'first_name',
           'last_name', 'middle_name'(опц.), 'birth_date', 'issuer_code', 'issue_date'}
        - {'type': 'inn_f', 'inn', 'birth_place'}
        """
        params = {'beneficiary_id': beneficiary_id, 'documents': documents}
        return self._make_jsonrpc_request('add_beneficiary_documents_data', params, version='v3')

    def get_beneficiary_documents_data(self, beneficiary_id: str) -> dict:
        """Данные документов бенефициара и статус их проверки.

        В ответе: `valid_documents` — успешно проверенные, `last_documents` —
        из последнего запроса со статусом validation_process.status
        (NEW / PENDING / IN_PROGRESS / SUCCESS / ERROR).
        """
        return self._make_jsonrpc_request(
            'get_beneficiary_documents_data', {'beneficiary_id': beneficiary_id}, version='v3',
        )

    def get_beneficiary(self, beneficiary_id: str) -> dict:
        """Информация о бенефициаре."""
        return self._make_jsonrpc_request('get_beneficiary', {"beneficiary_id": beneficiary_id})

    def list_beneficiary(self) -> dict:
        """Список бенефициаров."""
        return self._make_jsonrpc_request('list_beneficiary', {}).get('result', {})

    def update_beneficiary_ul(
        self,
        beneficiary_id: str,
        name: str,
        kpp: str,
        ogrn: str = None,
    ) -> dict:
        """Обновление данных бенефициара — ЮЛ (наименование/КПП/ОГРН)."""
        beneficiary_data = {'name': name, 'kpp': kpp}
        if ogrn:
            beneficiary_data['ogrn'] = ogrn
        params = {'beneficiary_id': beneficiary_id, 'beneficiary_data': beneficiary_data}
        return self._make_jsonrpc_request('update_beneficiary_ul', params)

    def activate_beneficiary(self, beneficiary_id: str) -> dict:
        """Активация бенефициара. Площадка обязана поддерживать актуальный статус
        бенефициаров по договору с Точкой."""
        return self._make_jsonrpc_request('activate_beneficiary', {"beneficiary_id": beneficiary_id})

    def deactivate_beneficiary(self, beneficiary_id: str) -> dict:
        """Деактивация бенефициара."""
        return self._make_jsonrpc_request('deactivate_beneficiary', {"beneficiary_id": beneficiary_id})

    def upload_document_for_beneficiary(
        self,
        file_path: str,
        beneficiary_id: str,
        document_type: str = "contract_offer",
        document_date: str = None,
        document_number: str = "001",
    ) -> dict:
        """Загрузка договора оферты (contract_offer) для бенефициара — один раз."""
        if document_date is None:
            document_date = datetime.now().strftime("%Y-%m-%d")
        params = {
            'beneficiary_id': beneficiary_id,
            'document_type': document_type,
            'document_date': document_date,
            'document_number': document_number,
        }
        return self._upload_document_request('upload_document/beneficiary', file_path, params)

    # ------------------------------------------------------------------ #
    # Виртуальные счета
    # ------------------------------------------------------------------ #
    def create_virtual_account(self, beneficiary_id: str) -> dict:
        """Создание виртуального счёта для бенефициара."""
        return self._make_jsonrpc_request('create_virtual_account', {"beneficiary_id": beneficiary_id})

    def get_virtual_account(self, virtual_account_id: str) -> dict:
        """Информация по виртуальному счёту (баланс: result.virtual_account.cash)."""
        return self._make_jsonrpc_request(
            'get_virtual_account', {"virtual_account": virtual_account_id}
        ).get('result', {})

    def list_virtual_account(self, beneficiary_id: str = None) -> dict:
        """Список ID виртуальных счетов (опционально по фильтру бенефициара)."""
        params = {}
        if beneficiary_id:
            params = {"filters": {"beneficiary": {"id": beneficiary_id}}}
        return self._make_jsonrpc_request('list_virtual_account', params).get('result', {})

    def refund_virtual_account(
        self,
        virtual_account_id: str,
        amount: float,
        recipient_account: str,
        recipient_bank_code: str,
        recipient_name: str,
        recipient_inn: str,
        recipient_kpp: str = None,
        document_number: str = None,
        purpose: str = None,
    ) -> dict:
        """Вывод идентифицированных денег с виртуального счёта по реквизитам
        (например, возврат ошибочно зачисленных средств бенефициару)."""
        recipient = {
            'amount': round(amount, 2),
            'account': recipient_account,
            'bank_code': recipient_bank_code,
            'name': recipient_name,
            'inn': recipient_inn,
        }
        if recipient_kpp:
            recipient['kpp'] = recipient_kpp
        if document_number:
            recipient['document_number'] = document_number
        params = {'virtual_account': virtual_account_id, 'recipient': recipient}
        if purpose:
            params['purpose'] = sanitize_purpose(purpose)
        return self._make_jsonrpc_request('refund_virtual_account', params)

    # ------------------------------------------------------------------ #
    # Платежи
    # ------------------------------------------------------------------ #
    def list_payments(self, identify_status: bool = None) -> dict:
        """Список входящих платежей (опционально по признаку идентификации)."""
        params = {"filters": {"incoming": True}}
        if identify_status is not None:
            params["filters"]["identify"] = identify_status
        return self._make_jsonrpc_request('list_payments', params).get('result', {})

    def get_payment(self, payment_id: str) -> dict:
        """Информация по платежу."""
        return self._make_jsonrpc_request('get_payment', {"payment_id": payment_id}).get('result', {})

    def identification_payment(self, payment_id: str, virtual_account_id: str, amount) -> dict:
        """Идентификация входящего платежа на виртуальный счёт бенефициара."""
        params = {
            "payment_id": payment_id,
            "owners": [{
                "virtual_account": virtual_account_id,
                "amount": amount,
            }],
        }
        return self._make_jsonrpc_request('identification_payment', params)

    def identification_returned_payment_by_deal(self, deal_id: str) -> dict:
        """Идентификация платежа, вернувшегося отдельным входящим платежом по
        уже исполненной сделке (в назначении — «возврат»). Привязывает возврат
        к сделке, чтобы можно было исправить реквизиты и переотправить платёж
        в рамках той же сделки, не создавая новую и не звоня в support."""
        return self._make_jsonrpc_request(
            'identification_returned_payment_by_deal', {"deal_id": deal_id}
        )

    def refund_payment(self, payment_id: str, amount: float = None, virtual_accounts: list = None) -> dict:
        """Возврат платежа: неидентифицированного (без реквизитов, только payment_id)
        либо идентифицированного платежа СБП (передаются amount и virtual_accounts —
        список виртуальных счетов, с которых списывается возврат)."""
        params = {"payment_id": payment_id}
        if amount is not None:
            params["amount"] = round(amount, 2)
        if virtual_accounts is not None:
            params["virtual_accounts"] = virtual_accounts
        return self._make_jsonrpc_request('refund_payment', params)

    # ------------------------------------------------------------------ #
    # Документы
    # ------------------------------------------------------------------ #
    def get_document(self, document_id: str) -> dict:
        """Информация по документу и статус его загрузки (поле success_added)."""
        return self._make_jsonrpc_request('get_document', {"document_id": document_id}).get('result', {})

    def list_documents(self, beneficiary_id: str = None, deal_id: str = None) -> dict:
        """Список загруженных документов (опционально по бенефициару/сделке)."""
        filters = {}
        if beneficiary_id:
            filters['beneficiary_id'] = beneficiary_id
        if deal_id:
            filters['deal_id'] = deal_id
        params = {"filters": filters} if filters else {}
        return self._make_jsonrpc_request('list_documents', params).get('result', {})

    # ------------------------------------------------------------------ #
    # Сделки
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sanitize_recipients(recipients: list) -> list:
        """Очистить назначение платежа у каждого получателя (см. sanitize_purpose)."""
        cleaned = []
        for r in recipients:
            if isinstance(r, dict) and r.get('purpose'):
                r = {**r, 'purpose': sanitize_purpose(r['purpose'])}
            cleaned.append(r)
        return cleaned

    def create_deal(self, amount: float, payers: list, recipients: list) -> dict:
        """Создание сделки."""
        params = {
            "amount": round(amount, 2),
            "payers": payers,
            "recipients": self._sanitize_recipients(recipients),
        }
        return self._make_jsonrpc_request('create_deal', params)

    def update_deal(self, deal_id: str, amount: float, payers: list, recipients: list) -> dict:
        """Обновление сделки (только в статусах new/correction)."""
        params = {
            "deal_id": deal_id,
            "deal_data": {
                "amount": round(amount, 2),
                "payers": payers,
                "recipients": self._sanitize_recipients(recipients),
            },
        }
        return self._make_jsonrpc_request('update_deal', params)

    def execute_deal(self, deal_id: str) -> dict:
        """Исполнение сделки (формирует платежи по реквизитам получателей)."""
        return self._make_jsonrpc_request('execute_deal', {"deal_id": deal_id})

    def get_deal(self, deal_id: str) -> dict:
        """Информация по сделке."""
        return self._make_jsonrpc_request('get_deal', {"deal_id": deal_id})

    def rejected_deal(self, deal_id: str) -> dict:
        """Отмена сделки — снимает блокировку суммы на виртуальных счетах
        плательщиков. Работает только для сделок в статусе new — не для уже
        исполненных и не для сделок в статусе correction (для них см.
        cancel_deal_with_executed_recipients)."""
        return self._make_jsonrpc_request('rejected_deal', {"deal_id": deal_id})

    def cancel_deal_with_executed_recipients(self, deal_id: str) -> dict:
        """Отмена сделки из статуса correction с одним плательщиком — снимает
        блокировку суммы, когда получатель СБП отклонил перевод (get_deal
        вернул recipient.status=reject) и сделка ушла в correction.
        rejected_deal в этом статусе отказывает (4418)."""
        return self._make_jsonrpc_request('cancel_deal_with_executed_recipients', {"deal_id": deal_id})

    def upload_document_for_deal(
        self,
        file_path: str,
        beneficiary_id: str,
        deal_id: str,
        document_type: str = "service_agreement",
        document_date: str = None,
        document_number: str = "001",
    ) -> dict:
        """Загрузка договора оказания услуг (service_agreement) на сделку —
        обязательна перед execute_deal."""
        if document_date is None:
            document_date = datetime.now().strftime("%Y-%m-%d")
        params = {
            'beneficiary_id': beneficiary_id,
            'deal_id': deal_id,
            'document_type': document_type,
            'document_date': document_date,
            'document_number': document_number,
        }
        return self._upload_document_request('upload_document/deal', file_path, params)

    # ------------------------------------------------------------------ #
    # Система быстрых платежей
    # ------------------------------------------------------------------ #
    def list_bank_sbp(self) -> dict:
        """Список банков-участников СБП с идентификаторами (bank_sbp_id)."""
        return self._make_jsonrpc_request('list_bank_sbp', {}).get('result', {})

    # ------------------------------------------------------------------ #
    # Тестовый слой (pre): пополнение номинального счёта
    # ------------------------------------------------------------------ #
    def transfer_money(self, recipient_account: str, recipient_bank_code: str,
                       amount: float, purpose: str = 'Тестовое пополнение, без НДС') -> dict:
        """Отправить тестовый платёж (helper pre-слоя tender-helpers).

        Создаёт входящий платёж на номинальный счёт — для проверки идентификации.
        Доступно только на тестовом слое (pre).
        """
        params = {
            "recipient_account": recipient_account,
            "recipient_bank_code": recipient_bank_code,
            "amount": amount,
            "purpose": sanitize_purpose(purpose),
        }
        data = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "transfer_money",
            "params": params,
        }
        data_str = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        body = data_str.encode('utf-8')

        # tender-helpers лежит рядом с cyclops: .../v1/cyclops → .../v1/tender-helpers
        base_v1 = self.base_url.rstrip('/').rsplit('/', 1)[0]
        request_url = f"{base_v1}/tender-helpers/jsonrpc"

        headers = {
            'sign-system': self.platform_id,
            'sign-thumbprint': self.cert_thumbprint,
            'sign-data': self._sign_header(body),
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        try:
            response = self.session.post(request_url, headers=headers, data=body, timeout=60)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error("Ошибка тестового платежа Cyclops: %s", e)
            raise
