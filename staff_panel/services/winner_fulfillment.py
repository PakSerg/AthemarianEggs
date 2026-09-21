from __future__ import annotations

from django.middleware.csrf import get_token
from django.utils.html import escape
from django.utils.safestring import mark_safe

from promotion.services.oki_doki import check_email_send_blocked

from .winners import WinnerRow


def _oki_issued(row: WinnerRow) -> bool:
    return bool((row.status_oki_document or '').strip() or (row.contract_link or '').strip())


def _csrf_input(request) -> str:
    return f'<input type="hidden" name="csrfmiddlewaretoken" value="{get_token(request)}">'


def _step(num: int, title: str, body: str, *, locked: bool = False, done: bool = False) -> str:
    state = 'is-locked' if locked else ('is-done' if done else '')
    return (
        f'<div class="panel-wwf-step {state}">'
        f'  <div class="panel-wwf-circle">{"✓" if done else num}</div>'
        f'  <div class="panel-wwf-card">'
        f'    <div class="panel-wwf-title">{title}</div>'
        f'    <div class="panel-wwf-body">{body}</div>'
        f'  </div>'
        f'</div>'
    )


def render_fulfillment_panel(row: WinnerRow, urls: dict, request, *, template_plan=None) -> str:
    """
    Тонкий рендер шагов оформления победителя в стиле панели. Вызывает те же
    сервисные функции (promotion.services.winner_workflow / publish_winners), что
    и Django admin — бизнес-логика не дублируется, здесь только вёрстка шагов.
    """
    oki_issued = _oki_issued(row)
    oki_status = (row.status_oki_document or '').strip()
    csrf_input = _csrf_input(request)

    def _issue_button(label: str) -> str:
        """Шаблон договора выбирать не нужно — он определяется призом, поэтому
        кнопка сразу отправляет POST на создание договора."""
        if template_plan is not None and not template_plan.ok:
            return (
                f'<p class="panel-wwf-error">⚠ {escape(template_plan.error)}</p>'
                f'<button type="button" class="panel-btn panel-btn--small" disabled>{escape(label)}</button>'
            )
        return (
            f'<form method="post" action="{urls["issue_oki"]}">'
            f'{csrf_input}'
            f'<button type="submit" class="panel-btn panel-btn--small">{escape(label)}</button>'
            f'</form>'
        )

    steps = []

    if row.kind == 'weekly':
        if row.is_published:
            step1_body = '<span class="panel-wwf-ok">✅ Опубликован</span>'
        else:
            step1_body = (
                f'<form method="post" action="{urls["publish"]}">'
                f'{csrf_input}'
                f'<button type="submit" class="panel-btn panel-btn--small">Опубликовать победителя</button>'
                f'</form>'
            )
        steps.append(_step(1, 'Публикация', step1_body, done=bool(row.is_published)))

        step2_locked = not row.is_published
        if oki_issued:
            links = ''
            if row.contract_link:
                links += f' <a class="panel-wwf-link" href="{escape(row.contract_link)}" target="_blank">ссылка ↗</a>'
            step2_body = f'<span class="panel-wwf-ok">✅ Договор создан: {escape(oki_status or "создан")}</span>{links}'
        elif step2_locked:
            step2_body = '<span class="panel-muted">Сначала опубликуйте победителя.</span>'
        else:
            step2_body = _issue_button('Создать договор')
        steps.append(_step(2, 'Договор OkiDoki', step2_body, locked=step2_locked, done=oki_issued))

        email_block_reason = (
            check_email_send_blocked(
                prize=row.prize, participant=row.participant, status_oki_document=row.status_oki_document,
            )
            if oki_issued and not row.is_send_email else None
        )
        step3_locked = not oki_issued or bool(email_block_reason)
        if row.is_send_email:
            step3_body = '<span class="panel-wwf-ok">✅ Письмо с договором отправлено.</span>'
        elif not oki_issued:
            step3_body = '<span class="panel-muted">Сначала создайте договор OkiDoki.</span>'
        elif email_block_reason:
            step3_body = (
                f'<p class="panel-wwf-error">⚠ Отправка недоступна: {escape(email_block_reason)}</p>'
                f'<button type="button" class="panel-btn panel-btn--small" disabled>Отправить письмо с договором</button>'
            )
        else:
            step3_body = (
                f'<form method="post" action="{urls["send_email"]}">'
                f'{csrf_input}'
                f'<button type="submit" class="panel-btn panel-btn--small">Отправить письмо с договором</button>'
                f'</form>'
            )
        steps.append(_step(3, 'Email с договором', step3_body, locked=step3_locked, done=row.is_send_email))

        is_signed = oki_status == 'Подписан'
        step4_locked = not (row.is_send_email and is_signed)
        if row.shipping_soon_sent:
            step4_body = '<span class="panel-wwf-ok">✅ Письмо о скорой отправке приза отправлено.</span>'
        elif step4_locked:
            if not row.is_send_email:
                reason = 'Сначала отправьте письмо с договором.'
            elif not is_signed:
                reason = f'Договор должен быть в статусе «Подписан» (сейчас: {escape(oki_status or "—")}).'
            else:
                reason = 'Недоступно.'
            step4_body = f'<span class="panel-muted">{reason}</span>'
        else:
            step4_body = (
                f'<form method="post" action="{urls["send_shipping_soon"]}">'
                f'{csrf_input}'
                f'<button type="submit" class="panel-btn panel-btn--small">Отправить письмо «приз скоро отправится»</button>'
                f'</form>'
            )
        steps.append(_step(4, 'Приз скоро будет отправлен', step4_body, locked=step4_locked, done=row.shipping_soon_sent))

    else:  # main
        if oki_issued:
            links = ''
            if row.contract_link:
                links += f' <a class="panel-wwf-link" href="{escape(row.contract_link)}" target="_blank">ссылка ↗</a>'
            step1_body = f'<span class="panel-wwf-ok">✅ Договор создан: {escape(oki_status or "создан")}</span>{links}'
        else:
            step1_body = _issue_button('Создать договор')
        steps.append(_step(1, 'Договор OkiDoki', step1_body, done=oki_issued))

        email_block_reason = (
            check_email_send_blocked(
                prize=row.prize, participant=row.participant, status_oki_document=row.status_oki_document,
            )
            if oki_issued and not row.is_send_email else None
        )
        step2_locked = not oki_issued or bool(email_block_reason)
        if row.is_send_email:
            step2_body = '<span class="panel-wwf-ok">✅ Письмо отправлено.</span>'
        elif not oki_issued:
            step2_body = '<span class="panel-muted">Сначала создайте договор OkiDoki.</span>'
        elif email_block_reason:
            step2_body = (
                f'<p class="panel-wwf-error">⚠ Отправка недоступна: {escape(email_block_reason)}</p>'
                f'<button type="button" class="panel-btn panel-btn--small" disabled>Отправить письмо победителю</button>'
            )
        else:
            step2_body = (
                f'<form method="post" action="{urls["send_email"]}">'
                f'{csrf_input}'
                f'<button type="submit" class="panel-btn panel-btn--small">Отправить письмо победителю</button>'
                f'</form>'
            )
        steps.append(_step(2, 'Письмо победителю', step2_body, locked=step2_locked, done=row.is_send_email))

    return mark_safe('<div class="panel-wwf-wrap">' + ''.join(steps) + '</div>')


def render_fulfillment_summary_masked(row: WinnerRow) -> str:
    """Версия для read-only/маскированной роли: только статусы шагов, без ссылок на договор."""
    oki_issued = _oki_issued(row)
    oki_status = (row.status_oki_document or '').strip()

    parts = ['<div class="panel-wwf-wrap">']
    if row.kind == 'weekly':
        parts.append(f'<p>Публикация: {"✅ опубликован" if row.is_published else "не опубликован"}</p>')
    parts.append(f'<p>Договор: {"✅ создан (" + escape(oki_status or "создан") + ")" if oki_issued else "не создан"}</p>')
    parts.append(f'<p>Письмо: {"✅ отправлено" if row.is_send_email else "не отправлено"}</p>')
    if row.kind == 'weekly':
        parts.append(f'<p>«Приз скоро отправится»: {"✅ отправлено" if row.shipping_soon_sent else "не отправлено"}</p>')
    parts.append('</div>')
    return mark_safe(''.join(parts))
