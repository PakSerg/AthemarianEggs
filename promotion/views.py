from datetime import datetime
import json
import logging

from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import render, redirect
from django.contrib import messages
from django.utils import timezone
from django.views import View
from psycopg2.extras import Json

from promotion.services.raffle_reminder import _week_num_on_date
from .models import AccountBlockedMessage, Raffle, User, Receipt, Prize, PromotionDrawResult, SbpBank
from main.models import ActionProducts, CompanyInfo
from .forms import (
    ParticipantLoginForm,
    ParticipantRegistrationForm,
    ManualChecksForm,
    ParticipantProfileForm,
    ChangeProfilePassword,
    EmailVerificationForm,
    ForgotPasswordForm,
    ResetPasswordForm,
)
from django.contrib.auth.mixins import LoginRequiredMixin
from functools import wraps
from django.conf import settings
from .context_processors import can_access_cabinet
from .tasks import process_participant_receipt
from .services.receipt_upload_limit import (
    ReceiptUploadRateLimited,
    ensure_receipt_upload_allowed,
    mark_receipt_uploaded,
    rate_limit_error_message,
)
from .services.email_verification import (
    create_and_send_code,
    verify_code,
    EmailVerificationPurpose,
    mark_code_sent,
    get_resend_cooldown_seconds_left,
    can_resend_code,
    format_resend_wait,
)
from .services.guaranteed_prize_email import send_guaranteed_prize_registered_email_if_needed
from .services.winner_prize_status import build_cards as build_prize_cards, claimable_prize_file
from .services.guaranteed_prize_payout import ensure_guaranteed_prize_payout
from .services import instant_prizes

logger = logging.getLogger(__name__)


class CabinetAccessMixin(LoginRequiredMixin):
    """Требует авторизации для доступа к личному кабинету.
    Доступ открыт всем пользователям, а не только суперпользователям/сотрудникам —
    ACTION_STARTED влияет только на видимость кнопки "Личный кабинет" на лендинге."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not can_access_cabinet(request.user):
            messages.error(request, 'Личный кабинет пока недоступен: акция ещё не началась')
            return redirect('main:home')
        return super().dispatch(request, *args, **kwargs)


def cabinet_access_required(view_func):
    """Аналог CabinetAccessMixin для функциональных view (AJAX-эндпоинты)."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not can_access_cabinet(request.user):
            return JsonResponse({'error': 'Личный кабинет пока недоступен: акция ещё не началась'}, status=403)
        return view_func(request, *args, **kwargs)
    return wrapper


class ParticipantLoginView(View):
    template_name = 'login.html'

    def get(self, request):

        form = ParticipantLoginForm()
        if request.user.is_authenticated:
            return redirect('participants:dashboard')
        email_verified = request.session.pop('email_verified_notice', False)
        return render(request, self.template_name, {'form': form, 'email_verified': email_verified})

    def post(self, request):
        form = ParticipantLoginForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})

        email = form.cleaned_data['email'].lower()
        password = form.cleaned_data['password']
        participant = User.objects.filter(email=email).first()

        if participant is None or not participant.check_password(password):
            messages.error(request, 'Неверный email или пароль')
            return render(request, self.template_name, {'form': form})

        if not participant.is_active:
            create_and_send_code(participant.email, EmailVerificationPurpose.REGISTRATION)
            mark_code_sent(request, EmailVerificationPurpose.REGISTRATION)
            request.session['pending_verification_email'] = participant.email
            messages.warning(
                request,
                'Email не подтверждён. Мы отправили код повторно — введите его для активации аккаунта.',
            )
            return redirect('participants:verify_email')

        login(request, participant)

        messages.success(request, 'Вы успешно вошли в аккаунт')
        return redirect('participants:dashboard')


class ParticipantRegisterView(View):
    template_name = 'register.html'

    def get(self, request):
        form = ParticipantRegistrationForm()
        if request.user.is_authenticated:
            return redirect('participants:dashboard')
        return render(request, self.template_name, {'form': form})

    def post(self, request):

        form = ParticipantRegistrationForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})

        email = form.cleaned_data['email']
        password = form.cleaned_data['password']

        participant = User.objects.filter(email=email).first()
        if participant and not participant.is_active:
            participant.set_password(password)
            participant.save(update_fields=['password'])
        else:
            participant = User(email=email, is_active=False)
            participant.set_password(password)
            participant.save()

        create_and_send_code(email, EmailVerificationPurpose.REGISTRATION)
        mark_code_sent(request, EmailVerificationPurpose.REGISTRATION)
        request.session['pending_verification_email'] = email

        messages.success(request, 'На ваш email отправлен код подтверждения')
        return redirect('participants:verify_email')


class ParticipantVerifyEmailView(View):
    template_name = 'verify_email.html'
    purpose = EmailVerificationPurpose.REGISTRATION

    def _get_email(self, request):
        return request.session.get('pending_verification_email', '').lower()

    def _render(self, request, form, email):
        return render(request, self.template_name, {
            'form': form,
            'email': email,
            'resend_seconds_left': get_resend_cooldown_seconds_left(request, self.purpose),
        })

    def get(self, request):
        email = self._get_email(request)
        if not email:
            if request.user.is_authenticated:
                return redirect('participants:dashboard')
            request.session['email_verified_notice'] = True
            messages.success(
                request,
                'Ссылка устарела или email уже подтверждён. Войдите с email и паролем, которые указали при регистрации.',
            )
            return redirect('participants:login')

        return self._render(request, EmailVerificationForm(), email)

    def post(self, request):
        email = self._get_email(request)
        if not email:
            if request.user.is_authenticated:
                return redirect('participants:dashboard')
            request.session['email_verified_notice'] = True
            messages.success(
                request,
                'Ссылка устарела или email уже подтверждён. Войдите с email и паролем, которые указали при регистрации.',
            )
            return redirect('participants:login')

        if 'resend' in request.POST:
            seconds_left = get_resend_cooldown_seconds_left(request, self.purpose)
            if not can_resend_code(request, self.purpose):
                messages.warning(
                    request,
                    f'Повторная отправка будет доступна через {format_resend_wait(seconds_left)}',
                )
                return self._render(request, EmailVerificationForm(), email)

            create_and_send_code(email, self.purpose)
            mark_code_sent(request, self.purpose)
            messages.success(request, 'Код отправлен повторно')
            return self._render(request, EmailVerificationForm(), email)

        form = EmailVerificationForm(request.POST)
        if not form.is_valid():
            return self._render(request, form, email)

        code = form.cleaned_data['code']
        if not verify_code(email, code, self.purpose):
            messages.error(request, 'Неверный или просроченный код')
            return self._render(request, form, email)

        participant = User.objects.filter(email=email).first()
        if participant is None:
            messages.error(request, 'Пользователь не найден')
            return redirect('participants:register')

        participant.is_active = True
        participant.save(update_fields=['is_active'])
        request.session.pop('pending_verification_email', None)

        login(request, participant)
        messages.success(request, 'Email подтверждён. Добро пожаловать!')
        return redirect('participants:dashboard')


class ParticipantForgotPasswordView(View):
    template_name = 'forgot_password.html'

    def get(self, request):
        return render(request, self.template_name, {
            'form': ForgotPasswordForm(),
        })

    def post(self, request):
        form = ForgotPasswordForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {
                'form': form,
            })

        email = form.cleaned_data['email']
        participant = User.objects.filter(email=email, is_active=True).first()
        if participant:
            create_and_send_code(email, EmailVerificationPurpose.PASSWORD_RESET)
            mark_code_sent(request, EmailVerificationPurpose.PASSWORD_RESET)
            request.session['password_reset_email'] = email

        messages.success(
            request,
            'Если аккаунт с таким email существует, мы отправили код для смены пароля.',
        )
        return redirect('participants:reset_password')


class ParticipantResetPasswordView(View):
    template_name = 'reset_password.html'
    purpose = EmailVerificationPurpose.PASSWORD_RESET

    def _get_email(self, request):
        return request.session.get('password_reset_email', '').lower()

    def _render(self, request, form, email):
        return render(request, self.template_name, {
            'form': form,
            'email': email,
            'resend_seconds_left': get_resend_cooldown_seconds_left(request, self.purpose),
        })

    def get(self, request):
        email = self._get_email(request)
        if not email:
            messages.error(request, 'Сначала укажите email для восстановления пароля')
            return redirect('participants:forgot_password')

        return self._render(request, ResetPasswordForm(), email)

    def post(self, request):
        email = self._get_email(request)
        if not email:
            messages.error(request, 'Сначала укажите email для восстановления пароля')
            return redirect('participants:forgot_password')

        if 'resend' in request.POST:
            seconds_left = get_resend_cooldown_seconds_left(request, self.purpose)
            if not can_resend_code(request, self.purpose):
                messages.warning(
                    request,
                    f'Повторная отправка будет доступна через {format_resend_wait(seconds_left)}',
                )
                return self._render(request, ResetPasswordForm(), email)

            create_and_send_code(email, self.purpose)
            mark_code_sent(request, self.purpose)
            messages.success(request, 'Код отправлен повторно')
            return self._render(request, ResetPasswordForm(), email)

        form = ResetPasswordForm(request.POST)
        if not form.is_valid():
            return self._render(request, form, email)

        code = form.cleaned_data['code']
        if not verify_code(email, code, self.purpose):
            messages.error(request, 'Неверный или просроченный код')
            return self._render(request, form, email)

        participant = User.objects.filter(email=email, is_active=True).first()
        if participant is None:
            messages.error(request, 'Пользователь не найден')
            return redirect('participants:forgot_password')

        participant.set_password(form.cleaned_data['password'])
        participant.save(update_fields=['password'])
        request.session.pop('password_reset_email', None)

        messages.success(request, 'Пароль успешно изменён. Войдите с новым паролем.')
        return redirect('participants:login')


def is_valid_req_data_user(participant):
    """Проверяет, что у участника заполнены обязательные для регистрации чеков поля."""
    REQ_FIELD = ['first_name', 'last_name', 'phone', 'bank']

    for field in REQ_FIELD:
        if not getattr(participant, field, None):
            return False
    return True


class ParticipantDashboardView(CabinetAccessMixin, View):
    template_name = 'dashboard.html'

    SORT_FIELDS = {
        'date': 'date',
        'status': 'status',
        'amount': 'amount',
        'date_registration': 'created_at',
    }

    def _build_query(self, request, **updates):
        query = request.GET.copy()
        for key, value in updates.items():
            if value is None:
                query.pop(key, None)
            else:
                query[key] = str(value)
        return query.urlencode()

    @staticmethod
    def _pagination_page_numbers(current_page, total_pages):
        """Первая, последняя, текущая и по одной соседней с каждой стороны."""
        if total_pages <= 0:
            return []
        pages = {1, total_pages, current_page}
        if current_page > 1:
            pages.add(current_page - 1)
        if current_page < total_pages:
            pages.add(current_page + 1)
        return sorted(pages)

    def _valid_req_data_user(self, participant):
        return is_valid_req_data_user(participant)

    @staticmethod
    def _showcase_context():
        """Витрины призов и товаров для кабинета — те же данные, что на лендинге.

        Секции лендинга переиспользуются как есть, поэтому им нужен тот же
        контекст: блоки текстов (ContentBlock) и наборы призов/товаров.
        """
        from main.views import showcase_context, visible_blocks

        return {'blocks': visible_blocks(), **showcase_context()}

    def _build_dashboard_context(self, request, participant, form_manual_check):
        selected_sort = request.GET.get('sort', 'date_registration')
        selected_direction = request.GET.get('direction', 'desc')
        if selected_sort not in self.SORT_FIELDS:
            selected_sort = 'date_registration'
        if selected_direction not in {'asc', 'desc'}:
            selected_direction = 'desc'

        order_field = self.SORT_FIELDS[selected_sort]
        order_by = f"-{order_field}" if selected_direction == 'desc' else order_field
        checks_queryset = Receipt.objects.filter(participant=participant).order_by(order_by)

        paginator = Paginator(checks_queryset, 6)
        page_obj = paginator.get_page(request.GET.get('page'))

        sort_links = {}
        for sort_key in self.SORT_FIELDS:
            next_direction = 'asc'
            if selected_sort == sort_key and selected_direction == 'asc':
                next_direction = 'desc'
            sort_links[sort_key] = self._build_query(
                request,
                sort=sort_key,
                direction=next_direction,
                page=None,
            )

        page_items = [
            (page_num, self._build_query(request, page=page_num))
            for page_num in self._pagination_page_numbers(
                page_obj.number, paginator.num_pages
            )
        ]

        is_valid_req_data_user = self._valid_req_data_user(participant)

        win_prizes = PromotionDrawResult.objects.select_related('prize', 'receipt').filter(
            participant=participant, is_reserve=False,
        )
        win_prize_cards = build_prize_cards(win_prizes)

        desktop_products = ActionProducts.objects.filter(
            show_in_products_block=True
        ).order_by('products_block_order')[:8]

        context = {
            # Витрины призов и товаров — те же секции и те же данные, что на
            # лендинге (см. _showcase_context): отдельной вёрстки для кабинета нет.
            **self._showcase_context(),
            'form_manual_check': form_manual_check,
            'checks': page_obj.object_list,
            'page_obj': page_obj,
            'paginator': paginator,
            'is_paginated': page_obj.has_other_pages(),
            'checks_total': checks_queryset.count(),
            'win_prizes_total': checks_queryset.filter(status='winner').count(),
            'selected_sort': selected_sort,
            'selected_direction': selected_direction,
            'sort_links': sort_links,
            'page_items': page_items,
            'prev_page_link': self._build_query(request,
                                                page=page_obj.previous_page_number()) if page_obj.has_previous() else '',
            'next_page_link': self._build_query(request,
                                                page=page_obj.next_page_number()) if page_obj.has_next() else '',
            'is_valid_req_data_user': is_valid_req_data_user,
            'win_prize_cards': win_prize_cards,
            'desktop_products': desktop_products,
            'account_blocked_message': AccountBlockedMessage.load().text,
        }

        return context

    def get(self, request):
        participant = request.user
        form_manual_check = ManualChecksForm()
        context = self._build_dashboard_context(request, participant, form_manual_check)

        if not context['is_valid_req_data_user']:
            messages.error(request, 'Для регистрации чека необходимо заполнить данные в профиле')

        return render(request, self.template_name, context=context)

    def post(self, request):
        participant = request.user
        form_manual_check = ManualChecksForm(request.POST)

        if participant.is_blocked:
            messages.error(request, AccountBlockedMessage.load().text)
            context = self._build_dashboard_context(request, participant, form_manual_check)
            return render(request, self.template_name, context=context)

        if not is_valid_req_data_user(participant):
            messages.error(request, 'Для регистрации чека необходимо заполнить данные в профиле')
            context = self._build_dashboard_context(request, participant, form_manual_check)
            return render(request, self.template_name, context=context)

        if form_manual_check.is_valid():
            try:
                ensure_receipt_upload_allowed(participant)
            except ReceiptUploadRateLimited as exc:
                messages.error(request, rate_limit_error_message(exc.retry_after))
            else:
                check = form_manual_check.save(commit=False)
                check.participant = participant
                check.input_method = Receipt.InputMethod.MANUAL

                if not form_manual_check.errors:
                    check.save()
                    mark_receipt_uploaded(participant)
                    process_participant_receipt.delay(check.id)
                    messages.success(
                        request,
                        'Чек принят. Идёт проверка — статус обновится через несколько секунд.',
                    )

                    return redirect('participants:dashboard')

        context = self._build_dashboard_context(request, participant, form_manual_check)
        return render(request, self.template_name, context=context)


class ParticipantPrizeFileView(CabinetAccessMixin, View):
    """
    Отдаёт победителю файл его электронного приза («Забрать приз» в кабинете).

    Файлы призов лежат в media и по прямой ссылке доступны кому угодно, поэтому
    наружу торчит не путь к файлу, а номер итога розыгрыша: проверяем, что итог
    принадлежит этому участнику и что приз действительно можно забрать
    (см. winner_prize_status.claimable_prize_file), и только тогда отдаём файл.
    """

    def get(self, request, draw_result_id):
        draw_result = (
            PromotionDrawResult.objects
            .select_related('prize', 'receipt')
            .filter(pk=draw_result_id, participant=request.user, is_reserve=False)
            .first()
        )
        if draw_result is None:
            raise Http404('Приз не найден')

        prize_file = claimable_prize_file(draw_result)
        if prize_file is None:
            raise Http404('Приз ещё не готов к выдаче')

        return FileResponse(
            prize_file.file.open('rb'), as_attachment=True, filename=prize_file.name,
        )


class ParticipantLogoutView(View):

    def get(self, request):
        logout(request)
        messages.success(request, 'Вы вышли из аккаунта')
        return redirect('participants:login')


class ParticipantProfileView(CabinetAccessMixin, View):
    template_name = 'profile.html'

    def get(self, request):
        participant = request.user

        form_profile = ParticipantProfileForm(instance=participant)
        form_change_profile_password = ChangeProfilePassword(participant=participant)
        
        reload_form_type = 'main'

        context = {
            'form_profile': form_profile,
            'form_change_profile_password': form_change_profile_password,
            'participant': participant,
            'dadata_token': getattr(settings, 'DADATA_API_KEY', ''),
            'reload_form_type': reload_form_type,
        }

        return render(request, self.template_name, context=context)

    def post(self, request):
        participant = request.user

        form_type = request.POST.get('form_type', 'profile')

        if form_type == 'subscription':
            subscribed = request.POST.get('is_subscribed_receipt_emails') == 'on'
            User.objects.filter(pk=participant.pk).update(is_subscribed_receipt_emails=subscribed)
            messages.success(request, 'Настройки рассылки сохранены')
            from django.urls import reverse
            return redirect(reverse('participants:profile') + '?tab=notifications')

        if form_type == 'password':
            form_profile = ParticipantProfileForm(instance=participant)
            form_change_profile_password = ChangeProfilePassword(request.POST, participant=participant)

            if form_change_profile_password.is_valid():
                new_password_1 = form_change_profile_password.cleaned_data['new_password_1']
                participant.set_password(new_password_1)
                participant.last_login = timezone.now()
                participant.save(update_fields=['last_login', 'password'])
                messages.success(request, 'Пароль успешно изменен')
                return redirect('participants:profile')

            messages.error(request, 'Ошибка при изменении пароля')

            return render(
                request,
                self.template_name,
                {
                    'form_profile': form_profile,
                    'form_change_profile_password': form_change_profile_password,
                    'participant': participant,
                    'dadata_token': getattr(settings, 'DADATA_API_KEY', ''),
                    'reload_form_type': 'sucuri',
                },
            )

        form_profile = ParticipantProfileForm(request.POST, instance=participant, files=request.FILES)
        form_change_profile_password = ChangeProfilePassword(participant=participant)

        if form_profile.is_valid():
            form_profile.save()
            send_guaranteed_prize_registered_email_if_needed(participant)
            ensure_guaranteed_prize_payout(participant)
            messages.success(request, 'Профиль обновлен')
            return redirect('participants:profile')

        for errors in form_profile.errors.values():
            if errors:
                messages.error(request, errors[0])
                break

        return render(
            request,
            self.template_name,
            {
                'form_profile': form_profile,
                'form_change_profile_password': form_change_profile_password,
                'participant': participant,
                'dadata_token': getattr(settings, 'DADATA_API_KEY', ''),
                'reload_form_type': 'main',
            },
        )


SBP_BANKS_SYNC_LOCK_KEY = 'sbp_banks_suggest:sync_lock'
SBP_BANKS_SYNC_LOCK_TIMEOUT = 300  # Cyclops list_bank_sbp: не чаще 1 запроса в 5 минут


@login_required
def sbp_banks_suggest(request):
    """Подсказки банков для профиля — только банки, поддерживающие СБП (справочник SbpBank из Cyclops)."""
    if not SbpBank.objects.exists() and cache.add(SBP_BANKS_SYNC_LOCK_KEY, True, SBP_BANKS_SYNC_LOCK_TIMEOUT):
        # Справочник обновляется раз в месяц по расписанию (promotion.tasks.sync_sbp_banks) —
        # если он ещё пуст (первый запуск, ещё не было плановой синхронизации), подтягиваем
        # его прямо здесь, чтобы подсказки в профиле не оставались пустыми до начала следующего месяца.
        try:
            from .services.cyclops_sbp import sync_sbp_banks
            sync_sbp_banks()
        except Exception:
            logger.exception('sbp_banks_suggest: не удалось синхронизировать справочник банков СБП')

    query = request.GET.get('q', '').strip()
    banks_qs = SbpBank.objects.all()
    if query:
        banks_qs = banks_qs.filter(
            Q(name_rus__icontains=query) | Q(name__icontains=query) | Q(bank_code__icontains=query)
        )

    suggestions = []
    seen_bik = set()
    for bank in banks_qs.order_by('name_rus')[:20]:
        bik = bank.bank_code
        if not bik or bik in seen_bik:
            continue
        seen_bik.add(bik)
        suggestions.append({'name': bank.name_rus or bank.name, 'bic': bik})

    return JsonResponse({'suggestions': suggestions})


@login_required
@cabinet_access_required
def upload_check_image(request):
    if 'check_photo' not in request.FILES:
        return JsonResponse({'error': 'No file uploaded'}, status=400)

    if request.user.is_blocked:
        return JsonResponse({'error': AccountBlockedMessage.load().text}, status=403)

    if not is_valid_req_data_user(request.user):
        return JsonResponse(
            {'error': 'Для регистрации чека необходимо заполнить данные в профиле'},
            status=400,
        )

    try:
        ensure_receipt_upload_allowed(request.user)
    except ReceiptUploadRateLimited as exc:
        return JsonResponse(
            {
                'error': rate_limit_error_message(exc.retry_after),
                'retry_after': exc.retry_after,
            },
            status=429,
        )

    try:
        file = request.FILES['check_photo']

        check = Receipt.objects.create(
            participant=request.user,
            receipt_image=file,
            input_method=Receipt.InputMethod.PHOTO,
        )
        mark_receipt_uploaded(request.user)

        process_participant_receipt.delay(check.id)

        return JsonResponse({
            'success': True,
            'check_id': check.id,
            'photo_url': check.receipt_image.url,
            'message': 'Чек принят. Идёт проверка по данным ФНС.',
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required()
@cabinet_access_required
def upload_check_qr(request):
    if request.user.is_blocked:
        return JsonResponse({'error': AccountBlockedMessage.load().text}, status=403)

    if not is_valid_req_data_user(request.user):
        return JsonResponse(
            {'error': 'Для регистрации чека необходимо заполнить данные в профиле'},
            status=400,
        )

    try:
        ensure_receipt_upload_allowed(request.user)
    except ReceiptUploadRateLimited as exc:
        return JsonResponse(
            {
                'error': rate_limit_error_message(exc.retry_after),
                'retry_after': exc.retry_after,
            },
            status=429,
        )

    try:
        data = json.loads(request.body)
        code = data.get('code')

        check = Receipt.objects.create(
            participant=request.user,
            qr_code_str=code,
            input_method=Receipt.InputMethod.CAMERA,
        )
        mark_receipt_uploaded(request.user)

        process_participant_receipt.delay(check.id)

        return JsonResponse({
            'success': True,
            'check_id': check.id,
            'message': 'Чек принят. Идёт проверка по данным ФНС.',
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def unsubscribe_receipt_emails(request, token):
    from .services.unsubscribe import parse_unsubscribe_token
    user_id = parse_unsubscribe_token(token)
    if user_id is None:
        return render(request, 'unsubscribe.html', {'error': True})
    User.objects.filter(pk=user_id).update(is_subscribed_receipt_emails=False)
    return render(request, 'unsubscribe.html', {'success': True})


def delete_all_receipts(request):
    if not settings.DEBUG:
        return JsonResponse({'status': 'error',})
    receipts = Receipt.objects.all()
    receipts.delete()
    return JsonResponse({'status': 'ok',})


def email_preview(request, template_name):
    """Просмотр HTML-писем в браузере. Только при DEBUG=True."""
    if not settings.DEBUG:
        from django.http import Http404
        raise Http404

    from .services.email_templates import base_email_context
    from .services.unsubscribe import build_unsubscribe_url

    previews = {
        'verification_code': ('verification_code', base_email_context(
            email_title='Подтверждение регистрации',
            heading='Подтверждение регистрации',
            intro_text='Введите код ниже на сайте, чтобы завершить\nрегистрацию в акции.',
            code='711834',
            ttl_minutes=15,
            action_url='#',
            action_label='Подтвердить email',
            footer_note='Если вы не регистрировались на <a href="https://af-promo.ru" style="color:#8B93AD;">af-promo.ru</a>, проигнорируйте это письмо.',
        )),
        'winner': ('winner', base_email_context(
            recipient_name='Елена',
            prize_name='Сертификат ДИГИФТ номиналом 3000 рублей',
            contract_link='https://desktop.doki.online/contract/6a2a29df57cee39070c35e5b',
        )),
        'receipt_confirmed': ('receipt_confirmed', base_email_context(
            receipt_id='482913',
            amount='1250.00',
            cabinet_url='#',
            unsubscribe_url='#',
        )),
        'receipt_rejected': ('receipt_rejected', base_email_context(
            receipt_id='482913',
            reason='Чек не соответствует условиям акции или не прошёл проверку',
            cabinet_url='#',
            unsubscribe_url='#',
        )),
        'guaranteed_prize_registered': ('guaranteed_prize_generic', _guaranteed_prize_preview_context(
            'registered', base_email_context, unsubscribe_url='#',
        )),
        'guaranteed_prize_sent': ('guaranteed_prize_generic', _guaranteed_prize_preview_context(
            'sent', base_email_context, unsubscribe_url='#',
        )),
    }

    entry = previews.get(template_name)
    if entry is None:
        from django.http import Http404
        raise Http404
    template_file, context = entry

    return render(request, f'emails/{template_file}.html', context)


def _guaranteed_prize_preview_context(code, base_email_context, **extra):
    from .models import GuaranteedPrizeEmailTemplate
    from .services.guaranteed_prize_email import _SafeFormatDict

    template = GuaranteedPrizeEmailTemplate.objects.filter(code=code).first()
    safe = _SafeFormatDict(recipient_name='Мария Петрова', prize_amount='50 рублей')
    subject = template.subject.format_map(safe) if template else '(шаблон не настроен в админке)'
    body_text = template.text.format_map(safe) if template else '(шаблон не настроен в админке)'
    return base_email_context(recipient_name='Мария Петрова', subject=subject, body_text=body_text, **extra)

@login_required
@require_POST
def instant_play(request):
    """
    Открыть яйцо: потратить попытку и вернуть исход.

    Исход считается здесь и сейчас (promotion.services.instant_prizes.play) и
    ни в каком виде не попадает в страницу заранее — «подсмотреть» выигрышное
    яйцо в разметке или в предыдущем ответе невозможно.
    """
    try:
        egg = int((json.loads(request.body or '{}') or {}).get('egg', 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({'ok': False, 'error': 'Не выбрано яйцо.'}, status=400)

    try:
        result = instant_prizes.play(request.user, egg)
    except instant_prizes.InstantPlayError as exc:
        return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
    except Exception:
        logger.exception('instant_play failed participant_id=%s', request.user.pk)
        return JsonResponse(
            {'ok': False, 'error': 'Не получилось открыть яйцо. Попробуйте ещё раз.'},
            status=500,
        )

    payload = {
        'ok': True,
        'egg': result.egg,
        'is_win': result.is_win,
        'attempts_left': result.attempts_left,
    }
    if result.is_win and result.prize is not None:
        payload['prize'] = {
            'name': result.prize.name or 'Приз',
            'description': result.prize.description or '',
            'image': result.prize.image.url if result.prize.image else '',
        }
    return JsonResponse(payload)
