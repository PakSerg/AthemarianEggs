import json as _json
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import path, reverse
from django.views.decorators.csrf import csrf_exempt
from django.contrib import admin
from django.db.models import Count
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from . import admin_analytics, admin_winner
import json as _json
from django import forms
from django.utils.safestring import mark_safe
from promotion.services.instant_prizes import grant_attempts_for_receipt
from promotion.services.promo_calendar import assign_promo_period, week_num_on_date
from .models import (AccountBlockedMessage, InstantAttempt, InstantMoment, InstantPrizeSettings,
                     Prize, PrizeCountChange, PrizeKind, PrizeShippingSoonMessage,
                     PromotionDrawResult,
                     Receipt, ReceiptMessageTemplate, SentEmail, Store, User,
                     KeywordProduct, ExcludingKeywordProduct,
                     EmailVerificationCode, GuaranteedPrizeSent, GuaranteedPrizeEmailTemplate,
                     PromotionDrawResultMainRaffle, Raffle, UTMVisit, SbpBank,
                     GuaranteedPrizePayout, GuaranteedPrizePayoutEmail, CyclopsBeneficiary,
                     CyclopsVirtualAccount, CyclopsPayment, CyclopsDocument, CyclopsSettings,
                     PayoutRegistry, ControlPayout, WinnerReplacement, WinnerReplacementSettings)
from .admin_winner import WinnerReceiptWorkflowMixin
from .models import OkiDokiTemplate
from promotion.services.validate_receipt import sync_promo_keywords_from_receipt
from promotion.messages import ReceiptMessage


class GuaranteedPrizePayoutInline(admin.StackedInline):
    """Показывает статус выплаты гарантированного приза прямо в карточке участника."""

    model = GuaranteedPrizePayout
    extra = 0
    can_delete = False
    fields = ('amount', 'status', 'deal_id', 'bank_sbp_id', 'phone_number', 'cnt_retry',
               'error_reason', 'document_link', 'created_at', 'sent_at', 'paid_at')
    readonly_fields = fields

    @admin.display(description='Документ-основание')
    def document_link(self, obj):
        if obj.document_id and obj.document.file:
            return format_html(
                '<a href="{}" target="_blank">{}</a>', obj.document.file.url, obj.document.document_number,
            )
        return '—'

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    inlines = [GuaranteedPrizePayoutInline]
    search_fields = ('email', 'phone', 'first_name', 'last_name')
    list_display = ('email', 'first_name', 'last_name', 'is_active', 'is_blocked', 'created_at')
    list_filter = ('is_blocked', 'is_active')


admin.site.register(KeywordProduct)
admin.site.register(ExcludingKeywordProduct)
admin.site.register(EmailVerificationCode)
admin.site.register(Raffle)
admin.site.register(Store)


@admin.register(OkiDokiTemplate)
class OkiDokiTemplateAdmin(admin.ModelAdmin):
    """Шаблон подбирается по полю «Для какой категории приза» — см.
    staff_panel.services.oki_templates. На каждую категорию нужен ровно один
    активный шаблон, поэтому категория вынесена в список и фильтры."""

    list_display = ('name', 'kind', 'oki_template_id', 'is_active')
    list_filter = ('kind', 'is_active')
    list_editable = ('kind', 'is_active')
    search_fields = ('name', 'oki_template_id')


@admin.register(UTMVisit)
class UTMVisitAdmin(admin.ModelAdmin):
    list_display = ('utm_medium_display', 'created_at')
    list_filter = ('utm_medium', 'created_at')
    readonly_fields = ('utm_medium', 'created_at')
    ordering = ('-created_at',)
    search_fields = ('utm_medium',)

    @admin.display(description='utm_medium')
    def utm_medium_display(self, obj):
        return obj.utm_medium or '(без метки)'


class GuaranteedPrizePayoutEmailInline(admin.TabularInline):
    """История писем, отправленных по этой выплате (см. GuaranteedPrizePayoutEmail)."""

    model = GuaranteedPrizePayoutEmail
    extra = 0
    can_delete = False
    fields = ('subject', 'message', 'sent_at')
    readonly_fields = fields
    ordering = ('-sent_at',)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(GuaranteedPrizePayout)
class GuaranteedPrizePayoutAdmin(admin.ModelAdmin):
    list_display = ('participant', 'amount', 'status', 'sent_manually', 'cnt_retry', 'deal_id',
                     'registry', 'document_link', 'paid_email_sent', 'created_at', 'paid_at',
                     'refresh_status_action')
    list_filter = ('status', 'sent_manually', 'paid_email_sent', 'error_email_sent',
                   'error_bank_email_sent', 'registry')
    list_select_related = ('participant', 'document', 'registry')
    search_fields = ('participant__email', 'participant__phone', 'deal_id', 'bank_sbp_id')
    readonly_fields = ('created_at', 'sent_at', 'paid_at', 'updated_at', 'sent_manually',
                        'paid_email_sent', 'paid_email_panel', 'error_email_sent',
                        'error_email_panel', 'error_bank_email_sent', 'error_bank_email_panel',
                        'api_log', 'refresh_status_panel')
    fields = ('participant', 'amount', 'status', 'sent_manually', 'deal_id', 'document', 'registry',
              'bank_sbp_id', 'phone_number', 'cnt_retry', 'error_reason', 'refresh_status_panel',
              'created_at', 'sent_at', 'paid_at', 'updated_at',
              'paid_email_sent', 'paid_email_panel',
              'error_email_sent', 'error_email_panel',
              'error_bank_email_sent', 'error_bank_email_panel', 'api_log')
    inlines = [GuaranteedPrizePayoutEmailInline]
    ordering = ('-created_at',)
    actions = ('retry_payout', 'export_payout_registry')
    change_list_template = 'admin/promotion/guaranteedprizepayout/change_list.html'

    @admin.display(description='Документ')
    def document_link(self, obj):
        if obj.document_id and obj.document.file:
            return format_html(
                '<a href="{}" target="_blank">{}</a>', obj.document.file.url, obj.document.document_number,
            )
        return '—'

    @admin.display(description='Письмо «приз отправлен»')
    def paid_email_panel(self, obj):
        if obj is None or obj.pk is None:
            return '—'
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        url = reverse('admin:%s_%s_send_paid_email' % info, args=[obj.pk])
        if obj.paid_email_sent:
            last = obj.emails.order_by('-sent_at').first()
            sent_note = f' ({timezone.localtime(last.sent_at):%d.%m.%Y %H:%M})' if last else ''
            return format_html(
                '<span style="color:#15803d;font-weight:600;">✅ Отправлено{}</span> '
                '<a href="{}" onclick="return confirm(\'Отправить письмо повторно?\');">переотправить</a>',
                sent_note, url,
            )
        return format_html(
            '<a class="button" href="{}" '
            'onclick="return confirm(\'Отправить участнику письмо «Гарантированный приз отправлен»?\');">'
            'Отправить письмо</a>',
            url,
        )

    @admin.display(description='Письмо «ошибка отправки» (ФИО/банк)')
    def error_email_panel(self, obj):
        if obj is None or obj.pk is None:
            return '—'
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        url = reverse('admin:%s_%s_send_error_email' % info, args=[obj.pk])
        if obj.error_email_sent:
            last = obj.emails.order_by('-sent_at').first()
            sent_note = f' ({timezone.localtime(last.sent_at):%d.%m.%Y %H:%M})' if last else ''
            return format_html(
                '<span style="color:#15803d;font-weight:600;">✅ Отправлено{}</span> '
                '<a href="{}" onclick="return confirm(\'Отправить письмо повторно?\');">переотправить</a>',
                sent_note, url,
            )
        return format_html(
            '<a class="button" href="{}" '
            'onclick="return confirm(\'Отправить участнику письмо «Ошибка отправки гарантированного приза»?\');">'
            'Отправить письмо</a>',
            url,
        )

    @admin.display(description='Письмо «банк отклонил платёж» (не ФИО)')
    def error_bank_email_panel(self, obj):
        if obj is None or obj.pk is None:
            return '—'
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        url = reverse('admin:%s_%s_send_bank_error_email' % info, args=[obj.pk])
        if obj.error_bank_email_sent:
            last = obj.emails.order_by('-sent_at').first()
            sent_note = f' ({timezone.localtime(last.sent_at):%d.%m.%Y %H:%M})' if last else ''
            return format_html(
                '<span style="color:#15803d;font-weight:600;">✅ Отправлено{}</span> '
                '<a href="{}" onclick="return confirm(\'Отправить письмо повторно?\');">переотправить</a>',
                sent_note, url,
            )
        return format_html(
            '<a class="button" href="{}" '
            'onclick="return confirm(\'Отправить участнику письмо «Банк отклонил платёж»?\');">'
            'Отправить письмо</a>',
            url,
        )

    @admin.display(description='Статус в Cyclops')
    def refresh_status_panel(self, obj):
        if obj is None or obj.pk is None:
            return '—'
        if not obj.deal_id:
            return 'Сделка ещё не создана — обновлять нечего'
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        url = reverse('admin:%s_%s_refresh_status' % info, args=[obj.pk])
        return format_html('<a class="button" href="{}">Обновить данные</a>', url)

    @admin.display(description='')
    def refresh_status_action(self, obj):
        if not obj.deal_id:
            return '—'
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        url = reverse('admin:%s_%s_refresh_status' % info, args=[obj.pk])
        return format_html('<a class="button" href="{}">Обновить данные</a>', url)

    def get_urls(self):
        urls = super().get_urls()
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        custom = [
            path(
                '<path:object_id>/send-paid-email/',
                self.admin_site.admin_view(self.send_paid_email_view),
                name='%s_%s_send_paid_email' % info,
            ),
            path(
                '<path:object_id>/send-error-email/',
                self.admin_site.admin_view(self.send_error_email_view),
                name='%s_%s_send_error_email' % info,
            ),
            path(
                '<path:object_id>/send-bank-error-email/',
                self.admin_site.admin_view(self.send_bank_error_email_view),
                name='%s_%s_send_bank_error_email' % info,
            ),
            path(
                '<path:object_id>/refresh-status/',
                self.admin_site.admin_view(self.refresh_status_view),
                name='%s_%s_refresh_status' % info,
            ),
            path(
                'run-next-payouts/',
                self.admin_site.admin_view(self.run_next_payouts_view),
                name='%s_%s_run_next_payouts' % info,
            ),
        ]
        return custom + urls

    def refresh_status_view(self, request, object_id):
        """Ручной опрос текущего статуса сделки в Cyclops (get_deal) по кнопке
        «Обновить данные» — без ожидания плановой задачи poll_prize_deals."""
        from .services.cyclops import CyclopsAPIClient
        from .services.guaranteed_prize_payout import apply_deal_status

        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Выплата не найдена.', level='error')
            return HttpResponseRedirect(reverse('admin:promotion_guaranteedprizepayout_changelist'))

        redirect_to = HttpResponseRedirect(reverse('admin:promotion_guaranteedprizepayout_change', args=[obj.pk]))

        if not obj.deal_id:
            self.message_user(request, 'У выплаты ещё нет сделки (deal_id) — обновлять нечего.', level='warning')
            return redirect_to

        try:
            client = CyclopsAPIClient()
            resp = client.get_deal(obj.deal_id)
            obj.log(f'get_deal (ручное обновление): {resp}')
            obj.save(update_fields=['api_log', 'updated_at'])
            deal = (resp.get('result') or {}).get('deal')
            if deal:
                apply_deal_status(obj, deal, client=client)
                obj.refresh_from_db()
                self.message_user(request, f'Статус обновлён: {obj.get_status_display()}.')
            else:
                self.message_user(request, f'Сделка не найдена в Cyclops: {resp}', level='error')
        except Exception as e:  # noqa: BLE001 — показываем причину прямо в админке
            self.message_user(request, f'Ошибка обновления статуса: {e}', level='error')

        return redirect_to

    def send_paid_email_view(self, request, object_id):
        from .services.guaranteed_prize_email import send_guaranteed_prize_paid_email

        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Выплата не найдена.', level='error')
            return HttpResponseRedirect(reverse('admin:promotion_guaranteedprizepayout_changelist'))

        sent = send_guaranteed_prize_paid_email(obj.participant, obj, force=True)
        if sent:
            self.message_user(request, f'Письмо отправлено участнику {obj.participant}.')
        else:
            self.message_user(
                request,
                'Письмо не отправлено — проверьте email участника и шаблон письма '
                '(GuaranteedPrizeEmailTemplate, код «sent»).',
                level='error',
            )
        return HttpResponseRedirect(reverse('admin:promotion_guaranteedprizepayout_change', args=[obj.pk]))

    def send_error_email_view(self, request, object_id):
        from .services.guaranteed_prize_email import send_guaranteed_prize_error_email

        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Выплата не найдена.', level='error')
            return HttpResponseRedirect(reverse('admin:promotion_guaranteedprizepayout_changelist'))

        sent = send_guaranteed_prize_error_email(obj.participant, obj, force=True)
        if sent:
            self.message_user(request, f'Письмо отправлено участнику {obj.participant}.')
        else:
            self.message_user(
                request,
                'Письмо не отправлено — проверьте email участника и шаблон письма '
                '(GuaranteedPrizeEmailTemplate, код «error_fio»).',
                level='error',
            )
        return HttpResponseRedirect(reverse('admin:promotion_guaranteedprizepayout_change', args=[obj.pk]))

    def send_bank_error_email_view(self, request, object_id):
        from .services.guaranteed_prize_email import send_guaranteed_prize_bank_error_email

        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Выплата не найдена.', level='error')
            return HttpResponseRedirect(reverse('admin:promotion_guaranteedprizepayout_changelist'))

        sent = send_guaranteed_prize_bank_error_email(obj.participant, obj, force=True)
        if sent:
            self.message_user(request, f'Письмо отправлено участнику {obj.participant}.')
        else:
            self.message_user(
                request,
                'Письмо не отправлено — проверьте email участника и шаблон письма '
                '(GuaranteedPrizeEmailTemplate, код «error_bank_declined»).',
                level='error',
            )
        return HttpResponseRedirect(reverse('admin:promotion_guaranteedprizepayout_change', args=[obj.pk]))

    def run_next_payouts_view(self, request, *args, **kwargs):
        from .services.guaranteed_prize_payout import run_next_payouts

        if request.method != 'POST':
            return HttpResponseRedirect(reverse('admin:promotion_guaranteedprizepayout_changelist'))

        try:
            n = int(request.POST.get('n', 10))
        except (TypeError, ValueError):
            n = 10
        n = max(1, min(n, 100))

        result = run_next_payouts(limit=n)
        processed, blocked = result['processed'], result['blocked']

        if not processed and not blocked:
            self.message_user(request, 'Нет накопившихся выплат в статусе «Отложена».')
        else:
            paid = sum(1 for p in processed if p.status == GuaranteedPrizePayout.STATUS_PAID)
            executing = sum(1 for p in processed if p.status == GuaranteedPrizePayout.STATUS_EXECUTING)
            failed = sum(1 for p in processed
                        if p.status in (GuaranteedPrizePayout.STATUS_FAILED,
                                        GuaranteedPrizePayout.STATUS_RETRYABLE,
                                        GuaranteedPrizePayout.STATUS_RETRY_LIMIT))
            msg = (f'Обработано: {len(processed)} (отправлено в банк: {executing}, '
                   f'подтверждено: {paid}, ошибка: {failed}). Исключено (данные непригодны): {len(blocked)}.')
            level = 'warning' if failed or blocked else 'info'
            self.message_user(request, msg, level=level)

        return HttpResponseRedirect(reverse('admin:promotion_guaranteedprizepayout_changelist'))

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        extra_context['run_next_payouts_url'] = reverse('admin:%s_%s_run_next_payouts' % info)
        hold_qs = GuaranteedPrizePayout.objects.filter(status=GuaranteedPrizePayout.STATUS_HOLD)
        extra_context['hold_count'] = hold_qs.count()
        # «Провести N выплат» пропускает записи без отчества (см. run_next_payouts) —
        # показываем отдельно, сколько из отложенных реально готовы к отправке.
        extra_context['hold_ready_count'] = (
            hold_qs.exclude(participant__middle_name__isnull=True).exclude(participant__middle_name='').count()
        )
        return super().changelist_view(request, extra_context=extra_context)

    @admin.action(description='Запустить/повторить выплату (сбросить в retryable)')
    def retry_payout(self, request, queryset):
        from .tasks import pay_guaranteed_prize
        count = 0
        for payout in queryset.exclude(status=GuaranteedPrizePayout.STATUS_PAID):
            payout.status = GuaranteedPrizePayout.STATUS_RETRYABLE
            # Ручной запуск даёт выплате заново полный запас авто-попыток,
            # иначе запись, упёршаяся в лимит, сразу вернулась бы в него.
            payout.cnt_retry = 0
            payout.save(update_fields=['status', 'cnt_retry', 'updated_at'])
            pay_guaranteed_prize.delay(payout.participant_id)
            count += 1
        self.message_user(request, f'Запущена повторная выплата: {count}')

    @admin.action(description='Скачать Реестр по выбранным (Excel, только для просмотра)')
    def export_payout_registry(self, request, queryset):
        from django.http import HttpResponse

        from .services.cyclops_registry import generate_registry_xlsx

        payouts = list(queryset.select_related('participant').order_by('paid_at', 'created_at'))
        try:
            xlsx = generate_registry_xlsx(payouts, registry_number=f'EXPORT-{timezone.localdate():%Y%m%d}')
        except ValueError as e:
            self.message_user(request, str(e), level='error')
            return
        response = HttpResponse(
            xlsx, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        filename = f'Реестр_выплат_{timezone.localdate().strftime("%Y%m%d")}.xlsx'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


admin.site.register(CyclopsBeneficiary)
admin.site.register(CyclopsVirtualAccount)
admin.site.register(CyclopsPayment)
admin.site.register(CyclopsDocument)
admin.site.register(CyclopsSettings)
admin.site.register(WinnerReplacementSettings)


@admin.register(PayoutRegistry)
class PayoutRegistryAdmin(admin.ModelAdmin):
    list_display = ('registry_number', 'participant_count', 'file_link', 'created_at')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)

    @admin.display(description='Участников')
    def participant_count(self, obj):
        return obj.payouts.count()

    @admin.display(description='Файл')
    def file_link(self, obj):
        if obj.file:
            return format_html('<a href="{}" target="_blank">{}</a>', obj.file.url, obj.file.name.rsplit('/', 1)[-1])
        return '—'


@admin.register(SbpBank)
class SbpBankAdmin(admin.ModelAdmin):
    list_display = ('name_rus', 'name', 'bank_code', 'sbp_id', 'updated_at')
    search_fields = ('name_rus', 'name', 'bank_code', 'sbp_id')
    ordering = ('name_rus',)
    readonly_fields = ('updated_at',)


@admin.register(GuaranteedPrizeSent)
class GuaranteedPrizeSentAdmin(admin.ModelAdmin):
    list_display = ('participant', 'sent_at', 'receipt')
    list_select_related = ('participant', 'receipt')
    readonly_fields = ('participant', 'receipt', 'sent_at')
    ordering = ('-sent_at',)


@admin.register(GuaranteedPrizeEmailTemplate)
class GuaranteedPrizeEmailTemplateAdmin(admin.ModelAdmin):
    list_display = ('code', 'subject', 'updated_at')
    readonly_fields = ('code', 'updated_at')
    fields = ('code', 'subject', 'text', 'updated_at')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
@admin.register(SentEmail)
class SentEmailAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "participant",
        "receipt",
        "created_at",
    )

    list_filter = (
        "created_at",
        "receipt__participant",
    )

    search_fields = (
        "name",
        "message",
        "receipt__id",
        "receipt__participant__username",
        "receipt__participant__email",
        "receipt__participant__first_name",
        "receipt__participant__last_name",
    )

    autocomplete_fields = ("receipt",)

    ordering = ("-created_at",)

    @admin.display(description="Участник", ordering="receipt__participant")
    def participant(self, obj):
        return obj.receipt.participant

@admin.register(PrizeShippingSoonMessage)
class PrizeShippingSoonMessageAdmin(admin.ModelAdmin):
    list_display = ['subject', 'updated_at']
    fields = ['subject', 'text']

    def has_add_permission(self, request):
        return not PrizeShippingSoonMessage.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AccountBlockedMessage)
class AccountBlockedMessageAdmin(admin.ModelAdmin):
    list_display = ['updated_at']
    fields = ['text']

    def has_add_permission(self, request):
        return not AccountBlockedMessage.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class SentEmailInline(admin.TabularInline): 
    model = SentEmail
    extra = 0
    fields = ('name', 'message',)
    readonly_fields = ('created_at',)
    
    
class PrizeCountChangeInline(admin.TabularInline):
    model = PrizeCountChange
    extra = 0
    can_delete = False
    fields = ('changed_at', 'changed_by', 'old_count', 'new_count', 'reason')
    readonly_fields = ('changed_at', 'changed_by', 'old_count', 'new_count', 'reason')
    ordering = ('-changed_at',)

    def has_add_permission(self, request, obj=None):
        return False


class PrizeAdminForm(forms.ModelForm):
    class Meta:
        model = Prize
        fields = '__all__'

    def clean(self):
        cleaned_data = super().clean()
        is_main = cleaned_data.get('is_main')
        draw_period = cleaned_data.get('draw_period')

        if is_main:
            # Главный приз не относится ни к одной неделе/месяцу.
            cleaned_data['week'] = None
            cleaned_data['month'] = None
            return cleaned_data

        if draw_period == Prize.DrawPeriod.WEEKLY:
            if not cleaned_data.get('week'):
                self.add_error('week', 'Укажите номер недели для еженедельного приза.')
            cleaned_data['month'] = None
        elif draw_period == Prize.DrawPeriod.MONTHLY:
            if not cleaned_data.get('month'):
                self.add_error('month', 'Укажите номер месяца для ежемесячного приза.')
            cleaned_data['week'] = None

        return cleaned_data


@admin.register(Prize)
class PrizeAdmin(admin.ModelAdmin):
    form = PrizeAdminForm
    inlines = (PrizeCountChangeInline,)
    list_display = (
        'name', 'type_prize', 'is_main', 'is_electronic', 'draw_period', 'week', 'month',
        'count', 'cost', 'is_active', 'created_at',
    )
    list_filter = ('is_main', 'is_electronic', 'draw_period', 'is_active')
    search_fields = ('name', 'description', 'type_prize')
    ordering = ('-is_main', 'draw_period', 'week', 'month', 'name')
    fieldsets = (
        (None, {
            'fields': ('name', 'description', 'image', 'type_prize', 'is_electronic'),
        }),
        ('Розыгрыш', {
            'fields': ('is_main', 'draw_period', 'week', 'month'),
            'description': (
                'Для еженедельного приза укажите номер недели акции, для ежемесячного — номер месяца. '
                'Главный приз разыгрывается отдельно и эти поля не использует.'
            ),
        }),
        ('Количество и стоимость', {
            'fields': ('count', 'cost', 'ndfl', 'is_active'),
        }),
        ('Системное', {
            'fields': ('kind', 'created_at', 'updated_at'),
            'classes': ('collapse',),
            'description': (
                'Вид приза подставляется автоматически по названию при создании приза. '
                'Менять его нужно только чтобы исправить ошибку: вид определяет, из какого '
                'склада файлов электронных призов черпают победители.'
            ),
        }),
    )
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('kind',)


@admin.register(PrizeKind)
class PrizeKindAdmin(admin.ModelAdmin):
    """
    Виды призов заводятся сами по названию приза — сюда заходят, чтобы понять,
    какие призы делят общий склад файлов электронных призов.
    """

    list_display = ('name', 'prizes_count', 'files_count', 'created_at')
    search_fields = ('name',)
    readonly_fields = ('created_at',)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _prizes_count=Count('prizes', distinct=True),
            _files_count=Count('prize_files', distinct=True),
        )

    @admin.display(description='Призов', ordering='_prizes_count')
    def prizes_count(self, obj):
        return obj._prizes_count

    @admin.display(description='Файлов', ordering='_files_count')
    def files_count(self, obj):
        return obj._files_count


@admin.register(ReceiptMessageTemplate)
class ReceiptMessageTemplateAdmin(admin.ModelAdmin):
    list_display = ['code', 'text']
    readonly_fields = ['code']
    fields = ['code', 'text']
    
    
class MessageTemplateSelectWidget(forms.Select):
    """Select, который пробрасывает в шаблон JSON-словарь code -> text для JS."""

    def __init__(self, *args, templates: dict[str, str] | None = None, **kwargs):
        self.templates = templates or {}
        super().__init__(*args, **kwargs)

    def render(self, name, value, attrs=None, renderer=None):
        select_html = super().render(name, value, attrs, renderer)
        templates_json = _json.dumps(self.templates, ensure_ascii=False)
        script = f'''
<script>
window.RECEIPT_MESSAGE_TEMPLATES = {templates_json};

function applyMessageTemplate(selectEl) {{
    var code = selectEl.value;
    var messageField = document.getElementById('id_message');
    if (!code || !messageField) return;
    var text = window.RECEIPT_MESSAGE_TEMPLATES[code];
    if (text !== undefined) {{
        messageField.value = text;
    }}
}}

document.addEventListener('DOMContentLoaded', function() {{
    var messageField = document.getElementById('id_message');
    var selectField = document.getElementById('id_message_template');
    if (!messageField || !selectField) return;
    // если текущий текст message совпадает с одним из шаблонов — подсветим его в select
    var current = messageField.value;
    for (var code in window.RECEIPT_MESSAGE_TEMPLATES) {{
        if (window.RECEIPT_MESSAGE_TEMPLATES[code] === current) {{
            selectField.value = code;
            break;
        }}
    }}
}});
</script>
'''
        return mark_safe(str(select_html) + script)


class ReceiptAdminForm(forms.ModelForm):
    message_template = forms.ChoiceField(
        required=False,
        label='Шаблон сообщения',
        help_text='Выберите шаблон — текст подставится ниже, его можно отредактировать вручную.',
    )

    class Meta:
        model = Receipt
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        context = self._message_template_context()
        templates = {
            code: ReceiptMessage.get(code, **context)
            for code, _ in ReceiptMessageTemplate.Code.choices
        }
        choices = [('', '— не выбрано / свой текст —')] + [
            (code, label) for code, label in ReceiptMessageTemplate.Code.choices
        ]
        self.fields['message_template'].choices = choices
        self.fields['message_template'].widget = MessageTemplateSelectWidget(
            choices=choices,
            templates=templates,
            attrs={'onchange': 'applyMessageTemplate(this)'},
        )

    def _message_template_context(self) -> dict:
        """Реальные значения плейсхолдеров ({week_num}, {prize_name}) для превью в списке шаблонов."""
        receipt = self.instance

        week_num = receipt.week
        if not week_num:
            raffle = Raffle.objects.first()
            if raffle:
                week_num = week_num_on_date(raffle, timezone.localtime().date())

        prize_name = 'приз'
        if receipt.pk:
            draw_result = receipt.draw_results.select_related('prize').first()
            if draw_result and draw_result.prize:
                prize_name = draw_result.prize.name

        context = {'prize_name': prize_name}
        if week_num:
            context['week_num'] = week_num
        return context



@admin.register(Receipt)
class ReceiptAdmin(WinnerReceiptWorkflowMixin, admin.ModelAdmin):
    list_display = [
        'public_id',
        'status',
        'participant',
        'amount',
        'date',
        'fns_retry_pending',
        'fns_retry_count',
        'system_message',
        'moderated_at',
        'created_at',
    ]
    list_filter = ['status', 'is_participation', 'ai_recommendation']
    search_fields = ['public_id', 'participant__email', 'fn', 'fd', 'fp']
    readonly_fields = [
        'public_id', 'created_at', 'updated_at', 'items_preview', 'winner_workflow_panel',
        # Разбор нейросети правится только кодом: это протокол её работы, а не поле
        # для заметок модератора.
        'ai_recommendation', 'ai_review_note', 'ai_reviewed_at',
    ]
    inlines = [SentEmailInline]
    form = ReceiptAdminForm

    fieldsets = (
        (
            'Победитель',
            {
                'fields': ('winner_workflow_panel',),
                'description': (
                    'Доступно только для чеков со статусом «Победный». '
                    'Проверьте данные, выберите шаблон договора OkiDoki и отправьте письмо.'
                ),
            },
        ),
        (
            None,
            {
                'fields': (
                    'public_id',
                    'participant',
                    'is_participation',
                )
            },
        ),
        (
            'Чек',
            {
                'fields': (
                    'receipt_image',
                    'fn',
                    'fd',
                    'fp',
                    'amount',
                    'date',
                    'store',
                    'address',
                    'inn',
                    'qr_code_str',
                    'week',
                    'month',
                )
            },
        ),
        (
            'Статусы',
            {
                'fields': (
                    'status',
                    'message_template',
                    'message',
                    'system_message',
                    'retry_count',
                )
            }
        ),
        (
            'Документы',
            {
                'fields': (
                    'link_oki_document',
                    'status_oki_document',
                    'link_oki_document_admin',
                    'date_result_raffle',
                )
            },
        ),
        ('Товары', {'fields': ('items', 'items_preview'), 'classes': ('wide',)}),
        (
            'Нейросеть',
            {
                'fields': ('ai_recommendation', 'ai_reviewed_at', 'ai_review_note'),
                'description': (
                    'Разбор чека нейросетью. Пока включён теневой режим '
                    '(OPENROUTER_SHADOW_MODE), это только её мнение: на статус чека, '
                    'сообщение участнику и ключевые слова оно не влияет.'
                ),
            },
        ),
        (
            'Системное',
            {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)},
        ),
    )

    def save_model(self, request, obj, form, change):
        if change:
            old_obj = Receipt.objects.get(pk=obj.pk)

            if old_obj.status != obj.status and obj.status in (Receipt.Status.CONFIRMED, Receipt.Status.REJECTED):
                obj.moderated_at = timezone.now()

            if old_obj.status != Receipt.Status.CONFIRMED and obj.status == Receipt.Status.CONFIRMED:
                # Период — по дате регистрации чека участником, с учётом переноса
                # по п. 4.5 Правил (см. promo_calendar). Считается после
                # moderated_at: от него зависит перенос. Если модератор в этой же
                # форме выставил неделю/месяц вручную, его значение приоритетнее.
                manual = {'week', 'month'} & set(form.changed_data)
                week_before, month_before = obj.week, obj.month
                assign_promo_period(obj)
                if 'week' in manual:
                    obj.week = week_before
                if 'month' in manual:
                    obj.month = month_before

            super().save_model(request, obj, form, change)

            if old_obj.status != obj.status:
                if obj.status == Receipt.Status.CONFIRMED:
                    sync_promo_keywords_from_receipt(obj)
                    # Те же попытки в лотке яиц, что и при приёме чека роботом
                    # или модератором из панели (вызов идемпотентен).
                    grant_attempts_for_receipt(obj)
                from .services.receipt_status_email import send_receipt_confirmed_email, send_receipt_rejected_email
                if obj.status == Receipt.Status.CONFIRMED:
                    send_receipt_confirmed_email(obj)
                elif obj.status == Receipt.Status.REJECTED:
                    send_receipt_rejected_email(obj)
        else:
            super().save_model(request, obj, form, change)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                '<path:object_id>/save-promo-items/',
                self.admin_site.admin_view(self.save_promo_items_view),
                name='promotion_receipt_save_promo_items',
            ),
        ]
        return custom + urls

    def save_promo_items_view(self, request, object_id):
        """POST: сохраняет promo_items из чекбоксов в items_preview."""
        if request.method != 'POST':
            from django.http import HttpResponseNotAllowed
            return HttpResponseNotAllowed(['POST'])

        receipt = Receipt.objects.filter(pk=object_id).first()
        if not receipt:
            return JsonResponse({'ok': False, 'error': 'not found'}, status=404)

        try:
            body = _json.loads(request.body)
            promo_items = body.get('promo_items', [])
        except (_json.JSONDecodeError, Exception):
            return JsonResponse({'ok': False, 'error': 'invalid json'}, status=400)

        # Валидируем: каждый элемент должен быть словарём с полем name
        cleaned = [
            item for item in promo_items
            if isinstance(item, dict) and item.get('name')
        ]

        receipt.promo_items = cleaned
        receipt.save(update_fields=['promo_items', 'updated_at'])
        # Чек мог быть подтверждён раньше, чем отмечены акционные товары —
        # досинхронизируем ключевые слова, чтобы порядок действий не влиял на результат.
        if receipt.status == Receipt.Status.CONFIRMED:
            sync_promo_keywords_from_receipt(receipt)
        return JsonResponse({'ok': True, 'saved': len(cleaned)})

    def items_preview(self, obj):
        if not obj or not obj.items:
            return 'Нет товаров'

        raw_items = obj.items if isinstance(obj.items, list) else []
        if not raw_items:
            return 'Нет товаров'

        promo_names: set[str] = {
            (item.get('name') or '').strip().lower()
            for item in (obj.promo_items or [])
            if isinstance(item, dict) and item.get('name')
        }

        save_url = reverse('admin:promotion_receipt_save_promo_items', args=[obj.pk])

        rows = ''
        for i, item in enumerate(raw_items):
            price = item.get('price', 0) / 100
            qty = item.get('quantity', 0)
            total = item.get('sum', 0) / 100
            name = item.get('name', '')
            is_promo = name.strip().lower() in promo_names
            bg = '#f9f9f9' if i % 2 == 0 else 'white'
            checked = 'checked' if is_promo else ''

            rows += (
                f'<tr style="background:{bg};">'
                f'<td style="padding:4px;border:1px solid #ddd;text-align:center;">'
                f'  <input type="checkbox" class="promo-item-cb" {checked} data-idx="{i}">'
                f'</td>'
                f'<td style="padding:4px;border:1px solid #ddd;text-align:center;">{i + 1}</td>'
                f'<td style="padding:4px;border:1px solid #ddd;">{name[:60]}</td>'
                f'<td style="padding:4px;border:1px solid #ddd;text-align:center;">{qty}</td>'
                f'<td style="padding:4px;border:1px solid #ddd;text-align:right;">{price:.2f}</td>'
                f'<td style="padding:4px;border:1px solid #ddd;text-align:right;">{total:.2f}</td>'
                f'</tr>'
            )

        # Весь массив items сериализуем один раз в <script>, не в data-атрибут
        items_js = _json.dumps(raw_items, ensure_ascii=False)

        html = (
            '<div id="promo-items-wrap">'
            '<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            '<thead><tr style="background:#2c3e50;color:white;">'
            '<th style="padding:6px;border:1px solid #ddd;width:36px;" title="Акционный товар">☑</th>'
            '<th style="padding:6px;border:1px solid #ddd;">#</th>'
            '<th style="padding:6px;border:1px solid #ddd;">Название</th>'
            '<th style="padding:6px;border:1px solid #ddd;">Кол-во</th>'
            '<th style="padding:6px;border:1px solid #ddd;">Цена</th>'
            '<th style="padding:6px;border:1px solid #ddd;">Сумма</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody>'
            '</table>'
            f'<div style="margin-top:10px;">'
            f'<button type="button" class="button" onclick="savePromoItems(\'{save_url}\')">'
            f'💾 Сохранить акционные товары</button>'
            f'<span id="promo-save-status" style="margin-left:12px;font-size:13px;"></span>'
            f'</div>'
            f'</div>'
            f'<script>'
            f'var RECEIPT_ALL_ITEMS = {items_js};'  # массив доступен глобально
            f'function savePromoItems(url) {{'
            f'  var checkboxes = document.querySelectorAll(".promo-item-cb");'
            f'  var promoItems = [];'
            f'  checkboxes.forEach(function(cb) {{'
            f'    if (cb.checked) {{'
            f'      var idx = parseInt(cb.getAttribute("data-idx"), 10);'
            f'      if (!isNaN(idx) && RECEIPT_ALL_ITEMS[idx]) {{'
            f'        promoItems.push(RECEIPT_ALL_ITEMS[idx]);'
            f'      }}'
            f'    }}'
            f'  }});'
            f'  var status = document.getElementById("promo-save-status");'
            f'  status.textContent = "Сохранение...";'
            f'  fetch(url, {{'
            f'    method: "POST",'
            f'    headers: {{"Content-Type": "application/json", "X-CSRFToken": getCookie("csrftoken")}},'
            f'    body: JSON.stringify({{promo_items: promoItems}})'
            f'  }})'
            f'  .then(function(r) {{ return r.json(); }})'
            f'  .then(function(data) {{'
            f'    status.style.color = data.ok ? "green" : "red";'
            f'    status.textContent = data.ok'
            f'      ? "✅ Сохранено: " + data.saved + " позиций"'
            f'      : "❌ Ошибка: " + (data.error || "");'
            f'  }})'
            f'  .catch(function() {{'
            f'    status.style.color = "red";'
            f'    status.textContent = "❌ Ошибка сети";'
            f'  }});'
            f'}}'
            f'function getCookie(name) {{'
            f'  var v = document.cookie.match("(^|;) ?" + name + "=([^;]*)(;|$)");'
            f'  return v ? v[2] : "";'
            f'}}'
            f'</script>'
        )
        return mark_safe(html)


@admin.register(ControlPayout)
class ControlPayoutAdmin(admin.ModelAdmin):
    """Контрольные выплаты по готовому реестру — только просмотр.

    Записи заводит команда cyclops_control_payout, статусы обновляет
    cyclops_control_payout_status. Руками их менять незачем, а случайная
    правка исказила бы историю реальных денежных операций.
    """

    list_display = ('fio', 'amount', 'bank_name', 'status', 'deal_id', 'batch', 'created_at')
    list_filter = ('status', 'batch', 'bank_name')
    search_fields = ('last_name', 'first_name', 'middle_name', 'phone', 'deal_id', 'batch')
    date_hierarchy = 'created_at'
    readonly_fields = [f.name for f in ControlPayout._meta.fields] + ['fio']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WinnerReplacement)
class WinnerReplacementAdmin(admin.ModelAdmin):
    """История замен победителей — только просмотр.

    Записи создаёт дашборд (staff_panel.services.winner_replacement); правка
    руками исказила бы историю того, кто и когда получил приз.
    """

    list_display = (
        'created_at', 'order', 'mode', 'old_participant_label', 'new_participant_label',
        'was_eligible', 'created_by',
    )
    list_filter = ('mode', 'was_eligible')
    search_fields = ('old_participant_label', 'new_participant_label', 'reason')
    date_hierarchy = 'created_at'
    readonly_fields = [f.name for f in WinnerReplacement._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ============================================================================ #
# Моментальные призы
# ============================================================================ #
@admin.register(InstantPrizeSettings)
class InstantPrizeSettingsAdmin(admin.ModelAdmin):
    """Синглтон-настройки лотка яиц."""

    def has_add_permission(self, request):
        # Настройки одни на всю Акцию — вторую строку заводить нечем и незачем.
        return not InstantPrizeSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InstantMoment)
class InstantMomentAdmin(admin.ModelAdmin):
    """
    Расписание призовых моментов — только для чтения.

    Моменты создаёт генератор (кнопка «Пересобрать расписание» в панели или
    команда generate_instant_moments): он следит и за равномерностью по дням, и
    за тем, чтобы их было ровно столько, сколько единиц приза заведено. Ручная
    правка обе гарантии ломает, поэтому добавление и изменение здесь запрещены.
    """

    list_display = ('scheduled_at', 'prize', 'week', 'is_claimed', 'claimed_at', 'participant')
    list_filter = ('is_claimed', 'week', 'prize')
    date_hierarchy = 'scheduled_at'
    search_fields = ('participant__email',)
    readonly_fields = [f.name for f in InstantMoment._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(InstantAttempt)
class InstantAttemptAdmin(admin.ModelAdmin):
    """Попытки участников — только для чтения: их начисляет приём чека."""

    list_display = ('created_at', 'participant', 'receipt', 'played_at', 'chosen_egg', 'is_win')
    list_filter = ('is_win', 'played_at')
    date_hierarchy = 'created_at'
    search_fields = ('participant__email',)
    readonly_fields = [f.name for f in InstantAttempt._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
