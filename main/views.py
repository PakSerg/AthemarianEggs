"""Публичная часть Акции «Купи и выиграй с Атемарской».

Главная собирается из готовых секций (`templates/partials/sections/`), как в
PromoTemplate: страница — это набор `{% include %}`, а не монолитный HTML, и
лишняя секция просто выключается галочкой у своего `ContentBlock`, а не
удаляется из шаблона. Тексты секций приезжают из админки, поэтому во вьюхе нет
ни одной строки, относящейся к конкретной акции.
"""

from datetime import datetime, time

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views import View

from main.services import get_winners_by_months, get_mock_winners
from promotion.models import Prize, Raffle
from promotion.services.promo_calendar import (
    last_week_num,
    week_num_on_date,
    weekly_draw_date,
)

from .models import (
    FAQ,
    ActionProducts,
    City,
    CompanyInfo,
    ContentBlock,
    MomentPrizes,
    MonthPrizes,
    ParticipationStep,
    SpecialPrizes,
    WeekPrizes,
)


def split_faq(faq):
    """Делит вопросы на две колонки блока «Вопросы и ответы».

    Левая колонка получает лишний вопрос при нечётном количестве — так первая
    колонка никогда не короче второй. Возвращает (левая, правая).
    """
    items = list(faq)
    half = (len(items) + 1) // 2
    return items[:half], items[half:]


def visible_blocks() -> dict:
    """Тексты секций главной по коду секции — `blocks.hero.title` в шаблоне."""
    return {block.code: block for block in ContentBlock.objects.filter(is_visible=True)}


def next_draw_info(raffle):
    """Ближайший предстоящий еженедельный розыгрыш: дата, номер недели и ISO для таймера.

    Дата берётся из календаря Акции (promo_calendar.weekly_draw_date), а не
    пересчитывается здесь семидневками от старта: первый недельный период
    Акции длиннее календарной недели, и собственная арифметика в лендинге
    разъезжалась бы с розыгрышем.
    """
    if raffle is None:
        return {'next_draw_date': None, 'next_draw_date_iso': '', 'next_draw_week': None}

    today = timezone.localtime().date()
    last = last_week_num(raffle)
    current_week = min(max(week_num_on_date(raffle, today), 1), last)

    for week in range(current_week, last + 1):
        draw_day = weekly_draw_date(raffle, week)
        if draw_day >= today:
            draw_at = timezone.make_aware(
                datetime.combine(draw_day, time(0, 0)), timezone.get_current_timezone(),
            )
            return {
                'next_draw_date': draw_at,
                'next_draw_date_iso': draw_at.isoformat(),
                'next_draw_week': week,
            }

    return {'next_draw_date': None, 'next_draw_date_iso': '', 'next_draw_week': None}


def showcase_context() -> dict:
    """Витрины призов и товаров — общие для главной и страницы товаров."""
    return {
        'moment_prizes': MomentPrizes.objects.all(),
        'week_prizes': WeekPrizes.objects.all(),
        'month_prizes': MonthPrizes.objects.all(),
        'special_prizes': SpecialPrizes.objects.all(),
        'main_prizes': Prize.objects.filter(is_active=True, is_main=True),
    }


class HomeView(View):
    template_name = 'pages/home.html'

    def get(self, request):
        company_info = CompanyInfo.get_instance()
        faq_left, faq_right = split_faq(FAQ.objects.all().order_by('order'))

        winners = get_winners_by_months()
        if not winners and settings.MOCK_WINNERS:
            winners = get_mock_winners()

        raffle = Raffle.objects.filter(is_active=True).first() or Raffle.objects.first()

        context = {
            'blocks': visible_blocks(),
            'steps': ParticipationStep.objects.all(),
            'faq_left': faq_left,
            'faq_right': faq_right,
            'action_products': ActionProducts.objects.all().order_by('created_at'),
            'desktop_products': ActionProducts.objects.filter(
                show_in_products_block=True,
            ).order_by('products_block_order')[:8],
            'winners': winners,
            'raffle': raffle,
            **showcase_context(),
            **next_draw_info(raffle),
            'title': company_info.promotion_name,
        }
        return render(request, self.template_name, context)


class AddressesView(View):
    template_name = 'pages/addresses.html'

    def get(self, request):
        company_info = CompanyInfo.get_instance()
        context = {
            'cities': City.objects.all(),
            'blocks': visible_blocks(),
            'title': f'Адреса магазинов — {company_info.promotion_name}',
            'description': 'Список магазинов, участвующих в акции',
        }
        return render(request, self.template_name, context)


class ProductsListView(View):
    template_name = 'pages/products.html'

    def get(self, request):
        company_info = CompanyInfo.get_instance()
        faq_left, faq_right = split_faq(FAQ.objects.all().order_by('order'))
        context = {
            'action_products': ActionProducts.objects.all().order_by('created_at'),
            'blocks': visible_blocks(),
            'faq_left': faq_left,
            'faq_right': faq_right,
            'title': f'Товары-участники акции — {company_info.promotion_name}',
            'description': f'Полный список товаров, участвующих в акции «{company_info.promotion_name}»',
        }
        return render(request, self.template_name, context)


class PlugView(View):
    """Заглушка до старта Акции: обратный отсчёт до даты начала."""

    template_name = 'pages/plug.html'

    def get(self, request):
        company_info = CompanyInfo.get_instance()
        raffle = Raffle.objects.filter(is_active=True).first() or Raffle.objects.first()
        start_at = raffle.start_date if raffle else None
        if start_at is None:
            start_at = timezone.make_aware(
                datetime.combine(settings.RAFFLE_RECEIPT_MIN_DATE, time(0, 0)),
                timezone.get_current_timezone(),
            )
        context = {
            'promotion_start_iso': timezone.localtime(start_at).isoformat(),
            'blocks': visible_blocks(),
            'title': company_info.promotion_name,
            'description': f'Акция стартует {timezone.localtime(start_at):%d.%m.%Y}',
        }
        return render(request, self.template_name, context)


class NotFoundPreviewView(View):
    template_name = 'pages/404.html'

    def get(self, request):
        company_info = CompanyInfo.get_instance()
        context = {
            'title': f'{company_info.promotion_name} | Страница не найдена',
            'description': 'Страница не найдена',
        }
        return render(request, self.template_name, context)


def page_not_found(request, exception=None):
    company_info = CompanyInfo.get_instance()
    context = {
        'title': f'{company_info.promotion_name} | Страница не найдена',
        'description': 'Страница не найдена',
    }
    return render(request, NotFoundPreviewView.template_name, context, status=404)


def robots_txt(request):
    company_info = CompanyInfo.get_instance()
    template_name = (
        'seo/robots_production.txt' if company_info.seo_production_mode else 'seo/robots_prelaunch.txt'
    )
    content = render_to_string(template_name, {
        'scheme': request.scheme,
        'domain': request.get_host(),
    })
    return HttpResponse(content, content_type='text/plain')
