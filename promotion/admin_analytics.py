import logging
from datetime import datetime

from django.contrib import admin
from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.urls import path

from .models import AnalyticsDashboard, Receipt
from .services.analytics_export import generate_analytics_excel
from .services.rejection_reasons import rejection_reasons_breakdown
from .services.winners_export import generate_winners_excel

logger = logging.getLogger(__name__)


@admin.register(AnalyticsDashboard)
class AnalyticsDashboardAdmin(admin.ModelAdmin):
    change_list_template = 'admin/promotion/analyticsdashboard/change_list.html'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_staff

    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_staff

    def get_urls(self):
        custom_urls = [
            path(
                'statistics/',
                self.admin_site.admin_view(self.download_statistics),
                name='promotion_analyticsdashboard_statistics',
            ),
            path(
                'winners/',
                self.admin_site.admin_view(self.download_winners),
                name='promotion_analyticsdashboard_winners',
            ),
        ]
        return custom_urls + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        context = {
            **self.admin_site.each_context(request),
            'opts': self.model._meta,
            'title': self.model._meta.verbose_name_plural,
            'rejection_reasons': rejection_reasons_breakdown(Receipt.objects.all()),
            'rejected_total': Receipt.objects.filter(status=Receipt.Status.REJECTED).count(),
        }
        if extra_context:
            context.update(extra_context)
        request.current_app = self.admin_site.name
        return TemplateResponse(request, self.change_list_template, context)

    def download_statistics(self, request):
        try:
            content = generate_analytics_excel()
        except Exception:
            logger.exception('Admin: ошибка формирования статистики')
            raise
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        filename = f'analytics_{timestamp}.xlsx'
        return self._excel_response(content, filename)

    def download_winners(self, request):
        try:
            content = generate_winners_excel()
        except Exception:
            logger.exception('Admin: ошибка формирования списка победителей')
            raise
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        filename = f'winners_{timestamp}.xlsx'
        return self._excel_response(content, filename)

    @staticmethod
    def _excel_response(content, filename):
        response = HttpResponse(
            content,
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
