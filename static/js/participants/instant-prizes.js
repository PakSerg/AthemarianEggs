/**
 * Лоток яиц — моментальные призы.
 *
 * Клиент не знает исхода до клика: он отправляет номер яйца на
 * /participants/api/instant/play/ и рисует то, что ответил сервер. Ни в
 * разметке, ни в предыдущих ответах выигрышного яйца нет — «подсмотреть» приз
 * нельзя даже из консоли.
 *
 * Модалка открывается сама при первом заходе после того, как приняли чек
 * (data-egg-autoopen). Если участник её закрыл, на этой вкладке повторно она
 * не всплывает — отметка живёт в sessionStorage, чтобы не преследовать
 * участника на каждой странице, но и не терять попытки навсегда.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'instant-prize-modal-dismissed';

  function getCookie(name) {
    var match = document.cookie.match(new RegExp('(^|;\\s*)' + name + '=([^;]*)'));
    return match ? decodeURIComponent(match[2]) : '';
  }

  function remember() {
    try {
      window.sessionStorage.setItem(STORAGE_KEY, '1');
    } catch (error) {
      /* приватный режим — просто не запоминаем */
    }
  }

  function isDismissed() {
    try {
      return window.sessionStorage.getItem(STORAGE_KEY) === '1';
    } catch (error) {
      return false;
    }
  }

  function init(modal) {
    var tray = modal.querySelector('[data-egg-tray]');
    var result = modal.querySelector('[data-egg-result]');
    var resultTitle = modal.querySelector('[data-egg-result-title]');
    var resultText = modal.querySelector('[data-egg-result-text]');
    var retryButton = modal.querySelector('[data-egg-retry]');
    var prizesLink = modal.querySelector('[data-egg-to-prizes]');
    var attemptsValue = modal.querySelector('[data-egg-attempts-value]');
    var subtitle = modal.querySelector('[data-egg-subtitle]');
    var playUrl = modal.getAttribute('data-egg-play-url');

    var attemptsLeft = parseInt(modal.getAttribute('data-egg-attempts'), 10) || 0;
    var busy = false;

    function open() {
      modal.hidden = false;
      document.body.classList.add('is-modal-open');
    }

    function close() {
      modal.hidden = true;
      document.body.classList.remove('is-modal-open');
      remember();
    }

    function resetTray() {
      result.hidden = true;
      retryButton.hidden = true;
      subtitle.textContent = 'Выберите яйцо — внутри может оказаться приз';
      Array.prototype.forEach.call(tray.querySelectorAll('.egg'), function (egg) {
        egg.classList.remove('is-open', 'is-win', 'is-empty');
        egg.disabled = false;
      });
    }

    function renderResult(data, egg) {
      egg.classList.add('is-open', data.is_win ? 'is-win' : 'is-empty');
      Array.prototype.forEach.call(tray.querySelectorAll('.egg'), function (item) {
        item.disabled = true;
      });

      result.hidden = false;
      if (data.is_win) {
        var prize = data.prize || {};
        resultTitle.textContent = 'Поздравляем! Вы выиграли';
        resultText.textContent = prize.name || 'приз';
        subtitle.textContent = 'Приз уже в личном кабинете — мы напишем вам на почту';
        if (prizesLink) {
          prizesLink.hidden = false;
        }
      } else {
        resultTitle.textContent = 'В этот раз пусто';
        resultText.textContent = attemptsLeft > 0
          ? 'Попробуйте ещё раз — у вас остались попытки'
          : 'Зарегистрируйте новый чек, чтобы получить ещё одну попытку';
        subtitle.textContent = '';
      }

      retryButton.hidden = attemptsLeft <= 0 || data.is_win;
    }

    function showError(message) {
      result.hidden = false;
      resultTitle.textContent = 'Не получилось';
      resultText.textContent = message;
      retryButton.hidden = attemptsLeft <= 0;
    }

    function play(egg) {
      if (busy || attemptsLeft <= 0) {
        return;
      }
      busy = true;
      egg.classList.add('is-loading');

      fetch(playUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': getCookie('csrftoken'),
          'X-Requested-With': 'XMLHttpRequest',
        },
        body: JSON.stringify({ egg: parseInt(egg.getAttribute('data-egg'), 10) }),
      })
        .then(function (response) {
          return response.json().then(function (data) {
            return { ok: response.ok, data: data };
          });
        })
        .then(function (payload) {
          egg.classList.remove('is-loading');
          busy = false;

          if (!payload.ok || !payload.data.ok) {
            showError((payload.data && payload.data.error) || 'Попробуйте ещё раз позже.');
            return;
          }

          attemptsLeft = payload.data.attempts_left;
          attemptsValue.textContent = attemptsLeft;
          renderResult(payload.data, egg);
        })
        .catch(function () {
          egg.classList.remove('is-loading');
          busy = false;
          showError('Нет связи с сервером. Попробуйте ещё раз.');
        });
    }

    tray.addEventListener('click', function (event) {
      var egg = event.target.closest('.egg');
      if (egg && !egg.disabled) {
        play(egg);
      }
    });

    retryButton.addEventListener('click', resetTray);

    Array.prototype.forEach.call(modal.querySelectorAll('[data-egg-close]'), function (node) {
      node.addEventListener('click', close);
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !modal.hidden) {
        close();
      }
    });

    // Кнопка «Открыть яйцо» где угодно на странице открывает лоток заново.
    Array.prototype.forEach.call(document.querySelectorAll('[data-egg-open]'), function (node) {
      node.addEventListener('click', function (event) {
        event.preventDefault();
        resetTray();
        open();
      });
    });

    if (modal.getAttribute('data-egg-autoopen') === '1' && !isDismissed()) {
      open();
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    var modal = document.querySelector('[data-egg-modal]');
    if (modal) {
      init(modal);
    }
  });
})();
