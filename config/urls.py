from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.sitemaps.views import sitemap

from main.models import CompanyInfo
from main.views import robots_txt
from promotion.services.oki_doki import callback_oki_doki
from .sitemaps import StaticViewSitemap, PrelaunchSitemap


def sitemap_view(request, *args, **kwargs):
    """Пока не включён боевой режим SEO (CompanyInfo.seo_production_mode), в sitemap
    попадает только главная — акция ещё не началась, показывать остальные страницы рано."""
    sitemaps = {'static': StaticViewSitemap if CompanyInfo.get_instance().seo_production_mode else PrelaunchSitemap}
    return sitemap(request, sitemaps, *args, **kwargs)


urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('main.urls')),
    path('participants/', include('promotion.urls', namespace='participants')),
    path('panel/', include('staff_panel.urls', namespace='panel')),
    path('oki-doki/callback/', callback_oki_doki, name='oki_doki_callback'),

    path('sitemap.xml', sitemap_view, name='sitemap'),
    path('robots.txt', robots_txt, name='robots_txt'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler404 = 'main.views.page_not_found'
