import re

from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags

SITE_URL = getattr(settings, 'SITE_URL', 'https://af.cheqly.ru')
SITE_NAME = 'Атемарская Ферма'
SUPPORT_EMAIL = getattr(settings, 'SUPPORT_EMAIL', 'af@cheqly.ru')
EMAIL_LOGO_URL = f'{SITE_URL}/static/icons/email-logo.svg'


def _support_email() -> str:
    from main.models import CompanyInfo
    return CompanyInfo.get_instance().email or SUPPORT_EMAIL


def base_email_context(**extra):
    return {
        'site_url': SITE_URL,
        'site_name': SITE_NAME,
        'support_email': _support_email(),
        'email_logo_url': EMAIL_LOGO_URL,
        **extra,
    }


def render_email_template(template_name: str, context: dict) -> tuple[str, str]:
    """HTML + plain-text fallback для почтовых клиентов без HTML."""
    html = render_to_string(template_name, context)
    text = strip_tags(html)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return html.strip(), text.strip()
