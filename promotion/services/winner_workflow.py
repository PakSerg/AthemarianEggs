"""Ручной workflow победителя в админке: OkiDoki и письмо."""

from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone
from django.utils.html import escape

from ..models import (
    PrizeShippingSoonMessage,
    PromotionDrawResult,
    PromotionDrawResultMainRaffle,
    Receipt,
)
from .notify_bot import notify as notify_bot
from .oki_doki import check_email_send_blocked, check_prize_ndfl_consistency, preview_oki_contract, send_link_oki_doki
from .send_email import (
    build_oki_data_from_record,
    send_email_prize_shipping_soon,
    send_email_winner,
)


@dataclass
class WorkflowResult:
    ok: bool
    message: str


def _oki_target_for_receipt(receipt: Receipt):
    """Объект с полями prize/participant/public_id/receipt для OkiDoki (еженедельный приз)."""
    draw = receipt.draw_results.select_related('prize', 'participant').first()
    if not draw:
        raise ValueError('Не найдена запись итога розыгрыша для этого чека')

    class _Target:
        pass

    target = _Target()
    target.prize = draw.prize
    target.participant = draw.participant
    target.public_id = receipt.public_id
    target.receipt = receipt
    target.link_oki_document = receipt.link_oki_document
    target.link_oki_document_admin = receipt.link_oki_document_admin
    target.status_oki_document = receipt.status_oki_document
    target.is_send_email = receipt.is_send_email
    return target


def get_oki_preview_for_receipt(receipt: Receipt) -> dict:
    return preview_oki_contract(_oki_target_for_receipt(receipt))


def get_oki_preview_for_main(draw_result: PromotionDrawResultMainRaffle) -> dict:
    return preview_oki_contract(draw_result)


def _notify_ndfl_issue(prize, *, external_ref: str, contract_link: str) -> None:
    """После создания договора сверяет стоимость приза и НДФЛ и, если они не
    согласованы (см. check_prize_ndfl_consistency), шлёт алерт в бот со ссылкой
    на договор — чтобы заметить некорректно оформленный договор сразу, а не
    когда его уже подпишут."""
    issue = check_prize_ndfl_consistency(prize)
    if not issue:
        return
    notify_bot(
        f'⚠️ Договор OkiDoki ({external_ref}): {issue}\n'
        f'Ссылка на договор: {contract_link or "—"}'
    )


def issue_oki_for_receipt(receipt: Receipt, *, oki_template=None) -> WorkflowResult:
    if receipt.status != Receipt.Status.WINNER:
        return WorkflowResult(False, 'Чек не в статусе «Победный»')

    target = _oki_target_for_receipt(receipt)
    if receipt.link_oki_document or receipt.link_oki_document_admin:
        return WorkflowResult(False, 'Договор OkiDoki уже создан')

    oki_data = send_link_oki_doki(target, oki_template=oki_template)
    _apply_oki_to_receipt(receipt, oki_data)

    if oki_data.get('_exception') or oki_data.get('status_oki_document') == 'Ошибка на стороне сервиса':
        return WorkflowResult(False, oki_data.get('status_oki_document') or 'Ошибка OkiDoki')

    _notify_ndfl_issue(
        target.prize,
        external_ref=f'чек {receipt.public_id}',
        contract_link=receipt.link_oki_document or receipt.link_oki_document_admin,
    )

    return WorkflowResult(True, f'Договор создан. Статус: {receipt.status_oki_document}')


def issue_oki_for_main(draw_result: PromotionDrawResultMainRaffle, *, oki_template) -> WorkflowResult:
    if draw_result.link_oki_document or draw_result.link_oki_document_admin:
        return WorkflowResult(False, 'Договор OkiDoki уже создан')

    oki_data = send_link_oki_doki(draw_result, oki_template=oki_template)
    _apply_oki_to_main(draw_result, oki_data)

    if oki_data.get('_exception') or oki_data.get('status_oki_document') == 'Ошибка на стороне сервиса':
        return WorkflowResult(False, oki_data.get('status_oki_document') or 'Ошибка OkiDoki')

    _notify_ndfl_issue(
        draw_result.prize,
        external_ref=f'главный приз {draw_result.public_id}',
        contract_link=draw_result.link_oki_document or draw_result.link_oki_document_admin,
    )

    return WorkflowResult(True, f'Договор создан. Статус: {draw_result.status_oki_document}')


def send_winner_email_for_receipt(receipt: Receipt) -> WorkflowResult:
    if receipt.status != Receipt.Status.WINNER:
        return WorkflowResult(False, 'Чек не в статусе «Победный»')
    if receipt.is_send_email:
        return WorkflowResult(False, 'Письмо уже отправлено')

    draw = receipt.draw_results.select_related('prize', 'participant').first()
    if not draw:
        return WorkflowResult(False, 'Не найден итог розыгрыша')

    participant = draw.participant
    if not participant.email:
        return WorkflowResult(False, 'У участника не указан email')

    block_reason = check_email_send_blocked(
        prize=draw.prize, participant=participant, status_oki_document=receipt.status_oki_document,
    )
    if block_reason:
        notify_bot(f'⛔ Письмо с договором для чека {receipt.public_id} не отправлено: {block_reason}')
        return WorkflowResult(False, block_reason)

    oki_data = build_oki_data_from_record(receipt)
    if not oki_data or oki_data.get('for_admin'):
        return WorkflowResult(
            False,
            'Сначала выставьте договор OkiDoki (нужна ссылка для участника, не только черновик для заказчика)',
        )

    try:
        send_email_winner(participant, draw.prize, oki_data=oki_data, receipt_id=receipt.pk)
    except Exception as exc:
        return WorkflowResult(False, str(exc))

    receipt.is_send_email = True
    receipt.email_sent_at = timezone.now()
    receipt.save(update_fields=['is_send_email', 'email_sent_at', 'updated_at'])
    return WorkflowResult(True, f'Письмо с договором отправлено на {participant.email}')


def send_winner_email_for_main(draw_result: PromotionDrawResultMainRaffle) -> WorkflowResult:
    if draw_result.is_send_email:
        return WorkflowResult(False, 'Письмо уже отправлено')

    participant = draw_result.participant
    if not participant.email:
        return WorkflowResult(False, 'У участника не указан email')

    block_reason = check_email_send_blocked(
        prize=draw_result.prize, participant=participant, status_oki_document=draw_result.status_oki_document,
    )
    if block_reason:
        notify_bot(f'⛔ Письмо с договором (главный приз {draw_result.public_id}) не отправлено: {block_reason}')
        return WorkflowResult(False, block_reason)

    oki_data = build_oki_data_from_record(draw_result)
    if not oki_data or oki_data.get('for_admin'):
        return WorkflowResult(
            False,
            'Сначала выставьте договор OkiDoki (нужна ссылка для участника)',
        )

    try:
        send_email_winner(participant, draw_result.prize, oki_data=oki_data)
    except Exception as exc:
        return WorkflowResult(False, str(exc))

    draw_result.is_send_email = True
    draw_result.email_sent_at = timezone.now()
    draw_result.save(update_fields=['is_send_email', 'email_sent_at'])
    return WorkflowResult(True, f'Письмо отправлено на {participant.email}')


def send_prize_shipping_soon_email_for_receipt(receipt: Receipt) -> WorkflowResult:
    """Отправляет письмо «приз скоро будет отправлен» победителю."""
    if (receipt.status_oki_document or '').strip() != 'Подписан':
        return WorkflowResult(False, 'Договор OkiDoki ещё не подписан')
    if not receipt.is_send_email:
        return WorkflowResult(False, 'Сначала отправьте письмо с договором победителю')
    if shipping_soon_email_sent(receipt):
        return WorkflowResult(False, 'Письмо о скорой отправке приза уже отправлено')

    draw_result = (
        PromotionDrawResult.objects.filter(receipt=receipt, is_reserve=False)
        .select_related('prize', 'participant')
        .first()
    )
    if not draw_result:
        return WorkflowResult(False, 'Нет записи о победе для этого чека.')

    try:
        send_email_prize_shipping_soon(
            draw_result.participant,
            draw_result.prize,
            receipt_id=receipt.pk,
        )
        return WorkflowResult(True, 'Письмо о скорой отправке приза отправлено.')
    except Exception as exc:
        return WorkflowResult(False, f'Ошибка отправки: {exc}')


def shipping_soon_email_sent(receipt: Receipt) -> bool:
    """Было ли уже отправлено письмо «приз скоро будет отправлен» по этому чеку."""
    subject = PrizeShippingSoonMessage.load().subject
    return receipt.emails.filter(name=subject).exists()


def _apply_oki_to_receipt(receipt: Receipt, oki_data: dict) -> None:
    if oki_data.get('for_admin'):
        receipt.link_oki_document_admin = oki_data.get('contract_link') or ''
    else:
        receipt.link_oki_document = oki_data.get('contract_link') or ''
    receipt.status_oki_document = oki_data.get('status_oki_document') or ''
    if oki_data.get('contract_link'):
        receipt.oki_document_issued_at = timezone.now()
    receipt.save(update_fields=[
        'link_oki_document',
        'link_oki_document_admin',
        'status_oki_document',
        'oki_document_issued_at',
        'updated_at',
    ])


def _apply_oki_to_main(draw_result: PromotionDrawResultMainRaffle, oki_data: dict) -> None:
    if oki_data.get('for_admin'):
        draw_result.link_oki_document_admin = oki_data.get('contract_link') or ''
    else:
        draw_result.link_oki_document = oki_data.get('contract_link') or ''
    draw_result.status_oki_document = oki_data.get('status_oki_document') or ''
    if oki_data.get('contract_link'):
        draw_result.oki_document_issued_at = timezone.now()
    draw_result.save(update_fields=[
        'link_oki_document',
        'link_oki_document_admin',
        'status_oki_document',
        'oki_document_issued_at',
    ])


def render_workflow_panel_html(preview: dict, *, issue_url: str, email_url: str) -> str:
    """HTML-блок этапов для страницы победителя в админке (главный розыгрыш, preview-режим)."""
    template = preview['template']
    entities_rows = ''.join(
        f'<li><b>{escape(item["keyword"])}</b>: {escape(item["value"])}</li>'
        for item in preview.get('entities', [])
    )
    system_rows = ''.join(
        f'<li><b>{escape(item["keyword"])}</b>: {escape(item["value"])}</li>'
        for item in preview.get('system_entities', [])
    )

    contract_block = ''
    if preview.get('already_issued'):
        if preview.get('status_oki_document'):
            contract_block += f'<p><b>Статус договора:</b> {escape(preview["status_oki_document"])}</p>'
        admin_link = preview.get('admin_link') or ''
        participant_link = preview.get('participant_link') or ''
        if admin_link:
            contract_block += (
                f'<p><b>Договор (заказчик):</b> '
                f'<a href="{escape(admin_link)}" target="_blank">{escape(admin_link)}</a></p>'
            )
        if participant_link:
            contract_block += (
                f'<p><b>Договор (участник):</b> '
                f'<a href="{escape(participant_link)}" target="_blank">{escape(participant_link)}</a></p>'
            )
        issue_btn = '<p><i>Договор уже создан</i></p>'
    else:
        issue_btn = f'<p><a class="button" href="{escape(issue_url)}">Выставить договор в OkiDoki</a></p>'

    email_sent = preview.get('email_sent', False)
    if email_sent:
        email_btn = '<p><i>Письмо о победе уже отправлено</i></p>'
    else:
        email_btn = f'<p><a class="button" href="{escape(email_url)}">Отправить письмо о победе</a></p>'

    template_url = template.get('template_url') or ''
    template_link = (
        f'<a href="{escape(template_url)}" target="_blank">{escape(template_url)}</a>'
        if template_url else '—'
    )

    return (
        '<div class="winner-workflow-panel" style="max-width:900px">'
        '<h3>Этапы обработки победителя</h3>'
        '<ol>'
        '<li><b>Победитель назначен</b> (автоматически при розыгрыше)</li>'
        '<li><b>Договор OkiDoki</b>'
        f'<p>Шаблон: <b>{escape(template.get("template_label", ""))}</b> '
        f'(id: <code>{escape(template.get("template_id", ""))}</code>)</p>'
        f'<p>Ссылка на шаблон: {template_link}</p>'
        f'<p>External ID: <code>{escape(preview.get("external_id", ""))}</code></p>'
        f'<p>Участник: {escape(preview["participant"]["name"])} · '
        f'{escape(preview["participant"]["email"])} · {escape(preview["participant"]["phone"])}</p>'
        f'<p>Приз: {escape(preview["prize"]["name"])} · '
        f'{int(preview["prize"]["cost"]) if preview["prize"]["cost"] else "—"} ₽</p>'
        '<p><b>Данные участника:</b></p>'
        f'<ul>{system_rows or "<li>—</li>"}</ul>'
        '<p><b>Поля договора (системные):</b></p>'
        f'<ul>{entities_rows or "<li>—</li>"}</ul>'
        f'{issue_btn}'
        f'{contract_block}'
        '</li>'
        '<li><b>Письмо победителю</b>'
        f'{email_btn}'
        '</li>'
        '</ol>'
        '</div>'
    )