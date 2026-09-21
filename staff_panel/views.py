from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.conf import settings
from django.db.models import Q
from django.core.paginator import Paginator
from django.http import FileResponse, HttpResponseNotAllowed, JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from promotion.messages import ReceiptMessage
from promotion.models import (
    InstantPrizeSettings,
    Prize,
    PrizeCountChange,
    PrizeFile,
    PromotionDrawResult,
    Raffle,
    Receipt,
    User,
    WinnerReplacementSettings,
)
from promotion.services import instant_prizes as instant_prizes_service
from promotion.services.publish_winners import publish_draw_results
from promotion.services.v2_validate_receipt.validate import promo_min_sum_kopecks
from promotion.services.validate_receipt import _normalize_receipt_items
from promotion.services.winner_workflow import (
    issue_oki_for_main,
    issue_oki_for_receipt,
    send_prize_shipping_soon_email_for_receipt,
    send_winner_email_for_main,
    send_winner_email_for_receipt,
)

from .forms import DrawResultCreateForm, ParticipantUpdateForm, PrizeUpdateForm, ReceiptUpdateForm
from .services import analytics as analytics_services
from .services import filters as filters_services
from .services import instant as instant_services
from .services.filters import FilterField, FilterSet
from .services import masking
from .services import oki_templates as oki_templates_services
from .services import participants as participants_services
from .services import prize_files as prize_files_services
from .services import prizes as prizes_services
from .services import receipts as receipts_services
from .services import winner_replacement
from .services import winners as winners_services
from .services.access import (
    can_manage_prize_fulfillment,
    can_manage_receipts,
    can_view_cyclops,
    is_pii_masked,
    is_read_only_staff,
    user_has_panel_access,
)
from .services.query import QueryMixin
from .services.winner_fulfillment import render_fulfillment_panel, render_fulfillment_summary_masked
from .services.xlsx_utils import build_list_workbook, workbook_http_response


class PanelAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    login_url = reverse_lazy('panel:login')
    active_nav = ''

    def test_func(self):
        return user_has_panel_access(self.request.user)

    def panel_context(self, request):
        """Общий контекст шапки панели — для вьюх, которые рендерят страницу вручную."""
        return {
            'active_nav': self.active_nav,
            'is_masked_user': is_pii_masked(request.user),
            'is_read_only_staff': is_read_only_staff(request.user),
            # Файлы призов и замена победителя — доступны и менеджеру (см. access.py).
            'can_manage_prize_fulfillment': can_manage_prize_fulfillment(request.user),
            'can_view_cyclops': can_view_cyclops(request.user),
            'can_manage_receipts': can_manage_receipts(request.user),
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.panel_context(self.request))
        return context


class PanelWriteAccessMixin(PanelAccessMixin):
    """Блокирует создание/изменение/удаление для read-only пользователей панели (staff, не суперюзер)."""

    def dispatch(self, request, *args, **kwargs):
        if is_read_only_staff(request.user):
            messages.error(request, 'У вашей роли доступ только для просмотра — изменения запрещены.')
            referer = request.META.get('HTTP_REFERER')
            return redirect(referer or reverse('panel:receipts'))
        return super().dispatch(request, *args, **kwargs)


class PrizeFulfillmentWriteAccessMixin(PanelAccessMixin):
    """
    Как PanelWriteAccessMixin, но разрешает запись менеджеру: файлы электронных
    призов и замена победителя — его работа (см. access.can_manage_prize_fulfillment).
    """

    def dispatch(self, request, *args, **kwargs):
        if not can_manage_prize_fulfillment(request.user):
            messages.error(request, 'У вашей роли доступ только для просмотра — изменения запрещены.')
            referer = request.META.get('HTTP_REFERER')
            return redirect(referer or reverse('panel:winners'))
        return super().dispatch(request, *args, **kwargs)


class ReceiptWriteAccessMixin(PanelAccessMixin):
    """Как PanelWriteAccessMixin, но разрешает запись менеджеру по обработке чеков."""

    def dispatch(self, request, *args, **kwargs):
        if not can_manage_receipts(request.user):
            messages.error(request, 'У вашей роли доступ только для просмотра — изменения запрещены.')
            referer = request.META.get('HTTP_REFERER')
            return redirect(referer or reverse('panel:receipts'))
        return super().dispatch(request, *args, **kwargs)


class IndexRedirectView(View):
    def get(self, request):
        return redirect('panel:receipts')


class LoginView(View):
    template_name = 'panel/login.html'

    def get(self, request):
        if user_has_panel_access(request.user):
            return redirect('panel:receipts')
        return render(request, self.template_name)

    def post(self, request):
        email = (request.POST.get('email') or '').strip().lower()
        password = request.POST.get('password') or ''
        user = authenticate(request, email=email, password=password)

        if user is None:
            messages.error(request, 'Неверный email или пароль.')
            return render(request, self.template_name)

        if not user_has_panel_access(user):
            messages.error(request, 'У этого аккаунта нет доступа к панели.')
            return render(request, self.template_name)

        login(request, user)
        return redirect(request.GET.get('next') or reverse('panel:receipts'))


class LogoutView(View):
    def post(self, request):
        logout(request)
        return redirect('panel:login')

    def get(self, request):
        logout(request)
        return redirect('panel:login')


# --- Чеки ---


class ReceiptListView(PanelAccessMixin, QueryMixin, ListView):
    template_name = 'panel/receipts_list.html'
    context_object_name = 'receipts'
    paginate_by = QueryMixin.PAGE_SIZE
    active_nav = 'receipts'

    def get_queryset(self):
        queryset = receipts_services.filter_receipts(receipts_services.get_receipts_queryset(), self.request)

        date_from = self.parse_date_param(self.request.GET.get('date_from'))
        date_to = self.parse_date_param(self.request.GET.get('date_to'))
        queryset = receipts_services.apply_date_range(queryset, field='date', date_from=date_from, date_to=date_to)

        reg_from = self.parse_date_param(self.request.GET.get('date_reg_from'))
        reg_to = self.parse_date_param(self.request.GET.get('date_reg_to'))
        queryset = receipts_services.apply_date_range(queryset, field='created_at', date_from=reg_from, date_to=reg_to)

        selected_sort, selected_direction, _ = self.build_sort_context(
            self.request, receipts_services.RECEIPT_SORT_FIELDS, default_sort='created_at',
        )
        return self.apply_model_sort(
            queryset, receipts_services.RECEIPT_SORT_FIELDS, selected_sort, selected_direction,
            default_sort='created_at',
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        selected_sort, selected_direction, sort_context = self.build_sort_context(
            request, receipts_services.RECEIPT_SORT_FIELDS, default_sort='created_at',
        )
        page_obj = context['page_obj']

        for receipt in page_obj.object_list:
            receipt.promo_total = sum(
                item['sum'] for item in receipts_services.items_with_promo_flags(receipt) if item['is_promo']
            )

        filter_set = FilterSet([
            FilterField('status', 'Статус', items=list(Receipt.Status.choices)),
            FilterField('city', 'Город', items=[
                (city, city) for city in participants_services.get_city_choices()
            ]),
            FilterField('input_method', 'Способ загрузки', items=list(Receipt.InputMethod.choices)),
            FilterField('date', 'Дата покупки', kind='range',
                        param_from='date_from', param_to='date_to'),
            FilterField('date_reg', 'Дата загрузки чека', kind='range',
                        param_from='date_reg_from', param_to='date_reg_to'),
            FilterField('amount', 'Сумма чека, ₽', kind='range', input_type='number',
                        param_from='amount_from', param_to='amount_to'),
        ])

        context.update({
            'selected_sort': selected_sort,
            'selected_direction': selected_direction,
            'sort_context': sort_context,
            'search_placeholder': 'Поиск: email, ФИО, телефон, магазин, ИНН',
            'filters': {'q': request.GET.get('q', '')},
            'search_clear_url': self.query_removing(request, 'q'),
            'open_modal_url': self._open_modal_url(request),
            **filter_set.context(request, reverse('panel:receipts')),
            **self.pagination_context(request, page_obj),
        })
        return context

    @staticmethod
    def _open_modal_url(request):
        open_id = request.GET.get('open', '')
        if not open_id.isdigit():
            return ''
        return reverse('panel:receipt_detail', args=[open_id])


def _build_receipt_modal_context(request, receipt: Receipt) -> dict:
    items_display = receipts_services.items_with_promo_flags(receipt)
    promo_total = sum(item['sum'] for item in items_display if item['is_promo'])
    promo_threshold = promo_min_sum_kopecks() / 100
    return {
        'receipt': receipt,
        'form': ReceiptUpdateForm(instance=receipt),
        'copyable_texts': receipts_services.get_copyable_texts(),
        'items_display': items_display,
        'items_total': sum(item['sum'] for item in items_display),
        'promo_total': promo_total,
        'promo_threshold': promo_threshold,
        'promo_threshold_reached': promo_total >= promo_threshold,
        # Автоподстановка шаблона сообщения при выборе статуса «Подтверждён»/«Победный» в форме.
        'accepted_message': ReceiptMessage.raw(ReceiptMessage.ACCEPTED),
        'winner_message': ReceiptMessage.raw(ReceiptMessage.WINNER),
        'list_query': request.GET.get('list_query', ''),
    }


class ReceiptDetailView(PanelAccessMixin, DetailView):
    template_name = 'panel/receipt_modal.html'
    context_object_name = 'receipt'
    active_nav = 'receipts'
    queryset = receipts_services.get_receipts_queryset()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_build_receipt_modal_context(self.request, self.object))
        return context


def _redirect_back_to_receipt(request, receipt, list_query=None):
    """
    Возвращает на ту же страницу списка чеков (с теми же фильтрами), с которой пришёл запрос.

    Фильтры списка передаются явно через скрытое поле формы `list_query` (заполняется
    в JS из текущего URL при открытии модалки) — это надёжнее, чем полагаться только на
    заголовок Referer, который браузер может не отправить. Referer остаётся резервным
    вариантом для случаев, когда list_query не пришёл (прямой переход, старый кэш и т.п.).
    """
    list_path = reverse('panel:receipts')
    if list_query is not None:
        query = QueryDict(list_query, mutable=True)
    else:
        query = QueryDict(mutable=True)
        referer = request.META.get('HTTP_REFERER')
        if referer:
            parts = urlsplit(referer)
            if parts.path == list_path:
                query = QueryDict(parts.query, mutable=True)
    query['open'] = str(receipt.pk)
    return redirect(f'{list_path}?{query.urlencode()}')


class ReceiptUpdateView(ReceiptWriteAccessMixin, View):
    def post(self, request, pk):
        receipt = get_object_or_404(Receipt, pk=pk)
        previous_status = receipt.status
        list_query = request.POST.get('list_query')
        form = ReceiptUpdateForm(request.POST, instance=receipt)
        if form.is_valid():
            items = _normalize_receipt_items(receipt.items)
            selected_indexes = set()
            for raw in request.POST.getlist('promo_item_idx'):
                if raw.isdigit():
                    selected_indexes.add(int(raw))
            promo_items = [items[i] for i in sorted(selected_indexes) if i < len(items)]
            promo_total = sum((item.get('sum', 0) or 0) for item in promo_items) / 100
            promo_threshold = promo_min_sum_kopecks() / 100

            warning = receipts_services.validate_status_promo_total(
                form.cleaned_data['status'], promo_total, promo_threshold,
            )
            if warning:
                messages.warning(request, warning)

            form.save()
            # Смена статуса из карточки чека должна давать те же побочные эффекты,
            # что и кнопки «Подтвердить»/«Отклонить» (период Акции, дата модерации).
            receipts_services.apply_status_change(receipt, previous_status)
            # save_promo_items тоже досинхронизирует ключевые слова, если чек уже
            # подтверждён — единый сабмит формы покрывает и статус, и акционные товары.
            receipts_services.save_promo_items(receipt, promo_items)

            messages.success(request, 'Чек обновлён.')
        else:
            messages.error(request, 'Не удалось сохранить: проверьте поля формы.')
        return _redirect_back_to_receipt(request, receipt, list_query=list_query)


class ReceiptSetStatusView(ReceiptWriteAccessMixin, View):
    def post(self, request, pk, status):
        receipt = get_object_or_404(Receipt, pk=pk)
        if status not in Receipt.Status.values:
            messages.error(request, 'Неизвестный статус.')
        else:
            items_display = receipts_services.items_with_promo_flags(receipt)
            promo_total = sum(item['sum'] for item in items_display if item['is_promo'])
            promo_threshold = promo_min_sum_kopecks() / 100
            warning = receipts_services.validate_status_promo_total(status, promo_total, promo_threshold)
            if warning:
                messages.warning(request, warning)

            receipts_services.set_status(receipt, status)
            messages.success(request, f'Статус изменён на «{receipt.get_status_display()}».')
        return _redirect_back_to_receipt(request, receipt)


# --- Призы ---


class PrizeListView(PanelAccessMixin, QueryMixin, ListView):
    template_name = 'panel/prizes_list.html'
    context_object_name = 'prizes'
    paginate_by = QueryMixin.PAGE_SIZE
    active_nav = 'prizes'

    def get_queryset(self):
        queryset = prizes_services.filter_prizes(prizes_services.get_prizes_queryset(), self.request)
        selected_sort, selected_direction, _ = self.build_sort_context(
            self.request, prizes_services.PRIZE_SORT_FIELDS, default_sort='created_at',
        )
        return self.apply_model_sort(
            queryset, prizes_services.PRIZE_SORT_FIELDS, selected_sort, selected_direction,
            default_sort='created_at',
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        selected_sort, selected_direction, sort_context = self.build_sort_context(
            request, prizes_services.PRIZE_SORT_FIELDS, default_sort='created_at',
        )
        page_obj = context['page_obj']

        filter_set = FilterSet([
            FilterField('draw_period', 'Период розыгрыша', items=list(Prize.DrawPeriod.choices)),
            FilterField('is_main', 'Главный приз', items=filters_services.yes_no_items()),
            FilterField('is_active', 'Активен', items=filters_services.yes_no_items()),
        ])

        context.update({
            'selected_sort': selected_sort,
            'selected_direction': selected_direction,
            'sort_context': sort_context,
            'search_placeholder': 'Поиск: название, тип, описание',
            'filters': {'q': request.GET.get('q', '')},
            'search_clear_url': self.query_removing(request, 'q'),
            'open_modal_url': self._open_modal_url(request),
            **filter_set.context(request, reverse('panel:prizes')),
            **self.pagination_context(request, page_obj),
        })
        return context

    @staticmethod
    def _open_modal_url(request):
        open_id = request.GET.get('open', '')
        if not open_id.isdigit():
            return ''
        return reverse('panel:prize_detail', args=[open_id])


class PrizeDetailView(PanelAccessMixin, DetailView):
    template_name = 'panel/prize_modal.html'
    context_object_name = 'prize'
    active_nav = 'prizes'
    queryset = prizes_services.get_prizes_queryset()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form'] = PrizeUpdateForm(instance=self.object)
        context['count_changes'] = self.object.count_changes.select_related('changed_by').all()
        return context


class PrizeUpdateView(PanelWriteAccessMixin, View):
    def post(self, request, pk):
        prize = get_object_or_404(Prize, pk=pk)
        old_count = prize.count
        form = PrizeUpdateForm(request.POST, request.FILES, instance=prize)
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        if form.is_valid():
            saved_prize = form.save()
            if old_count != saved_prize.count:
                PrizeCountChange.objects.create(
                    prize=saved_prize,
                    changed_by=request.user,
                    old_count=old_count or 0,
                    new_count=saved_prize.count or 0,
                    reason='Изменено в панели',
                )
            messages.success(request, 'Приз обновлён.')
            redirect_url = reverse('panel:prizes') + f'?open={prize.pk}'
            if is_ajax:
                return JsonResponse({'ok': True, 'redirect': redirect_url})
            return redirect(redirect_url)

        if is_ajax:
            html = render_to_string('panel/prize_modal.html', {
                'prize': prize,
                'form': form,
                'count_changes': prize.count_changes.select_related('changed_by').all(),
            }, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=400)

        error_texts = [str(error) for errors in form.errors.values() for error in errors]
        messages.error(
            request,
            'Не удалось сохранить приз: ' + ' '.join(error_texts)
            if error_texts else 'Не удалось сохранить: проверьте поля формы.',
        )
        return redirect(reverse('panel:prizes') + f'?open={prize.pk}')


# --- Участники ---


class ParticipantListView(PanelAccessMixin, QueryMixin, ListView):
    template_name = 'panel/participants_list.html'
    context_object_name = 'participants'
    paginate_by = QueryMixin.PAGE_SIZE
    active_nav = 'participants'

    def get_queryset(self):
        queryset = participants_services.filter_participants(
            participants_services.get_participants_queryset(), self.request,
        )
        selected_sort, selected_direction, _ = self.build_sort_context(
            self.request, participants_services.PARTICIPANT_SORT_FIELDS, default_sort='created_at',
        )
        return self.apply_model_sort(
            queryset, participants_services.PARTICIPANT_SORT_FIELDS, selected_sort, selected_direction,
            default_sort='created_at',
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        selected_sort, selected_direction, sort_context = self.build_sort_context(
            request, participants_services.PARTICIPANT_SORT_FIELDS, default_sort='created_at',
        )
        page_obj = context['page_obj']

        for participant in page_obj.object_list:
            participant.status_label = participants_services.participant_status_label(participant)

        engagement_choices = (
            ('no_receipts', 'Зарегистрировались, но не загрузили ни одного чека'),
            ('one_pending', 'Загрузили один чек (на проверке)'),
            ('one_confirmed', 'Загрузили один чек (подтверждён)'),
            ('one_rejected', 'Загрузили один чек (отклонён)'),
            ('one_frozen', 'Загрузили один чек (заморожен)'),
            ('multiple', 'Загрузили более одного чека'),
        )

        filter_set = FilterSet([
            FilterField('city', 'Город', items=[
                (city, city) for city in participants_services.get_city_choices()
            ]),
            FilterField('win_type', 'Победитель', items=[
                ('weekly', 'Еженедельный приз'), ('main', 'Главный приз'),
            ]),
            FilterField('engagement', 'Статус участия', items=list(engagement_choices)),
            FilterField('is_blocked', 'Заблокирован', items=filters_services.yes_no_items()),
            FilterField('email_confirmed', 'Email подтверждён', items=filters_services.yes_no_items()),
            FilterField('count', 'Количество чеков', kind='range', input_type='number',
                        param_from='count_from', param_to='count_to'),
            FilterField('date_reg', 'Дата регистрации', kind='range',
                        param_from='date_reg_from', param_to='date_reg_to'),
        ])

        context.update({
            'selected_sort': selected_sort,
            'selected_direction': selected_direction,
            'sort_context': sort_context,
            'search_placeholder': 'Поиск: email, ФИО, телефон (в любом формате), город',
            'filters': {'q': request.GET.get('q', '')},
            'search_clear_url': self.query_removing(request, 'q'),
            'open_modal_url': self._open_modal_url(request),
            **filter_set.context(request, reverse('panel:participants')),
            **self.pagination_context(request, page_obj),
        })
        return context

    @staticmethod
    def _open_modal_url(request):
        open_id = request.GET.get('open', '')
        if not open_id.isdigit():
            return ''
        return reverse('panel:participant_detail', args=[open_id])


class ParticipantDetailView(PanelAccessMixin, DetailView):
    template_name = 'panel/participant_modal.html'
    context_object_name = 'participant'
    active_nav = 'participants'

    def get_queryset(self):
        return User.objects.all() if settings.DEBUG else User.objects.filter(is_staff=False)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        participant = self.object
        context['receipts_stats'] = participants_services.get_receipt_stats(participant)
        context['receipts'] = Receipt.objects.filter(participant=participant).order_by('-created_at')
        context['form'] = ParticipantUpdateForm(instance=participant)
        context['dadata_token'] = getattr(settings, 'DADATA_API_KEY', '')
        context['participant_status'] = participants_services.participant_status_label(participant)
        context['payout'] = participants_services.guaranteed_prize_payout_info(participant)
        context['return_to'] = _safe_return_to(self.request, self.request.GET.get('return_to'))
        return context


def _safe_return_to(request, value):
    """Куда вернуть после сохранения карточки участника: только внутренний
    адрес этого же сайта. Внешние ссылки отбрасываются — иначе параметр в URL
    модалки превращается в open redirect."""
    value = (value or '').strip()
    if not value:
        return ''
    if not url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return ''
    return value


class ParticipantUpdateView(PanelWriteAccessMixin, View):
    def post(self, request, pk):
        if settings.DEBUG:
            participant = get_object_or_404(User, pk=pk)
        else:
            participant = get_object_or_404(User, pk=pk, is_staff=False)
        form = ParticipantUpdateForm(request.POST, instance=participant)
        if form.is_valid():
            form.save()
            messages.success(request, 'Участник обновлён.')
        else:
            messages.error(request, 'Не удалось сохранить: проверьте поля формы.')
        # Карточку открывают и из других разделов (например, из таблицы выплат
        # в Cyclops) — туда же и возвращаемся, см. _safe_return_to.
        return_to = _safe_return_to(request, request.POST.get('return_to'))
        if return_to:
            return redirect(return_to)
        return redirect(reverse('panel:participants') + f'?open={participant.pk}')


# --- Победители ---


class WinnerListView(PanelAccessMixin, QueryMixin, View):
    template_name = 'panel/winners_list.html'
    active_nav = 'winners'

    def get(self, request, *args, **kwargs):
        rows = winners_services.collect_winner_rows()
        rows = winners_services.filter_winner_rows(rows, request)

        selected_sort, selected_direction, sort_context = self.build_sort_context(
            request, winners_services.WINNER_SORT_FIELDS, default_sort='created_at',
        )
        rows = winners_services.sort_winner_rows(
            rows,
            sort=selected_sort or 'created_at',
            direction=selected_direction or 'desc',
        )

        page_obj, paginator = winners_services.paginate_winner_rows(
            rows, request.GET.get('page'), self.get_paginate_by(None),
        )

        prize_choices = winners_services.get_prize_choices()

        filter_set = FilterSet([
            FilterField('draw_type', 'Тип розыгрыша', items=list(winners_services.DRAW_TYPE_CHOICES)),
            FilterField('week', 'Неделя', value_prefix='неделя ',
                        items=[(w, str(w)) for w in winners_services.get_available_weeks()]),
            FilterField('month', 'Месяц', value_prefix='месяц ',
                        items=[(m, str(m)) for m in winners_services.get_available_months()]),
            FilterField('stage', 'Этап', items=list(winners_services.WINNER_STAGE_CHOICES)),
            FilterField('prize', 'Приз', items=[(p.pk, p.name) for p in prize_choices]),
            FilterField('contract_status', 'Договор',
                        items=list(winners_services.CONTRACT_STATUS_CHOICES)),
            FilterField('email_sent', 'Письмо отправлено', items=filters_services.yes_no_items()),
            FilterField('delivery_status', 'Доставлено', items=[
                *[(value, value) for value, _ in winners_services.DELIVERY_STATUS_CHOICES],
                ('__empty__', 'Не заполнено'),
            ]),
            FilterField('prize_file', 'Файл приза', items=[('1', 'Выдан'), ('0', 'Не выдан')]),
        ])

        context = {
            **self.panel_context(request),
            # Раздел «Победители» — исключение: ПДн здесь никогда не маскируются.
            'is_masked_user': False,
            'page_obj': page_obj,
            'paginator': paginator,
            'winners_count': len(rows),
            'delivery_status_choices': winners_services.DELIVERY_STATUS_CHOICES,
            'selected_sort': selected_sort,
            'selected_direction': selected_direction,
            'sort_context': sort_context,
            'search_placeholder': 'Поиск: email, ФИО, приз, файл приза',
            'search_clear_url': self.query_removing(request, 'q'),
            'filters': {'q': request.GET.get('q', '')},
            'open_modal_url': self._open_modal_url(request),
            'replacement_settings': WinnerReplacementSettings.load(),
            **filter_set.context(request, reverse('panel:winners')),
            **self.pagination_context(request, page_obj),
        }
        return render(request, self.template_name, context)

    @staticmethod
    def _open_modal_url(request):
        open_id = request.GET.get('open', '')
        if ':' not in open_id:
            return ''
        kind, _, pk = open_id.partition(':')
        if not pk.isdigit():
            return ''
        return reverse('panel:winner_detail', args=[kind, pk])


def _fulfillment_urls(kind, pk):
    return {
        'publish': reverse('panel:winner_publish_one', args=[pk]) if kind == 'weekly' else '',
        'issue_oki': reverse('panel:winner_issue_oki', args=[kind, pk]),
        'send_email': reverse('panel:winner_send_email', args=[kind, pk]),
        'send_shipping_soon': reverse('panel:winner_send_shipping_soon', args=[pk]) if kind == 'weekly' else '',
    }


class WinnerDetailView(PanelAccessMixin, View):
    active_nav = 'winners'

    def get(self, request, kind, pk):
        row = winners_services.find_winner_row(kind, pk)
        if row is None:
            messages.error(request, 'Победитель не найден.')
            return redirect('panel:winners')

        read_only = is_read_only_staff(request.user)
        can_replace_winner = can_manage_prize_fulfillment(request.user)
        template_plan = oki_templates_services.plan_for_row(row)
        if read_only:
            fulfillment_panel_html = render_fulfillment_summary_masked(row)
        else:
            fulfillment_panel_html = render_fulfillment_panel(
                row, _fulfillment_urls(kind, pk), request, template_plan=template_plan,
            )

        context = {
            'active_nav': self.active_nav,
            'is_masked_user': False,
            'row': row,
            'template_plan': template_plan,
            'fulfillment_panel_html': fulfillment_panel_html,
            'delete_url': '' if read_only else reverse('panel:winner_delete', args=[kind, pk]),
            'replace_url': (
                reverse('panel:winner_replace', args=[kind, pk])
                if can_replace_winner and row.can_replace else ''
            ),
            'replace_block_reason': (
                row.replace_block_reason if can_replace_winner and not row.can_replace else ''
            ),
            'replacement_history': winner_replacement.history_for_row(row),
        }
        return render(request, 'panel/winner_modal.html', context)


class WinnerIssueOkiView(PanelWriteAccessMixin, View):
    """
    Выставляет договор победителю. Шаблон не выбирается руками — он определяется
    призом (главный приз / до 4 000 ₽ / дороже 4 000 ₽), см.
    staff_panel.services.oki_templates. Что именно подставится в договор, видно
    в карточке победителя заранее, ещё до нажатия кнопки.
    """

    def post(self, request, kind, pk):
        row = winners_services.find_winner_row(kind, pk)
        if row is None:
            messages.error(request, 'Победитель не найден.')
            return redirect('panel:winners')

        plan = oki_templates_services.plan_for_row(row)
        if not plan.ok:
            messages.error(request, plan.error or 'Не удалось подобрать шаблон договора.')
            return redirect(reverse('panel:winners') + f'?open={kind}:{pk}')

        if kind == 'weekly':
            result = issue_oki_for_receipt(row.receipt, oki_template=plan.template)
        else:
            result = issue_oki_for_main(row.obj, oki_template=plan.template)

        if result.ok:
            messages.success(request, f'{result.message} Шаблон: «{plan.template_name}».')
        else:
            messages.error(request, result.message)
        return redirect(reverse('panel:winners') + f'?open={kind}:{pk}')


class WinnerSendEmailView(PanelWriteAccessMixin, View):
    def post(self, request, kind, pk):
        row = winners_services.find_winner_row(kind, pk)
        if row is None:
            messages.error(request, 'Победитель не найден.')
            return redirect('panel:winners')

        if kind == 'weekly':
            result = send_winner_email_for_receipt(row.receipt)
        else:
            result = send_winner_email_for_main(row.obj)

        messages.success(request, result.message) if result.ok else messages.warning(request, result.message)
        return redirect(reverse('panel:winners') + f'?open={kind}:{pk}')


class WinnerSendShippingSoonView(PanelWriteAccessMixin, View):
    def post(self, request, pk):
        draw_result = get_object_or_404(PromotionDrawResult, pk=pk)
        if not draw_result.receipt_id:
            messages.error(request, 'Чек не привязан.')
            return redirect(reverse('panel:winners') + f'?open=weekly:{pk}')

        result = send_prize_shipping_soon_email_for_receipt(draw_result.receipt)
        messages.success(request, result.message) if result.ok else messages.warning(request, result.message)
        return redirect(reverse('panel:winners') + f'?open=weekly:{pk}')


class WinnerPublishOneView(PanelWriteAccessMixin, View):
    def post(self, request, pk):
        published, errors = publish_draw_results(ids=[pk])
        if published:
            messages.success(request, 'Победитель опубликован.')
        for error in errors:
            messages.error(request, error)
        return redirect(reverse('panel:winners') + f'?open=weekly:{pk}')


class WinnerDeleteView(PanelWriteAccessMixin, View):
    def post(self, request, kind, pk):
        row = winners_services.find_winner_row(kind, pk)
        if row is None:
            messages.error(request, 'Победитель не найден.')
        else:
            row.obj.delete()
            messages.success(request, 'Итог розыгрыша удалён.')
        return redirect('panel:winners')


class WinnerReplaceView(PrizeFulfillmentWriteAccessMixin, View):
    """
    Замена победителя: модалка с выбором режима (автоматически / вручную) и сама замена.

    Замена не трогает опубликованные итоги на лендинге — см.
    staff_panel.services.winner_replacement.
    """

    template_name = 'panel/winner_replace.html'

    def get(self, request, kind, pk):
        row = winners_services.find_winner_row(kind, pk)
        if row is None:
            messages.error(request, 'Победитель не найден.')
            return redirect('panel:winners')

        conditions = winner_replacement.conditions_summary(row)
        eligible = winner_replacement.eligible_candidates(row) if row.can_replace else []

        context = {
            'active_nav': 'winners',
            'row': row,
            'is_masked_user': False,
            'can_replace': row.can_replace,
            'block_reason': winner_replacement.replace_block_reason(row),
            'conditions': conditions,
            'eligible_count': len(eligible),
            'eligible_preview': [c.as_option() for c in eligible[:5]],
            'history': winner_replacement.history_for_row(row),
            'candidates_url': reverse('panel:winner_replace_candidates', args=[kind, pk]),
            'form_action': reverse('panel:winner_replace', args=[kind, pk]),
        }
        return render(request, self.template_name, context)

    def post(self, request, kind, pk):
        row = winners_services.find_winner_row(kind, pk)
        if row is None:
            return JsonResponse({'ok': False, 'error': 'Победитель не найден.'}, status=404)

        mode = (request.POST.get('mode') or '').strip()
        reason = request.POST.get('reason') or ''

        try:
            if mode == 'auto':
                candidate = winner_replacement.pick_auto_candidate(row)
                participant_id = candidate.participant_id
                receipt_id = candidate.receipt.pk if candidate.receipt else None
            else:
                participant_id = request.POST.get('participant_id')
                receipt_id = request.POST.get('receipt_id') or None

            replacement = winner_replacement.replace_winner(
                row,
                new_participant_id=participant_id,
                new_receipt_id=receipt_id,
                mode=mode,
                reason=reason,
                user=request.user,
            )
        except winner_replacement.WinnerReplacementError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

        note = '' if replacement.was_eligible else ' (не подходит под условия розыгрыша)'
        messages.success(
            request,
            f'Победитель заменён: {replacement.old_participant_label} → '
            f'{replacement.new_participant_label}{note}.',
        )
        return JsonResponse({
            'ok': True,
            'redirect': reverse('panel:winners') + f'?open={kind}:{pk}',
        })


class WinnerReplaceCandidatesView(PrizeFulfillmentWriteAccessMixin, View):
    """Кандидаты в новые победители для выпадающего списка с поиском."""

    def get(self, request, kind, pk):
        row = winners_services.find_winner_row(kind, pk)
        if row is None:
            return JsonResponse({'ok': False, 'error': 'Победитель не найден.'}, status=404)

        data = winner_replacement.candidate_options(row, query=request.GET.get('q', ''))
        return JsonResponse({'ok': True, **data})


class WinnerReplacementSettingsView(PrizeFulfillmentWriteAccessMixin, View):
    """
    Модалка настройки срока на подпись договора (см.
    staff_panel.services.winner_replacement.deadline_status): пока с момента
    отправки письма с договором не прошло столько дней, заменить победителя
    нельзя. Настройка одна на всю акцию.
    """

    template_name = 'panel/winner_replacement_settings.html'

    def get(self, request):
        context = {
            'active_nav': 'winners',
            'settings_obj': WinnerReplacementSettings.load(),
            'form_action': reverse('panel:winner_replacement_settings'),
        }
        return render(request, self.template_name, context)

    def post(self, request):
        raw = (request.POST.get('sign_deadline_days') or '').strip()
        if not raw.isdigit():
            messages.error(request, 'Срок должен быть числом дней (0 — без ограничения).')
            return redirect('panel:winners')

        settings_obj = WinnerReplacementSettings.load()
        settings_obj.sign_deadline_days = int(raw)
        settings_obj.save(update_fields=['sign_deadline_days', 'updated_at'])
        if settings_obj.sign_deadline_days:
            messages.success(request, f'Срок на подпись договора: {settings_obj.sign_deadline_days} дн.')
        else:
            messages.success(request, 'Срок на подпись договора отключён — замена не ограничена.')
        return redirect('panel:winners')


class WinnerAutoDistributeView(PrizeFulfillmentWriteAccessMixin, View):
    """
    Модалка авто-раздачи файлов электронных призов победителям одного выбранного
    розыгрыша (см. staff_panel.services.prize_files.preview_auto_distribution).
    """

    template_name = 'panel/winner_auto_distribute.html'

    @staticmethod
    def _parse_kind_period(source):
        kind = (source.get('kind') or '').strip()
        period_raw = (source.get('period') or '').strip()
        period = int(period_raw) if period_raw.isdigit() else None
        return kind, period_raw, period

    def _context(self, kind, period_raw, period, *, error=None):
        draws = winners_services.available_draws()
        if not kind and draws:
            kind, period_raw, period = draws[0]['kind'], str(draws[0]['period'] or ''), draws[0]['period']

        preview = prize_files_services.preview_auto_distribution(kind, period) if kind else None
        return {
            'draws': draws,
            'selected_value': f'{kind}:{period_raw}' if kind else '',
            'selected_kind': kind,
            'selected_period_raw': period_raw,
            'preview': preview,
            'error': error,
        }

    def get(self, request):
        kind, period_raw, period = self._parse_kind_period(request.GET)
        context = self._context(kind, period_raw, period)
        return render(request, self.template_name, context)

    def post(self, request):
        kind, period_raw, period = self._parse_kind_period(request.POST)
        try:
            result = prize_files_services.apply_auto_distribution(kind, period)
        except prize_files_services.PrizeFileError as exc:
            context = self._context(kind, period_raw, period, error=str(exc))
            html = render_to_string(self.template_name, context, request=request)
            return JsonResponse({'ok': False, 'html': html})

        note = f' Без подходящего свободного файла осталось: {result.unmatched_count}.' if result.unmatched_count else ''
        messages.success(
            request,
            f'Файлы розданы: {result.assigned} · {result.kind_label.lower()}, '
            f'{result.period_label.lower()}.{note}',
        )
        return JsonResponse({'ok': True, 'redirect': reverse('panel:winners')})


class WinnerUpdateFieldView(PanelAccessMixin, View):
    """Инлайн-правка ячейки в таблице победителей: «Доставлено» и «Файл приза»."""

    # Обе колонки — «Доставлено» и «Файл приза» — правит и менеджер: это вручение
    # приза, его работа (см. access.can_manage_prize_fulfillment).
    EDITABLE_FIELDS = {'delivery_status', 'prize_file'}

    def post(self, request, kind, pk):
        field = (request.POST.get('field') or '').strip()
        if field not in self.EDITABLE_FIELDS:
            return JsonResponse({'ok': False, 'error': 'Это поле нельзя изменить.'}, status=400)

        if not can_manage_prize_fulfillment(request.user):
            return JsonResponse(
                {'ok': False, 'error': 'У вашей роли доступ только для просмотра.'}, status=403,
            )

        row = winners_services.find_winner_row(kind, pk)
        if row is None:
            return JsonResponse({'ok': False, 'error': 'Победитель не найден.'}, status=404)

        value = (request.POST.get('value') or '').strip()

        if field == 'delivery_status':
            if len(value) > 100:
                return JsonResponse(
                    {'ok': False, 'error': 'Значение длиннее 100 символов.'}, status=400,
                )
            row.obj.delivery_status = value or None
            row.obj.save(update_fields=['delivery_status'])
            # Этап строки считается по самой строке, а не по модели — обновляем
            # и её, иначе ответ вернёт этап, каким он был до сохранения.
            row.delivery_status = value
            return JsonResponse({
                'ok': True,
                'value': value,
                'color': winners_services.DELIVERY_STATUS_COLORS.get(value.lower(), 'gray'),
                'stage': row.stage,
                'stage_label': row.stage_label,
                'stage_color': row.stage_color,
                'message': (
                    f'«Доставлено» — {value}' if value else 'Значение «Доставлено» очищено'
                ),
            })

        # field == 'prize_file'
        if not value:
            prize_files_services.clear_file_for_row(row)
            return JsonResponse({
                'ok': True, 'value': '', 'file_id': None, 'download_url': '',
                'message': 'Файл приза снят с победителя',
            })

        try:
            prize_file = prize_files_services.assign_file_to_row(row, value)
        except prize_files_services.PrizeFileError as exc:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)

        return JsonResponse({
            'ok': True,
            'value': prize_file.name,
            'file_id': prize_file.pk,
            'download_url': reverse('panel:prize_file_download', args=[prize_file.pk]),
            'message': f'Файл «{prize_file.name}» выдан победителю',
        })


class WinnerPrizeFileOptionsView(PanelAccessMixin, View):
    """Свободные файлы призов для выпадающего списка с поиском."""

    def get(self, request, kind, pk):
        row = winners_services.find_winner_row(kind, pk)
        if row is None:
            return JsonResponse({'ok': False, 'error': 'Победитель не найден.'}, status=404)

        if not prize_files_services.row_supports_prize_file(row):
            return JsonResponse({
                'ok': False,
                'error': 'Приз этого победителя не электронный — файл приза ему не выдаётся.',
            }, status=400)

        options = prize_files_services.free_files_for_row(row, query=request.GET.get('q', ''))
        current = row.prize_file
        return JsonResponse({
            'ok': True,
            'options': options,
            'current': {'id': current.pk, 'name': current.name} if current else None,
            'prize': row.prize.name if row.prize else '',
            'draw_type_label': prize_files_services.DRAW_TYPE_LABELS.get(
                prize_files_services.row_draw_type(row), '',
            ),
        })


class PrizeFileListView(PanelAccessMixin, QueryMixin, View):
    """Хранилище файлов электронных призов: загрузка партиями и их учёт."""

    template_name = 'panel/prize_files.html'
    active_nav = 'winners'

    def get(self, request, *args, **kwargs):
        qs = PrizeFile.objects.select_related(
            'kind', 'uploaded_by', 'draw_result__participant', 'main_draw_result__participant',
        ).all()

        kind_ids = [v for v in filters_services.values_for(request, 'prize') if v.isdigit()]
        draw_types = [
            v for v in filters_services.values_for(request, 'draw_type')
            if v in prize_files_services.DRAW_TYPE_LABELS
        ]
        statuses = [v for v in filters_services.values_for(request, 'status') if v in ('free', 'assigned')]
        search = request.GET.get('q', '').strip()

        if kind_ids:
            qs = qs.filter(kind_id__in=[int(v) for v in kind_ids])
        if draw_types:
            qs = qs.filter(draw_type__in=draw_types)
        if len(statuses) == 1:
            if statuses[0] == 'free':
                qs = qs.filter(draw_result__isnull=True, main_draw_result__isnull=True)
            else:
                qs = qs.filter(Q(draw_result__isnull=False) | Q(main_draw_result__isnull=False))
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(kind__name__icontains=search))

        selected_sort, selected_direction, sort_context = self.build_sort_context(
            request, prize_files_services.PRIZE_FILE_SORT_FIELDS, default_sort='uploaded_at',
        )
        qs = self.apply_model_sort(
            qs, prize_files_services.PRIZE_FILE_SORT_FIELDS, selected_sort, selected_direction,
            default_sort='uploaded_at',
        )

        paginator = Paginator(qs, self.get_paginate_by(None))
        page_obj = paginator.get_page(request.GET.get('page'))

        kind_choices = prize_files_services.upload_kind_choices()
        filter_kind_choices = prize_files_services.kinds_with_files(kind_choices)

        filter_set = FilterSet([
            FilterField('prize', 'Приз', items=[(k.pk, k.name or '—') for k in filter_kind_choices]),
            FilterField('draw_type', 'Тип розыгрыша', items=list(PrizeFile.DrawType.choices)),
            FilterField('status', 'Состояние', items=[('free', 'Свободные'), ('assigned', 'Выданные')]),
        ])

        # Типы розыгрыша в форме загрузки зависят от выбранного приза: показываем
        # только те, в которых он реально разыгрывается (см. draw_types_by_kind).
        kind_draw_types = prize_files_services.draw_types_by_kind(k.pk for k in kind_choices)

        context = {
            **self.panel_context(request),
            'page_obj': page_obj,
            'paginator': paginator,
            'files_count': paginator.count,
            'kind_choices': kind_choices,
            'draw_type_labels': prize_files_services.DRAW_TYPE_LABELS,
            'kind_draw_types': {str(kind_id): types for kind_id, types in kind_draw_types.items()},
            'stats': prize_files_services.prize_file_stats(),
            'stats_totals': prize_files_services.prize_file_totals(),
            'selected_sort': selected_sort,
            'selected_direction': selected_direction,
            'sort_context': sort_context,
            'search_placeholder': 'Поиск: имя файла, приз',
            'search_clear_url': self.query_removing(request, 'q'),
            'max_batch_mb': prize_files_services.MAX_UPLOAD_BATCH_SIZE // (1024 * 1024),
            'filters': {'q': search},
            **filter_set.context(request, reverse('panel:prize_files')),
            **self.pagination_context(request, page_obj),
        }
        return render(request, self.template_name, context)


class PrizeFileUploadView(PrizeFulfillmentWriteAccessMixin, View):
    def post(self, request):
        redirect_to = reverse('panel:prize_files')
        try:
            result = prize_files_services.upload_prize_files(
                kind_id=request.POST.get('prize', ''),
                draw_type=(request.POST.get('draw_type') or '').strip(),
                files=request.FILES.getlist('files'),
                user=request.user,
            )
        except prize_files_services.PrizeFileError as exc:
            messages.error(request, str(exc))
            return redirect(redirect_to)

        messages.success(
            request,
            f'Загружено файлов: {len(result.created)} — приз «{result.kind.name}», '
            f'{prize_files_services.DRAW_TYPE_LABELS[result.draw_type].lower()} розыгрыш.',
        )
        return redirect(redirect_to)


class PrizeFileDeleteView(PrizeFulfillmentWriteAccessMixin, View):
    def post(self, request, pk):
        prize_file = get_object_or_404(PrizeFile, pk=pk)
        name = prize_file.name
        try:
            prize_files_services.delete_prize_file(prize_file)
        except prize_files_services.PrizeFileError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f'Файл «{name}» удалён.')
        return redirect(request.META.get('HTTP_REFERER') or reverse('panel:prize_files'))


class PrizeFileDownloadView(PanelAccessMixin, View):
    def get(self, request, pk):
        prize_file = get_object_or_404(PrizeFile, pk=pk)
        if not prize_file.file:
            messages.error(request, 'Файл отсутствует в хранилище.')
            return redirect('panel:prize_files')
        return FileResponse(
            prize_file.file.open('rb'), as_attachment=True, filename=prize_file.name,
        )


class WinnerCreateView(PanelWriteAccessMixin, View):
    active_nav = 'winners'
    template_name = 'panel/winner_create.html'

    def get(self, request):
        form = DrawResultCreateForm()
        return render(request, self.template_name, {'form': form, 'active_nav': self.active_nav})

    def post(self, request):
        form = DrawResultCreateForm(request.POST)
        if form.is_valid():
            draw_result = form.save()
            messages.success(request, 'Итог розыгрыша добавлен.')
            return redirect(reverse('panel:winners') + f'?open=weekly:{draw_result.pk}')
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
        return redirect('panel:winners')


class WinnerParticipantSearchView(PanelWriteAccessMixin, View):
    def get(self, request):
        query = request.GET.get('q', '').strip()
        if not query:
            return JsonResponse({'results': []})
        base_queryset = User.objects.all() if settings.DEBUG else User.objects.filter(is_staff=False)
        participants = base_queryset.filter(
            Q(email__icontains=query) | Q(first_name__icontains=query) | Q(last_name__icontains=query),
        ).order_by('last_name')[:20]
        return JsonResponse({
            'results': [
                {'id': p.pk, 'text': f'{p.last_name} {p.first_name} ({p.email})'}
                for p in participants
            ],
        })


class WinnerReceiptsForParticipantView(PanelWriteAccessMixin, View):
    def get(self, request):
        participant_id = request.GET.get('participant_id')
        if not participant_id or not participant_id.isdigit():
            return JsonResponse({'results': []})
        receipts = (
            Receipt.objects.filter(participant_id=participant_id)
            .filter(status__in=[Receipt.Status.CONFIRMED, Receipt.Status.WINNER])
            .exclude(draw_results__isnull=False)
            .select_related('participant')
            .order_by('-created_at')[:100]
        )
        return JsonResponse({
            'results': [{'id': r.pk, 'text': str(r)} for r in receipts],
        })


# --- Аналитика ---


class AnalyticsView(PanelAccessMixin, QueryMixin, TemplateView):
    template_name = 'panel/analytics.html'
    active_nav = 'analytics'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        date_from = self.parse_date_param(request.GET.get('date_from'))
        date_to = self.parse_date_param(request.GET.get('date_to'))

        if date_from is None and date_to is None and 'date_from' not in request.GET:
            date_from, date_to = Raffle.get_default_analytics_range()
            if date_from is None:
                today = timezone.localdate()
                date_from = today - timedelta(days=today.weekday())
                date_to = today

        analytics = analytics_services.get_analytics_data(date_from=date_from, date_to=date_to)
        context['analytics'] = analytics
        context['filters'] = {
            'date_from': request.GET.get('date_from', analytics['date_from'].isoformat()),
            'date_to': request.GET.get('date_to', analytics['date_to'].isoformat()),
            'tab': request.GET.get('tab', 'receipts'),
        }
        # Экспорт должен получать тот же диапазон дат, что реально показан на
        # экране, а не сырые query-параметры запроса — если пользователь ни разу
        # не трогал фильтр, в request.GET их нет вообще, хотя на дашборде уже
        # подставлен диапазон по умолчанию (см. выше). Иначе Excel-выгрузка
        # молча уезжает на «весь период» вместо показанного на дашборде.
        context['export_url'] = reverse('panel:analytics_export') + '?' + urlencode({
            'date_from': context['filters']['date_from'],
            'date_to': context['filters']['date_to'],
        })
        return context


class AnalyticsExportView(PanelAccessMixin, QueryMixin, View):
    def get(self, request):
        date_from = self.parse_date_param(request.GET.get('date_from'))
        date_to = self.parse_date_param(request.GET.get('date_to'))
        wb = analytics_services.build_analytics_workbook(date_from=date_from, date_to=date_to)
        return workbook_http_response(wb, 'analytics')


# --- Экспорт в Excel ---


class ReceiptExportView(PanelAccessMixin, QueryMixin, View):
    def get(self, request):
        masked = is_pii_masked(request.user)
        queryset = receipts_services.filter_receipts(receipts_services.get_receipts_queryset(), request)

        date_from = self.parse_date_param(request.GET.get('date_from'))
        date_to = self.parse_date_param(request.GET.get('date_to'))
        queryset = receipts_services.apply_date_range(queryset, field='date', date_from=date_from, date_to=date_to)

        reg_from = self.parse_date_param(request.GET.get('date_reg_from'))
        reg_to = self.parse_date_param(request.GET.get('date_reg_to'))
        queryset = receipts_services.apply_date_range(queryset, field='created_at', date_from=reg_from, date_to=reg_to)

        headers = [
            'Дата загрузки', 'Участник', 'Email', 'Статус', 'Способ ввода',
            'Сумма, ₽', 'Сумма продукции ОБ, ₽', 'Магазин', 'Город', 'Дата покупки',
            'ИНН', 'ФН', 'ФД', 'ФП',
        ]
        rows = []
        for receipt in queryset.select_related('participant').order_by('-created_at').iterator(chunk_size=500):
            promo_total = sum(
                item['sum'] for item in receipts_services.items_with_promo_flags(receipt) if item['is_promo']
            )
            rows.append([
                receipt.created_at.strftime('%d.%m.%Y %H:%M') if receipt.created_at else '',
                masking.display_name(receipt.participant, masked),
                masking.display_email(receipt.participant.email, masked),
                receipt.get_status_display(),
                receipt.get_input_method_display() if receipt.input_method else '',
                float(receipt.amount) if receipt.amount is not None else '',
                promo_total,
                receipt.store or '',
                receipt.participant.city or '',
                receipt.date.strftime('%d.%m.%Y %H:%M') if receipt.date else '',
                receipt.inn or '',
                receipt.fn or '',
                receipt.fd or '',
                receipt.fp or '',
            ])
        wb = build_list_workbook('Чеки', headers, rows)
        return workbook_http_response(wb, 'receipts')


class PrizeExportView(PanelAccessMixin, View):
    def get(self, request):
        queryset = prizes_services.filter_prizes(prizes_services.get_prizes_queryset(), request)

        headers = ['Название', 'Тип', 'Период', 'Кол-во', 'Стоимость, ₽', 'Главный', 'Активен', 'Создан']
        rows = []
        for prize in queryset.order_by('-created_at').iterator(chunk_size=500):
            if prize.is_main:
                period = '—'
            elif prize.draw_period == Prize.DrawPeriod.MONTHLY:
                period = f'Ежемесячный · мес. {prize.month or "—"}'
            else:
                period = f'Еженедельный · нед. {prize.week or "—"}'
            rows.append([
                prize.name or '',
                prize.type_prize or '',
                period,
                prize.count if prize.count is not None else '',
                prize.cost if prize.cost is not None else '',
                'да' if prize.is_main else 'нет',
                'да' if prize.is_active else 'нет',
                prize.created_at.strftime('%d.%m.%Y %H:%M') if prize.created_at else '',
            ])
        wb = build_list_workbook('Призы', headers, rows)
        return workbook_http_response(wb, 'prizes')


class ParticipantExportView(PanelAccessMixin, View):
    def get(self, request):
        masked = is_pii_masked(request.user)
        queryset = participants_services.filter_participants(
            participants_services.get_participants_queryset(), request,
        )

        headers = ['ФИО', 'Email', 'Телефон', 'Город', 'Чеков', 'Статус участия', 'Победитель', 'Регистрация']
        rows = []
        for participant in queryset.order_by('-created_at').iterator(chunk_size=500):
            win_labels = []
            if participant.is_weekly_winner:
                win_labels.append('Неделя')
            if participant.is_main_winner:
                win_labels.append('Главный')
            rows.append([
                masking.display_name(participant, masked),
                masking.display_email(participant.email, masked),
                masking.display_phone(participant.phone, masked) if participant.phone else '',
                participant.city or '',
                participant.receipts_count,
                participants_services.participant_status_label(participant),
                ', '.join(win_labels) or '—',
                participant.created_at.strftime('%d.%m.%Y') if participant.created_at else '',
            ])
        wb = build_list_workbook('Участники', headers, rows)
        return workbook_http_response(wb, 'participants')


class WinnerExportView(PanelAccessMixin, View):
    def get(self, request):
        # Раздел «Победители» — исключение: ПДн здесь никогда не маскируются.
        masked = False
        rows_data = winners_services.collect_winner_rows()
        rows_data = winners_services.filter_winner_rows(rows_data, request)

        # Набор и названия колонок повторяют таблицу на странице «Победители»:
        # телефон, город и дата итогов оттуда убраны, добавлены «Этап» и даты
        # выставления договора и отправки письма.
        headers = [
            'Дата', 'Тип', 'Период', 'ФИО', 'Приз', 'Email', 'Этап',
            'Договор', 'Ссылка на договор', 'Договор выставлен',
            'Доставлено', 'Файл приза', 'Письмо', 'Письмо отправлено',
        ]
        rows = []
        for row in rows_data:
            rows.append([
                row.created_at.strftime('%d.%m.%Y') if row.created_at else '',
                row.draw_type_short_label,
                row.period_label,
                masking.display_name(row.participant, masked),
                row.prize.name if row.prize else '',
                masking.display_email(row.email, masked) if row.email else '',
                row.stage_label,
                row.status_oki_document or '',
                row.contract_link or '',
                row.contract_issued_at.strftime('%d.%m.%Y %H:%M') if row.contract_issued_at else '',
                row.delivery_status or '',
                row.prize_file_name,
                'Отправлено' if row.is_send_email else 'Не отправлено',
                row.email_sent_at.strftime('%d.%m.%Y %H:%M') if row.email_sent_at else '',
            ])
        wb = build_list_workbook('Победители', headers, rows)
        return workbook_http_response(wb, 'winners')


# ---------------------------------------------------------------------------
# Моментальные призы
# ---------------------------------------------------------------------------
class InstantPrizesView(PanelAccessMixin, TemplateView):
    """
    Состояние лотка яиц: призовой фонд, равномерность по дням, последние выигрыши.

    Главный вопрос вкладки — «уходят ли призы ровно по графику». Поэтому на
    первом экране стоят две цифры (разыграно/осталось) и столбики по дням, а не
    таблица моментов: смотреть построчно расписание из тысячи строк некому.
    """

    template_name = 'panel/instant_prizes.html'
    active_nav = 'instant'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'config': InstantPrizeSettings.load(),
            'fund': instant_services.fund_summary(),
            'prizes': instant_services.prizes_summary(),
            'schedule': instant_services.daily_schedule(),
            'recent_wins': instant_services.recent_wins(),
        })
        return context


class InstantPrizesGenerateView(PanelWriteAccessMixin, View):
    """Пересобрать расписание призовых моментов.

    Операция идемпотентна и безопасна: уже разыгранные моменты не трогаются,
    лишние неразыгранные снимаются, недостающие досоздаются (см.
    promotion.services.instant_prizes.generate_moments).
    """

    def post(self, request):
        result = instant_prizes_service.generate_moments()
        if result.get('error'):
            messages.error(request, result['error'])
        else:
            messages.success(request, (
                f'Расписание обновлено: создано {result["created"]}, '
                f'снято {result["removed"]}, оставлено без изменений {result["kept"]}.'
            ))
        return redirect('panel:instant_prizes')
