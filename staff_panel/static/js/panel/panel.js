(function () {
  // ─── Тосты: тот же SweetAlert2 и те же настройки, что в FarPostLogs
  // (ad_list.js → showAdListToast): всплывашка справа снизу, таймер с полосой,
  // ошибки висят дольше. Если CDN SweetAlert2 недоступен, показываем тот же
  // текст простым фолбэком, чтобы действие не оставалось без ответа.

  var TOAST_TIMERS = { error: 6500, warning: 5200, success: 4200, info: 4000 };

  function fallbackToast(message, level) {
    var node = document.createElement('div');
    node.className = 'panel-toast-fallback panel-toast-fallback--' + (level || 'info');
    node.setAttribute('role', 'alert');
    node.textContent = message;
    document.body.appendChild(node);
    setTimeout(function () { node.remove(); }, TOAST_TIMERS[level] || 4200);
  }

  function panelToast(message, level) {
    message = (message || '').toString().trim();
    if (!message) return;
    level = level || 'success';
    if (typeof Swal === 'undefined') { fallbackToast(message, level); return; }
    Swal.fire({
      toast: true,
      position: 'bottom-end',
      icon: level,
      title: message,
      showConfirmButton: false,
      timer: TOAST_TIMERS[level] || 4200,
      timerProgressBar: true,
      customClass: { popup: 'panel-swalert' },
    });
  }

  /** Уровень тоста по классу сообщения Django (messages framework). */
  function levelFromTags(tags) {
    var t = (tags || '').toLowerCase();
    if (t.indexOf('error') >= 0 || t.indexOf('danger') >= 0) return 'error';
    if (t.indexOf('warning') >= 0) return 'warning';
    if (t.indexOf('info') >= 0 || t.indexOf('debug') >= 0) return 'info';
    return 'success';
  }

  function flushServerMessages() {
    var holder = document.getElementById('panel-server-messages');
    if (!holder) return;
    Array.prototype.forEach.call(holder.children, function (el, index) {
      var message = (el.textContent || '').trim();
      var level = levelFromTags(el.dataset.level);
      // Несколько сообщений подряд SweetAlert2 показывает по одному —
      // разносим их во времени, иначе видно только последнее.
      setTimeout(function () { panelToast(message, level); }, index * 400);
    });
    holder.remove();
  }

  window.panelToast = panelToast;
  window.panelFlushServerMessages = flushServerMessages;
})();

(function () {
  function modalEls() {
    return {
      backdrop: document.getElementById('panel-modal-backdrop'),
      modal: document.getElementById('panel-modal'),
      content: document.getElementById('panel-modal-content'),
    };
  }

  function openModal(url) {
    var els = modalEls();
    if (!els.modal) return;
    els.content.innerHTML = '<div class="panel-drawer__loading">Загрузка…</div>';
    els.backdrop.classList.add('is-open');
    els.modal.classList.add('is-open');
    els.modal.setAttribute('aria-hidden', 'false');
    document.body.classList.add('panel-no-scroll');
    var sep = url.indexOf('?') >= 0 ? '&' : '?';
    // Передаём текущие фильтры страницы списка на сервер, чтобы после сохранения
    // формы в модалке (обычный, не ajax, POST-редирект) вернуться на список с теми
    // же фильтрами — Referer для этого ненадёжен (не все браузеры/настройки его шлют).
    var listQuery = window.location.search ? window.location.search.slice(1) : '';
    fetch(url + sep + 'partial=1&list_query=' + encodeURIComponent(listQuery), { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        modalEls().content.innerHTML = html;
        initCustomSelects(modalEls().content);
        initAjaxForms(modalEls().content);
        initBirthDatePicker();
        initCityAutocomplete();
        initWinnerCreateForm();
        initWinnerReplaceForm(modalEls().content);
        initReceiptPromoValidation(modalEls().content);
        initAutoDistributeSelect(modalEls().content);
        initConfirmForms(modalEls().content);
      })
      .catch(function () {
        modalEls().content.innerHTML =
          '<div class="panel-drawer__loading">Не удалось загрузить карточку.</div>';
        window.panelToast('Не удалось загрузить карточку — попробуйте ещё раз', 'error');
      });
  }

  function initAutoDistributeSelect(root) {
    var select = (root || document).querySelector('#panel-ad-draw-select');
    if (!select || select.dataset.bound) return;
    select.dataset.bound = '1';
    select.addEventListener('panel-select-change', function (e) {
      var base = select.dataset.baseUrl;
      var value = (e.detail && e.detail.value) || '';
      if (!value) { openModal(base); return; }
      var sep = value.indexOf(':');
      var kind = value.slice(0, sep);
      var period = value.slice(sep + 1);
      openModal(base + '?kind=' + encodeURIComponent(kind) + '&period=' + encodeURIComponent(period));
    });
  }

  function submitAjaxForm(form) {
    var submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    fetch(form.action, {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      body: new FormData(form),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          window.location = data.redirect;
          return;
        }
        modalEls().content.innerHTML = data.html;
        initCustomSelects(modalEls().content);
        initAjaxForms(modalEls().content);
        initAutoDistributeSelect(modalEls().content);
        window.panelToast(data.error || 'Проверьте заполнение формы', 'error');
      })
      .catch(function () {
        if (submitBtn) submitBtn.disabled = false;
        form.submit();
      });
  }

  function initAjaxForms(root) {
    (root || document).querySelectorAll('form[data-panel-ajax-form]').forEach(function (form) {
      if (form.dataset.ajaxBound) return;
      form.dataset.ajaxBound = '1';
      form.addEventListener('submit', function (e) {
        e.preventDefault();
        submitAjaxForm(form);
      });
    });
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function initWinnerCreateForm() {
    var form = document.querySelector('form[data-participant-search-url]');
    if (!form) return;
    var searchUrl = form.dataset.participantSearchUrl;
    var receiptsUrl = form.dataset.receiptsUrl;
    var searchInput = document.getElementById('id_participant_search');
    var suggestionsBox = document.getElementById('panel-winner-participant-suggestions');
    var participantHidden = document.getElementById('id_participant');
    var receiptSelect = document.getElementById('id_receipt_select');
    if (!searchInput || !suggestionsBox || !participantHidden || !receiptSelect) return;

    var receiptList = receiptSelect.querySelector('.panel-custom-select__list');
    var receiptValueEl = receiptSelect.querySelector('.panel-custom-select__value');
    var debounceTimer = null;
    var requestSeq = 0;
    var SELECT_EVENT = window.PointerEvent ? 'pointerdown' : 'mousedown';

    function hideSuggestions() {
      suggestionsBox.style.display = 'none';
      suggestionsBox.innerHTML = '';
    }

    function loadReceipts(participantId) {
      receiptSelect.classList.add('is-disabled');
      receiptSelect.querySelectorAll('input[type="hidden"]').forEach(function (i) { i.remove(); });
      receiptList.innerHTML = '';
      if (!participantId) {
        receiptValueEl.textContent = 'Сначала выберите победителя';
        return;
      }
      receiptValueEl.textContent = 'Загрузка…';
      fetch(receiptsUrl + '?participant_id=' + encodeURIComponent(participantId), {
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var noneLabel = '— без привязки к QR-коду —';
          var html = '<li class="panel-custom-select__item is-selected" data-value="" data-label="'
            + escapeHtml(noneLabel) + '"><span class="panel-custom-select__check"></span>' + escapeHtml(noneLabel) + '</li>';
          (data.results || []).forEach(function (item) {
            html += '<li class="panel-custom-select__item" data-value="' + escapeHtml(item.id) + '" data-label="'
              + escapeHtml(item.text) + '"><span class="panel-custom-select__check"></span>' + escapeHtml(item.text) + '</li>';
          });
          receiptList.innerHTML = html;
          receiptSelect.classList.remove('is-disabled');
          refreshCustomSelect(receiptSelect);
        })
        .catch(function () {
          receiptValueEl.textContent = 'Не удалось загрузить QR-коды';
        });
    }

    function renderSuggestions(items) {
      suggestionsBox.innerHTML = '';
      if (!items.length) { hideSuggestions(); return; }
      items.forEach(function (item) {
        var option = document.createElement('div');
        option.className = 'panel-city-suggestion-item';
        option.textContent = item.text;
        // pointerdown/mousedown вместо click: click срабатывает после blur,
        // который успевает удалить подсказку из DOM — выбор терялся.
        option.addEventListener(SELECT_EVENT, function (e) {
          e.preventDefault();
          if (debounceTimer) clearTimeout(debounceTimer);
          debounceTimer = null;
          requestSeq += 1;
          searchInput.value = item.text;
          participantHidden.value = item.id;
          hideSuggestions();
          loadReceipts(item.id);
        });
        suggestionsBox.appendChild(option);
      });
      suggestionsBox.style.display = 'block';
    }

    searchInput.addEventListener('input', function () {
      var value = searchInput.value.trim();
      participantHidden.value = '';
      if (debounceTimer) clearTimeout(debounceTimer);
      if (value.length < 2) { requestSeq += 1; hideSuggestions(); return; }
      debounceTimer = setTimeout(function () {
        var seq = ++requestSeq;
        fetch(searchUrl + '?q=' + encodeURIComponent(value), { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            // Ответ на устаревший запрос (или пришедший после выбора) игнорируем.
            if (seq !== requestSeq) return;
            renderSuggestions(data.results || []);
          })
          .catch(function () { if (seq === requestSeq) hideSuggestions(); });
      }, 250);
    });

    // Клик по подсказке больше не снимает фокус (preventDefault выше),
    // поэтому blur означает реальный уход из поля — прячем список сразу.
    searchInput.addEventListener('blur', function () {
      hideSuggestions();
    });

    form.addEventListener('submit', function (e) {
      if (!participantHidden.value) {
        e.preventDefault();
        searchInput.focus();
      }
    });
  }

  // --- Замена победителя (модалка «Заменить победителя») ---

  function initWinnerReplaceForm(root) {
    var form = (root || document).querySelector('form[data-winner-replace-form]');
    if (!form || form.dataset.wrBound) return;
    form.dataset.wrBound = '1';

    var modeInput = form.querySelector('[data-wr-mode]');
    var participantInput = form.querySelector('[data-wr-participant]');
    var receiptInput = form.querySelector('[data-wr-receipt]');
    var errorEl = form.querySelector('[data-wr-error]');
    var chosenHint = form.querySelector('[data-wr-chosen-hint]');
    var submitBtn = form.querySelector('[data-wr-submit]');
    var select = form.querySelector('[data-wr-select]');
    var trigger = select && select.querySelector('.panel-wr-select__trigger');
    var panel = select && select.querySelector('.panel-wr-select__panel');
    var searchInput = select && select.querySelector('.panel-wr-select__input');
    var list = select && select.querySelector('.panel-wr-select__list');
    var loaded = false;
    var debounceTimer = null;
    var requestSeq = 0;

    function showError(message) {
      if (!errorEl) return;
      errorEl.textContent = message;
      errorEl.hidden = !message;
    }

    function setMode(mode) {
      modeInput.value = mode;
      form.querySelectorAll('[data-wr-mode-btn]').forEach(function (btn) {
        btn.classList.toggle('is-active', btn.dataset.wrModeBtn === mode);
      });
      form.querySelectorAll('[data-wr-pane]').forEach(function (pane) {
        pane.hidden = pane.dataset.wrPane !== mode;
      });
      showError('');
      if (mode === 'manual' && !loaded) { loadCandidates(''); }
    }

    function renderMessage(text) {
      list.innerHTML = '<div class="panel-wr-select__empty">' + escapeHtml(text) + '</div>';
    }

    function renderGroup(title, options, eligible) {
      if (!options.length) return '';
      var html = '<div class="panel-wr-select__group">' + escapeHtml(title) + '</div>';
      options.forEach(function (option) {
        var tags = '';
        if (option.reserve_rank) {
          tags += '<span class="panel-wr-tag panel-wr-tag--reserve">резерв №' + option.reserve_rank + '</span>';
        }
        tags += eligible
          ? '<span class="panel-wr-tag panel-wr-tag--ok">подходит</span>'
          : '<span class="panel-wr-tag panel-wr-tag--warn">не подходит</span>';
        html += (
          '<div class="panel-wr-option' + (eligible ? ' panel-wr-option--ok' : '') + '"' +
          ' data-participant="' + option.participant_id + '"' +
          ' data-receipt="' + (option.receipt_id === null ? '' : option.receipt_id) + '"' +
          ' data-label="' + escapeHtml(option.label) + '">' +
          '<div class="panel-wr-option__head"><span class="panel-wr-option__name">' +
          escapeHtml(option.label) + '</span>' + tags + '</div>' +
          '<div class="panel-wr-option__meta">' + escapeHtml(option.note) +
          ' · ' + escapeHtml(option.receipt_label) + '</div>' +
          '</div>'
        );
      });
      return html;
    }

    function renderOptions(data) {
      var eligible = data.eligible || [];
      var others = data.others || [];
      if (!eligible.length && !others.length) {
        renderMessage('Никого не найдено.');
        return;
      }
      list.innerHTML =
        renderGroup('Подходят под условия розыгрыша', eligible, true) +
        renderGroup('Не подходят под условия — но выбрать можно', others, false);

      list.querySelectorAll('.panel-wr-option').forEach(function (item) {
        item.addEventListener('click', function () { choose(item); });
      });
    }

    function choose(item) {
      participantInput.value = item.dataset.participant;
      receiptInput.value = item.dataset.receipt || '';
      var valueEl = select.querySelector('.panel-wr-select__value');
      valueEl.textContent = item.dataset.label;
      valueEl.classList.remove('panel-wr-select__value--empty');
      if (chosenHint) {
        var meta = item.querySelector('.panel-wr-option__meta');
        chosenHint.textContent = meta ? meta.textContent : '';
        chosenHint.hidden = !chosenHint.textContent;
      }
      panel.hidden = true;
      trigger.setAttribute('aria-expanded', 'false');
      showError('');
    }

    function loadCandidates(query) {
      loaded = true;
      renderMessage('Загрузка…');
      var seq = ++requestSeq;
      fetch(form.dataset.candidatesUrl + '?q=' + encodeURIComponent(query || ''), {
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (seq !== requestSeq) return;
          if (!data.ok) { renderMessage(data.error || 'Не удалось загрузить список.'); return; }
          renderOptions(data);
        })
        .catch(function () {
          if (seq === requestSeq) renderMessage('Не удалось загрузить список.');
        });
    }

    form.querySelectorAll('[data-wr-mode-btn]').forEach(function (btn) {
      btn.addEventListener('click', function () { setMode(btn.dataset.wrModeBtn); });
    });

    if (select) {
      trigger.addEventListener('click', function (e) {
        e.stopPropagation();
        var open = !panel.hidden;
        panel.hidden = open;
        trigger.setAttribute('aria-expanded', String(!open));
        if (!open) {
          if (!loaded) loadCandidates('');
          searchInput.focus();
        }
      });
      panel.addEventListener('click', function (e) { e.stopPropagation(); });
      searchInput.addEventListener('input', function () {
        var value = searchInput.value;
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function () { loadCandidates(value); }, 250);
      });
      searchInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); loadCandidates(searchInput.value); }
      });
      document.addEventListener('click', function () { panel.hidden = true; });
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (modeInput.value === 'manual' && !participantInput.value) {
        showError('Выберите нового победителя из списка.');
        return;
      }
      showError('');
      if (submitBtn) submitBtn.disabled = true;
      fetch(form.action, {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        body: new FormData(form),
      })
        .then(function (r) {
          return r.json().catch(function () {
            return { ok: false, error: 'Не удалось выполнить замену.' };
          });
        })
        .then(function (data) {
          if (data.ok) { window.location = data.redirect; return; }
          if (submitBtn) submitBtn.disabled = false;
          showError(data.error || 'Не удалось выполнить замену.');
        })
        .catch(function () {
          if (submitBtn) submitBtn.disabled = false;
          showError('Не удалось выполнить замену.');
        });
    });

    setMode(modeInput.value || 'auto');
  }

  function showConfirm(text, danger) {
    return new Promise(function (resolve) {
      var backdrop = document.getElementById('panel-confirm');
      var textEl = document.getElementById('panel-confirm-text');
      var yesBtn = document.getElementById('panel-confirm-yes');
      var noBtn = document.getElementById('panel-confirm-no');
      if (!backdrop || !textEl || !yesBtn || !noBtn) { resolve(true); return; }
      textEl.textContent = text;
      backdrop.classList.add('is-open');
      backdrop.classList.toggle('is-danger', !!danger);

      function cleanup(result) {
        backdrop.classList.remove('is-open');
        backdrop.classList.remove('is-danger');
        yesBtn.removeEventListener('click', onYes);
        noBtn.removeEventListener('click', onNo);
        backdrop.removeEventListener('click', onBackdrop);
        resolve(result);
      }
      function onYes() { cleanup(true); }
      function onNo() { cleanup(false); }
      function onBackdrop(e) { if (e.target === backdrop) cleanup(false); }

      yesBtn.addEventListener('click', onYes);
      noBtn.addEventListener('click', onNo);
      backdrop.addEventListener('click', onBackdrop);
    });
  }

  function initReceiptPromoValidation(root) {
    var form = (root || document).querySelector('form[data-receipt-form]');
    if (!form) return;
    var checkboxes = form.querySelectorAll('input[name="promo_item_idx"]');
    var statusWrap = form.querySelector('.panel-custom-select[data-name="status"]');
    var statusHidden = statusWrap && statusWrap.querySelector('input[type="hidden"]');
    var errorEl = form.querySelector('[data-promo-error]');
    var hintEl = form.querySelector('[data-promo-hint]');
    var submitBtn = form.querySelector('button[type="submit"]');
    var threshold = parseFloat(form.dataset.promoThreshold || '250');

    function computeTotal() {
      var total = 0;
      checkboxes.forEach(function (cb) {
        if (cb.checked) total += parseFloat(cb.dataset.sum || '0');
      });
      return total;
    }

    // Кнопка «Сохранить» никогда не блокируется — чек должен быть редактируемым
    // всегда, в том числе уже отклонённый или принятый. При несогласованности статуса
    // и суммы акционных товаров модератор только предупреждается текстом и, при
    // отправке формы, подтверждающим диалогом (см. обработчик submit ниже).
    function validate() {
      var total = computeTotal();
      var status = statusHidden ? statusHidden.value : '';
      if (hintEl) {
        hintEl.textContent = 'Акционных товаров отмечено на ' + total.toFixed(2) + ' ₽ из '
          + threshold.toFixed(0) + ' ₽ необходимых — '
          + (total >= threshold ? 'порог достигнут, чек можно принимать.' : 'порог ещё не достигнут.');
      }
      var message = '';
      if (status === 'rejected' && total >= threshold) {
        message = 'Внимание: сумма акционных товаров ' + total.toFixed(2)
          + ' ₽ соответствует условию (≥ ' + threshold.toFixed(0) + ' ₽), но чек отклоняется.';
      }
      if (errorEl) {
        errorEl.textContent = message;
        errorEl.hidden = !message;
      }
      return !message;
    }

    checkboxes.forEach(function (cb) { cb.addEventListener('change', validate); });
    if (statusWrap) {
      // Каждый item внутри статуса уже помечает себя is-selected и обновляет скрытый
      // input в своём собственном click-обработчике (см. bindCustomSelectItems), который
      // останавливает всплытие — поэтому валидацию вешаем на сами item, а не на wrap,
      // и она сработает следующим обработчиком click на том же элементе.
      statusWrap.querySelectorAll('.panel-custom-select__item').forEach(function (item) {
        item.addEventListener('click', validate);
      });
    }
    form.addEventListener('submit', function (e) {
      var confirmed = validate();
      var status = statusHidden ? statusHidden.value : '';
      var total = computeTotal();
      if (!confirmed) {
        e.preventDefault();
        showConfirm(
          'Внимание: в поле «Статус» сейчас выбрано «Отклонён» — чек будет сохранён ОТКЛОНЁННЫМ. '
          + 'При этом сумма акционных товаров ' + total.toFixed(2) + ' ₽ соответствует условию (≥ '
          + threshold.toFixed(0) + ' ₽). Если хотите принять чек — нажмите «Нет» и переключите статус '
          + 'на «Подтверждён». Сохранить как отклонённый?',
        ).then(function (ok) {
          if (ok) form.submit();
        });
        return;
      }
      if (status === 'confirmed' && total < threshold) {
        e.preventDefault();
        showConfirm(
          'Внимание: в поле «Статус» сейчас выбрано «Подтверждён» — чек будет сохранён ПРИНЯТЫМ. '
          + 'При этом сумма акционных товаров меньше ' + threshold.toFixed(0) + ' ₽. '
          + 'Сохранить как принятый?',
        ).then(function (ok) {
          // form.submit() не порождает событие submit, поэтому этот обработчик
          // не сработает повторно и не зациклится.
          if (ok) form.submit();
        });
      }
    });
    validate();
  }

  function initBirthDatePicker() {
    var el = document.getElementById('id_birth_date');
    if (!el || typeof flatpickr === 'undefined') return;
    flatpickr(el, {
      locale: 'ru',
      dateFormat: 'd-m-Y',
      allowInput: true,
      disableMobile: false,
    });
  }

  function initCityAutocomplete() {
    var cityInput = document.getElementById('id_city');
    var suggestionsBox = document.getElementById('panel-city-suggestions');
    var cityFieldError = document.getElementById('panel-city-field-error');
    if (!cityInput || !suggestionsBox || !cityFieldError) return;
    var token = cityInput.dataset.dadataToken;
    if (!token) return;

    var debounceTimer = null;
    var requestSeq = 0;
    var SELECT_EVENT = window.PointerEvent ? 'pointerdown' : 'mousedown';

    function hideSuggestions() {
      suggestionsBox.style.display = 'none';
      suggestionsBox.innerHTML = '';
    }

    function renderSuggestions(items) {
      suggestionsBox.innerHTML = '';
      if (!items.length) { hideSuggestions(); return; }
      items.forEach(function (item) {
        var option = document.createElement('div');
        option.className = 'panel-city-suggestion-item';
        option.textContent = item;
        // pointerdown/mousedown вместо click: click срабатывает после blur,
        // который успевает удалить подсказку из DOM — выбор терялся.
        option.addEventListener(SELECT_EVENT, function (e) {
          e.preventDefault();
          if (debounceTimer) clearTimeout(debounceTimer);
          debounceTimer = null;
          requestSeq += 1;
          cityInput.value = item;
          hideSuggestions();
        });
        suggestionsBox.appendChild(option);
      });
      suggestionsBox.style.display = 'block';
    }

    function fetchCitySuggestions(query) {
      var seq = ++requestSeq;
      fetch('https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
          'Authorization': 'Token ' + token,
        },
        body: JSON.stringify({
          query: query,
          count: 7,
          from_bound: { value: 'city' },
          to_bound: { value: 'city' },
        }),
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          // Ответ на устаревший запрос (или пришедший после выбора) игнорируем.
          if (seq !== requestSeq) return;
          if (!data) { hideSuggestions(); return; }
          var seen = {};
          var cities = [];
          (data.suggestions || []).forEach(function (s) {
            var city = (s.data && (s.data.city_with_type || s.data.city)) || '';
            if (city && !seen[city]) { seen[city] = true; cities.push(city); }
          });
          renderSuggestions(cities);
        })
        .catch(function () { if (seq === requestSeq) hideSuggestions(); });
    }

    cityInput.addEventListener('input', function () {
      var value = cityInput.value.trim();
      if (debounceTimer) clearTimeout(debounceTimer);
      if (value.length < 2) { requestSeq += 1; hideSuggestions(); return; }
      debounceTimer = setTimeout(function () { fetchCitySuggestions(value); }, 250);
    });

    // Клик по подсказке больше не снимает фокус (preventDefault выше),
    // поэтому blur означает реальный уход из поля — прячем список сразу.
    cityInput.addEventListener('blur', function () {
      hideSuggestions();
    });
  }

  function closeModal() {
    var els = modalEls();
    if (!els.modal) return;
    els.backdrop.classList.remove('is-open');
    els.modal.classList.remove('is-open');
    els.modal.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('panel-no-scroll');
  }

  function openLightbox(src) {
    var img = document.getElementById('panel-lightbox-img');
    var box = document.getElementById('panel-lightbox');
    if (!img || !box) return;
    img.src = src;
    box.classList.add('is-open');
  }

  function closeLightbox() {
    var box = document.getElementById('panel-lightbox');
    if (box) box.classList.remove('is-open');
  }

  function customSelectSelectedItems(wrap) {
    return Array.from(wrap.querySelectorAll('.panel-custom-select__item.is-selected'));
  }

  function customSelectUpdateTrigger(wrap) {
    var valueEl = wrap.querySelector('.panel-custom-select__value');
    var items = customSelectSelectedItems(wrap);
    if (items.length === 0) {
      valueEl.textContent = wrap.dataset.allLabel || 'Все';
      valueEl.classList.add('panel-custom-select__value--empty');
    } else if (items.length === 1) {
      valueEl.textContent = items[0].dataset.label || items[0].textContent.trim();
      valueEl.classList.remove('panel-custom-select__value--empty');
    } else {
      valueEl.textContent = 'Выбрано: ' + items.length;
      valueEl.classList.remove('panel-custom-select__value--empty');
    }
  }

  /** Текущее значение селекта (для одиночного выбора) — '' если ничего не выбрано. */
  function customSelectValue(wrap) {
    var items = customSelectSelectedItems(wrap);
    return items.length ? (items[0].dataset.value || '') : '';
  }

  /**
   * Поиск по пунктам — для длинных списков (призы, реестры). Включается классом
   * .panel-custom-select--searchable: искать глазами среди трёх десятков призов
   * невозможно, а нативный <select> в дашборде не используется принципиально.
   */
  function initCustomSelectSearch(wrap) {
    if (!wrap.classList.contains('panel-custom-select--searchable')) return;
    var panel = wrap.querySelector('.panel-custom-select__panel');
    var list = wrap.querySelector('.panel-custom-select__list');
    if (!panel || !list || panel.querySelector('.panel-custom-select__search')) return;

    var searchWrap = document.createElement('div');
    searchWrap.className = 'panel-custom-select__search-wrap';
    var input = document.createElement('input');
    input.type = 'search';
    input.className = 'panel-custom-select__search';
    input.placeholder = wrap.dataset.searchPlaceholder || 'Поиск…';
    input.autocomplete = 'off';
    searchWrap.appendChild(input);
    panel.insertBefore(searchWrap, list);

    var empty = document.createElement('div');
    empty.className = 'panel-custom-select__empty';
    empty.textContent = 'Ничего не найдено';
    empty.hidden = true;
    panel.appendChild(empty);

    input.addEventListener('click', function (e) { e.stopPropagation(); });
    input.addEventListener('keydown', function (e) { e.stopPropagation(); });
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      wrap.querySelectorAll('.panel-custom-select__item').forEach(function (item) {
        var label = (item.dataset.label || item.textContent || '').toLowerCase();
        var match = !q || label.indexOf(q) !== -1;
        item.hidden = !match;
        if (match) shown++;
      });
      empty.hidden = shown > 0;
    });

    wrap.addEventListener('panel-select-open', function () {
      input.value = '';
      input.dispatchEvent(new Event('input'));
      input.focus();
    });
  }

  function customSelectSyncHiddenInputs(wrap) {
    var isMulti = wrap.classList.contains('panel-custom-select--multi');
    var fieldName = wrap.dataset.name;
    var items = customSelectSelectedItems(wrap);
    wrap.querySelectorAll('input[type="hidden"]').forEach(function (i) { i.remove(); });
    if (isMulti) {
      items.forEach(function (item) {
        var inp = document.createElement('input');
        inp.type = 'hidden'; inp.name = fieldName; inp.value = item.dataset.value;
        wrap.appendChild(inp);
      });
    } else {
      var inp = document.createElement('input');
      inp.type = 'hidden'; inp.name = fieldName;
      inp.value = items.length ? items[0].dataset.value : '';
      wrap.appendChild(inp);
    }
  }

  function bindCustomSelectItems(wrap) {
    var isMulti = wrap.classList.contains('panel-custom-select--multi');
    var autoSubmit = wrap.hasAttribute('data-auto-submit');
    var dp = wrap.querySelector('.panel-custom-select__panel');
    customSelectUpdateTrigger(wrap);
    Array.from(wrap.querySelectorAll('.panel-custom-select__item')).forEach(function (item) {
      if (item.dataset.csBound) return;
      item.dataset.csBound = '1';
      item.addEventListener('click', function (e) {
        e.stopPropagation();
        if (wrap.classList.contains('is-disabled')) return;
        var prevHiddenInput = wrap.dataset.name === 'status' ? wrap.querySelector('input[type="hidden"]') : null;
        var previousStatusValue = prevHiddenInput ? prevHiddenInput.value : null;
        if (isMulti) {
          item.classList.toggle('is-selected');
        } else {
          Array.from(wrap.querySelectorAll('.panel-custom-select__item')).forEach(function (i) { i.classList.remove('is-selected'); });
          item.classList.add('is-selected');
          dp.hidden = true;
        }
        customSelectUpdateTrigger(wrap);
        customSelectSyncHiddenInputs(wrap);
        if (wrap.dataset.onChange === 'copyableText') {
          var form = wrap.closest('form');
          var textarea = form && form.querySelector('textarea[name="message"]');
          if (textarea && item.dataset.fullText !== undefined) { textarea.value = item.dataset.fullText; }
        }
        // Автоподстановка шаблона сообщения — только при реальной смене статуса,
        // иначе повторный клик по уже выбранному статусу затирал бы вручную
        // отредактированный текст сообщения.
        var statusForm = wrap.dataset.name === 'status' ? wrap.closest('form[data-receipt-form]') : null;
        if (statusForm && item.dataset.value !== previousStatusValue) {
          var statusTextarea = statusForm.querySelector('textarea[name="message"]');
          var autoTemplate = null;
          if (item.dataset.value === 'confirmed') { autoTemplate = statusForm.dataset.acceptedMessage; }
          else if (item.dataset.value === 'winner') { autoTemplate = statusForm.dataset.winnerMessage; }
          if (statusTextarea && autoTemplate) { statusTextarea.value = autoTemplate; }
        }
        wrap.dispatchEvent(new CustomEvent('panel-select-change', {
          bubbles: true,
          detail: { name: wrap.dataset.name, value: customSelectValue(wrap) },
        }));
        if (autoSubmit) {
          var f = wrap.closest('form');
          if (f) f.submit();
        }
      });
    });
  }

  function bindCustomSelect(wrap) {
    var trigger = wrap.querySelector('.panel-custom-select__trigger');
    var dp = wrap.querySelector('.panel-custom-select__panel');
    if (!trigger || !dp) return;
    bindCustomSelectItems(wrap);
    initCustomSelectSearch(wrap);
    if (!trigger.dataset.csBound) {
      trigger.dataset.csBound = '1';
      trigger.addEventListener('click', function (e) {
        e.stopPropagation();
        if (wrap.classList.contains('is-disabled')) return;
        var isOpen = !dp.hidden;
        document.querySelectorAll('.panel-custom-select__panel').forEach(function (p) { p.hidden = true; });
        document.querySelectorAll('.panel-custom-select').forEach(function (w) { w.classList.remove('is-open'); });
        if (!isOpen) {
          dp.hidden = false;
          wrap.classList.add('is-open');
          wrap.dispatchEvent(new CustomEvent('panel-select-open'));
        }
      });
    }
  }

  function initCustomSelects(root) {
    (root || document).querySelectorAll('.panel-custom-select').forEach(bindCustomSelect);
    if (!initCustomSelects._docBound) {
      initCustomSelects._docBound = true;
      document.addEventListener('click', function () {
        document.querySelectorAll('.panel-custom-select__panel').forEach(function (p) { p.hidden = true; });
        document.querySelectorAll('.panel-custom-select').forEach(function (w) { w.classList.remove('is-open'); });
      });
    }
  }

  function refreshCustomSelect(wrap) {
    if (!wrap) return;
    bindCustomSelectItems(wrap);
  }

  // ─── Инлайн-правка ячеек таблицы победителей («Доставлено», «Файл приза») ───

  function panelCsrfToken() {
    var input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input && input.value) return input.value;
    var match = document.cookie.match('(^|;\\s*)csrftoken=([^;]*)');
    return match ? decodeURIComponent(match[2]) : '';
  }

  var DELIVERY_COLORS = { 'да': 'green', 'нет': 'red', 'оформлен': 'blue' };
  function deliveryColorClass(value) {
    return DELIVERY_COLORS[(value || '').trim().toLowerCase()] || 'gray';
  }

  function postFieldUpdate(cell, field, value, onSuccess, onError) {
    cell.classList.add('panel-editable-cell__saving');
    fetch(cell.dataset.updateUrl, {
      method: 'POST',
      headers: {
        'X-CSRFToken': panelCsrfToken(),
        'X-Requested-With': 'XMLHttpRequest',
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: new URLSearchParams({ field: field, value: value }).toString(),
    })
      .then(function (r) {
        return r.json()
          .then(function (data) { return { ok: r.ok, data: data }; })
          .catch(function () { return { ok: false, data: {} }; });
      })
      .then(function (res) {
        cell.classList.remove('panel-editable-cell__saving');
        if (res.data && res.data.ok) {
          window.panelToast(res.data.message || 'Сохранено', 'success');
          onSuccess(res.data);
        } else {
          var message = (res.data && res.data.error) || 'Ошибка сохранения';
          window.panelToast(message, 'error');
          onError(message);
        }
      })
      .catch(function () {
        cell.classList.remove('panel-editable-cell__saving');
        window.panelToast('Ошибка сети, попробуйте ещё раз', 'error');
        onError('Ошибка сети, попробуйте ещё раз');
      });
  }

  function cellErrorHelpers(cell) {
    var errorEl = null;
    return {
      clear: function () {
        cell.classList.remove('panel-editable-cell--error');
        if (errorEl) { errorEl.remove(); errorEl = null; }
      },
      show: function (message) {
        cell.classList.add('panel-editable-cell--error');
        if (!errorEl) {
          errorEl = document.createElement('div');
          errorEl.className = 'panel-editable-cell__error';
          cell.appendChild(errorEl);
        }
        errorEl.textContent = message;
      },
    };
  }

  // Выпадашка внутри .panel-table-wrap (overflow: auto) обрезалась бы контейнером,
  // поэтому на время открытия она позиционируется fixed по координатам триггера —
  // и при нехватке места снизу раскрывается вверх.
  function positionTableSelectPanel(select) {
    var trigger = select.querySelector('.panel-table-select__trigger');
    var panel = select.querySelector('.panel-table-select__panel');
    if (!trigger || !panel || panel.hidden) return;

    var rect = trigger.getBoundingClientRect();
    panel.style.position = 'fixed';
    panel.style.left = '0px';
    panel.style.top = '0px';
    panel.style.right = 'auto';

    var width = panel.offsetWidth;
    var height = panel.offsetHeight;
    var left = Math.min(rect.left, window.innerWidth - width - 8);
    var top = rect.bottom + 4;
    if (top + height > window.innerHeight - 8) {
      top = Math.max(8, rect.top - height - 4);
    }
    panel.style.left = Math.max(8, left) + 'px';
    panel.style.top = top + 'px';
  }

  function openTableSelect(select) {
    var trigger = select.querySelector('.panel-table-select__trigger');
    var panel = select.querySelector('.panel-table-select__panel');
    select.classList.add('is-open');
    if (trigger) trigger.setAttribute('aria-expanded', 'true');
    if (panel) panel.hidden = false;
    positionTableSelectPanel(select);
  }

  function closeTableSelect(select) {
    select.classList.remove('is-open');
    var trigger = select.querySelector('.panel-table-select__trigger');
    if (trigger) trigger.setAttribute('aria-expanded', 'false');
    var panel = select.querySelector('.panel-table-select__panel');
    if (panel) {
      panel.hidden = true;
      panel.style.position = '';
      panel.style.left = '';
      panel.style.top = '';
      panel.style.right = '';
    }
  }

  function closeOtherTableSelects(except) {
    document.querySelectorAll('.panel-table-select.is-open').forEach(function (select) {
      if (select !== except) closeTableSelect(select);
    });
  }

  function initDeliverySelects(root) {
    (root || document)
      .querySelectorAll('.panel-editable-cell[data-editable-field="delivery_status"] .panel-table-select')
      .forEach(function (select) {
        if (select.dataset.bound) return;
        select.dataset.bound = '1';

        var cell = select.closest('.panel-editable-cell');
        var trigger = select.querySelector('.panel-table-select__trigger');
        var valueEl = select.querySelector('.panel-table-select__value');
        var panel = select.querySelector('.panel-table-select__panel');
        var customInput = select.querySelector('.panel-table-select__input');
        var err = cellErrorHelpers(cell);

        function renderValue(value) {
          valueEl.dataset.raw = value;
          valueEl.textContent = value || 'Не выбрано';
          valueEl.title = value || '';
          valueEl.className = 'panel-table-select__value'
            + (value ? ' panel-table-select__value--' + deliveryColorClass(value)
                     : ' panel-table-select__value--empty');
        }

        /** «Приз отправлен» зависит от «Доставлено», поэтому этап в строке
            обновляем сразу, не дожидаясь перезагрузки страницы. */
        function renderStage(data) {
          if (!data || !data.stage_label) return;
          var row = cell.closest('tr');
          var stageCell = row && row.querySelector('[data-stage-cell]');
          if (!stageCell) return;
          stageCell.innerHTML = '';
          var badge = document.createElement('span');
          badge.className = 'panel-badge panel-badge--' + (data.stage_color || 'gray');
          badge.textContent = data.stage_label;
          stageCell.appendChild(badge);
        }

        function save(value) {
          value = (value || '').trim();
          if (value === (valueEl.dataset.raw || '')) { closeTableSelect(select); return; }
          postFieldUpdate(cell, 'delivery_status', value, function (data) {
            renderValue(data.value || '');
            renderStage(data);
            customInput.value = data.value || '';
            err.clear();
            closeTableSelect(select);
            var row = cell.closest('tr');
            var fileCell = row && row.querySelector('.panel-editable-cell[data-editable-field="prize_file"]');
            if (fileCell) fileCell.dataset.deliveryStatus = data.value || '';
          }, function (message) { err.show(message); });
        }

        trigger.addEventListener('click', function (e) {
          e.stopPropagation();
          var isOpen = select.classList.contains('is-open');
          closeOtherTableSelects(select);
          if (isOpen) { closeTableSelect(select); return; }
          openTableSelect(select);
          customInput.focus();
        });

        panel.addEventListener('click', function (e) { e.stopPropagation(); });

        select.querySelectorAll('.panel-table-select__item').forEach(function (item) {
          item.addEventListener('click', function () { save(item.dataset.value); });
        });

        customInput.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') { e.preventDefault(); save(customInput.value); }
          else if (e.key === 'Escape') { e.preventDefault(); closeTableSelect(select); }
        });
      });
  }

  function initPrizeFileSelects(root) {
    (root || document)
      .querySelectorAll('.panel-editable-cell[data-editable-field="prize_file"]')
      .forEach(function (cell) {
        var select = cell.querySelector('.panel-table-select');
        if (!select || select.dataset.bound) return;
        select.dataset.bound = '1';

        var trigger = select.querySelector('.panel-table-select__trigger');
        var valueEl = select.querySelector('.panel-table-select__value');
        var panel = select.querySelector('.panel-table-select__panel');
        var searchInput = select.querySelector('.panel-table-select__input');
        var list = select.querySelector('.panel-table-select__list--files');
        var clearBtn = select.querySelector('.panel-file-select__clear');
        var downloadLink = cell.querySelector('.panel-file-cell__download');
        var err = cellErrorHelpers(cell);
        var debounceTimer = null;
        var requestSeq = 0;

        function renderValue(data) {
          var value = data.value || '';
          valueEl.dataset.raw = value;
          valueEl.dataset.fileId = data.file_id || '';
          valueEl.textContent = value || 'Не выдан';
          /* Значение обрезается многоточием — полное имя показываем в подсказке. */
          valueEl.title = value || '';
          valueEl.className = 'panel-table-select__value'
            + (value ? ' panel-table-select__value--blue' : ' panel-table-select__value--empty');
          if (downloadLink) {
            if (data.download_url) {
              downloadLink.href = data.download_url;
              downloadLink.hidden = false;
            } else {
              downloadLink.href = '#';
              downloadLink.hidden = true;
            }
          }
          clearBtn.hidden = !value;
        }

        function renderMessage(text) {
          list.innerHTML = '';
          var li = document.createElement('li');
          li.className = 'panel-file-select__hint';
          li.textContent = text;
          list.appendChild(li);
          positionTableSelectPanel(select);
        }

        function renderOptions(data) {
          var options = data.options || [];
          if (!options.length) {
            renderMessage(
              searchInput.value.trim()
                ? 'Ничего не найдено среди свободных файлов.'
                : 'Свободных файлов нет — загрузите их на странице «Файлы призов».'
            );
            return;
          }
          list.innerHTML = '';
          options.forEach(function (option) {
            var li = document.createElement('li');
            li.className = 'panel-file-option';
            li.dataset.value = option.id;

            var name = document.createElement('div');
            name.className = 'panel-file-option__name';
            name.textContent = option.name;

            var meta = document.createElement('div');
            meta.className = 'panel-file-option__meta';
            var tag = document.createElement('span');
            tag.className = 'panel-file-option__tag panel-file-option__tag--'
              + (option.matches ? 'match' : 'other');
            tag.textContent = option.match_label || (option.matches ? 'этот приз' : 'другой приз');
            tag.title = option.matches
              ? 'Файл загружен под этот же приз и этот же тип розыгрыша'
              : 'Файл загружен из другого склада — выдать можно, но проверьте';
            meta.appendChild(tag);
            var descr = document.createElement('span');
            descr.textContent = option.prize + ' · ' + option.draw_type_label;
            meta.appendChild(descr);

            li.appendChild(name);
            li.appendChild(meta);
            li.addEventListener('click', function () {
              if (option.matches) { save(option.id); return; }
              var reason = option.match_label === 'другой розыгрыш'
                ? 'загружен под этот же приз, но под другой тип розыгрыша (' + option.draw_type_label + ')'
                : 'загружен под другой приз («' + option.prize + '»)';
              showConfirm('Файл «' + option.name + '» ' + reason + '. Выдать его всё равно?')
                .then(function (confirmed) { if (confirmed) save(option.id); });
            });
            list.appendChild(li);
          });
          positionTableSelectPanel(select);
        }

        function loadOptions() {
          var seq = ++requestSeq;
          renderMessage('Загрузка…');
          var url = cell.dataset.optionsUrl + '?q=' + encodeURIComponent(searchInput.value.trim());
          fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(function (r) { return r.json(); })
            .then(function (data) {
              if (seq !== requestSeq) return;
              if (!data.ok) { renderMessage(data.error || 'Не удалось загрузить список.'); return; }
              renderOptions(data);
            })
            .catch(function () {
              if (seq === requestSeq) renderMessage('Не удалось загрузить список.');
            });
        }

        function commit(value) {
          postFieldUpdate(cell, 'prize_file', value === null ? '' : String(value), function (data) {
            renderValue(data);
            err.clear();
            closeTableSelect(select);
          }, function (message) {
            err.show(message);
            loadOptions();
          });
        }

        function save(value) {
          var delivered = (cell.dataset.deliveryStatus || '').trim().toLowerCase() === 'да';
          if (!delivered) { commit(value); return; }
          var text = value
            ? 'Этому победителю уже отмечена доставка приза («Доставлено: Да»). Сменить выданный файл всё равно?'
            : 'Этому победителю уже отмечена доставка приза («Доставлено: Да»). Снять файл приза всё равно?';
          showConfirm(text, true).then(function (confirmed) { if (confirmed) commit(value); });
        }

        trigger.addEventListener('click', function (e) {
          e.stopPropagation();
          var isOpen = select.classList.contains('is-open');
          closeOtherTableSelects(select);
          if (isOpen) { closeTableSelect(select); return; }
          clearBtn.hidden = !(valueEl.dataset.raw || '');
          searchInput.value = '';
          openTableSelect(select);
          searchInput.focus();
          loadOptions();
        });

        panel.addEventListener('click', function (e) { e.stopPropagation(); });

        searchInput.addEventListener('input', function () {
          clearTimeout(debounceTimer);
          debounceTimer = setTimeout(loadOptions, 250);
        });

        searchInput.addEventListener('keydown', function (e) {
          if (e.key === 'Escape') { e.preventDefault(); closeTableSelect(select); }
        });

        clearBtn.addEventListener('click', function () { save(''); });
      });
  }

  function initEditableCells(root) {
    initDeliverySelects(root);
    initPrizeFileSelects(root);
    if (!initEditableCells._docBound) {
      initEditableCells._docBound = true;
      document.addEventListener('click', function () { closeOtherTableSelects(null); });
      function repositionOpenSelects() {
        document.querySelectorAll('.panel-table-select.is-open').forEach(positionTableSelectPanel);
      }
      window.addEventListener('scroll', repositionOpenSelects, true);
      window.addEventListener('resize', repositionOpenSelects);
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeOtherTableSelects(null);
      });
    }
  }

  // ─── Страница «Файлы призов»: выбор файлов, drag&drop, проверка дублей ───

  function initPrizeFileUploadForm() {
    var form = document.getElementById('panel-prize-file-form');
    if (!form) return;

    var input = form.querySelector('input[type="file"]');
    var dropzone = form.querySelector('.panel-dropzone');
    var textEl = dropzone.querySelector('.panel-dropzone__text');
    var errorEl = document.getElementById('panel-prize-file-error');
    var prizeSelect = form.querySelector('#panel-prize-file-prize');
    var drawTypeSelect = form.querySelector('#panel-prize-file-draw-type');
    var submitBtn = form.querySelector('button[type="submit"]');
    var maxBatchBytes = (parseFloat(form.dataset.maxBatchMb) || 25) * 1024 * 1024;

    function readJson(id) {
      var el = document.getElementById(id);
      if (!el) return {};
      try { return JSON.parse(el.textContent); } catch (e) { return {}; }
    }

    var kindDrawTypes = readJson('panel-kind-draw-types');
    var drawTypeLabels = readJson('panel-draw-type-labels');

    function showError(message) {
      errorEl.textContent = message || '';
      errorEl.hidden = !message;
    }

    function baseName(path) {
      return String(path || '').split(/[\/]/).pop().trim();
    }

    function describeSelection() {
      var files = Array.from(input.files || []);
      if (!files.length) {
        textEl.textContent = 'Перетащите файлы сюда или нажмите, чтобы выбрать';
        dropzone.classList.remove('panel-dropzone--filled');
        showError('');
        return;
      }
      dropzone.classList.add('panel-dropzone--filled');
      textEl.textContent = files.length === 1
        ? baseName(files[0].name)
        : 'Выбрано файлов: ' + files.length;

      var seen = {};
      var duplicates = [];
      files.forEach(function (file) {
        var key = baseName(file.name).toLowerCase();
        if (seen[key]) duplicates.push(baseName(file.name));
        seen[key] = true;
      });
      if (duplicates.length) {
        showError('В выборе есть файлы с одинаковыми именами: ' + duplicates.join(', ')
          + '. Имена файлов должны быть уникальными.');
        return;
      }

      var totalSize = files.reduce(function (sum, file) { return sum + (file.size || 0); }, 0);
      if (totalSize > maxBatchBytes) {
        showError('Максимальный совокупный размер загружаемых за раз файлов — '
          + (maxBatchBytes / (1024 * 1024)).toFixed(0) + ' МБ, а выбрано '
          + (totalSize / (1024 * 1024)).toFixed(1) + ' МБ. Загрузите файлы меньшими партиями.');
        return;
      }

      showError('');
    }

    dropzone.addEventListener('click', function () { input.click(); });
    input.addEventListener('change', describeSelection);

    ['dragenter', 'dragover'].forEach(function (name) {
      dropzone.addEventListener(name, function (e) {
        e.preventDefault();
        dropzone.classList.add('is-dragover');
      });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      dropzone.addEventListener(name, function (e) {
        e.preventDefault();
        dropzone.classList.remove('is-dragover');
      });
    });
    dropzone.addEventListener('drop', function (e) {
      if (!e.dataTransfer || !e.dataTransfer.files.length) return;
      input.files = e.dataTransfer.files;
      describeSelection();
    });

    function selectedValue(wrap) {
      var item = wrap && wrap.querySelector('.panel-custom-select__item.is-selected');
      return item ? (item.dataset.value || '') : '';
    }

    /**
     * Типы розыгрыша зависят от приза: показываем только те, в которых он
     * действительно разыгрывается (сервер проверяет то же самое). Если тип
     * один — он сразу и выбран, менеджеру нечего трогать.
     */
    function renderDrawTypes(kindId) {
      if (!drawTypeSelect) return;
      var list = drawTypeSelect.querySelector('.panel-custom-select__list');
      var types = kindDrawTypes[String(kindId || '')] || [];
      list.innerHTML = '';
      types.forEach(function (type, index) {
        var label = drawTypeLabels[type] || type;
        var li = document.createElement('li');
        li.className = 'panel-custom-select__item' + (index === 0 ? ' is-selected' : '');
        li.dataset.value = type;
        li.dataset.label = label;
        var check = document.createElement('span');
        check.className = 'panel-custom-select__check';
        li.appendChild(check);
        li.appendChild(document.createTextNode(label));
        list.appendChild(li);
      });
      drawTypeSelect.dataset.allLabel = types.length
        ? '— выберите тип розыгрыша —'
        : '— сначала выберите приз —';
      bindCustomSelectItems(drawTypeSelect);
      customSelectSyncHiddenInputs(drawTypeSelect);
    }

    if (prizeSelect) {
      prizeSelect.addEventListener('panel-select-change', function (e) {
        renderDrawTypes(e.detail && e.detail.value);
      });
      renderDrawTypes(selectedValue(prizeSelect));
    }

    form.addEventListener('submit', function (e) {
      if (!selectedValue(prizeSelect)) {
        e.preventDefault();
        showError('Выберите приз.');
        window.panelToast('Выберите приз', 'warning');
        return;
      }
      if (drawTypeSelect && !selectedValue(drawTypeSelect)) {
        e.preventDefault();
        showError('Выберите тип розыгрыша.');
        window.panelToast('Выберите тип розыгрыша', 'warning');
        return;
      }
      if (!input.files || !input.files.length) {
        e.preventDefault();
        showError('Выберите хотя бы один файл.');
        window.panelToast('Выберите хотя бы один файл', 'warning');
        return;
      }
      if (!errorEl.hidden) {
        e.preventDefault();
        window.panelToast(errorEl.textContent, 'warning');
        return;
      }
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Загрузка…';
      }
    });

    describeSelection();
  }

  /**
   * Формы с data-panel-confirm спрашивают подтверждение диалогом дашборда
   * (showConfirm), а не системным confirm() — он выглядит чужеродно и на
   * части браузеров подавляется.
   */
  function initConfirmForms(root) {
    (root || document).querySelectorAll('form[data-panel-confirm]').forEach(function (form) {
      if (form.dataset.confirmBound) return;
      form.dataset.confirmBound = '1';
      form.addEventListener('submit', function (e) {
        if (form.dataset.confirmed === '1') { form.dataset.confirmed = ''; return; }
        e.preventDefault();
        showConfirm(form.dataset.panelConfirm, true).then(function (confirmed) {
          if (!confirmed) return;
          form.dataset.confirmed = '1';
          form.submit();
        });
      });
    });
  }

  function initGlobalClicks() {
    document.addEventListener('click', function (e) {
      if (e.target.closest('#panel-modal-close') || e.target.id === 'panel-modal-backdrop') {
        closeModal();
        return;
      }
      if (e.target.id === 'panel-lightbox' || e.target.closest('#panel-lightbox-close')) {
        closeLightbox();
        return;
      }
      var photoBtn = e.target.closest('.panel-drawer__photo-btn');
      if (photoBtn) { openLightbox(photoBtn.dataset.img); return; }

      var copyBtn = e.target.closest('.panel-copy-btn');
      if (copyBtn) {
        navigator.clipboard.writeText(copyBtn.dataset.copy).then(function () {
          var orig = copyBtn.innerHTML;
          copyBtn.innerHTML = '✓';
          setTimeout(function () { copyBtn.innerHTML = orig; }, 1200);
          window.panelToast('Скопировано: ' + copyBtn.dataset.copy, 'success');
        }, function () {
          window.panelToast('Не удалось скопировать', 'error');
        });
        return;
      }

      var navRow = e.target.closest('.panel-table-row--navigate');
      if (navRow && !e.target.closest('[data-stop]')) { window.location = navRow.dataset.url; return; }

      var row = e.target.closest('.panel-table-row--clickable');
      if (row && !e.target.closest('[data-stop]')) { openModal(row.dataset.url); return; }

      var openBtn = e.target.closest('.panel-open-modal-btn');
      if (openBtn) { openModal(openBtn.dataset.url); }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var confirmBackdrop = document.getElementById('panel-confirm');
      if (confirmBackdrop && confirmBackdrop.classList.contains('is-open')) {
        var noBtn = document.getElementById('panel-confirm-no');
        if (noBtn) noBtn.click();
        return;
      }
      closeModal();
      closeLightbox();
    });
  }

  function init() {
    window.panelFlushServerMessages();
    initGlobalClicks();
    initCustomSelects();
    initConfirmForms();
    initEditableCells();
    initPrizeFileUploadForm();

    var root = document.getElementById('panel-modal-root');
    var initialUrl = root && root.dataset.initialUrl;
    if (initialUrl) { openModal(initialUrl); }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
