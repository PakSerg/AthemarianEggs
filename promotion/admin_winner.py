"""Админка: ручные этапы OkiDoki и письма победителю."""

from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.safestring import mark_safe

from .models import (
    OkiDokiTemplate,
    PromotionDrawResult,
    PromotionDrawResultMainRaffle,
    Receipt,
)
from .services.winner_workflow import (
    get_oki_preview_for_main,
    issue_oki_for_main,
    issue_oki_for_receipt,
    send_prize_shipping_soon_email_for_receipt,
    send_winner_email_for_main,
    send_winner_email_for_receipt,
    shipping_soon_email_sent,
)
from .services.publish_winners import publish_draw_results


# ---------------------------------------------------------------------------
# Стили панели (единая тема для всех панелей в этом файле)
# ---------------------------------------------------------------------------

WWF_STYLE = """
<style>
.wwf-wrap{max-width:620px;font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;}
.wwf-step{display:flex;gap:14px;margin-bottom:14px;align-items:stretch;}
.wwf-step.locked{opacity:.45;}
.wwf-circle{flex-shrink:0;width:30px;height:30px;border-radius:50%;background:#417690;
  color:#fff;display:flex;align-items:center;justify-content:center;
  font-weight:600;font-size:13px;}
.wwf-step.locked .wwf-circle{background:#9ca3af;}
.wwf-step.done .wwf-circle{background:#00a03c;}
.wwf-card{border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;flex:1;background:#fff;}
.wwf-title{font-size:13px;font-weight:600;color:#417690;letter-spacing:.2px;}
.wwf-body{margin-top:6px;font-size:13px;color:#374151;line-height:1.5;}
.wwf-btn{display:inline-block;padding:7px 14px;background:#417690;color:#fff !important;
  border:none;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;
  text-decoration:none;transition:background .15s ease;}
.wwf-btn:hover{background:#29617d;}
.wwf-btn.is-disabled{background:#cbd5e1;color:#64748b !important;cursor:not-allowed;pointer-events:none;}
.wwf-ok{color:#15803d;font-weight:600;}
.wwf-muted{color:#94a3b8;}
.wwf-link{color:#417690;text-decoration:underline;font-size:12px;margin-left:8px;}
</style>
"""


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _email_sent_for_receipt(receipt: Receipt) -> bool:
    """Письмо с договором отправлено (флаг is_send_email)."""
    return bool(receipt.is_send_email)


def _oki_issued(obj) -> bool:
    """Договор создан, если заполнено хотя бы одно из полей ссылки."""
    admin_link = (getattr(obj, 'link_oki_document_admin', None) or '').strip()
    participant_link = (getattr(obj, 'link_oki_document', None) or '').strip()
    return bool(admin_link or participant_link)


# ---------------------------------------------------------------------------
# HTML-панель: четыре шага обработки победителя (страница чека)
# ---------------------------------------------------------------------------

def _step_html(num: int, title: str, body: str, *, locked: bool = False, done: bool = False) -> str:
    state = 'locked' if locked else ('done' if done else '')
    return (
        f'<div class="wwf-step {state}">'
        f'  <div class="wwf-circle">{"✓" if done else num}</div>'
        f'  <div class="wwf-card">'
        f'    <div class="wwf-title">{title}</div>'
        f'    <div class="wwf-body">{body}</div>'
        f'  </div>'
        f'</div>'
    )


def _build_winner_steps_html(
    *,
    is_published: bool,
    published_at,
    oki_issued: bool,
    oki_status: str,
    oki_link_admin: str,
    oki_link_participant: str,
    issue_url: str,
    email_sent: bool,
    email_url: str,
    shipping_sent: bool,
    shipping_url: str,
) -> str:
    """Четыре вертикальных шага обработки победителя на странице чека."""

    # --- Шаг 1: Публикация (выполняется в разделе «Итоги розыгрышей», здесь только статус) ---
    if is_published:
        step1_body = f'<span class="wwf-ok">✅ Опубликован{f": {published_at}" if published_at else ""}</span>'
    else:
        step1_body = (
            '<span class="wwf-muted">Не опубликован. Публикация выполняется в разделе '
            '«Итоги розыгрышей» — после неё чек автоматически получает статус «Победный».</span>'
        )

    # --- Шаг 2: Договор OkiDoki ---
    step2_locked = not is_published
    if oki_issued:
        links = ''
        if oki_link_admin:
            links += f' <a class="wwf-link" href="{oki_link_admin}" target="_blank">заказчик ↗</a>'
        if oki_link_participant:
            links += f' <a class="wwf-link" href="{oki_link_participant}" target="_blank">участник ↗</a>'
        step2_body = f'<span class="wwf-ok">✅ Договор создан: {oki_status or "создан"}</span>{links}'
    elif step2_locked:
        step2_body = '<span class="wwf-muted">Сначала опубликуйте победителя.</span>'
    else:
        step2_body = f'<a class="wwf-btn" href="{issue_url}">Выбрать шаблон и создать договор</a>'

    # --- Шаг 3: письмо с договором ---
    step3_locked = not oki_issued
    if email_sent:
        step3_body = '<span class="wwf-ok">✅ Письмо с договором отправлено.</span>'
    elif step3_locked:
        step3_body = '<span class="wwf-muted">Сначала создайте договор OkiDoki.</span>'
    else:
        step3_body = (
            f'<a class="wwf-btn" href="{email_url}" '
            f'onclick="return confirm(\'Отправить победителю письмо с договором?\');">'
            f'Отправить победителю email с договором</a>'
        )

    # --- Шаг 4: письмо «приз скоро будет отправлен» — только если договор подписан ---
    is_signed = (oki_status or '').strip() == 'Подписан'
    step4_locked = not (email_sent and is_signed)
    if shipping_sent:
        step4_body = '<span class="wwf-ok">✅ Письмо о скорой отправке приза отправлено.</span>'
    elif step4_locked:
        if not email_sent:
            reason = 'Сначала отправьте победителю email с договором.'
        elif not is_signed:
            reason = f'Договор должен быть в статусе «Подписан» (сейчас: {oki_status or "—"}).'
        else:
            reason = 'Недоступно.'
        step4_body = f'<span class="wwf-muted">{reason}</span>'
    else:
        step4_body = (
            f'<a class="wwf-btn" href="{shipping_url}" '
            f'onclick="return confirm(\'Отправить письмо о том, что приз скоро будет отправлен?\');">'
            f'Отправить письмо о том, что приз скоро будет отправлен</a>'
        )

    return (
        WWF_STYLE
        + '<div class="wwf-wrap">'
        + _step_html(1, 'Публикация победителя', step1_body, done=is_published)
        + _step_html(2, 'Договор OkiDoki', step2_body, locked=step2_locked, done=oki_issued)
        + _step_html(3, 'Email с договором', step3_body, locked=step3_locked, done=email_sent)
        + _step_html(4, 'Приз скоро будет отправлен', step4_body, locked=step4_locked, done=shipping_sent)
        + '</div>'
    )


# ---------------------------------------------------------------------------
# Страница выбора шаблона OkiDoki
# ---------------------------------------------------------------------------

def _render_template_select(request, admin_instance, *, post_action_url, back_url, title):
    """Страница выбора шаблона OkiDoki перед созданием договора."""
    templates = OkiDokiTemplate.objects.filter(is_active=True)
    context = {
        **admin_instance.admin_site.each_context(request),
        'opts': admin_instance.model._meta,
        'templates': templates,
        'form_action': post_action_url,
        'back_url': back_url,
        'title': title,
    }
    return render(request, 'admin/promotion/receipt/select_oki_template.html', context)


# ---------------------------------------------------------------------------
# Mixin для ReceiptAdmin
# ---------------------------------------------------------------------------

class WinnerReceiptWorkflowMixin:
    """Панель управления победителем на странице чека."""

    def change_view(self, request, object_id, form_url='', extra_context=None):
        self._current_request = request
        return super().change_view(request, object_id, form_url, extra_context)

    def get_urls(self):
        urls = super().get_urls()
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        custom = [
            path(
                '<path:object_id>/oki-template/',
                self.admin_site.admin_view(self.select_oki_template_view),
                name='%s_%s_select_oki_template' % info,
            ),
            path(
                '<path:object_id>/issue-oki/',
                self.admin_site.admin_view(self.issue_oki_view),
                name='%s_%s_issue_oki' % info,
            ),
            path(
                '<path:object_id>/send-winner-email/',
                self.admin_site.admin_view(self.send_winner_email_view),
                name='%s_%s_send_winner_email' % info,
            ),
            path(
                '<path:object_id>/send-shipping-soon-email/',
                self.admin_site.admin_view(self.send_shipping_soon_email_view),
                name='%s_%s_send_shipping_soon_email' % info,
            ),
        ]
        return custom + urls

    # --- Шаг 2: Выбор шаблона (GET) → создание договора (POST) ---

    def select_oki_template_view(self, request, object_id):
        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Объект не найден.', level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:promotion_receipt_changelist'))

        opts = self.model._meta
        info = opts.app_label, opts.model_name
        post_url = reverse('admin:%s_%s_issue_oki' % info, args=[object_id])
        back_url = reverse('admin:promotion_receipt_change', args=[object_id])

        return _render_template_select(
            request, self,
            post_action_url=post_url,
            back_url=back_url,
            title='Выберите шаблон договора OkiDoki',
        )

    def issue_oki_view(self, request, object_id):
        if request.method != 'POST':
            opts = self.model._meta
            info = opts.app_label, opts.model_name
            return HttpResponseRedirect(
                reverse('admin:%s_%s_select_oki_template' % info, args=[object_id])
            )

        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Объект не найден.', level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:promotion_receipt_changelist'))

        template_id = request.POST.get('template_id')
        oki_template = None
        if template_id:
            try:
                oki_template = OkiDokiTemplate.objects.get(pk=template_id, is_active=True)
            except OkiDokiTemplate.DoesNotExist:
                pass

        if not oki_template:
            self.message_user(request, 'Шаблон не выбран или не найден.', level=messages.ERROR)
            opts = self.model._meta
            info = opts.app_label, opts.model_name
            return HttpResponseRedirect(
                reverse('admin:%s_%s_select_oki_template' % info, args=[object_id])
            )

        result = issue_oki_for_receipt(obj, oki_template=oki_template)
        self.message_user(
            request, result.message, level=messages.SUCCESS if result.ok else messages.ERROR,
        )
        return HttpResponseRedirect(reverse('admin:promotion_receipt_change', args=[obj.pk]))

    # --- Шаг 3: Email с договором ---

    def send_winner_email_view(self, request, object_id):
        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Объект не найден.', level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:promotion_receipt_changelist'))

        result = send_winner_email_for_receipt(obj)
        self.message_user(
            request, result.message, level=messages.SUCCESS if result.ok else messages.ERROR,
        )
        return HttpResponseRedirect(reverse('admin:promotion_receipt_change', args=[obj.pk]))

    # --- Шаг 4: «Приз скоро будет отправлен» ---

    def send_shipping_soon_email_view(self, request, object_id):
        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Объект не найден.', level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:promotion_receipt_changelist'))

        result = send_prize_shipping_soon_email_for_receipt(obj)
        self.message_user(
            request, result.message, level=messages.SUCCESS if result.ok else messages.ERROR,
        )
        return HttpResponseRedirect(reverse('admin:promotion_receipt_change', args=[obj.pk]))

    # --- Readonly-поле для fieldset ---

    @admin.display(description='Обработка победителя')
    def winner_workflow_panel(self, obj):
        if not obj or obj.status != Receipt.Status.WINNER:
            return '—'

        opts = self.model._meta
        info = opts.app_label, opts.model_name

        draw_result = PromotionDrawResult.objects.filter(receipt=obj, is_reserve=False).first()
        is_published = bool(draw_result and draw_result.is_published)
        published_at = draw_result.published_at if draw_result else None

        issue_url = reverse('admin:%s_%s_select_oki_template' % info, args=[obj.pk])
        email_url = reverse('admin:%s_%s_send_winner_email' % info, args=[obj.pk])
        shipping_url = reverse('admin:%s_%s_send_shipping_soon_email' % info, args=[obj.pk])

        html = _build_winner_steps_html(
            is_published=is_published,
            published_at=published_at,
            oki_issued=_oki_issued(obj),
            oki_status=(obj.status_oki_document or '').strip(),
            oki_link_admin=(obj.link_oki_document_admin or '').strip(),
            oki_link_participant=(obj.link_oki_document or '').strip(),
            issue_url=issue_url,
            email_sent=_email_sent_for_receipt(obj),
            email_url=email_url,
            shipping_sent=shipping_soon_email_sent(obj),
            shipping_url=shipping_url,
        )
        return mark_safe(html)

    def _winner_readonly(self, readonly_fields):
        fields = list(readonly_fields)
        if 'winner_workflow_panel' not in fields:
            fields.append('winner_workflow_panel')
        return fields


# ---------------------------------------------------------------------------
# PromotionDrawResultMainRaffleAdmin (главный розыгрыш — без шага публикации)
# ---------------------------------------------------------------------------

@admin.register(PromotionDrawResultMainRaffle)
class PromotionDrawResultMainRaffleAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'participant', 'prize', 'receipt', 'is_reserve', 'reserve_rank',
        'status_oki_document', 'is_send_email', 'created_at',
    )
    list_filter = ('is_reserve', 'is_send_email', 'prize', 'created_at')
    search_fields = (
        'participant__email', 'participant__first_name', 'participant__last_name', 'public_id',
    )
    list_select_related = ('participant', 'prize', 'receipt')
    raw_id_fields = ('participant', 'prize', 'receipt')
    readonly_fields = ('public_id', 'created_at', 'is_reserve', 'reserve_rank', 'winner_workflow_panel')

    fieldsets = (
        ('Обработка победителя', {
            'fields': ('winner_workflow_panel',),
            'description': (
                'Проверьте шаблон OkiDoki и поля, выставьте договор, '
                'затем отправьте письмо победителю. '
                'Резервные победители (см. «Резервный победитель» ниже) не обрабатываются — '
                'они используются только для замены основного победителя вручную.'
            ),
        }),
        ('Победитель', {
            'fields': (
                'public_id', 'participant', 'prize', 'receipt', 'created_at',
                ('is_reserve', 'reserve_rank'),
            ),
        }),
        ('Документы OkiDoki', {
            'fields': (
                'link_oki_document', 'status_oki_document', 'link_oki_document_admin', 'is_send_email',
            ),
        }),
    )

    def get_urls(self):
        urls = super().get_urls()
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        custom = [
            path(
                '<path:object_id>/oki-template/',
                self.admin_site.admin_view(self.select_oki_template_view),
                name='%s_%s_select_oki_template' % info,
            ),
            path(
                '<path:object_id>/issue-oki/',
                self.admin_site.admin_view(self.issue_oki_main_view),
                name='%s_%s_issue_oki' % info,
            ),
            path(
                '<path:object_id>/send-winner-email/',
                self.admin_site.admin_view(self.send_winner_email_main_view),
                name='%s_%s_send_winner_email' % info,
            ),
        ]
        return custom + urls

    def select_oki_template_view(self, request, object_id):
        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Объект не найден.', level=messages.ERROR)
            return HttpResponseRedirect(
                reverse('admin:promotion_promotiondrawresultmainraffle_changelist')
            )
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        post_url = reverse('admin:%s_%s_issue_oki' % info, args=[object_id])
        back_url = reverse('admin:promotion_promotiondrawresultmainraffle_change', args=[object_id])
        return _render_template_select(
            request, self,
            post_action_url=post_url,
            back_url=back_url,
            title='Выберите шаблон договора OkiDoki (главный розыгрыш)',
        )

    def issue_oki_main_view(self, request, object_id):
        if request.method != 'POST':
            opts = self.model._meta
            info = opts.app_label, opts.model_name
            return HttpResponseRedirect(
                reverse('admin:%s_%s_select_oki_template' % info, args=[object_id])
            )

        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Объект не найден.', level=messages.ERROR)
            return HttpResponseRedirect(
                reverse('admin:promotion_promotiondrawresultmainraffle_changelist')
            )

        template_id = request.POST.get('template_id')
        oki_template = None
        if template_id:
            try:
                oki_template = OkiDokiTemplate.objects.get(pk=template_id, is_active=True)
            except OkiDokiTemplate.DoesNotExist:
                pass

        if not oki_template:
            self.message_user(request, 'Шаблон не выбран или не найден.', level=messages.ERROR)
            opts = self.model._meta
            info = opts.app_label, opts.model_name
            return HttpResponseRedirect(
                reverse('admin:%s_%s_select_oki_template' % info, args=[object_id])
            )

        result = issue_oki_for_main(obj, oki_template=oki_template)
        self.message_user(
            request, result.message, level=messages.SUCCESS if result.ok else messages.ERROR,
        )
        return HttpResponseRedirect(
            reverse('admin:promotion_promotiondrawresultmainraffle_change', args=[obj.pk])
        )

    def send_winner_email_main_view(self, request, object_id):
        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Объект не найден.', level=messages.ERROR)
            return HttpResponseRedirect(
                reverse('admin:promotion_promotiondrawresultmainraffle_changelist')
            )

        result = send_winner_email_for_main(obj)
        if result.ok:
            PromotionDrawResultMainRaffle.objects.filter(pk=obj.pk).update(is_send_email=True)
        self.message_user(
            request, result.message, level=messages.SUCCESS if result.ok else messages.ERROR,
        )
        return HttpResponseRedirect(
            reverse('admin:promotion_promotiondrawresultmainraffle_change', args=[obj.pk])
        )

    @admin.display(description='Обработка победителя')
    def winner_workflow_panel(self, obj):
        if not obj:
            return '—'

        opts = self.model._meta
        info = opts.app_label, opts.model_name
        issue_url = reverse('admin:%s_%s_select_oki_template' % info, args=[obj.pk])
        email_url = reverse('admin:%s_%s_send_winner_email' % info, args=[obj.pk])

        oki_issued = _oki_issued(obj)
        oki_link_admin = (obj.link_oki_document_admin or '').strip()
        oki_link_participant = (obj.link_oki_document or '').strip()
        oki_status = (obj.status_oki_document or '').strip()
        email_sent = bool(obj.is_send_email)

        if oki_issued:
            links = ''
            if oki_link_admin:
                links += f' <a class="wwf-link" href="{oki_link_admin}" target="_blank">заказчик ↗</a>'
            if oki_link_participant:
                links += f' <a class="wwf-link" href="{oki_link_participant}" target="_blank">участник ↗</a>'
            step_oki_body = f'<span class="wwf-ok">✅ Договор создан: {oki_status or "создан"}</span>{links}'
        else:
            step_oki_body = f'<a class="wwf-btn" href="{issue_url}">Выбрать шаблон и создать договор</a>'

        step2_locked = not oki_issued
        if email_sent:
            step_email_body = '<span class="wwf-ok">✅ Письмо отправлено.</span>'
        elif step2_locked:
            step_email_body = '<span class="wwf-muted">Сначала создайте договор OkiDoki.</span>'
        else:
            step_email_body = (
                f'<a class="wwf-btn" href="{email_url}" '
                f'onclick="return confirm(\'Отправить письмо победителю?\');">'
                f'Отправить письмо победителю</a>'
            )

        html = (
            WWF_STYLE
            + '<div class="wwf-wrap">'
            + _step_html(1, 'Договор OkiDoki', step_oki_body, done=oki_issued)
            + _step_html(2, 'Письмо победителю', step_email_body, locked=step2_locked, done=email_sent)
            + '</div>'
        )
        return mark_safe(html)


# ---------------------------------------------------------------------------
# PromotionDrawResultAdmin
# ---------------------------------------------------------------------------

@admin.register(PromotionDrawResult)
class PromotionDrawResultAdmin(admin.ModelAdmin):
    change_list_template = 'admin/promotion/promotiondrawresult/change_list.html'
    list_display = (
        'id', 'week_num', 'month_num', 'is_published', 'is_reserve', 'reserve_rank',
        'participant', 'prize', 'receipt', 'receipt_status_display', 'created_at', 'published_at',
    )
    list_filter = ('week_num', 'month_num', 'is_published', 'is_reserve', 'prize', 'created_at')
    search_fields = (
        'participant__email', 'participant__first_name', 'participant__last_name', 'receipt__public_id',
    )
    list_select_related = ('participant', 'prize', 'receipt')
    raw_id_fields = ('participant', 'prize', 'receipt')
    # is_published — только через кнопку «Опубликовать победителя» (publish_draw_results),
    # прямое редактирование запрещено, иначе чек не получит статус/сообщение/дату.
    # is_reserve/reserve_rank — проставляются только розыгрышем, вручную не редактируются.
    readonly_fields = (
        'created_at',
        'published_at',
        'is_published',
        'is_reserve',
        'reserve_rank',
        'winner_status_panel',
    )
    actions = ('publish_selected_winners',)

    fieldsets = (
        (None, {
            'fields': (
                'participant', 'prize', 'receipt', 'week_num', 'month_num',
                ('is_published', 'published_at'),
                ('is_reserve', 'reserve_rank'),
                'created_at', 'winner_status_panel',
            ),
            'description': (
                'После розыгрыша победители создаются неопубликованными. '
                'Нажмите «Опубликовать победителя» — это установит статус и сообщение чека '
                'и откроет карточку чека для дальнейшей обработки. '
                'Резервные победители (Правила, п. 8.4) публикации не подлежат — используйте их '
                'для замены основного победителя через «Заменить победителя», если тот лишён приза.'
            ),
        }),
    )

    def get_urls(self):
        urls = super().get_urls()
        opts = self.model._meta
        info = opts.app_label, opts.model_name
        custom = [
            path(
                'publish-week/',
                self.admin_site.admin_view(self.publish_week_view),
                name='%s_%s_publish_week' % info,
            ),
            path(
                '<path:object_id>/publish/',
                self.admin_site.admin_view(self.publish_one_view),
                name='%s_%s_publish' % info,
            ),
            path(
                '<path:object_id>/replace-winner/',
                self.admin_site.admin_view(self.replace_winner_view),
                name='%s_%s_replace_winner' % info,
            ),
        ]
        return custom + urls

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        week_num = request.GET.get('week_num__exact')
        if week_num:
            try:
                week_num_int = int(week_num)
                extra_context['publish_week_url'] = (
                    reverse('admin:promotion_promotiondrawresult_publish_week')
                    + f'?week_num={week_num_int}'
                )
                extra_context['publish_week_num'] = week_num_int
                extra_context['publish_week_hint'] = (
                    f'Отфильтрована неделя {week_num_int}. '
                    'Кнопка «Опубликовать неделю» опубликует всех неопубликованных '
                    'победителей этой недели.'
                )
            except (TypeError, ValueError):
                pass
        else:
            extra_context['publish_week_hint'] = (
                'Выберите неделю в фильтре справа, чтобы опубликовать всех победителей '
                'за неделю одной кнопкой.'
            )
        return super().changelist_view(request, extra_context=extra_context)

    def publish_week_view(self, request):
        week_num_raw = request.GET.get('week_num')
        if not week_num_raw:
            self.message_user(request, 'Укажите неделю (week_num).', level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:promotion_promotiondrawresult_changelist'))
        try:
            week_num = int(week_num_raw)
        except (TypeError, ValueError):
            self.message_user(request, 'Некорректный номер недели.', level=messages.ERROR)
            return HttpResponseRedirect(reverse('admin:promotion_promotiondrawresult_changelist'))

        published, errors = publish_draw_results(week_num=week_num)
        if published:
            self.message_user(request, f'Опубликовано победителей: {published}.', level=messages.SUCCESS)
        else:
            self.message_user(
                request, 'Нет неопубликованных победителей для этой недели.', level=messages.WARNING,
            )
        for error in errors[:5]:
            self.message_user(request, error, level=messages.ERROR)

        return HttpResponseRedirect(
            reverse('admin:promotion_promotiondrawresult_changelist') + f'?week_num__exact={week_num}'
        )

    def publish_one_view(self, request, object_id):
        from django.http import JsonResponse, HttpResponseNotAllowed

        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])

        obj = self.get_object(request, object_id)
        if obj is None:
            self.message_user(request, 'Объект не найден.', level=messages.ERROR)
            return JsonResponse({
                'ok': False,
                'message': 'Объект не найден.',
                'redirect_url': reverse('admin:promotion_promotiondrawresult_changelist'),
            })

        if obj.is_published:
            self.message_user(request, 'Победитель уже опубликован.', level=messages.WARNING)
            return JsonResponse({
                'ok': False,
                'message': 'Победитель уже опубликован.',
                'redirect_url': reverse('admin:promotion_promotiondrawresult_change', args=[obj.pk]),
            })

        published, errors = publish_draw_results(ids=[obj.pk])

        if published:
            self.message_user(request, 'Победитель опубликован.', level=messages.SUCCESS)
            obj.refresh_from_db()
            redirect_url = (
                reverse('admin:promotion_receipt_change', args=[obj.receipt_id])
                if obj.receipt_id else
                reverse('admin:promotion_promotiondrawresult_change', args=[obj.pk])
            )
            return JsonResponse({'ok': True, 'message': 'Победитель опубликован.', 'redirect_url': redirect_url})

        error_message = errors[0] if errors else 'Не удалось опубликовать победителя.'
        self.message_user(request, error_message, level=messages.ERROR)
        return JsonResponse({
            'ok': False,
            'message': error_message,
            'redirect_url': reverse('admin:promotion_promotiondrawresult_change', args=[obj.pk]),
        })


    @admin.action(description='Опубликовать выбранных победителей')
    def publish_selected_winners(self, request, queryset):
        ids = list(queryset.filter(is_published=False).values_list('pk', flat=True))
        if not ids:
            self.message_user(
                request, 'Среди выбранных нет неопубликованных записей.', level=messages.WARNING,
            )
            return
        published, errors = publish_draw_results(ids=ids)
        if published:
            self.message_user(request, f'Опубликовано: {published}.', level=messages.SUCCESS)
        for error in errors[:5]:
            self.message_user(request, error, level=messages.ERROR)

    @admin.display(description='Статус чека')
    def receipt_status_display(self, obj):
        if not obj or not obj.receipt_id:
            return '—'
        return obj.receipt.get_status_display()

    def change_view(self, request, object_id, form_url='', extra_context=None):
        self._current_request = request
        return super().change_view(request, object_id, form_url, extra_context)

    @admin.display(description='Обработка победителя')
    def winner_status_panel(self, obj):
        if not obj or not obj.pk:
            return '—'

        publish_url = reverse('admin:promotion_promotiondrawresult_publish', args=[obj.pk])

        if obj.is_published:
            pub_block = f'<span class="wwf-ok">✅ Опубликован: {obj.published_at or ""}</span>'
        else:
            pub_block = (
                f'<button type="button" class="wwf-btn" id="wwf-publish-btn-{obj.pk}" '
                f'onclick="wwfPublishWinner(\'{publish_url}\', {obj.pk})">'
                f'Опубликовать победителя</button>'
                f'<span id="wwf-publish-status-{obj.pk}" style="margin-left:10px;font-size:12px;"></span>'
                f'<script>'
                f'function wwfGetCookie(name) {{'
                f'  var v = document.cookie.match("(^|;) ?" + name + "=([^;]*)(;|$)");'
                f'  return v ? v[2] : "";'
                f'}}'
                f'function wwfPublishWinner(url, pk) {{'
                f'  if (!confirm("Опубликовать победителя? Статус чека изменится на «Победный», '
                f'вы будете перенаправлены в карточку чека.")) return;'
                f'  var status = document.getElementById("wwf-publish-status-" + pk);'
                f'  var btn = document.getElementById("wwf-publish-btn-" + pk);'
                f'  btn.disabled = true;'
                f'  status.textContent = "Публикация...";'
                f'  fetch(url, {{'
                f'    method: "POST",'
                f'    headers: {{'
                f'      "X-CSRFToken": wwfGetCookie("csrftoken"),'
                f'      "X-Requested-With": "XMLHttpRequest"'
                f'    }}'
                f'  }})'
                f'  .then(function(r) {{ return r.json(); }})'
                f'  .then(function(data) {{'
                f'    if (data.redirect_url) {{'
                f'      window.location.href = data.redirect_url;'
                f'    }} else {{'
                f'      btn.disabled = false;'
                f'      status.style.color = data.ok ? "green" : "red";'
                f'      status.textContent = data.message || "";'
                f'    }}'
                f'  }})'
                f'  .catch(function() {{'
                f'    btn.disabled = false;'
                f'    status.style.color = "red";'
                f'    status.textContent = "Ошибка сети";'
                f'  }});'
                f'}}'
                f'</script>'
            )

        if obj.is_published and obj.receipt_id:
            receipt_url = reverse('admin:promotion_receipt_change', args=[obj.receipt_id])
            after_publish = (
                f'<div style="margin-top:12px;">'
                f'<a class="wwf-btn" href="{receipt_url}">Перейти к чеку →</a>'
                f'</div>'
            )
        elif obj.is_published and not obj.receipt_id:
            after_publish = (
                '<div class="wwf-muted" style="margin-top:10px;">'
                'Чек не привязан — договор и письмо недоступны.'
                '</div>'
            )
        else:
            after_publish = ''

        replace_url = reverse('admin:promotion_promotiondrawresult_replace_winner', args=[obj.pk])
        replace_block = (
            f'<div style="margin-top:16px;padding-top:14px;border-top:1px solid #e2e8f0;">'
            f'<div class="wwf-title" style="color:#b91c1c;margin-bottom:8px;">⚠ Заменить победителя</div>'
            f'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">'
            f'<label style="font-size:13px;">ID нового чека:</label>'
            f'<input type="text" id="replace-receipt-id-{obj.pk}" placeholder="ID или UUID чека" '
            f'style="width:100px;padding:5px 8px;border:1px solid #d1d5db;border-radius:4px;font-size:13px;">'
            f'<button type="button" class="wwf-btn" id="replace-btn-{obj.pk}" '
            f'style="background:#b91c1c;" '
            f'onclick="wwfReplaceWinner(\'{replace_url}\', {obj.pk})">'
            f'Заменить</button>'
            f'<span id="replace-status-{obj.pk}" style="font-size:12px;margin-left:6px;"></span>'
            f'</div>'
            f'<script>'
            f'function wwfReplaceWinner(url, pk) {{'
            f'  var receiptId = document.getElementById("replace-receipt-id-" + pk).value;'
            f'  if (!receiptId) {{ alert("Укажите ID чека"); return; }}'
            f'  if (!confirm("Заменить победителя? Старый чек вернётся в статус Подтверждён.")) return;'
            f'  var btn = document.getElementById("replace-btn-" + pk);'
            f'  var status = document.getElementById("replace-status-" + pk);'
            f'  btn.disabled = true; status.textContent = "Замена...";'
            f'  var m = document.cookie.match("(^|;) ?csrftoken=([^;]*)(;|$)");'
            f'  var csrf = m ? m[2] : "";'
            f'  var body = new FormData();'
            f'  body.append("new_receipt_id", receiptId);'
            f'  body.append("csrfmiddlewaretoken", csrf);'
            f'  fetch(url, {{'
            f'    method: "POST",'
            f'    headers: {{"X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest"}},'
            f'    body: body'
            f'  }})'
            f'  .then(function(r) {{ return r.json(); }})'
            f'  .then(function(data) {{'
            f'    if (data.redirect_url) {{ window.location.href = data.redirect_url; }}'
            f'    else {{'
            f'      btn.disabled = false;'
            f'      status.style.color = data.ok ? "green" : "red";'
            f'      status.textContent = data.message || "";'
            f'    }}'
            f'  }})'
            f'  .catch(function() {{'
            f'    btn.disabled = false;'
            f'    status.style.color = "red"; status.textContent = "Ошибка сети";'
            f'  }});'
            f'}}'
            f'</script>'
            f'</div>'
        )

        html = (
            WWF_STYLE
            + '<div class="wwf-wrap">'
            + _step_html(1, 'Публикация победителя', pub_block, done=obj.is_published)
            + after_publish
            + replace_block
            + '</div>'
        )
        return mark_safe(html)

    def replace_winner_view(self, request, object_id):
        from django.http import JsonResponse
        from django.db import transaction
        from django.utils import timezone
        from promotion.messages import ReceiptMessage

        if request.method != 'POST':
            return HttpResponseRedirect(
                reverse('admin:promotion_promotiondrawresult_change', args=[object_id])
            )

        def err(msg):
            return JsonResponse({'ok': False, 'message': msg})

        try:
            draw = PromotionDrawResult.objects.select_related(
                'participant', 'prize', 'receipt'
            ).get(pk=object_id)
        except PromotionDrawResult.DoesNotExist:
            return err('Запись розыгрыша не найдена.')

        new_receipt_id = request.POST.get('new_receipt_id', '').strip()
        if not new_receipt_id:
            return err('Укажите ID нового чека.')

        try:
            import uuid as _uuid
            try:
                _uuid.UUID(new_receipt_id)
                new_receipt = Receipt.objects.select_related('participant').get(public_id=new_receipt_id)
            except ValueError:
                new_receipt = Receipt.objects.select_related('participant').get(pk=new_receipt_id)
        except Receipt.DoesNotExist:
            return err(f'Чек {new_receipt_id} не найден.')

        if new_receipt.status != Receipt.Status.CONFIRMED:
            return err(
                f'Чек #{new_receipt_id} имеет статус «{new_receipt.get_status_display()}», '
                f'а не «Подтверждён». Замена невозможна.'
            )

        with transaction.atomic():
            old_receipt = draw.receipt
            if old_receipt:
                old_receipt.status = Receipt.Status.CONFIRMED
                old_receipt.is_participation = False
                old_receipt.message = ''
                old_receipt.save(update_fields=['status', 'is_participation', 'message', 'updated_at'])

            draw.participant = new_receipt.participant
            draw.receipt = new_receipt
            draw.is_published = True
            draw.published_at = draw.published_at or timezone.now()
            draw.save(update_fields=['participant', 'receipt', 'is_published', 'published_at'])

            new_receipt.status = Receipt.Status.WINNER
            new_receipt.is_participation = True
            new_receipt.message = ReceiptMessage.get(ReceiptMessage.WINNER)
            new_receipt.save(update_fields=['status', 'is_participation', 'message', 'updated_at'])

        redirect_url = reverse('admin:promotion_receipt_change', args=[new_receipt.pk])
        return JsonResponse({
            'ok': True,
            'message': f'Победитель заменён. Новый: {new_receipt.participant} (чек #{new_receipt.pk}).',
            'redirect_url': redirect_url,
        })