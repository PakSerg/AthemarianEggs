import os

from django import template
from django.conf import settings
from django.contrib.staticfiles import finders
from django.templatetags.static import static as static_url

from ..services import masking

register = template.Library()


@register.simple_tag
def static_v(path):
    """
    Как {% static %}, но с ?v=<mtime> — иначе браузер и nginx годами отдают
    закешированные dashboard.css/panel.js из panel/base.html, и правки в них
    (например, крестик очистки поиска) не долетают до пользователей без
    принудительного сброса кеша.
    """
    url = static_url(path)
    file_path = finders.find(path) or (
        os.path.join(settings.STATIC_ROOT, path) if settings.STATIC_ROOT else None
    )
    try:
        version = int(os.path.getmtime(file_path))
    except (OSError, TypeError):
        return url
    sep = '&' if '?' in url else '?'
    return f'{url}{sep}v={version}'


@register.filter
def display_name(participant, masked):
    return masking.display_name(participant, bool(masked))


@register.filter
def display_email(value, masked):
    return masking.display_email(value, bool(masked))


@register.filter
def display_phone(value, masked):
    return masking.display_phone(value, bool(masked))
