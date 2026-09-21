import logging

from .email_templates import SITE_NAME, SITE_URL, base_email_context
from .guaranteed_prize_email import send_guaranteed_prize_registered_email_if_needed
from .send_email import send_html_mail
from .unsubscribe import build_unsubscribe_url

logger = logging.getLogger(__name__)

CABINET_URL = f'{SITE_URL}/participants/dashboard/'


def _is_subscribed(participant) -> bool:
    return getattr(participant, 'is_subscribed_receipt_emails', False)


def send_receipt_confirmed_email(receipt):
    participant = receipt.participant
    recipient = participant.email
    if not recipient:
        logger.warning('send_receipt_confirmed_email: no email receipt_id=%s', receipt.pk)
        return

    send_guaranteed_prize_registered_email_if_needed(participant)

    if not _is_subscribed(participant):
        logger.info('send_receipt_confirmed_email: unsubscribed receipt_id=%s', receipt.pk)
        return

    amount = receipt.amount or '—'
    context = base_email_context(
        amount=amount,
        receipt_id=str(receipt.public_id),
        cabinet_url=CABINET_URL,
        unsubscribe_url=build_unsubscribe_url(participant.pk),
    )
    subject = f'{SITE_NAME}. Ваш чек принят!'
    try:
        send_html_mail(subject, recipient, 'emails/receipt_confirmed.html', context, receipt_id=receipt.pk)
        logger.info('send_receipt_confirmed_email: sent receipt_id=%s', receipt.pk)
    except Exception:
        logger.exception('send_receipt_confirmed_email: failed receipt_id=%s', receipt.pk)

    


def send_receipt_rejected_email(receipt):
    participant = receipt.participant
    recipient = participant.email
    if not recipient:
        logger.warning('send_receipt_rejected_email: no email receipt_id=%s', receipt.pk)
        return
    if not _is_subscribed(participant):
        logger.info('send_receipt_rejected_email: unsubscribed receipt_id=%s', receipt.pk)
        return

    reason = (receipt.message or '').strip() or 'Чек не соответствует условиям акции или не прошёл проверку.'
    context = base_email_context(
        reason=reason,
        receipt_id=str(receipt.public_id),
        cabinet_url=CABINET_URL,
        unsubscribe_url=build_unsubscribe_url(participant.pk),
    )
    subject = f'{SITE_NAME}. Чек не прошёл проверку'
    try:
        send_html_mail(subject, recipient, 'emails/receipt_rejected.html', context, receipt_id=receipt.pk)
        logger.info('send_receipt_rejected_email: sent receipt_id=%s', receipt.pk)
    except Exception:
        logger.exception('send_receipt_rejected_email: failed receipt_id=%s', receipt.pk)
