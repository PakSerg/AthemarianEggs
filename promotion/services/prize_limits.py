"""
Сколько призов может выиграть один участник.

Условия этой Акции (бриф, п. 5 «Может ли один человек выиграть дважды» плюс
уточнение Заказчика) сводятся к двум независимым ограничениям:

1. Не более ОДНОГО моментального приза за всю Акцию.
2. Не более ОДНОГО приза в еженедельных и ежемесячных розыгрышах ВМЕСТЕ ВЗЯТЫХ
   за всю Акцию: выиграл еженедельный — ни второй еженедельный, ни ежемесячный
   ему уже не достанутся.

Это строже, чем было в предыдущих акциях, где ограничение действовало только
внутри одного розыгрыша (можно было выигрывать каждую неделю). Поэтому правило
вынесено сюда одной функцией: пулы розыгрыша, назначение приза и подбор
резервных победителей спрашивают её, а не повторяют условие каждый у себя.

Главный розыгрыш по умолчанию считается отдельной корзиной: в брифе он в это
ограничение не включён, и победа в еженедельном розыгрыше не лишает участника
шанса на холодильник. Если Заказчик решит иначе, достаточно переключить
MAIN_DRAW_SHARES_REGULAR_LIMIT — правило подхватят все три розыгрыша сразу.
"""

from __future__ import annotations

from ..models import PromotionDrawResult, PromotionDrawResultMainRaffle

# Считать ли победу в Главном розыгрыше такой же «обычной» победой, как
# еженедельная и ежемесячная. False — Главный розыгрыш независим (см. модульную
# документацию выше).
MAIN_DRAW_SHARES_REGULAR_LIMIT = False


def regular_winner_ids() -> set[int]:
    """Участники, уже выигравшие приз в еженедельном или ежемесячном розыгрыше.

    Резервные победители сюда не входят: резерв — это ещё не выигрыш, а очередь
    на случай, если основной победитель не выйдет на связь.
    """
    ids = set(
        PromotionDrawResult.objects
        .filter(is_reserve=False, is_instant=False)
        .values_list('participant_id', flat=True)
    )
    if MAIN_DRAW_SHARES_REGULAR_LIMIT:
        ids |= main_winner_ids()
    return ids


def main_winner_ids() -> set[int]:
    """Участники, уже выигравшие Главный приз."""
    return set(
        PromotionDrawResultMainRaffle.objects
        .filter(is_reserve=False)
        .values_list('participant_id', flat=True)
    )


def instant_winner_ids() -> set[int]:
    """Участники, уже выигравшие моментальный приз."""
    return set(
        PromotionDrawResult.objects
        .filter(is_reserve=False, is_instant=True)
        .values_list('participant_id', flat=True)
    )


def has_regular_prize(participant_id: int) -> bool:
    """Есть ли у участника приз еженедельного/ежемесячного розыгрыша."""
    exists = PromotionDrawResult.objects.filter(
        participant_id=participant_id, is_reserve=False, is_instant=False,
    ).exists()
    if exists or not MAIN_DRAW_SHARES_REGULAR_LIMIT:
        return exists
    return has_main_prize(participant_id)


def has_main_prize(participant_id: int) -> bool:
    return PromotionDrawResultMainRaffle.objects.filter(
        participant_id=participant_id, is_reserve=False,
    ).exists()


def has_instant_prize(participant_id: int) -> bool:
    """Выигрывал ли участник моментальный приз — ключевая проверка лотка яиц."""
    return PromotionDrawResult.objects.filter(
        participant_id=participant_id, is_reserve=False, is_instant=True,
    ).exists()


def can_win_regular(participant_id: int) -> bool:
    return not has_regular_prize(participant_id)


def can_win_main(participant_id: int) -> bool:
    if has_main_prize(participant_id):
        return False
    if MAIN_DRAW_SHARES_REGULAR_LIMIT and has_regular_prize(participant_id):
        return False
    return True


def can_win_instant(participant_id: int) -> bool:
    return not has_instant_prize(participant_id)
