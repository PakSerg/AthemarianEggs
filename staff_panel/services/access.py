def user_has_panel_access(user) -> bool:
    return bool(user and user.is_authenticated and user.is_staff)


def is_pii_masked(user) -> bool:
    """Staff, но не суперюзер — видит страницы «Участники» и «Чеки» (дашборд и Excel-выгрузки)
    с замаскированными ПДн. Раздел «Победители» из этого правила исключён намеренно —
    там ПДн всегда показываются без маскировки, см. вызовы в views.py."""
    return bool(user and user.is_authenticated and user.is_staff and not user.is_superuser)


def is_read_only_staff(user) -> bool:
    """Staff, но не суперюзер — видит все страницы панели в режиме read-only (без права
    сохранять/удалять/публиковать). Это отдельная от маскировки ПДн настройка доступа —
    см. is_pii_masked."""
    return bool(user and user.is_authenticated and user.is_staff and not user.is_superuser)


def can_manage_prize_fulfillment(user) -> bool:
    """
    Вручение призов — файлы электронных призов и замена победителя — доступно и
    менеджеру (staff без суперправ), а не только суперюзеру: это его повседневная
    работа, а не админка. Раздел «Победители» и так одинаков для всех ролей
    (см. is_pii_masked), теперь такова же и работа с призами.

    Всё остальное для менеджера остаётся read-only (см. is_read_only_staff):
    выставление договоров, письма, публикация и удаление победителей.
    """
    return bool(user and user.is_authenticated and user.is_staff)


# Менеджер по обработке чеков — единственное исключение из общего read-only режима:
# ему разрешена полная обработка раздела «Чеки», при этом ПДн остаются замаскированы
# везде (как и для любого staff, не суперюзера), а Cyclops (выплаты) от него скрыт.
RECEIPTS_MANAGER_EMAILS = {'manager@cheqly.ru'}


def is_receipts_manager(user) -> bool:
    return bool(user and user.is_authenticated and user.email in RECEIPTS_MANAGER_EMAILS)


def can_manage_receipts(user) -> bool:
    """Полный доступ (в т.ч. запись) к разделу «Чеки»: суперюзер или менеджер по чекам."""
    return bool(user and user.is_authenticated and (user.is_superuser or is_receipts_manager(user)))


def can_view_cyclops(user) -> bool:
    """Cyclops (выплаты через Точка Банк) скрыт от менеджера по чекам — даже ссылка в хедере."""
    return bool(user and user.is_authenticated and user.is_staff and not is_receipts_manager(user))
