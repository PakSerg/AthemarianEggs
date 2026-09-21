import logging

import requests
from django.conf import settings

from promotion.models import Receipt, SentEmail

from .email_templates import render_email_template

logger = logging.getLogger(__name__)

SEND_EMAIL_URL = 'https://sendemail.space/send-email/'
SEND_EMAIL_IP = getattr(settings, 'SEND_EMAIL_IP', '104.171.137.186')


def send_mail(subject, recipient, message=None, *, html_message=None, text_message=None, files=None):
    """
    Отправка письма через sendemail.space.
    message — устаревший аргумент (plain text), для совместимости.
    Предпочтительно: html_message + text_message.
    """
    if getattr(settings, 'EMAIL_DISABLED', False):
        logger.info('EMAIL_DISABLED: письмо не отправлено recipient=%s subject=%s', recipient, subject)
        return

    from django.utils.html import strip_tags

    plain = text_message if text_message is not None else message
    if plain is None and html_message:
        plain = strip_tags(html_message)

    mail_data = {
        'recipient': recipient,
        'subject': subject,
        'content': plain or '',
        'ip': SEND_EMAIL_IP,
    }

    if html_message:
        mail_data['content'] = html_message

    try:
        response = requests.post(SEND_EMAIL_URL, data=mail_data, files=files, timeout=60)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        logger.exception('send_mail failed recipient=%s subject=%s', recipient, subject)
        raise


def send_html_mail(subject, recipient, template_name, context, files=None, receipt_id: int = None):
    html_message, text_message = render_email_template(template_name, context)
    
    if receipt_id: 
        SentEmail.objects.create(
            receipt=Receipt.objects.get(pk=receipt_id),
            name=subject,
            message=html_message if html_message else text_message,
        )
    send_mail(
        subject,
        recipient,
        html_message=html_message,
        text_message=text_message,
        files=files,
    )

def send_text_mail(subject, recipient, template_name, context, files=None, receipt_id: int = None):
    html_message, text_message = render_email_template(template_name, context)
    body = text_message if text_message is not None else html_message

    if receipt_id:
        SentEmail.objects.create(
            receipt=Receipt.objects.get(pk=receipt_id),
            name=subject,
            message=body or '',
        )

    send_mail(
        subject,
        recipient,
        text_message=body,
        files=files,
    )


def build_oki_data_from_record(record) -> dict | None:
    """Данные OkiDoki из сохранённых полей чека или итога главного розыгрыша."""
    participant_link = (getattr(record, 'link_oki_document', None) or '').strip()
    admin_link = (getattr(record, 'link_oki_document_admin', None) or '').strip()
    status = getattr(record, 'status_oki_document', None) or ''

    if participant_link:
        return {
            'contract_link': participant_link,
            'for_admin': False,
            'status_oki_document': status,
        }
    if admin_link:
        return {
            'contract_link': admin_link,
            'for_admin': True,
            'status_oki_document': status,
        }
    return None


def _contract_link_for_winner_email(oki_data: dict | None) -> str | None:
    """Ссылка для победителя: только если договор уже выставлен на подпись участнику."""
    if not oki_data:
        return None
    if oki_data.get('for_admin'):
        return None
    link = (oki_data.get('contract_link') or '').strip()
    return link or None


def send_email_winner(participant, prize, *, oki_data=None, receipt_id: int = None):
    recipient = participant.email
    if not recipient:
        logger.warning('send_email_winner: no email participant_id=%s', participant.pk)
        return

    name = participant.get_full_name() if hasattr(participant, 'get_full_name') else ''
    if not name and participant.first_name:
        name = participant.first_name.strip()

    from .email_templates import base_email_context

    contract_link = _contract_link_for_winner_email(oki_data)

    context = base_email_context(
        recipient_name=name,
        prize_name=prize.name,
        contract_link=contract_link,
    )

    subject = f'Поздравляем! Вы выиграли приз: {prize.name}'
    send_text_mail(subject, recipient, 'emails/winner.html', context, receipt_id=receipt_id)



def send_email_prize_shipping_soon(participant, prize, *, receipt_id: int = None):
    """Отправляет plain-text письмо о том, что приз скоро будет отправлен."""
    from promotion.models import PrizeShippingSoonMessage  

    recipient = participant.email
    if not recipient:
        logger.warning(
            'send_email_prize_shipping_soon: no email participant_id=%s', participant.pk,
        )
        return

    settings_obj = PrizeShippingSoonMessage.load()
    prize_name = prize.name if prize else ''

    try:
        text = settings_obj.text.format(prize_name=prize_name)
    except (KeyError, IndexError):
        text = settings_obj.text

    if receipt_id:
        SentEmail.objects.create(
            receipt=Receipt.objects.get(pk=receipt_id),
            name=settings_obj.subject,
            message=text,
        )

    send_mail(
        settings_obj.subject,
        recipient,
        message=text,
    )