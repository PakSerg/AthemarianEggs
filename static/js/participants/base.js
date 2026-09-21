document.addEventListener('DOMContentLoaded', function () {
    // Дропдаун пользователя
    const trigger = document.querySelector('.lk-user-trigger');
    const dropdown = document.querySelector('.lk-dropdown');

    if (trigger && dropdown) {
        trigger.addEventListener('click', function (e) {
            e.stopPropagation();
            const open = dropdown.classList.contains('lk-dropdown--open');
            dropdown.classList.toggle('lk-dropdown--open', !open);
        });

        document.addEventListener('click', function (e) {
            if (!trigger.contains(e.target) && !dropdown.contains(e.target)) {
                dropdown.classList.remove('lk-dropdown--open');
            }
        });
    }

    // Кнопки "Добавить чек" — открывают модалку без Bootstrap
    document.querySelectorAll('[data-open-check-modal]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            const modal = document.getElementById('checkModal');
            if (!modal) return;
            modal.style.display = 'flex';
            modal.classList.add('show');
            document.body.classList.add('modal-open');
        });
    });

    // Закрытие модалки
    document.addEventListener('click', function (e) {
        if (e.target.classList.contains('lk-modal-overlay')) {
            closeLkModal(e.target.closest('.lk-modal'));
        }
        if (e.target.closest('[data-close-modal]')) {
            const modal = e.target.closest('.lk-modal');
            if (modal) closeLkModal(modal);
        }
    });

    function closeLkModal(modal) {
        if (!modal) return;
        modal.style.display = 'none';
        modal.classList.remove('show');
        document.body.classList.remove('modal-open');
    }
});
