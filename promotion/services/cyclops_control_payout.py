"""Контрольные (тестовые) выплаты по заранее подготовленному файлу реестра.

Отличие от штатной выплаты гарантированного приза
(`guaranteed_prize_payout.py`): реестр НЕ генерируется приложением, а берётся
готовым файлом — тем самым, который согласован с Заказчиком. Из него же
читается список получателей, чтобы файл и фактические выплаты гарантированно
совпадали: переписывать людей руками во второе место негде, значит и разойтись
им негде.

Записи `GuaranteedPrizePayout` не создаются и не изменяются — штатный флоу
выплат участникам акции эта логика не затрагивает.
"""

import logging
import re
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from openpyxl import load_workbook

from ..models import ControlPayout, CyclopsSettings, SbpBank
from .cyclops import CyclopsAPIClient, sanitize_purpose
from .guaranteed_prize_payout import normalize_phone

logger = logging.getLogger(__name__)

# Строка заголовка таблицы участников и колонки — как в согласованной форме
# «Реестр перечисления денежных средств Победителям Акции».
HEADER_ROW = 14
COL_NUM, COL_LAST, COL_FIRST, COL_MIDDLE, COL_STATUS, COL_REASON, \
    COL_AMOUNT, COL_NDFL, COL_PHONE, COL_BANK = range(1, 11)

# Организационно-правовые формы, которые в названии банка ничего не различают:
# в реестре пишут «АО «Альфа-Банк»», в справочнике СБП — «Альфа-Банк».
LEGAL_FORMS = ('пао', 'ао', 'оао', 'зао', 'ооо', 'нко', 'кб', 'акб')


class ControlPayoutError(Exception):
    pass


def normalize_bank_name(name):
    """Привести название банка к виду, в котором его можно сравнивать.

    Снимает кавычки, дефисы, пробелы, организационно-правовую форму и
    различие ё/е: «АО «Альфа-Банк»» и «Альфа-Банк» → 'альфабанк',
    «АО «ТБанк»» и «Т-Банк» → 'тбанк'.
    """
    s = (name or '').lower().replace('ё', 'е')
    s = re.sub(r'[«»"\'’`()]', ' ', s)
    s = re.sub(r'[\s\-–—]+', ' ', s).strip()
    return ''.join(w for w in s.split(' ') if w and w not in LEGAL_FORMS)


def build_bank_index():
    """Справочник СБП, разложенный по нормализованному названию."""
    index = {}
    for bank in SbpBank.objects.all():
        index.setdefault(normalize_bank_name(bank.name_rus or bank.name), []).append(bank)
    return index


def resolve_bank(name, index=None, overrides=None):
    """Найти банк СБП по названию из реестра.

    `overrides` — заданное вручную соответствие {название из реестра: sbp_id},
    имеет приоритет над поиском по справочнику.

    Возвращает SbpBank. Бросает ControlPayoutError, если банк не найден или
    название подходит сразу нескольким — угадывать в выплате реальных денег
    нельзя, пусть лучше команда остановится.
    """
    index = build_bank_index() if index is None else index
    key = normalize_bank_name(name)

    override_sbp_id = (overrides or {}).get(key)
    if override_sbp_id:
        bank = SbpBank.objects.filter(sbp_id=override_sbp_id).first()
        if not bank:
            raise ControlPayoutError(
                f'банк {name!r}: указанный вручную sbp_id {override_sbp_id!r} '
                f'отсутствует в справочнике СБП'
            )
        return bank

    hits = index.get(key, [])
    if not hits:
        raise ControlPayoutError(
            f'банк {name!r} не найден в справочнике СБП (нормализовано: {key!r}). '
            f'Обновите справочник командой sync_sbp_banks или укажите банк точнее.'
        )
    if len(hits) > 1:
        variants = ', '.join(f'{b.name_rus} (БИК {b.bank_code}, sbp_id {b.sbp_id})' for b in hits)
        raise ControlPayoutError(
            f'банк {name!r} подходит сразу нескольким записям справочника: {variants}. '
            f'Выберите нужный явно: --bank "{name}=<sbp_id>"'
        )
    return hits[0]


def read_registry(path, *, amount_override=None, purpose=None, bank_overrides=None):
    """Прочитать получателей из файла реестра и сопоставить банки со справочником.

    Возвращает список словарей, готовых к выплате. Любая проблема в любой
    строке — исключение: на боевом слое лучше не отправить ничего, чем
    отправить часть списка.
    """
    default_purpose = purpose or settings.CYCLOPS_CONFIG['PRIZE_PURPOSE']
    overrides = {normalize_bank_name(k): v for k, v in (bank_overrides or {}).items()}
    workbook = load_workbook(path, data_only=True)
    sheet = workbook['Реестр'] if 'Реестр' in workbook.sheetnames else workbook.active

    index = build_bank_index()
    recipients, errors = [], []

    for row in range(HEADER_ROW + 1, sheet.max_row + 1):
        def cell(col):
            value = sheet.cell(row=row, column=col).value
            return '' if value is None else str(value).strip()

        last_name, first_name = cell(COL_LAST), cell(COL_FIRST)
        # Таблица заканчивается строкой «ИТОГО:» — она стоит в первой колонке.
        if cell(COL_NUM).upper().startswith('ИТОГО') or cell(COL_LAST).upper().startswith('ИТОГО'):
            break
        if not last_name and not first_name:
            continue

        where = f'строка {row}'
        if not last_name or not first_name:
            errors.append(f'{where}: не заполнены фамилия или имя')
            continue

        phone = normalize_phone(cell(COL_PHONE))
        if not phone:
            errors.append(f'{where} ({last_name}): некорректный телефон {cell(COL_PHONE)!r}')
            continue

        raw_amount = amount_override if amount_override is not None else cell(COL_AMOUNT)
        try:
            amount = Decimal(str(raw_amount).replace(',', '.')).quantize(Decimal('0.01'))
        except (InvalidOperation, TypeError):
            errors.append(f'{where} ({last_name}): некорректная сумма {raw_amount!r}')
            continue
        if amount <= 0:
            errors.append(f'{where} ({last_name}): сумма должна быть больше нуля')
            continue

        bank_source = cell(COL_BANK)
        try:
            bank = resolve_bank(bank_source, index, overrides)
        except ControlPayoutError as e:
            errors.append(f'{where} ({last_name}): {e}')
            continue

        recipients.append({
            'row_number': row,
            'last_name': last_name,
            'first_name': first_name,
            'middle_name': cell(COL_MIDDLE),
            'phone': phone,
            'phone_source': cell(COL_PHONE),
            'bank_source': bank_source,
            'bank': bank,
            'amount': amount,
            'purpose': sanitize_purpose(default_purpose),
        })

    if errors:
        raise ControlPayoutError('Реестр не прошёл проверку:\n  ' + '\n  '.join(errors))
    if not recipients:
        raise ControlPayoutError(f'В реестре {path} не найдено ни одного получателя')
    return recipients


def find_previous_payouts(recipients):
    """Кому из списка контрольная выплата уже уходила в прошлых прогонах.

    Команда создаёт на каждый запуск новый прогон и ничего не помнит о
    предыдущих, поэтому повторный запуск того же реестра отправил бы деньги
    второй раз. Сверяем по нормализованному телефону среди выплат, которые уже
    приняты Точкой (отправлены или подтверждены) — неудачные попытки не в счёт,
    их как раз имеет смысл повторить.
    """
    phones = {r['phone'] for r in recipients}
    return list(
        ControlPayout.objects
        .filter(phone__in=phones,
                status__in=(ControlPayout.STATUS_EXECUTING, ControlPayout.STATUS_PAID))
        .order_by('created_at')
    )


def payer_config():
    """(virtual_account_id, beneficiary_id, живой баланс) активного плательщика."""
    cs = CyclopsSettings.load()
    if not cs.is_payout_configured:
        raise ControlPayoutError(
            'Плательщик Cyclops не настроен: укажите бенефициара и виртуальный счёт '
            'в панели (/panel/cyclops/, раздел «Настройки плательщика»).'
        )
    virtual_account = cs.payout_virtual_account.virtual_account_id
    beneficiary_id = cs.payout_beneficiary.beneficiary_id

    response = CyclopsAPIClient().get_virtual_account(virtual_account)
    cash = (response.get('virtual_account') or {}).get('cash')
    if cash is None:
        raise ControlPayoutError(f'Не удалось получить баланс счёта {virtual_account}: {response}')
    return virtual_account, beneficiary_id, Decimal(str(cash)), cs


def create_payouts(recipients, registry_path, *, batch=None):
    """Завести записи ControlPayout на прогон (ещё ничего не отправляя)."""
    batch = batch or uuid.uuid4().hex[:12]
    virtual_account, beneficiary_id, _cash, _cs = payer_config()

    with open(registry_path, 'rb') as fh:
        registry_bytes = fh.read()
    filename = str(registry_path).replace('\\', '/').rsplit('/', 1)[-1]

    payouts = []
    stored_name = None  # файл реестра сохраняем один раз на прогон и переиспользуем
    for item in recipients:
        payout = ControlPayout(
            batch=batch,
            row_number=item['row_number'],
            last_name=item['last_name'],
            first_name=item['first_name'],
            middle_name=item['middle_name'],
            phone=item['phone'],
            bank_name_source=item['bank_source'],
            bank_name=item['bank'].name_rus or item['bank'].name,
            bank_bik=item['bank'].bank_code,
            bank_sbp_id=item['bank'].sbp_id,
            amount=item['amount'],
            purpose=item['purpose'],
            beneficiary_id=beneficiary_id,
            virtual_account=virtual_account,
        )
        payout.log(
            f'подготовлена: {payout.fio}, {payout.amount}₽, тел {payout.phone}, '
            f'банк {payout.bank_name} (sbp_id {payout.bank_sbp_id})'
        )
        payout.save()
        if stored_name is None:
            # Первая запись кладёт файл на диск, остальные ссылаются на него же:
            # документ у всех получателей прогона обязан быть буквально одним и
            # тем же файлом, а не семью копиями с разными именами.
            payout.registry_file.save(filename, ContentFile(registry_bytes), save=True)
            stored_name = payout.registry_file.name
        else:
            payout.registry_file.name = stored_name
            payout.save(update_fields=['registry_file', 'updated_at'])
        payouts.append(payout)

    logger.info('control_payout: подготовлен прогон %s на %s получателей', batch, len(payouts))
    return batch, payouts


def send_payout(payout, api=None):
    """Отправить одну контрольную выплату: create_deal → документ → execute_deal.

    Документ-основание — файл реестра, сохранённый в самой записи, один и тот же
    для всех получателей прогона.
    """
    api = api or CyclopsAPIClient()

    if payout.status in (ControlPayout.STATUS_EXECUTING, ControlPayout.STATUS_PAID):
        payout.log('пропуск: выплата уже отправлена')
        payout.save(update_fields=['api_log', 'updated_at'])
        return payout

    recipient = {
        'number': 1,
        'type': 'payment_contract_by_sbp',
        'amount': float(payout.amount),
        'first_name': payout.first_name,
        'last_name': payout.last_name,
        'phone_number': payout.phone,
        'bank_sbp_id': payout.bank_sbp_id,
        'purpose': payout.purpose,
    }
    if payout.middle_name:
        recipient['middle_name'] = payout.middle_name
    payers = [{'virtual_account': payout.virtual_account, 'amount': float(payout.amount)}]

    try:
        if not payout.deal_id:
            payout.log(f'create_deal: {recipient}')
            response = api.create_deal(float(payout.amount), payers, [recipient])
            if 'result' not in response:
                raise ControlPayoutError(f'create_deal: {response.get("error") or response}')
            payout.deal_id = response['result']['deal_id']
            payout.log(f'create_deal OK: deal_id={payout.deal_id}')
            payout.save(update_fields=['deal_id', 'api_log', 'updated_at'])

        if not payout.document_id:
            payout.log(f'upload_document/deal: {payout.registry_file.name}')
            doc = api.upload_document_for_deal(
                payout.registry_file.path, payout.beneficiary_id, payout.deal_id,
            )
            payout.document_id = doc.get('document_id') if isinstance(doc, dict) else None
            payout.log(f'upload_document OK: {doc}')
            payout.save(update_fields=['document_id', 'api_log', 'updated_at'])

        payout.log(f'execute_deal: {payout.deal_id}')
        response = api.execute_deal(payout.deal_id)
        if 'result' not in response:
            raise ControlPayoutError(f'execute_deal: {response.get("error") or response}')
        payout.log(f'execute_deal OK: {response.get("result")}')
    except Exception as e:  # noqa: BLE001 — отчитываемся по каждому получателю отдельно
        payout.status = ControlPayout.STATUS_FAILED
        payout.error_reason = str(e)
        payout.log(f'ОШИБКА: {e}')
        payout.save(update_fields=['status', 'error_reason', 'api_log', 'updated_at'])
        logger.exception('control_payout: не отправлена выплата #%s %s', payout.pk, payout.fio)
        return payout

    payout.status = ControlPayout.STATUS_EXECUTING
    payout.sent_at = timezone.now()
    payout.error_reason = None
    payout.save(update_fields=['status', 'sent_at', 'error_reason', 'api_log', 'updated_at'])
    logger.info('control_payout: отправлена выплата #%s %s deal_id=%s',
                payout.pk, payout.fio, payout.deal_id)
    return payout


def refresh_status(payout, api=None):
    """Опросить сделку и обновить статус выплаты (дошли деньги или нет)."""
    api = api or CyclopsAPIClient()
    if not payout.deal_id:
        return payout

    response = api.get_deal(payout.deal_id)
    deal = (response.get('result') or {}).get('deal')
    if not deal:
        payout.log(f'get_deal: сделка не найдена, ответ {response}')
        payout.save(update_fields=['api_log', 'updated_at'])
        return payout

    recipients = deal.get('recipients') or []
    rec = recipients[0] if recipients else {}
    deal_status, rec_status = deal.get('status'), rec.get('status')
    payout.log(f'get_deal: сделка={deal_status}, получатель={rec_status}, '
               f'error_reason={rec.get("error_reason")}')

    if rec_status == 'success':
        payout.status = ControlPayout.STATUS_PAID
        payout.paid_at = payout.paid_at or timezone.now()
        payout.error_reason = None
    elif rec_status == 'reject':
        payout.status = ControlPayout.STATUS_FAILED
        payout.error_reason = rec.get('error_reason') or 'Платёж отклонён банком получателя'
    elif payout.status != ControlPayout.STATUS_FAILED:
        payout.status = ControlPayout.STATUS_EXECUTING

    payout.save(update_fields=['status', 'paid_at', 'error_reason', 'api_log', 'updated_at'])
    return payout
