/**
 * Модалка «Поздравляем! Вы выиграли приз!».
 *
 * Разметку отдаёт сервер (promotion.context_processors.win_prize_modal_context)
 * по последней опубликованной победе. Показывать её или нет, решает браузер:
 * закрытую модалку по конкретной победе участник больше не увидит — отметка
 * живёт в localStorage и переживает перезагрузку страницы.
 */
(function () {
  'use strict';

  var STORAGE_PREFIX = 'win-prize-modal-dismissed:';

  function key(id) {
    return STORAGE_PREFIX + id;
  }

  function isDismissed(id) {
    try {
      return window.localStorage.getItem(key(id)) === '1';
    } catch (error) {
      return false;
    }
  }

  function dismiss(id) {
    try {
      window.localStorage.setItem(key(id), '1');
    } catch (error) {
      /* приватный режим — просто не запоминаем */
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    var modal = document.querySelector('[data-win-prize-modal]');
    if (!modal) {
      return;
    }

    var id = modal.getAttribute('data-win-prize-modal');
    if (isDismissed(id)) {
      return;
    }

    modal.hidden = false;
    document.body.classList.add('is-modal-open');

    function close() {
      modal.hidden = true;
      document.body.classList.remove('is-modal-open');
      dismiss(id);
    }

    Array.prototype.forEach.call(
      modal.querySelectorAll('[data-win-prize-modal-close], [data-win-prize-modal-backdrop]'),
      function (node) {
        node.addEventListener('click', close);
      }
    );

    var action = modal.querySelector('[data-win-prize-modal-action]');
    if (action) {
      action.addEventListener('click', function () {
        dismiss(id);
      });
    }

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !modal.hidden) {
        close();
      }
    });
  });
})();
