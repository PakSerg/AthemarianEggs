from django.core import signing
from django.conf import settings

SALT = 'receipt-email-unsubscribe'
MAX_AGE = 60 * 60 * 24 * 30  # 30 дней


def make_unsubscribe_token(user_id: int) -> str:
    return signing.dumps({'uid': user_id}, salt=SALT)


def parse_unsubscribe_token(token: str) -> int | None:
    try:
        data = signing.loads(token, salt=SALT, max_age=MAX_AGE)
        return int(data['uid'])
    except Exception:
        return None


def build_unsubscribe_url(user_id: int) -> str:
    site_url = getattr(settings, 'SITE_URL', 'https://af.cheqly.ru')
    token = make_unsubscribe_token(user_id)
    return f'{site_url}/participants/unsubscribe/{token}/'
