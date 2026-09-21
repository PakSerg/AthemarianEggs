/**
 * Интерактив лендинга: бургер-меню, таймер до розыгрыша, вкладки победителей,
 * аккордеон FAQ. Один файл на весь лендинг — отдельная сборка (Vite, SCSS)
 * здесь не нужна: страница статична, а оформление живёт в CSS-токенах.
 */
(function () {
  'use strict';

  function initHeader() {
    var toggle = document.querySelector('[data-header-toggle]');
    var nav = document.querySelector('[data-header-nav]');
    if (!toggle || !nav) {
      return;
    }
    toggle.addEventListener('click', function () {
      var isOpen = nav.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    });
    nav.addEventListener('click', function (event) {
      if (event.target.closest('a')) {
        nav.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  function pad(value) {
    return String(value).padStart(2, '0');
  }

  function initCountdowns() {
    var nodes = document.querySelectorAll('[data-countdown]');
    if (!nodes.length) {
      return;
    }

    Array.prototype.forEach.call(nodes, function (node) {
      var target = new Date(node.getAttribute('data-countdown')).getTime();
      if (Number.isNaN(target)) {
        return;
      }

      var cells = {
        days: node.querySelector('[data-countdown-days]'),
        hours: node.querySelector('[data-countdown-hours]'),
        minutes: node.querySelector('[data-countdown-minutes]'),
        seconds: node.querySelector('[data-countdown-seconds]'),
      };

      function tick() {
        var left = Math.max(target - Date.now(), 0);
        var seconds = Math.floor(left / 1000);
        if (cells.days) cells.days.textContent = pad(Math.floor(seconds / 86400));
        if (cells.hours) cells.hours.textContent = pad(Math.floor((seconds % 86400) / 3600));
        if (cells.minutes) cells.minutes.textContent = pad(Math.floor((seconds % 3600) / 60));
        if (cells.seconds) cells.seconds.textContent = pad(seconds % 60);
      }

      tick();
      window.setInterval(tick, 1000);
    });
  }

  function initWinners() {
    var root = document.querySelector('[data-winners]');
    if (!root) {
      return;
    }
    root.addEventListener('click', function (event) {
      var tab = event.target.closest('[data-winners-tab]');
      if (!tab) {
        return;
      }
      var key = tab.getAttribute('data-winners-tab');
      Array.prototype.forEach.call(root.querySelectorAll('[data-winners-tab]'), function (node) {
        node.classList.toggle('is-active', node === tab);
      });
      Array.prototype.forEach.call(root.querySelectorAll('[data-winners-panel]'), function (panel) {
        panel.classList.toggle('is-hidden', panel.getAttribute('data-winners-panel') !== key);
      });
    });
  }

  function initAccordion() {
    Array.prototype.forEach.call(document.querySelectorAll('[data-accordion]'), function (root) {
      root.addEventListener('click', function (event) {
        var trigger = event.target.closest('[data-accordion-trigger]');
        if (!trigger) {
          return;
        }
        var item = trigger.closest('[data-accordion-item]');
        var panel = item && item.querySelector('[data-accordion-panel]');
        if (!panel) {
          return;
        }
        var isOpen = trigger.getAttribute('aria-expanded') === 'true';
        trigger.setAttribute('aria-expanded', isOpen ? 'false' : 'true');
        panel.hidden = isOpen;
      });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initHeader();
    initCountdowns();
    initWinners();
    initAccordion();
  });
})();
