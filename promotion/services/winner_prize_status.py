"""
Что победитель видит про свой приз в личном кабинете (блок «Ваши призы»).

Этапы обработки победителя внутри системы живут в staff_panel (WINNER_STAGES) и
написаны языком менеджера: «Договор создан», «Договор отправлен». Участнику нужен
другой набор состояний — не «что мы сделали», а «что сейчас делать ему»: подписать
договор, подождать проверку, забрать приз. Этот модуль переводит данные итога
розыгрыша (договор OkiDoki на чеке + «Доставлено» + файл электронного приза) в
одну карточку статуса с текстом и, если есть куда вести, ссылкой.

Тексты — с макета личного кабинета, менять их можно только вместе с дизайном.

Про договор известно ровно то, что записано в чеке: статус OkiDoki и ссылка для
победителя. Момент, когда договор подписывает организатор, OkiDoki нам отдельно
не сообщает — статус «Подписан» приходит по подписи победителя. Поэтому
«Договор подписан организатором. Скоро отправим приз.» показывается не по
договору, а по нашей же отметке «Доставлено: Оформлен» — это ближайший честный
признак того, что призом уже занимаются. Во всех остальных подписанных случаях
показывается нейтральное «Договор подписан и находится на проверке».

Ссылка link_oki_document_admin (договор-черновик для заказчика) участнику не
показывается никогда — она ведёт в чужой кабинет OkiDoki.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

from ..models import PrizeFile

# Статус договора в OkiDoki, означающий, что победитель его подписал.
CONTRACT_SIGNED = 'Подписан'

# Значения поля «Доставлено» (PromotionDrawResult.delivery_status), сравниваются в нижнем регистре.
DELIVERY_DONE = 'да'
DELIVERY_ARRANGED = 'оформлен'

# Коды состояний карточки — на них завязаны и шаблон, и выдача файла приза.
CONTRACT_PREPARING = 'contract_preparing'
CONTRACT_AWAITING_SIGNATURE = 'contract_awaiting_signature'
CONTRACT_SIGNED_ON_REVIEW = 'contract_signed_on_review'
CONTRACT_SIGNED_BY_ORGANIZER = 'contract_signed_by_organizer'
PRIZE_READY_TO_CLAIM = 'prize_ready_to_claim'
PRIZE_SENT = 'prize_sent'

CONTRACT_LINK_LABEL = 'Перейти к договору'
PRIZE_LINK_LABEL = 'Забрать приз'

STATUS_TEXTS = {
    CONTRACT_PREPARING: 'Договор готовится. Мы пришлём ссылку, когда его можно будет подписать',
    CONTRACT_AWAITING_SIGNATURE: 'Пожалуйста, подпишите договор, чтобы получить приз',
    CONTRACT_SIGNED_ON_REVIEW: 'Договор подписан и находится на проверке.',
    CONTRACT_SIGNED_BY_ORGANIZER: 'Договор подписан организатором. Скоро отправим приз.',
    PRIZE_READY_TO_CLAIM: 'Всё готово! Договор подписан. Заберите ваш приз здесь.',
    PRIZE_SENT: 'Приз отправлен',
}


@dataclass(frozen=True)
class PrizeCardStatus:
    """Статус приза глазами победителя: текст и, если есть, одна ссылка-действие."""

    code: str
    text: str
    link_url: str = ''
    link_label: str = ''

    @property
    def has_link(self) -> bool:
        return bool(self.link_url and self.link_label)


@dataclass(frozen=True)
class PrizeCard:
    """Карточка приза в блоке «Ваши призы»."""

    draw_result: object
    status: PrizeCardStatus
    prize_file: object | None = None

    @property
    def prize(self):
        return self.draw_result.prize

    @property
    def purchase_date(self):
        """Дата покупки по победному чеку — её и показывает макет."""
        receipt = getattr(self.draw_result, 'receipt', None)
        return receipt.date if receipt else None

    @property
    def draw_label(self) -> str:
        if getattr(self.draw_result, 'is_instant', False):
            return 'Моментальный приз'
        if self.draw_result.month_num:
            return f'{self.draw_result.month_num} месяц'
        if self.draw_result.week_num:
            return f'{self.draw_result.week_num} неделя'
        return '—'


def _delivery(draw_result) -> str:
    return (draw_result.delivery_status or '').strip().lower()


def _contract_status(draw_result) -> str:
    receipt = getattr(draw_result, 'receipt', None)
    return (receipt.status_oki_document or '').strip() if receipt else ''


def _contract_link(draw_result) -> str:
    """Ссылка на договор для победителя (ссылка для заказчика сюда не попадает)."""
    receipt = getattr(draw_result, 'receipt', None)
    return (receipt.link_oki_document or '').strip() if receipt else ''


def _prize_file_url(draw_result) -> str:
    return reverse('participants:prize-file', args=[draw_result.pk])


def build_status(draw_result, *, prize_file=None) -> PrizeCardStatus:
    """
    Статус одного итога розыгрыша для личного кабинета.

    prize_file — выданный участнику файл электронного приза (PrizeFile) или None.
    Передаётся снаружи, чтобы на список карточек ушёл один запрос, а не по одному
    на каждую (см. build_cards).
    """
    prize = draw_result.prize
    delivery = _delivery(draw_result)

    if delivery == DELIVERY_DONE:
        # Электронный приз отдаём ссылкой, но только если файл действительно выдан:
        # без файла забирать нечего, и такой приз честнее показать как отправленный.
        if prize and prize.is_electronic and prize_file is not None and prize_file.file:
            return PrizeCardStatus(
                code=PRIZE_READY_TO_CLAIM,
                text=STATUS_TEXTS[PRIZE_READY_TO_CLAIM],
                link_url=_prize_file_url(draw_result),
                link_label=PRIZE_LINK_LABEL,
            )
        return PrizeCardStatus(code=PRIZE_SENT, text=STATUS_TEXTS[PRIZE_SENT])

    contract_link = _contract_link(draw_result)

    if _contract_status(draw_result) == CONTRACT_SIGNED:
        code = (
            CONTRACT_SIGNED_BY_ORGANIZER
            if delivery == DELIVERY_ARRANGED
            else CONTRACT_SIGNED_ON_REVIEW
        )
        return PrizeCardStatus(
            code=code,
            text=STATUS_TEXTS[code],
            link_url=contract_link,
            link_label=CONTRACT_LINK_LABEL if contract_link else '',
        )

    if contract_link:
        return PrizeCardStatus(
            code=CONTRACT_AWAITING_SIGNATURE,
            text=STATUS_TEXTS[CONTRACT_AWAITING_SIGNATURE],
            link_url=contract_link,
            link_label=CONTRACT_LINK_LABEL,
        )

    return PrizeCardStatus(
        code=CONTRACT_PREPARING,
        text=STATUS_TEXTS[CONTRACT_PREPARING],
    )


def prize_files_by_draw_result(draw_results) -> dict[int, PrizeFile]:
    """Выданные файлы электронных призов для списка итогов — одним запросом."""
    ids = [dr.pk for dr in draw_results]
    if not ids:
        return {}
    return {
        prize_file.draw_result_id: prize_file
        for prize_file in PrizeFile.objects.filter(draw_result_id__in=ids)
    }


def build_cards(draw_results) -> list[PrizeCard]:
    """Карточки блока «Ваши призы» для списка итогов розыгрыша."""
    draw_results = list(draw_results)
    files = prize_files_by_draw_result(draw_results)
    return [
        PrizeCard(
            draw_result=draw_result,
            status=build_status(draw_result, prize_file=files.get(draw_result.pk)),
            prize_file=files.get(draw_result.pk),
        )
        for draw_result in draw_results
    ]


def claimable_prize_file(draw_result) -> PrizeFile | None:
    """
    Файл электронного приза, который победителю прямо сейчас можно отдать.

    Единственная точка правды для выдачи файла: условия те же, по которым в
    кабинете появляется ссылка «Забрать приз», — иначе ссылка и проверка доступа
    разъехались бы.
    """
    prize_file = PrizeFile.objects.filter(draw_result_id=draw_result.pk).first()
    status = build_status(draw_result, prize_file=prize_file)
    if status.code != PRIZE_READY_TO_CLAIM:
        return None
    return prize_file
