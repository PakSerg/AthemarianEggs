from django.conf import settings
from django.utils.functional import SimpleLazyObject


def can_access_cabinet(user):
    return True


def cabinet_access_context(request):
    return {
        'ACTION_STARTED': settings.ACTION_STARTED,
        'CABINET_ACCESSIBLE': can_access_cabinet(request.user),
        'is_participants': (request.path_info or '/').startswith('/participants/'),
    }


def _latest_published_win_card(user):
    """Карточка последней опубликованной победы участника или None."""
    from .models import PromotionDrawResult
    from .services.winner_prize_status import build_cards

    draw_result = (
        PromotionDrawResult.objects
        .select_related('prize', 'receipt')
        .filter(participant=user, is_published=True, is_reserve=False)
        .order_by('-published_at', '-created_at')
        .first()
    )
    if draw_result is None:
        return None
    return build_cards([draw_result])[0]


def win_prize_modal_context(request):
    """
    Победа, о которой участнику показывается модалка «Поздравляем!».

    Берём последний опубликованный итог розыгрыша: до публикации победа ещё не
    объявлена, и поздравлять рано. Показывать её или нет, решает уже браузер —
    отметка о закрытии модалки живёт в localStorage (см. winPrizeModal.js),
    поэтому сервер каждый раз честно отдаёт последнюю победу.

    Значение ленивое: контекстные процессоры выполняются на каждый шаблон, а
    модалка подключена только в base.html и base_promotion.html — на страницах
    админки и панели запрос в БД не уйдёт вовсе.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {}

    return {'win_prize_modal_card': SimpleLazyObject(lambda: _latest_published_win_card(user))}


def instant_game_context(request):
    """
    Состояние лотка яиц для модалки «Моментальный приз».

    Модалка подключена в base.html и base_promotion.html, поэтому состояние
    считается лениво: на страницах админки и панели запрос в БД не уйдёт вовсе.
    Показывать модалку или нет, решает уже браузер (instant-prizes.js помнит,
    закрывал ли участник её в этой сессии) — сервер каждый раз честно отдаёт
    текущее число попыток.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {}

    from .services.instant_prizes import game_state

    return {'instant_game': SimpleLazyObject(lambda: game_state(user))}
