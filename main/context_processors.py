from django.conf import settings

from main.models import CompanyInfo, SEOSettings


def debug_context(request):
    return {
        'DEBUG': settings.DEBUG,
        # Счётчики подключаются только при заполненных настройках — см.
        # templates/seo/. Держим их в общем контексте, чтобы partials не
        # тянули settings сами.
        'YANDEX_METRIKA_ID': settings.YANDEX_METRIKA_ID,
        'YANDEX_VERIFICATION': settings.YANDEX_VERIFICATION,
    }

def hero_video_context(request):
    return {
        'hero_video_url': settings.HERO_VIDEO_URL
    }

def company_info_context(request):
    company_info = CompanyInfo.get_instance()
    return {
        'company_info': company_info
    }

def seo_settings_context(request):
    return {
        'seo_settings': SEOSettings.get_instance()
    }