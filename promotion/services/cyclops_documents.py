"""Генерация документов Cyclops из согласованных шаблонов с подстановкой данных.

- service_agreement (документ-основание на сделку) — больше не генерируется
  здесь: это Реестр выплат (Excel), см. promotion/services/cyclops_registry.py
  и модель PayoutRegistry. Один реестр может покрывать многих участников.
- contract_offer — Договор присоединения к Оферте № 2508/2026/1 ООО «Хелиос»
  для регистрации бенефициара, объединяемый с полным текстом самой Оферты
  (static/legal/oferta_2508_2026_1.pdf — согласованный с Точкой оригинал).

Текст шаблона: templates/cyclops/contract_offer.txt — согласован с
юридической службой и комплаенс Точка Банка (см. docs/ — исходные документы
от 25.08.2026).

Готовый PDF прикладывается к бенефициару методом upload_document/beneficiary
и сохраняется в БД как CyclopsDocument.
"""

import logging
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from django.conf import settings
from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from django.utils import timezone

from num2words import num2words
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

logger = logging.getLogger(__name__)

_FONT_DIR = settings.BASE_DIR / 'static' / 'fonts' / 'Normalidad Text'
_FONT_REGULAR = ('CyclopsDocRegular', _FONT_DIR / 'Normalidad Text-Regular-Web.ttf')
_FONT_BOLD = ('CyclopsDocBold', _FONT_DIR / 'Normalidad Text-Bold-Web.ttf')
_fonts_registered = False

# Базовый текст Оферты № 2508/2026/1 ООО «Хелиос», согласованный с Точкой —
# общая часть contract_offer, одинаковая для всех бенефициаров. Договор
# присоединения (индивидуальная часть с реквизитами конкретного Заказчика)
# генерируется отдельно и приклеивается к этому файлу.
_OFFER_BASE_PDF = settings.BASE_DIR / 'static' / 'legal' / 'oferta_2508_2026_1.pdf'


def _ensure_fonts_registered():
    """Шрифт проекта (Normalidad) уже используется на сайте и покрывает всю
    кириллицу — переиспользуем его для PDF вместо встроенных шрифтов reportlab,
    которые кириллицу не поддерживают."""
    global _fonts_registered
    if _fonts_registered:
        return
    for name, path in (_FONT_REGULAR, _FONT_BOLD):
        pdfmetrics.registerFont(TTFont(name, str(path)))
    _fonts_registered = True


def _render_text_pdf(template_name: str, context: dict, title: str) -> bytes:
    """Отрендерить текстовый Django-шаблон (абзацы через пустую строку,
    первый абзац — заголовок) в PDF с кириллическим шрифтом проекта."""
    _ensure_fonts_registered()
    text = render_to_string(template_name, context)

    title_style = ParagraphStyle(
        'CyclopsDocTitle', fontName=_FONT_BOLD[0], fontSize=13, leading=17, spaceAfter=10,
    )
    body_style = ParagraphStyle(
        'CyclopsDocBody', fontName=_FONT_REGULAR[0], fontSize=11, leading=15, spaceAfter=8,
    )

    blocks = [b.strip() for b in text.strip().split('\n\n') if b.strip()]
    story = []
    for i, block in enumerate(blocks):
        html = escape(block).replace('\n', '<br/>')
        style = title_style if i == 0 else body_style
        story.append(Paragraph(html, style))
        if i == 0:
            story.append(Spacer(1, 6))

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=22 * mm, bottomMargin=20 * mm, leftMargin=22 * mm, rightMargin=22 * mm,
        title=title,
    )
    doc.build(story)
    return buf.getvalue()


def generate_service_agreement_document(payout):
    """Получить документ-основание (service_agreement) для сделки и сохранить
    его как CyclopsDocument, привязанный к payout.

    Источник содержимого — Реестр выплат (PayoutRegistry, Excel): если у
    выплаты ещё нет реестра, создаётся реестр на неё одну (точечная ручная
    выплата); если реестр уже назначен (например, плановой еженедельной
    рассылкой на многих участников сразу) — используется он как есть, без
    повторной генерации. См. promotion/services/cyclops_registry.py.

    Идемпотентно: если документ для этой выплаты уже сгенерирован (при
    повторной попытке после сбоя) — возвращает существующий, не создаёт новый.
    """
    from ..models import CyclopsDocument, CyclopsSettings
    from . import cyclops_registry

    if payout.document_id:
        return payout.document

    if not payout.registry_id:
        cyclops_registry.create_single_participant_registry(payout)
        payout.refresh_from_db(fields=['registry'])

    registry = payout.registry
    filename = registry.file.name.rsplit('/', 1)[-1]

    cs = CyclopsSettings.load()
    document = CyclopsDocument.objects.create(
        beneficiary=cs.payout_beneficiary,
        deal_id=payout.deal_id or '',
        document_type=CyclopsDocument.TYPE_SERVICE_AGREEMENT,
        document_number=registry.registry_number,
        document_date=timezone.localdate(),
    )
    registry.file.open('rb')
    try:
        document.file.save(filename, ContentFile(registry.file.read()), save=True)
    finally:
        registry.file.close()

    payout.document = document
    payout.save(update_fields=['document', 'updated_at'])
    logger.info('Документ-основание для выплаты participant_id=%s: реестр №%s',
                payout.participant_id, registry.registry_number)
    return document


# ---------------------------------------------------------------------------- #
# Договор присоединения к Оферте (contract_offer) — разово, при регистрации
# бенефициара. Итоговый файл = полный текст Оферты (static/legal, оригинал от
# Точки/юристов, без изменений) + сгенерированный Договор присоединения с
# реквизитами конкретного Заказчика.
# ---------------------------------------------------------------------------- #
def _rub_in_words(amount) -> str:
    """«1 100 (одна тысяча сто) рублей» — для сумм в договоре."""
    amount = Decimal(str(amount))
    whole = int(amount)
    return f'{whole} ({num2words(whole, lang="ru")}) рублей'


def _commission_clause(beneficiary) -> str:
    """Текст п. 2.1 Договора присоединения — заполняется ровно одним из двух
    условий вознаграждения, указанных у бенефициара."""
    if beneficiary.commission_percent:
        clause = (
            f'Вознаграждение Исполнителя по настоящему Договору составляет '
            f'{beneficiary.commission_percent}% от общей суммы кешбэка, перечисляемой '
            f'Заказчиком на Номинальный счет для выплаты Победителям Акции'
        )
        if beneficiary.commission_min_amount:
            clause += f', но не менее {_rub_in_words(beneficiary.commission_min_amount)} за одну Акцию.'
        else:
            clause += '.'
        return clause
    if beneficiary.commission_fixed_amount:
        return (
            f'Вознаграждение Исполнителя по настоящему Договору составляет фиксированную '
            f'сумму в размере {_rub_in_words(beneficiary.commission_fixed_amount)} за одну Акцию.'
        )
    raise ValueError(
        'У бенефициара не заданы условия вознаграждения платформы '
        '(commission_percent или commission_fixed_amount) — контракт не может быть сгенерирован'
    )


def build_contract_offer_context(beneficiary) -> dict:
    """Собрать данные для подстановки в шаблон Договора присоединения по
    реквизитам конкретного бенефициара (Заказчика)."""
    cfg = settings.CYCLOPS_CONFIG
    return {
        'document_date': timezone.localdate().strftime('%d.%m.%Y'),
        'customer_name': beneficiary.name,
        'customer_inn': beneficiary.inn,
        'customer_kpp': beneficiary.kpp or '—',
        'customer_ogrn': beneficiary.ogrn or '—',
        'customer_address': beneficiary.legal_address or '—',
        'customer_bank_account': beneficiary.bank_account or '—',
        'customer_bank_name': beneficiary.bank_name or '—',
        'customer_bank_bic': beneficiary.bank_bic or '—',
        'customer_bank_corr_account': beneficiary.bank_corr_account or '—',
        'customer_email': beneficiary.contact_email or '—',
        'signatory_name': beneficiary.signatory_name or '—',
        'signatory_basis': beneficiary.signatory_basis or 'Устава',
        'commission_clause': _commission_clause(beneficiary),
        'nominal_account': cfg.get('NOMINAL_ACCOUNT', '—'),
        'nominal_bank_name': cfg.get('NOMINAL_BANK_NAME', 'ООО «БАНК ТОЧКА» г. Москва'),
        'nominal_bank_bic': cfg.get('NOMINAL_BANK_BIC', '044525104'),
        'nominal_corr_account': cfg.get('NOMINAL_BANK_CORR_ACCOUNT', '30101810745374525104'),
    }


def render_contract_offer_pdf(context: dict) -> bytes:
    """Отрендерить только Договор присоединения (без текста самой Оферты)."""
    return _render_text_pdf('cyclops/contract_offer.txt', context, 'Договор присоединения к Оферте')


def render_combined_contract_offer_pdf(beneficiary) -> bytes:
    """Итоговый contract_offer для загрузки в Cyclops: полный текст Оферты
    (оригинал, без изменений) + Договор присоединения с реквизитами бенефициара."""
    accession_pdf = render_contract_offer_pdf(build_contract_offer_context(beneficiary))

    writer = PdfWriter()
    if _OFFER_BASE_PDF.exists():
        for page in PdfReader(str(_OFFER_BASE_PDF)).pages:
            writer.add_page(page)
    else:
        logger.warning(
            'Базовый файл Оферты не найден (%s) — contract_offer будет содержать '
            'только Договор присоединения', _OFFER_BASE_PDF,
        )
    for page in PdfReader(BytesIO(accession_pdf)).pages:
        writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()
