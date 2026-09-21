import logging
import secrets
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from ..models import EmailVerificationCode
from .email_templates import SITE_NAME, SITE_URL, base_email_context
from .send_email import send_html_mail

logger = logging.getLogger(__name__)

CODE_LENGTH = 6
CODE_TTL_MINUTES = 15
RESEND_COOLDOWN_SECONDS = 180


class EmailVerificationPurpose:
    REGISTRATION = 'registration'
    PASSWORD_RESET = 'password_reset'


def _generate_code() -> str:
    return f'{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}'


def _invalidate_previous_codes(email: str, purpose: str) -> None:
    EmailVerificationCode.objects.filter(
        email=email,
        purpose=purpose,
        is_used=False,
    ).update(is_used=True)


def create_and_send_code(email: str, purpose: str) -> None:
    email = email.lower().strip()
    code = _generate_code()
    expires_at = timezone.now() + timedelta(minutes=CODE_TTL_MINUTES)

    _invalidate_previous_codes(email, purpose)
    EmailVerificationCode.objects.create(
        email=email,
        code=code,
        purpose=purpose,
        expires_at=expires_at,
    )

    site_domain = SITE_URL.replace('https://', '').replace('http://', '')

    if purpose == EmailVerificationPurpose.REGISTRATION:
        subject = f'Подтверждение регистрации — {SITE_NAME}'
        context = base_email_context(
            email_title='Подтверждение регистрации',
            heading='Подтверждение регистрации',
            intro_text='Введите код ниже на сайте, чтобы завершить\nрегистрацию в акции.',
            code=code,
            ttl_minutes=CODE_TTL_MINUTES,
            action_url=f'{SITE_URL}{reverse("participants:verify_email")}',
            action_label='Подтвердить email',
            footer_note=f'Если вы не регистрировались на <a href="{SITE_URL}" style="color:#8B93AD;">{site_domain}</a>, проигнорируйте это письмо.',
        )
    else:
        subject = f'Восстановление пароля — {SITE_NAME}'
        context = base_email_context(
            email_title='Восстановление пароля',
            heading='Восстановление пароля',
            intro_text='Введите код ниже, чтобы задать новый пароль\nдля вашего аккаунта.',
            code=code,
            ttl_minutes=CODE_TTL_MINUTES,
            action_url=f'{SITE_URL}{reverse("participants:reset_password")}',
            action_label='Сменить пароль',
            footer_note=f'Если вы не запрашивали смену пароля на <a href="{SITE_URL}" style="color:#8B93AD;">{site_domain}</a>, проигнорируйте это письмо.',
        )

    from django.conf import settings
    if getattr(settings, 'EMAIL_DISABLED', False):
        print(f'\n[DEV] Код верификации для {email}: {code}\n', flush=True)

    send_html_mail(subject, email, 'emails/verification_code.html', context)


def _code_sent_session_key(purpose: str) -> str:
    return f'code_sent_at_{purpose}'


def mark_code_sent(request, purpose: str) -> None:
    request.session[_code_sent_session_key(purpose)] = timezone.now().timestamp()


def get_resend_cooldown_seconds_left(request, purpose: str) -> int:
    sent_at = request.session.get(_code_sent_session_key(purpose))
    if sent_at is None:
        return RESEND_COOLDOWN_SECONDS
    elapsed = timezone.now().timestamp() - float(sent_at)
    return max(0, int(RESEND_COOLDOWN_SECONDS - elapsed))


def can_resend_code(request, purpose: str) -> bool:
    return get_resend_cooldown_seconds_left(request, purpose) == 0


def format_resend_wait(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    if minutes:
        return f'{minutes} мин {secs:02d} сек'
    return f'{secs} сек'


def verify_code(email: str, code: str, purpose: str) -> bool:
    email = email.lower().strip()
    code = code.strip()

    verification = (
        EmailVerificationCode.objects.filter(
            email=email,
            purpose=purpose,
            code=code,
            is_used=False,
            expires_at__gte=timezone.now(),
        )
        .order_by('-created_at')
        .first()
    )
    if verification is None:
        return False

    verification.is_used = True
    verification.save(update_fields=['is_used'])
    return True
