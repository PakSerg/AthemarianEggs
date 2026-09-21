"""Управление сервисом Cyclops из интерфейса staff_panel.

Обёртки над CyclopsAPIClient + сохранение результатов в БД: регистрация
бенефициара (с виртуальным счётом и договором оферты), синхронизация платежей
и бенефициаров, идентификация входящего платежа. Логика перенесена из проекта
Cyclops и адаптирована под модели promotion. См. docs/cyclops-integration-plan.md
"""

import logging
from decimal import Decimal, InvalidOperation

from django.core.files.base import ContentFile
from django.utils import timezone

from ..models import (
    CyclopsBeneficiary,
    CyclopsDocument,
    CyclopsPayment,
    CyclopsVirtualAccount,
)
from . import cyclops_documents
from .cyclops import CyclopsAPIClient

logger = logging.getLogger(__name__)


class CyclopsManagerError(Exception):
    pass


def _dec(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0')


# Тип клиента в API (legal_type) → внутренний тип бенефициара
_LEGAL_TYPE_MAP = {
    'J': CyclopsBeneficiary.TYPE_UL,   # юрлицо
    'I': CyclopsBeneficiary.TYPE_IP,   # ИП
    'F': CyclopsBeneficiary.TYPE_IP,   # физлицо
}


# ---------------------------------------------------------------------------- #
# Загрузка договора оферты (contract_offer) — при регистрации и постфактум
# ---------------------------------------------------------------------------- #
def upload_beneficiary_document(beneficiary, document_file,
                                document_type=None, document_date=None):
    """Загрузить договор оферты (или любой другой документ) для УЖЕ
    зарегистрированного бенефициара — например, если при регистрации файл не
    приложили и теперь identification_payment/create_deal падает с ошибкой
    вида «Document not found» (Точка требует документ на бенефициаре).

    В отличие от `register_beneficiary`, не создаёт нового бенефициара в
    Cyclops — только прикладывает документ к уже существующему.
    """
    if not beneficiary.beneficiary_id:
        raise CyclopsManagerError('Бенефициар ещё не зарегистрирован в Cyclops (нет beneficiary_id)')

    doc = CyclopsDocument.objects.create(
        beneficiary=beneficiary,
        document_type=document_type or CyclopsDocument.TYPE_CONTRACT_OFFER,
        file=document_file,
        document_date=document_date or timezone.now().date(),
    )
    api = CyclopsAPIClient()
    try:
        up = api.upload_document_for_beneficiary(doc.file.path, beneficiary.beneficiary_id)
        doc.document_id = up.get('document_id') if isinstance(up, dict) else None
        doc.uploaded = True
        doc.save(update_fields=['document_id', 'uploaded'])
    except Exception as e:
        logger.exception('Ошибка загрузки документа для beneficiary_id=%s', beneficiary.beneficiary_id)
        raise CyclopsManagerError(f'Ошибка загрузки документа: {e}')
    return doc


# ---------------------------------------------------------------------------- #
# Регистрация бенефициара (create_beneficiary_ul/ip + вирт.счёт + договор оферты)
# ---------------------------------------------------------------------------- #
def register_beneficiary(*, client_type, name, inn, kpp=None, ogrn=None,
                         first_name=None, last_name=None, middle_name=None,
                         document=None, legal_address=None,
                         bank_account=None, bank_name=None, bank_bic=None, bank_corr_account=None,
                         contact_email=None, signatory_name=None, signatory_basis=None,
                         commission_percent=None, commission_min_amount=None, commission_fixed_amount=None):
    """Зарегистрировать бенефициара в Cyclops и сохранить в БД.

    Договор оферты (contract_offer) для регистрации берётся одним из двух
    способов:
    - `document` передан — используется загруженный файл как есть (например,
      уже подписанный скан/PDF);
    - `document` не передан, но переданы реквизиты Заказчика и условия
      вознаграждения (bank_*, signatory_*, commission_*) — Договор
      присоединения к Оферте № 2508/2026/1 генерируется автоматически и
      склеивается с текстом самой Оферты (см. cyclops_documents.py).

    Возвращает CyclopsBeneficiary. Бросает CyclopsManagerError при ошибке API.
    """
    api = CyclopsAPIClient()

    if client_type == CyclopsBeneficiary.TYPE_UL:
        if not kpp:
            raise CyclopsManagerError('Для юрлица необходимо указать КПП')
        response = api.create_beneficiary_ul(name=name, inn=inn, kpp=kpp)
    elif client_type == CyclopsBeneficiary.TYPE_IP:
        response = api.create_beneficiary_ip(
            first_name=first_name, last_name=last_name, inn=inn, middle_name=middle_name,
        )
        name = name or f'ИП {last_name or ""} {first_name or ""} {middle_name or ""}'.strip()
    else:
        raise CyclopsManagerError('Не выбран тип бенефициара')

    if 'result' not in response:
        err = (response.get('error') or {}).get('message', response)
        raise CyclopsManagerError(f'Ошибка создания бенефициара: {err}')

    beneficiary_data = response['result'].get('beneficiary', {})
    beneficiary_id = beneficiary_data.get('id')
    if not beneficiary_id:
        raise CyclopsManagerError(f'API не вернул ID бенефициара. Ответ: {response}')

    beneficiary = CyclopsBeneficiary.objects.create(
        beneficiary_id=beneficiary_id,
        beneficiary_type=client_type,
        name=name,
        inn=inn,
        kpp=kpp or None,
        ogrn=ogrn or None,
        first_name=first_name or None,
        last_name=last_name or None,
        middle_name=middle_name or None,
        legal_address=legal_address or None,
        bank_account=bank_account or None,
        bank_name=bank_name or None,
        bank_bic=bank_bic or None,
        bank_corr_account=bank_corr_account or None,
        contact_email=contact_email or None,
        signatory_name=signatory_name or None,
        signatory_basis=signatory_basis or None,
        commission_percent=commission_percent or None,
        commission_min_amount=commission_min_amount or None,
        commission_fixed_amount=commission_fixed_amount or None,
    )

    # Договор оферты (contract_offer): вручную загруженный файл — либо, если
    # его нет, но реквизитов Заказчика достаточно — автогенерация.
    if document is None and beneficiary.has_contract_offer_details:
        try:
            pdf_bytes = cyclops_documents.render_combined_contract_offer_pdf(beneficiary)
        except Exception as e:
            logger.exception('Ошибка генерации Договора присоединения beneficiary_id=%s', beneficiary_id)
            raise CyclopsManagerError(
                f'Бенефициар создан, но не удалось сгенерировать договор оферты: {e}'
            )
        document = ContentFile(pdf_bytes, name=f'Договор_присоединения_{inn}.pdf')

    if document is not None:
        try:
            upload_beneficiary_document(beneficiary, document)
        except CyclopsManagerError as e:
            raise CyclopsManagerError(f'Бенефициар создан, но договор оферты не загружен: {e}')

    # Виртуальный счёт
    try:
        va_response = api.create_virtual_account(beneficiary_id)
        virtual_account_id = (va_response.get('result') or {}).get('virtual_account')
        if virtual_account_id:
            CyclopsVirtualAccount.objects.create(
                beneficiary=beneficiary,
                virtual_account_id=virtual_account_id,
                account_name='Счёт по умолчанию',
            )
    except Exception as e:
        logger.exception('Ошибка создания вирт.счёта beneficiary_id=%s', beneficiary_id)
        raise CyclopsManagerError(f'Бенефициар создан, но виртуальный счёт не создан: {e}')

    return beneficiary


# ---------------------------------------------------------------------------- #
# Идентификация входящего платежа
# ---------------------------------------------------------------------------- #
def identify_payment(payment_id, virtual_account_id, amount=None):
    """Идентифицировать платёж на виртуальный счёт бенефициара.

    По умолчанию идентифицируется вся сумма платежа (amount=None).
    """
    payment = CyclopsPayment.objects.filter(payment_id=payment_id).first()
    if amount is None:
        if not payment or payment.amount is None:
            raise CyclopsManagerError('Не удалось определить сумму платежа для идентификации')
        amount = payment.amount

    api = CyclopsAPIClient()
    response = api.identification_payment(payment_id, virtual_account_id, float(amount))
    if 'result' not in response:
        err = (response.get('error') or {}).get('message', response)
        raise CyclopsManagerError(f'Ошибка идентификации платежа: {err}')

    if payment:
        payment.identify = True
        va = CyclopsVirtualAccount.objects.filter(virtual_account_id=virtual_account_id).first()
        if va and va.beneficiary_id:
            payment.beneficiary = va.beneficiary
        payment.save(update_fields=['identify', 'beneficiary'])
    return response


# ---------------------------------------------------------------------------- #
# Синхронизация платежей (list_payments → get_payment → БД)
# ---------------------------------------------------------------------------- #
def sync_payments():
    """Загрузить входящие платежи из Cyclops и сохранить/обновить в БД."""
    api = CyclopsAPIClient()
    payments = (api.list_payments() or {}).get('payments', [])

    processed = 0
    for payment_id in payments:
        detail = api.get_payment(payment_id) or {}
        p = detail.get('payment') or {}
        payer = p.get('payer') or {}
        CyclopsPayment.objects.update_or_create(
            payment_id=payment_id,
            defaults={
                'amount': _dec(p.get('amount')),
                'status': p.get('status') or 'new',
                'purpose': p.get('purpose'),
                'type': p.get('type'),
                'identify': bool(p.get('identify')),
                'deal_id': p.get('deal_id') or None,
                'payer_name': payer.get('name'),
                'payer_inn': payer.get('tax_code'),
                'payer_account': payer.get('account'),
                'payer_bic': payer.get('bank_code'),
                'created_at': p.get('created_at') or None,
            },
        )
        processed += 1

    logger.info('sync_payments: обработано %s платежей', processed)
    return processed


# ---------------------------------------------------------------------------- #
# Синхронизация бенефициаров и виртуальных счетов
# ---------------------------------------------------------------------------- #
def sync_beneficiaries():
    """Загрузить бенефициаров (с полными данными) и их виртуальные счета из Cyclops.

    list_beneficiary отдаёт только id/inn/legal_type — детали (наименование, КПП,
    ОГРН и т.д.) берём через get_beneficiary. Виртуальные счета синхронизируются
    отдельно (list_virtual_account → get_virtual_account) и привязываются к
    бенефициару по beneficiary_id из ответа.
    """
    api = CyclopsAPIClient()
    result = api.list_beneficiary() or {}
    items = result.get('beneficiaries') or []

    processed = 0
    for item in items:
        beneficiary_id = item.get('id')
        if not beneficiary_id:
            continue
        try:
            _upsert_beneficiary(api, beneficiary_id, item)
            processed += 1
        except Exception:
            logger.exception('sync_beneficiaries: ошибка по бенефициару %s', beneficiary_id)

    sync_virtual_accounts(api)
    logger.info('sync_beneficiaries: обработано %s бенефициаров', processed)
    return processed


def _upsert_beneficiary(api, beneficiary_id, list_item=None):
    """Получить детали бенефициара (get_beneficiary) и сохранить/обновить в БД."""
    list_item = list_item or {}
    detail = (api.get_beneficiary(beneficiary_id) or {}).get('result') or {}
    ben = detail.get('beneficiary') or {}
    data = ben.get('beneficiary_data') or {}
    nominal = ben.get('nominal_account') or {}

    legal_type = ben.get('legal_type') or list_item.get('legal_type')
    btype = _LEGAL_TYPE_MAP.get(legal_type, CyclopsBeneficiary.TYPE_UL)

    defaults = {
        'name': data.get('name') or ben.get('name') or beneficiary_id,
        'inn': ben.get('inn') or list_item.get('inn') or '',
        'kpp': data.get('kpp'),
        'ogrn': ben.get('ogrn'),
        'legal_type': legal_type,
        'beneficiary_type': btype,
        'first_name': data.get('first_name'),
        'last_name': data.get('last_name'),
        'middle_name': data.get('middle_name'),
        'nominal_account_code': nominal.get('code') or list_item.get('nominal_account_code'),
        'nominal_account_bic': nominal.get('bic') or list_item.get('nominal_account_bic'),
        'permission': ben.get('permission'),
        'permission_description': ben.get('permission_description'),
        'is_added_to_ms': bool(data.get('is_added_to_ms', False)),
        'is_active': bool(ben.get('is_active', list_item.get('is_active', True))),
    }
    beneficiary, _ = CyclopsBeneficiary.objects.update_or_create(
        beneficiary_id=beneficiary_id, defaults=defaults,
    )
    return beneficiary


def set_beneficiary_active(pk, active: bool):
    """Активировать/деактивировать бенефициара — обязательное по договору с Точкой
    действие для поддержания актуального статуса бенефициаров."""
    beneficiary = CyclopsBeneficiary.objects.filter(pk=pk).first()
    if not beneficiary or not beneficiary.beneficiary_id:
        raise CyclopsManagerError('Бенефициар не найден или ещё не зарегистрирован в Cyclops')

    api = CyclopsAPIClient()
    response = (
        api.activate_beneficiary(beneficiary.beneficiary_id) if active
        else api.deactivate_beneficiary(beneficiary.beneficiary_id)
    )
    if 'result' not in response:
        err = (response.get('error') or {}).get('message', response)
        action = 'активации' if active else 'деактивации'
        raise CyclopsManagerError(f'Ошибка {action} бенефициара: {err}')

    beneficiary.is_active = active
    beneficiary.save(update_fields=['is_active'])
    return beneficiary


def update_beneficiary(pk, *, name=None, kpp=None, ogrn=None):
    """Обновить данные бенефициара — ЮЛ (наименование/КПП/ОГРН) в Cyclops и в БД."""
    beneficiary = CyclopsBeneficiary.objects.filter(pk=pk).first()
    if not beneficiary or not beneficiary.beneficiary_id:
        raise CyclopsManagerError('Бенефициар не найден или ещё не зарегистрирован в Cyclops')
    if beneficiary.beneficiary_type != CyclopsBeneficiary.TYPE_UL:
        raise CyclopsManagerError('Обновление данных доступно только для бенефициаров-ЮЛ')

    new_name = (name or beneficiary.name or '').strip()
    new_kpp = (kpp or beneficiary.kpp or '').strip()
    if not new_kpp:
        raise CyclopsManagerError('Для юрлица необходимо указать КПП')

    api = CyclopsAPIClient()
    response = api.update_beneficiary_ul(
        beneficiary_id=beneficiary.beneficiary_id,
        name=new_name,
        kpp=new_kpp,
        ogrn=(ogrn or beneficiary.ogrn or None),
    )
    if 'result' not in response:
        err = (response.get('error') or {}).get('message', response)
        raise CyclopsManagerError(f'Ошибка обновления бенефициара: {err}')

    beneficiary.name = new_name
    beneficiary.kpp = new_kpp
    if ogrn:
        beneficiary.ogrn = ogrn
    beneficiary.save(update_fields=['name', 'kpp', 'ogrn'])
    return beneficiary


def reject_deal(deal_id):
    """Отменить сделку (снять блокировку суммы на виртуальном счёте плательщика)."""
    api = CyclopsAPIClient()
    response = api.rejected_deal(deal_id)
    if 'result' not in response:
        err = (response.get('error') or {}).get('message', response)
        raise CyclopsManagerError(f'Ошибка отмены сделки: {err}')
    return response


def identify_returned_payment(deal_id):
    """Идентифицировать платёж, вернувшийся отдельным входящим платежом по уже
    исполненной сделке (см. рекомендацию поддержки Точки от 12.02) — позволяет
    исправить реквизиты и переотправить платёж в рамках той же сделки."""
    api = CyclopsAPIClient()
    response = api.identification_returned_payment_by_deal(deal_id)
    if 'result' not in response:
        err = (response.get('error') or {}).get('message', response)
        raise CyclopsManagerError(f'Ошибка идентификации возвращённого платежа: {err}')
    return response


def refund_incoming_payment(payment_id, amount=None, virtual_accounts=None):
    """Вернуть ошибочно зачисленный или неверно идентифицированный входящий платёж."""
    api = CyclopsAPIClient()
    response = api.refund_payment(payment_id, amount=amount, virtual_accounts=virtual_accounts)
    if 'result' not in response:
        err = (response.get('error') or {}).get('message', response)
        raise CyclopsManagerError(f'Ошибка возврата платежа: {err}')

    CyclopsPayment.objects.filter(payment_id=payment_id).update(status='refund_requested')
    return response


def refund_virtual_account_funds(virtual_account_pk, *, amount, recipient_account,
                                  recipient_bank_code, recipient_name, recipient_inn,
                                  recipient_kpp=None, purpose=None):
    """Вывести средства с виртуального счёта бенефициара по реквизитам
    (например, вернуть ошибочно зачисленные в призовой фонд деньги)."""
    va = CyclopsVirtualAccount.objects.filter(pk=virtual_account_pk).first()
    if not va:
        raise CyclopsManagerError('Виртуальный счёт не найден')

    api = CyclopsAPIClient()
    response = api.refund_virtual_account(
        virtual_account_id=va.virtual_account_id,
        amount=float(amount),
        recipient_account=recipient_account,
        recipient_bank_code=recipient_bank_code,
        recipient_name=recipient_name,
        recipient_inn=recipient_inn,
        recipient_kpp=recipient_kpp,
        purpose=purpose,
    )
    if 'result' not in response:
        err = (response.get('error') or {}).get('message', response)
        raise CyclopsManagerError(f'Ошибка вывода средств с виртуального счёта: {err}')
    return response


def send_test_payment(recipient_account, recipient_bank_code, amount, purpose=None):
    """Отправить тестовый платёж на номинальный счёт (только pre-слой)."""
    api = CyclopsAPIClient()
    response = api.transfer_money(
        recipient_account=recipient_account,
        recipient_bank_code=recipient_bank_code,
        amount=float(amount),
        purpose=purpose or 'Тестовое пополнение, без НДС',
    )
    if 'result' not in response:
        err = (response.get('error') or {}).get('message', response)
        raise CyclopsManagerError(f'Ошибка тестового платежа: {err}')
    return response


def sync_virtual_accounts(api=None):
    """Синхронизировать виртуальные счета: список ID → детали → привязка к бенефициару."""
    api = api or CyclopsAPIClient()
    va_ids = (api.list_virtual_account() or {}).get('virtual_accounts', [])

    processed = 0
    for va_id in va_ids:
        try:
            info = (api.get_virtual_account(va_id) or {}).get('virtual_account') or {}
            beneficiary = None
            ben_id = info.get('beneficiary_id')
            if ben_id:
                beneficiary = CyclopsBeneficiary.objects.filter(beneficiary_id=ben_id).first()
            CyclopsVirtualAccount.objects.update_or_create(
                virtual_account_id=va_id,
                defaults={
                    'beneficiary': beneficiary,
                    'account_type': info.get('type'),
                    'available_balance': _dec(info.get('cash', 0)),
                    'blocked_balance': _dec(info.get('blocked_cash', 0)),
                },
            )
            processed += 1
        except Exception:
            logger.exception('sync_virtual_accounts: ошибка по счёту %s', va_id)

    logger.info('sync_virtual_accounts: обработано %s счетов', processed)
    return processed
