_COOKIE_NAME = 'utm_tracked'
_COOKIE_DAYS = 30

_BOT_UA = (
    'bot', 'crawler', 'spider', 'crawling', 'slurp', 'bingbot', 'googlebot',
    'yandexbot', 'baiduspider', 'duckduckbot', 'facebookexternalhit',
    'semrushbot', 'ahrefsbot', 'mj12bot', 'petalbot', 'bytespider',
)


def _is_bot(request) -> bool:
    ua = request.META.get('HTTP_USER_AGENT', '').lower()
    return any(token in ua for token in _BOT_UA)


class UTMMiddleware:
    """
    Считает уникальные визиты на главную по кукам.
    Если куки нет — сохраняет utm_medium (или пустую строку) и ставит куку на 30 дней.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.method == 'GET' and request.path == '/' and _COOKIE_NAME not in request.COOKIES and not _is_bot(request):
            utm_medium = request.GET.get('utm_medium', '')
            try:
                from .models import UTMVisit
                UTMVisit.objects.create(utm_medium=utm_medium)
            except Exception:
                pass
            response.set_cookie(
                _COOKIE_NAME, '1',
                max_age=_COOKIE_DAYS * 24 * 3600,
                httponly=True,
                samesite='Lax',
            )

        return response
