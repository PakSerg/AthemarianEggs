from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticViewSitemap(Sitemap):
    changefreq = 'weekly'
    protocol = 'https'

    def items(self):
        return [
            'main:home',
            'main:addresses',
        ]

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        if item == 'main:home':
            return 1.0
        return 0.7


class PrelaunchSitemap(Sitemap):
    """До старта акции сайт не индексируется — в sitemap попадает только главная (заглушка)."""
    changefreq = 'weekly'
    protocol = 'https'
    priority = 0.5

    def items(self):
        return ['main:home']

    def location(self, item):
        return reverse(item)

