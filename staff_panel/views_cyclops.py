"""Интерфейс управления сервисом Cyclops (Точка Банк) в панели персонала.

Разделы: синхронизация банков СБП, платежи, бенефициары (регистрация),
идентификация платежа, настройки плательщика призового фонда.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from promotion.models import (
    CyclopsBeneficiary,
    CyclopsDocument,
    CyclopsPayment,
    CyclopsSettings,
    CyclopsVirtualAccount,
    GuaranteedPrizePayout,
    PayoutRegistry,
    SbpBank,
    User,
)
from promotion.services import cyclops_documents, cyclops_manager
from promotion.services import guaranteed_prize_payout as gpp
from promotion.services.cyclops_sbp import sync_sbp_banks as sync_sbp_banks_service

from .services import payouts as payouts_services
from .services.access import can_view_cyclops, user_has_panel_access
from .services.query import QueryMixin
from .services.xlsx_utils import build_list_workbook, workbook_http_response
from .views import PanelAccessMixin, PanelWriteAccessMixin

logger = logging.getLogger(__name__)


class CyclopsAccessMixin(PanelAccessMixin):
    """Cyclops (выплаты через Точка Банк) скрыт от менеджера по чекам."""

    def test_func(self):
        return user_has_panel_access(self.request.user) and can_view_cyclops(self.request.user)


class CyclopsWriteAccessMixin(PanelWriteAccessMixin):
    """Как PanelWriteAccessMixin, но также скрывает Cyclops от менеджера по чекам."""

    def test_func(self):
        return user_has_panel_access(self.request.user) and can_view_cyclops(self.request.user)


class CyclopsView(CyclopsAccessMixin, QueryMixin, TemplateView):
    template_name = 'panel/cyclops.html'
    active_nav = 'cyclops'

    PAYOUT_SORT_FIELDS = {
        'created_at': 'created_at',
        'paid_at': 'paid_at',
        'updated_at': 'updated_at',
        'amount': 'amount',
        # Сортируем по осмысленному порядку этапов, а не по алфавиту их ключей
        # (см. payouts.annotate_stage).
        'stage': 'stage_order',
        'cnt_retry': 'cnt_retry',
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings_obj = CyclopsSettings.load()
        base_url = settings.CYCLOPS_CONFIG.get('BASE_URL', '')
        # Виртуальные счета основного плательщика — для идентификации платежей
        payout_accounts = []
        if settings_obj.payout_beneficiary_id:
            payout_accounts = CyclopsVirtualAccount.objects.filter(
                beneficiary_id=settings_obj.payout_beneficiary_id,
            )
        payer_total_balance = sum((va.available_balance for va in payout_accounts), Decimal('0'))

        beneficiaries = list(CyclopsBeneficiary.objects.prefetch_related(
            'virtual_accounts',
            Prefetch(
                'documents',
                queryset=CyclopsDocument.objects.filter(
                    document_type=CyclopsDocument.TYPE_CONTRACT_OFFER,
                ).order_by('-created_at'),
                to_attr='contract_offer_docs',
            ),
        ).all())
        # Деньги в таблице бенефициаров плохо заметны построчно — считаем сумму
        # по виртуальным счетам каждого и выносим главного (текущего плательщика)
        # первой строкой, чтобы призовой фонд сразу бросался в глаза.
        for b in beneficiaries:
            b.total_balance = sum((va.available_balance for va in b.virtual_accounts.all()), Decimal('0'))
            b.is_payer = bool(settings_obj.payout_beneficiary_id) and b.pk == settings_obj.payout_beneficiary_id
        beneficiaries.sort(key=lambda b: (not b.is_payer, b.name))

        context.update(self._payouts_context())
        context.update({
            'settings_obj': settings_obj,
            'beneficiaries': beneficiaries,
            'payer_total_balance': payer_total_balance,
            'virtual_accounts': CyclopsVirtualAccount.objects.select_related('beneficiary').all(),
            'payout_accounts': payout_accounts,
            'payments': CyclopsPayment.objects.select_related('beneficiary').all()[:200],
            'unidentified_payments': CyclopsPayment.objects.filter(identify=False),
            # Только вручную загруженные шаблоны (без deal_id) — сгенерированные
            # персонально на каждую выплату документы сюда не попадают, иначе
            # список зарастёт сотнями одноразовых PDF.
            'service_agreements': CyclopsDocument.objects.filter(
                document_type=CyclopsDocument.TYPE_SERVICE_AGREEMENT,
            ).filter(Q(deal_id='') | Q(deal_id__isnull=True)),
            'sbp_bank_count': SbpBank.objects.count(),
            'beneficiary_types': CyclopsBeneficiary.TYPE_CHOICES,
            'is_test_layer': settings.CYCLOPS_CONFIG.get('USE_TEST_KEY', True),
            'nominal_account': settings.CYCLOPS_CONFIG.get('NOMINAL_ACCOUNT', ''),
            # Индикатор слоя: на боевом слое каждое действие в этом разделе
            # трогает реальные деньги, и это должно быть видно сразу.
            'cyclops_base_url': base_url,
            'cyclops_is_prod': base_url.startswith('https://api.tochka.com'),
            'cyclops_thumbprint': settings.CYCLOPS_CONFIG.get('CERT_THUMBPRINT', ''),
        })
        return context

    @classmethod
    def build_payouts_context(cls, request):
        """Контекст раздела выплат вне рендера страницы — для выгрузки в Excel,
        чтобы экран и выгрузка гарантированно понимали фильтры одинаково."""
        view = cls()
        view.request = request
        return view._payouts_context()

    def _payouts_context(self):
        """Контекст раздела «Выплаты гарантированного приза».

        Фильтры здесь намеренно не повторяют list_filter из админки: письма об
        итоге («приз отправлен», «ошибка ФИО», «банк отклонил») и признак
        ручного запуска — внутренняя кухня рассылок, по ней никто не выбирает
        данные. Вместо них — то, чем реально пользуются: поиск по участнику,
        человеческий этап выплаты (payouts.STAGES), реестр и периоды дат.
        """
        request = self.request
        base_qs = payouts_services.annotate_stage(
            GuaranteedPrizePayout.objects.select_related('participant', 'document', 'registry'),
        )

        search = request.GET.get('payout_q', '').strip()
        registry_id = request.GET.get('payout_registry', '')
        created_from = self.parse_date_param(request.GET.get('payout_created_from'))
        created_to = self.parse_date_param(request.GET.get('payout_created_to'))
        paid_from = self.parse_date_param(request.GET.get('payout_paid_from'))
        paid_to = self.parse_date_param(request.GET.get('payout_paid_to'))
        stages = [v for v in request.GET.getlist('payout_stage') if v in payouts_services.STAGE_LABELS]

        # Все фильтры, КРОМЕ этапа, применяются отдельно: на их основе строится
        # разбивка по этапам ниже. Иначе при выборе одного этапа вся сводка
        # схлопнулась бы в него же и перестала показывать общую картину.
        filtered_qs = payouts_services.apply_search(base_qs, search)
        if registry_id:
            filtered_qs = filtered_qs.filter(registry_id=registry_id)
        if created_from:
            filtered_qs = filtered_qs.filter(created_at__date__gte=created_from)
        if created_to:
            filtered_qs = filtered_qs.filter(created_at__date__lte=created_to)
        if paid_from:
            filtered_qs = filtered_qs.filter(paid_at__date__gte=paid_from)
        if paid_to:
            filtered_qs = filtered_qs.filter(paid_at__date__lte=paid_to)

        breakdown = payouts_services.stage_breakdown(filtered_qs)
        summary = payouts_services.summarize(breakdown)

        payouts_qs = filtered_qs
        if stages:
            payouts_qs = payouts_qs.filter(stage__in=stages)

        selected = [row for row in breakdown if not stages or row['value'] in stages]

        selected_sort, selected_direction, sort_context = self.build_sort_context(
            request, self.PAYOUT_SORT_FIELDS, default_sort='created_at',
        )
        payouts_qs = self.apply_model_sort(
            payouts_qs, self.PAYOUT_SORT_FIELDS, selected_sort, selected_direction,
            default_sort='created_at',
        )
        # Ссылки сортировки/пагинации должны возвращать на якорь секции —
        # иначе после клика страница прыгает наверх, к разделу «Плательщик».
        for col in sort_context.values():
            col['link'] = f"{col['link']}#payouts"
            col['reset_link'] = f"{col['reset_link']}#payouts"

        paginator = Paginator(payouts_qs, self.get_paginate_by(None))
        page_obj = paginator.get_page(request.GET.get('page'))
        pagination_ctx = self.pagination_context(request, page_obj)
        pagination_ctx['prev_page_link'] = (
            f"{pagination_ctx['prev_page_link']}#payouts" if pagination_ctx['prev_page_link'] else ''
        )
        pagination_ctx['next_page_link'] = (
            f"{pagination_ctx['next_page_link']}#payouts" if pagination_ctx['next_page_link'] else ''
        )
        pagination_ctx['page_items'] = [
            (num, f'{link}#payouts' if link else link) for num, link in pagination_ctx['page_items']
        ]

        # Плашка этапа на каждой строке — чтобы шаблон не повторял логику
        # раскладки статусов, уже посчитанную в SQL (annotate_stage).
        stage_meta = {row['value']: row for row in payouts_services.STAGES}
        for payout in page_obj:
            payout.stage_meta = stage_meta.get(payout.stage)

        # Карточки сводки — заодно и фильтр: клик по карточке сужает выборку до
        # этого этапа, сохраняя поиск и периоды и сбрасывая страницу.
        for row in breakdown:
            row['link'] = self.build_query(request, page=None, payout_stage=row['value']) + '#payouts'
            row['is_selected'] = row['value'] in stages

        export_query = request.GET.copy()
        export_query.pop('page', None)

        return {
            'payouts': page_obj,
            'payout_stages': payouts_services.STAGES,
            'payout_registries': PayoutRegistry.objects.all()[:200],
            'payout_stats': {
                'selected_count': sum(row['count'] for row in selected),
                'selected_amount': sum(row['amount'] for row in selected),
                'stage_breakdown': breakdown,
                **summary,
            },
            'payout_export_query': export_query.urlencode(),
            'selected_sort': selected_sort,
            'selected_direction': selected_direction,
            'sort_context': sort_context,
            'payout_filters': {
                'q': search,
                'stages': stages,
                'registry': registry_id,
                'created_from': request.GET.get('payout_created_from', ''),
                'created_to': request.GET.get('payout_created_to', ''),
                'paid_from': request.GET.get('payout_paid_from', ''),
                'paid_to': request.GET.get('payout_paid_to', ''),
                'is_active': bool(
                    search or stages or registry_id
                    or request.GET.get('payout_created_from') or request.GET.get('payout_created_to')
                    or request.GET.get('payout_paid_from') or request.GET.get('payout_paid_to')
                ),
            },
            **pagination_ctx,
        }


class CyclopsPayoutExportView(CyclopsAccessMixin, QueryMixin, View):
    """Выгрузка выплат гарантированного приза в Excel — ровно та выборка, что
    сейчас на экране (учитываются все фильтры и сортировка раздела)."""

    HEADERS = [
        'Email', 'Фамилия', 'Имя', 'Отчество', 'Телефон', 'БИК банка', 'Сумма',
        'Этап', 'Технический статус', 'Попыток', 'Причина ошибки', 'ID сделки',
        'Реестр', 'Создана', 'Отправлена в банк', 'Выплачена', 'Обновлена',
        'Отказ от приза', 'Запущена вручную',
    ]

    def get(self, request):
        # Выборка собирается тем же кодом, что и таблица на странице, — иначе
        # выгрузка и экран рано или поздно разъедутся по смыслу фильтров.
        context = CyclopsView.build_payouts_context(request)
        queryset = context['payouts'].paginator.object_list

        stage_labels = payouts_services.STAGE_LABELS

        def _dt(value):
            return timezone.localtime(value).strftime('%d.%m.%Y %H:%M') if value else ''

        rows = []
        for payout in queryset.iterator(chunk_size=500):
            participant = payout.participant
            rows.append([
                participant.email,
                participant.last_name or '',
                participant.first_name or '',
                participant.middle_name or '',
                payout.phone_number or participant.phone or '',
                participant.bank_bik or '',
                float(payout.amount),
                stage_labels.get(payout.stage, payout.stage),
                payout.get_status_display(),
                payout.cnt_retry,
                payout.error_reason or '',
                payout.deal_id or '',
                payout.registry.registry_number if payout.registry_id else '',
                _dt(payout.created_at),
                _dt(payout.sent_at),
                _dt(payout.paid_at),
                _dt(payout.updated_at),
                'да' if participant.guaranteed_prize_opt_out else 'нет',
                'да' if payout.sent_manually else 'нет',
            ])

        wb = build_list_workbook('Выплаты приза', self.HEADERS, rows)
        return workbook_http_response(wb, 'guaranteed_prize_payouts')


class CyclopsServiceAgreementPreviewView(CyclopsAccessMixin, View):
    """Тестовая генерация Реестра выплат с фиктивными данными — чтобы
    проверить вёрстку шаблона без реальной выплаты."""

    def get(self, request):
        from promotion.services import cyclops_registry

        xlsx_bytes = cyclops_registry.render_preview_xlsx()
        response = HttpResponse(
            xlsx_bytes, content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = 'inline; filename="registry_preview.xlsx"'
        return response


class CyclopsSyncSbpBanksView(CyclopsWriteAccessMixin, View):
    def post(self, request):
        try:
            count = sync_sbp_banks_service()
            messages.success(request, f'Справочник банков СБП обновлён: {count} банков')
        except Exception as e:
            logger.error('cyclops_sync_sbp_banks failed: %s', e, exc_info=True)
            messages.error(request, f'Ошибка синхронизации банков СБП: {e}')
        return redirect(reverse('panel:cyclops') + '#banks')


class CyclopsSyncPaymentsView(CyclopsWriteAccessMixin, View):
    def post(self, request):
        try:
            count = cyclops_manager.sync_payments()
            messages.success(request, f'Платежи обновлены: {count}')
        except Exception as e:
            logger.error('cyclops_sync_payments failed: %s', e, exc_info=True)
            messages.error(request, f'Ошибка синхронизации платежей: {e}')
        return redirect(reverse('panel:cyclops') + '#payments')


class CyclopsSyncBeneficiariesView(CyclopsWriteAccessMixin, View):
    def post(self, request):
        try:
            count = cyclops_manager.sync_beneficiaries()
            messages.success(request, f'Бенефициары обновлены: {count}')
        except Exception as e:
            logger.error('cyclops_sync_beneficiaries failed: %s', e, exc_info=True)
            messages.error(request, f'Ошибка синхронизации бенефициаров: {e}')
        return redirect(reverse('panel:cyclops') + '#beneficiaries')


class CyclopsBeneficiaryCreateView(CyclopsWriteAccessMixin, View):
    def post(self, request):
        client_type = request.POST.get('client_type')

        def _dec(name):
            raw = (request.POST.get(name) or '').strip()
            if not raw:
                return None
            try:
                return Decimal(raw)
            except InvalidOperation:
                return None

        try:
            beneficiary = cyclops_manager.register_beneficiary(
                client_type=client_type,
                name=(request.POST.get('name') or '').strip(),
                inn=(request.POST.get('inn') or '').strip(),
                kpp=(request.POST.get('kpp') or '').strip() or None,
                ogrn=(request.POST.get('ogrn') or '').strip() or None,
                first_name=(request.POST.get('first_name') or '').strip() or None,
                last_name=(request.POST.get('last_name') or '').strip() or None,
                middle_name=(request.POST.get('middle_name') or '').strip() or None,
                document=request.FILES.get('document'),
                legal_address=(request.POST.get('legal_address') or '').strip() or None,
                bank_account=(request.POST.get('bank_account') or '').strip() or None,
                bank_name=(request.POST.get('bank_name') or '').strip() or None,
                bank_bic=(request.POST.get('bank_bic') or '').strip() or None,
                bank_corr_account=(request.POST.get('bank_corr_account') or '').strip() or None,
                contact_email=(request.POST.get('contact_email') or '').strip() or None,
                signatory_name=(request.POST.get('signatory_name') or '').strip() or None,
                signatory_basis=(request.POST.get('signatory_basis') or '').strip() or None,
                commission_percent=_dec('commission_percent'),
                commission_min_amount=_dec('commission_min_amount'),
                commission_fixed_amount=_dec('commission_fixed_amount'),
            )
            messages.success(request, f'Бенефициар «{beneficiary.name}» зарегистрирован')
        except Exception as e:
            logger.error('cyclops_beneficiary_create failed: %s', e, exc_info=True)
            messages.error(request, f'Ошибка регистрации бенефициара: {e}')
        return redirect(reverse('panel:cyclops') + '#beneficiaries')


class CyclopsBeneficiaryToggleActiveView(CyclopsWriteAccessMixin, View):
    """Активация/деактивация бенефициара — по договору с Точкой площадка обязана
    поддерживать актуальный статус бенефициаров (activate/deactivate_beneficiary)."""

    def post(self, request, pk):
        active = request.POST.get('active') == '1'
        try:
            beneficiary = cyclops_manager.set_beneficiary_active(pk, active)
            action = 'активирован' if active else 'деактивирован'
            messages.success(request, f'Бенефициар «{beneficiary.name}» {action}')
        except Exception as e:
            logger.error('cyclops_beneficiary_toggle_active failed: %s', e, exc_info=True)
            messages.error(request, f'Ошибка изменения статуса бенефициара: {e}')
        return redirect(reverse('panel:cyclops') + '#beneficiaries')


class CyclopsBeneficiaryUploadDocumentView(CyclopsWriteAccessMixin, View):
    """Приложить договор оферты уже зарегистрированному бенефициару — если при
    регистрации файл не загрузили (например, identification_payment/create_deal
    падают с «Document not found», потому что у бенефициара нет документа)."""

    def post(self, request, pk):
        beneficiary = CyclopsBeneficiary.objects.filter(pk=pk).first()
        if not beneficiary:
            messages.error(request, 'Бенефициар не найден')
            return redirect(reverse('panel:cyclops') + '#beneficiaries')

        document = request.FILES.get('document')
        if not document:
            messages.error(request, 'Не выбран файл')
            return redirect(reverse('panel:cyclops') + '#beneficiaries')

        try:
            cyclops_manager.upload_beneficiary_document(beneficiary, document)
            messages.success(request, f'Договор оферты загружен для «{beneficiary.name}»')
        except Exception as e:
            logger.error('cyclops_beneficiary_upload_document failed beneficiary_pk=%s: %s', pk, e, exc_info=True)
            messages.error(request, f'Ошибка загрузки документа: {e}')
        return redirect(reverse('panel:cyclops') + '#beneficiaries')


class CyclopsIdentifyPaymentView(CyclopsWriteAccessMixin, View):
    def post(self, request):
        payment_id = request.POST.get('payment_id')
        virtual_account_id = request.POST.get('virtual_account_id')
        try:
            # Сумма по умолчанию — вся сумма платежа (amount=None)
            cyclops_manager.identify_payment(payment_id, virtual_account_id)
            # Точка предупреждает, что сама идентификация — не мгновенная:
            # до 0,5 часа на pre-слое и до 96 часов на проде, прежде чем деньги
            # реально появятся на виртуальном счёте (см. гайд, «Платежи»).
            messages.success(
                request,
                f'Заявка на идентификацию платежа {payment_id} принята. Деньги на виртуальном счёте '
                f'могут появиться не сразу — по документации Точки это может занять до 0,5 часа на '
                f'pre-слое и до 96 часов на боевом. Баланс обновится в разделе «Бенефициары» после '
                f'«Обновить из Cyclops».',
            )
        except Exception as e:
            logger.error(
                'cyclops_identify_payment failed payment_id=%s virtual_account_id=%s: %s',
                payment_id, virtual_account_id, e, exc_info=True,
            )
            messages.error(request, f'Ошибка идентификации платежа: {e}')
        return redirect(reverse('panel:cyclops') + '#payments')


class CyclopsTestPaymentView(CyclopsWriteAccessMixin, View):
    def post(self, request):
        try:
            cyclops_manager.send_test_payment(
                recipient_account=(request.POST.get('recipient_account') or '').strip(),
                recipient_bank_code=(request.POST.get('recipient_bank_code') or '').strip(),
                amount=request.POST.get('amount'),
                purpose=(request.POST.get('purpose') or '').strip() or None,
            )
            messages.success(request, 'Тестовый платёж отправлен. Обновите платежи через несколько минут.')
        except Exception as e:
            logger.error('cyclops_test_payment failed: %s', e, exc_info=True)
            messages.error(request, f'Ошибка тестового платежа: {e}')
        return redirect(reverse('panel:cyclops') + '#payments')


def _trigger_payout_now(request, participant):
    """Выполнить (или повторить) выплату гарантированного приза конкретному
    участнику синхронно — той же функцией, что и в проде — и отчитаться
    сообщением на странице. Общая логика для выплаты по email и по кнопке
    в таблице выплат.

    Защита от двойной выплаты: `GuaranteedPrizePayout.participant` —
    OneToOneField (в БД физически не может быть двух записей на участника),
    а сама `perform_payout` захватывает запись через `select_for_update` и
    не трогает статусы paid/processing/executing — так что даже одновременное
    нажатие кнопки дважды не приведёт к повторной отправке денег.
    """
    if participant.guaranteed_prize_opt_out:
        messages.warning(
            request,
            f'{participant.email}: участник отказался от гарантированного приза — выплата не '
            f'отправлена. Снимите галочку «Отказ от гарантированного приза» в карточке участника, '
            f'если отказ больше не актуален.',
        )
        return

    payout, created = GuaranteedPrizePayout.objects.get_or_create(
        participant=participant,
        defaults={
            'amount': Decimal(str(settings.CYCLOPS_CONFIG['PRIZE_AMOUNT'])),
            'status': GuaranteedPrizePayout.STATUS_NEW,
        },
    )
    if payout.status == GuaranteedPrizePayout.STATUS_PAID:
        messages.warning(request, f'{participant.email}: приз уже выплачен (deal_id={payout.deal_id})')
        return
    if payout.status in (GuaranteedPrizePayout.STATUS_PROCESSING, GuaranteedPrizePayout.STATUS_EXECUTING):
        messages.warning(request, f'{participant.email}: выплата уже в работе ({payout.get_status_display()})')
        return
    if payout.status in (GuaranteedPrizePayout.STATUS_FAILED,
                         GuaranteedPrizePayout.STATUS_RETRY_LIMIT,
                         GuaranteedPrizePayout.STATUS_HOLD):
        payout.status = GuaranteedPrizePayout.STATUS_RETRYABLE
        # Запуск руками = новый запас автоматических попыток.
        payout.cnt_retry = 0
        payout.save(update_fields=['status', 'cnt_retry', 'updated_at'])

    try:
        gpp.perform_payout(participant.pk)
    except Exception as e:
        messages.error(request, f'Непредвиденная ошибка выплаты: {e}')
        return

    payout.refresh_from_db()
    if payout.status == GuaranteedPrizePayout.STATUS_FAILED:
        messages.error(request, f'{participant.email}: выплата не прошла — {payout.error_reason}')
    elif payout.status == GuaranteedPrizePayout.STATUS_RETRY_LIMIT:
        messages.error(
            request,
            f'{participant.email}: исчерпан лимит автоматических повторов '
            f'({payout.cnt_retry}) — {payout.error_reason}',
        )
    elif payout.status == GuaranteedPrizePayout.STATUS_RETRYABLE:
        messages.warning(request, f'{participant.email}: временная ошибка, будет повторено автоматически — {payout.error_reason}')
    else:
        messages.success(request, f'{participant.email}: статус «{payout.get_status_display()}», deal_id={payout.deal_id}')


class CyclopsTestParticipantPayoutView(CyclopsWriteAccessMixin, View):
    """Выплатить гарантированный приз конкретному участнику вручную (по email) —
    не дожидаясь, пока он сам заполнит профиль на сайте. Выполняется синхронно
    (той же функцией, что и в проде), результат виден сразу же на этой странице."""

    def post(self, request):
        email = (request.POST.get('email') or '').strip()
        participant = User.objects.filter(email__iexact=email).first()
        if not participant:
            messages.error(request, f'Участник с email «{email}» не найден')
            return redirect(reverse('panel:cyclops') + '#payouts')

        _trigger_payout_now(request, participant)
        return redirect(reverse('panel:cyclops') + '#payouts')


class CyclopsPayoutTriggerView(CyclopsWriteAccessMixin, View):
    """Кнопка «Оплатить»/«Повторить» в строке таблицы выплат — та же логика,
    что и «Тестовая выплата участнику», но без набора email вручную."""

    def post(self, request, pk):
        payout = GuaranteedPrizePayout.objects.select_related('participant').filter(pk=pk).first()
        if not payout:
            messages.error(request, 'Запись о выплате не найдена')
            return redirect(reverse('panel:cyclops') + '#payouts')

        _trigger_payout_now(request, payout.participant)
        return redirect(reverse('panel:cyclops') + '#payouts')


class CyclopsSettingsView(CyclopsWriteAccessMixin, View):
    def post(self, request):
        settings_obj = CyclopsSettings.load()
        beneficiary_id = request.POST.get('payout_beneficiary')
        virtual_account_id = request.POST.get('payout_virtual_account')
        service_agreement_id = request.POST.get('service_agreement')

        settings_obj.payout_beneficiary = (
            CyclopsBeneficiary.objects.filter(pk=beneficiary_id).first() if beneficiary_id else None
        )
        settings_obj.payout_virtual_account = (
            CyclopsVirtualAccount.objects.filter(pk=virtual_account_id).first() if virtual_account_id else None
        )
        settings_obj.service_agreement = (
            CyclopsDocument.objects.filter(pk=service_agreement_id).first() if service_agreement_id else None
        )
        settings_obj.payout_enabled = request.POST.get('payout_enabled') == '1'

        # Загрузка нового шаблона service_agreement (если приложен файл)
        new_template = request.FILES.get('service_agreement_file')
        if new_template:
            doc = CyclopsDocument.objects.create(
                document_type=CyclopsDocument.TYPE_SERVICE_AGREEMENT,
                file=new_template,
            )
            settings_obj.service_agreement = doc

        settings_obj.save()
        messages.success(request, 'Настройки плательщика сохранены')
        return redirect(reverse('panel:cyclops') + '#settings')
