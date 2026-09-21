"""Письма о гарантированном призе 50₽.

Четыре отдельных, независимых события:
1. Регистрация — участник впервые заполнил все обязательные данные профиля.
   Приз ещё не отправлен, только поставлен в очередь (см. guaranteed_prize_payout.py).
2. Приз фактически отправлен и подтверждён банком (GuaranteedPrizePayout.paid_at) —
   отправляется автоматически из apply_deal_status.
3. Отказ банка, похожий на ошибку в ФИО/реквизитах получателя — отправляется
   автоматически из apply_deal_status, см. _is_fio_related_error.
4. Отказ банка по любой другой причине (банк отклонил платёж) — отправляется
   автоматически из apply_deal_status, см. её else-ветку.

Тексты всех писем редактируются в админке (GuaranteedPrizeEmailTemplate).
"""

import logging

from .email_templates import base_email_context
from .send_email import send_html_mail
from .unsubscribe import build_unsubscribe_url

logger = logging.getLogger(__name__)

DEFAULT_GUARANTEED_PRIZE_VALUE = '50 рублей'


class _SafeFormatDict(dict):
    """Плейсхолдер, которого нет среди переданных значений, остаётся в тексте
    как {name} вместо того, чтобы страховочно откатывать форматирование целиком
    (как делает str.format при KeyError) — так опечатка в одном плейсхолдере
    не мешает подставиться остальным, документированным."""

    def __missing__(self, key):
        return '{' + key + '}'


def _prize_amount_display(payout=None) -> str:
    if payout is not None and payout.amount is not None:
        amount = payout.amount
        if amount == amount.to_integral_value():
            amount = int(amount)
        return f'{amount} рублей'
    return DEFAULT_GUARANTEED_PRIZE_VALUE


def _render_template(code, **placeholders):
    from ..models import GuaranteedPrizeEmailTemplate

    template = GuaranteedPrizeEmailTemplate.objects.filter(code=code).first()
    if not template:
        logger.error('guaranteed_prize_email(%s): шаблон не настроен в админке', code)
        return None, None
    safe = _SafeFormatDict(**placeholders)
    return template.subject.format_map(safe), template.text.format_map(safe)


def _send(participant, code, placeholders, receipt=None) -> bool:
    recipient = participant.email
    if not recipient:
        logger.warning('guaranteed_prize_email(%s): нет email participant_id=%s', code, participant.pk)
        return False

    subject, body_text = _render_template(code, **placeholders)
    if subject is None:
        return False

    name = (getattr(participant, 'first_name', '') or '').strip()
    context = base_email_context(
        recipient_name=name,
        subject=subject,
        body_text=body_text,
        unsubscribe_url=build_unsubscribe_url(participant.pk),
    )
    try:
        send_html_mail(
            subject, recipient, 'emails/guaranteed_prize_generic.html', context,
            receipt_id=receipt.pk if receipt else None,
        )
        logger.info('guaranteed_prize_email(%s): отправлено participant_id=%s', code, participant.pk)
        return True
    except Exception:
        logger.exception('guaranteed_prize_email(%s): ошибка отправки participant_id=%s', code, participant.pk)
        return False


def send_guaranteed_prize_registered_email_if_needed(participant):
    """Письмо «вы успешно зарегистрировались, приз придёт позже» — один раз,
    сразу как только участник впервые заполнил все обязательные данные профиля."""
    if participant.is_guaranteed_prize_sent:
        return
    from ..models import GuaranteedPrizeEmailTemplate

    placeholders = {
        'recipient_name': (getattr(participant, 'first_name', '') or '').strip(),
        'prize_amount': _prize_amount_display(),
    }
    _send(participant, GuaranteedPrizeEmailTemplate.Code.REGISTERED, placeholders)
    participant.is_guaranteed_prize_sent = True
    participant.save(update_fields=['is_guaranteed_prize_sent'])


def send_guaranteed_prize_paid_email(participant, payout, *, force=False):
    """Письмо «гарантированный приз отправлен».

    Отправляется автоматически из guaranteed_prize_payout.apply_deal_status,
    как только сделка подтверждена банком (status=paid) — фоновым опросом
    (poll_prize_deals) или при обработке ретрая. Можно отправить и вручную со
    страницы конкретной выплаты в админке (GuaranteedPrizePayoutAdmin) — этой
    же функцией с force=True для переотправки. Привязка писем — к самой
    выплате (GuaranteedPrizePayoutEmail), не к участнику и не к чеку.

    `force=True` разрешает переотправку уже отправленного письма (например,
    если участник просит переслать).
    """
    from ..models import GuaranteedPrizeEmailTemplate, GuaranteedPrizeSent

    if payout.paid_email_sent and not force:
        return False

    recipient = participant.email
    if not recipient:
        logger.warning('guaranteed_prize_email(sent): нет email participant_id=%s', participant.pk)
        return False

    name = (getattr(participant, 'first_name', '') or '').strip()
    placeholders = {
        'recipient_name': name,
        'prize_amount': _prize_amount_display(payout),
    }
    subject, body_text = _render_template(GuaranteedPrizeEmailTemplate.Code.SENT, **placeholders)
    if subject is None:
        return False

    context = base_email_context(
        recipient_name=name,
        subject=subject,
        body_text=body_text,
        unsubscribe_url=build_unsubscribe_url(participant.pk),
    )
    try:
        send_html_mail(subject, recipient, 'emails/guaranteed_prize_generic.html', context)
    except Exception:
        logger.exception('guaranteed_prize_email(sent): ошибка отправки participant_id=%s', participant.pk)
        return False

    payout.emails.create(subject=subject, message=body_text or '')
    payout.paid_email_sent = True
    payout.save(update_fields=['paid_email_sent'])
    # Оставляем и старую модель — на неё завязан экспорт аналитики (analytics_export.py).
    GuaranteedPrizeSent.objects.get_or_create(participant=participant)
    logger.info('guaranteed_prize_email(sent): отправлено participant_id=%s payout_id=%s',
                participant.pk, payout.pk)
    return True


def send_guaranteed_prize_error_email(participant, payout, *, force=False):
    """Письмо «ошибка отправки гарантированного приза» — банк отклонил выплату
    по причине, похожей на несовпадение ФИО/реквизитов получателя.

    Отправляется автоматически из guaranteed_prize_payout.apply_deal_status
    (см. _is_fio_related_error) — не по любой ошибке, а только когда причина
    отказа банка похожа на проблему с данными получателя, а не на временный
    сбой, и только для выплат плановой фоновой задачи (payout.sent_manually
    == False). Для выплат, запущенных вручную кнопкой «Провести N выплат»,
    письмо не шлётся само — только вручную со страницы этой выплаты в
    админке (GuaranteedPrizePayoutAdmin), этой же функцией с force=True для
    переотправки. Привязка писем — к самой выплате (GuaranteedPrizePayoutEmail).
    """
    from ..models import GuaranteedPrizeEmailTemplate

    if payout.error_email_sent and not force:
        return False

    recipient = participant.email
    if not recipient:
        logger.warning('guaranteed_prize_email(error_fio): нет email participant_id=%s', participant.pk)
        return False

    name = (getattr(participant, 'first_name', '') or '').strip()
    placeholders = {
        'recipient_name': name,
        'prize_amount': _prize_amount_display(payout),
    }
    subject, body_text = _render_template(GuaranteedPrizeEmailTemplate.Code.ERROR_FIO, **placeholders)
    if subject is None:
        return False

    context = base_email_context(
        recipient_name=name,
        subject=subject,
        body_text=body_text,
        unsubscribe_url=build_unsubscribe_url(participant.pk),
    )
    try:
        send_html_mail(subject, recipient, 'emails/guaranteed_prize_generic.html', context)
    except Exception:
        logger.exception('guaranteed_prize_email(error_fio): ошибка отправки participant_id=%s', participant.pk)
        return False

    payout.emails.create(subject=subject, message=body_text or '')
    payout.error_email_sent = True
    payout.save(update_fields=['error_email_sent'])
    logger.info('guaranteed_prize_email(error_fio): отправлено participant_id=%s payout_id=%s',
                participant.pk, payout.pk)
    return True


def send_guaranteed_prize_bank_error_email(participant, payout, *, force=False):
    """Письмо «ошибка отправки гарантированного приза» — банк отклонил выплату
    по причине, НЕ похожей на несовпадение ФИО/реквизитов получателя (нехватка
    средств у получателя, закрытый счёт и т.п., либо банк вообще не прислал причину).

    Отправляется автоматически из guaranteed_prize_payout.apply_deal_status (её
    else-ветка, когда _is_fio_related_error вернула False) — только для выплат
    плановой фоновой задачи (payout.sent_manually == False). Для выплат,
    запущенных вручную кнопкой «Провести N выплат», письмо не шлётся само —
    только вручную со страницы этой выплаты в админке (GuaranteedPrizePayoutAdmin),
    этой же функцией с force=True для переотправки. Привязка писем — к самой
    выплате (GuaranteedPrizePayoutEmail).
    """
    from ..models import GuaranteedPrizeEmailTemplate

    if payout.error_bank_email_sent and not force:
        return False

    recipient = participant.email
    if not recipient:
        logger.warning('guaranteed_prize_email(error_bank): нет email participant_id=%s', participant.pk)
        return False

    name = (getattr(participant, 'first_name', '') or '').strip()
    placeholders = {
        'recipient_name': name,
        'prize_amount': _prize_amount_display(payout),
    }
    subject, body_text = _render_template(GuaranteedPrizeEmailTemplate.Code.ERROR_BANK_DECLINED, **placeholders)
    if subject is None:
        return False

    context = base_email_context(
        recipient_name=name,
        subject=subject,
        body_text=body_text,
        unsubscribe_url=build_unsubscribe_url(participant.pk),
    )
    try:
        send_html_mail(subject, recipient, 'emails/guaranteed_prize_generic.html', context)
    except Exception:
        logger.exception('guaranteed_prize_email(error_bank): ошибка отправки participant_id=%s', participant.pk)
        return False

    payout.emails.create(subject=subject, message=body_text or '')
    payout.error_bank_email_sent = True
    payout.save(update_fields=['error_bank_email_sent'])
    logger.info('guaranteed_prize_email(error_bank): отправлено participant_id=%s payout_id=%s',
                participant.pk, payout.pk)
    return True
