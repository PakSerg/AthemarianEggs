/**
 * Выпадающее меню «Добавить фильтр» — порт фронтенда фильтров FarPostLogs
 * (src/admin_app/static/admin_app/js/ad_list_filters.js) на разметку дашборда.
 *
 * Логика та же: первое меню — список полей с поиском, второе — значения этого
 * поля с галочками; выбор накапливается и применяется одной перезагрузкой при
 * закрытии меню. Значения одного поля пишутся в URL через запятую.
 *
 * Добавлено к оригиналу: поля-диапазоны (kind='range') — два инпута и кнопка
 * «Применить» в том же меню; в FarPostLogs диапазон живёт отдельным контролом,
 * здесь их удобнее держать в общем списке фильтров.
 */
(function () {
    'use strict';

    function escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function paramValues(url, param) {
        var raw = (url.searchParams.get(param) || '').trim();
        return raw ? raw.split(',').filter(Boolean) : [];
    }

    function initPanelFilterDropdown(cfg) {
        var catalogEl = document.getElementById(cfg.catalogId);
        var btn = document.getElementById(cfg.toggleId);
        var menus = document.getElementById(cfg.menusId);
        var primary = document.getElementById(cfg.primaryId);
        var secondary = document.getElementById(cfg.secondaryId);
        var wrap = btn ? btn.closest('.panel-filter-dropdown-wrap') : null;

        if (!catalogEl || !wrap || !btn || !menus || !primary || !secondary) return;

        var catalog = {};
        try { catalog = JSON.parse(catalogEl.textContent); } catch (e) { return; }
        var order = catalog.order || [];
        var meta = catalog.meta || {};
        var items = catalog.items || {};

        var open = false;
        var pendingParam = null;
        var pendingSelections = [];
        var originalSelections = [];

        function applyPendingSelections() {
            if (pendingParam === null) return;
            var before = originalSelections.slice().sort().join(',');
            var after = pendingSelections.slice().sort().join(',');
            var param = pendingParam;
            pendingParam = null;
            if (before === after) {
                originalSelections = [];
                pendingSelections = [];
                return;
            }
            var u = new URL(window.location.href);
            if (pendingSelections.length === 0) u.searchParams.delete(param);
            else u.searchParams.set(param, pendingSelections.join(','));
            u.searchParams.delete('page');
            window.location.href = u.pathname + u.search;
        }

        function closeMenus() {
            applyPendingSelections();
            open = false;
            menus.setAttribute('hidden', '');
            primary.innerHTML = '';
            secondary.innerHTML = '';
            secondary.setAttribute('hidden', '');
            secondary.classList.remove('open');
            primary.classList.remove('open');
            primary.removeAttribute('hidden');
            btn.setAttribute('aria-expanded', 'false');
        }

        function positionMenus() {
            if (!open) return;
            var r = btn.getBoundingClientRect();
            var width = menus.offsetWidth || 260;
            var left = Math.min(r.left, document.documentElement.clientWidth - width - 12);
            menus.style.left = Math.max(8, left) + 'px';
            menus.style.top = (r.bottom + 6) + 'px';
        }

        function openMenusPanel() {
            open = true;
            menus.removeAttribute('hidden');
            btn.setAttribute('aria-expanded', 'true');
            secondary.setAttribute('hidden', '');
            secondary.classList.remove('open');
            secondary.innerHTML = '';
            primary.removeAttribute('hidden');
            primary.classList.add('open');
            renderPrimary('');
            positionMenus();
        }

        window.addEventListener('scroll', function (e) {
            if (!open) return;
            if (wrap.contains(e.target)) return;
            closeMenus();
        }, true);
        window.addEventListener('resize', positionMenus);

        function activeCountFor(key) {
            var m = meta[key] || {};
            var u = new URL(window.location.href);
            if (m.kind === 'range') {
                var n = 0;
                if ((u.searchParams.get(m.param_from) || '').trim()) n++;
                if ((u.searchParams.get(m.param_to) || '').trim()) n++;
                return n ? 1 : 0;
            }
            return paramValues(u, m.param || key).length;
        }

        function renderPrimary(searchValue) {
            var search = (searchValue || '').trim().toLowerCase();
            var head =
                '<div class="panel-filter-menu__search">' +
                '<input type="search" class="panel-filter-menu__input" id="' + cfg.catSearchId +
                '" placeholder="Название поля" autocomplete="off" aria-label="Поиск поля фильтра">' +
                '</div>' +
                '<div class="panel-filter-menu__section">Поля</div>' +
                '<div class="panel-filter-menu__list" role="none">';

            var body = '';
            for (var i = 0; i < order.length; i++) {
                var key = order[i];
                var m = meta[key];
                if (!m) continue;
                var label = m.label || key;
                if (search && label.toLowerCase().indexOf(search) === -1) continue;
                var count = activeCountFor(key);
                body +=
                    '<button type="button" class="panel-filter-menu__cat" role="menuitem" data-category="' +
                    escapeHtml(key) + '">' +
                    '<span class="panel-filter-menu__cat-label">' + escapeHtml(label) + '</span>' +
                    (count ? '<span class="panel-filter-menu__cat-count">' + count + '</span>' : '') +
                    '<span class="panel-filter-menu__cat-chevron" aria-hidden="true">›</span>' +
                    '</button>';
            }
            if (!body) body = '<div class="panel-filter-menu__empty">Нет подходящих полей</div>';

            primary.innerHTML = head + body + '</div>';

            var input = document.getElementById(cfg.catSearchId);
            if (input) {
                input.value = searchValue || '';
                input.focus();
                input.addEventListener('input', function () {
                    var value = input.value;
                    renderPrimary(value);
                    var again = document.getElementById(cfg.catSearchId);
                    if (again) {
                        again.value = value;
                        again.focus();
                        again.setSelectionRange(value.length, value.length);
                    }
                });
                input.addEventListener('click', function (e) { e.stopPropagation(); });
            }

            primary.querySelectorAll('.panel-filter-menu__cat').forEach(function (el) {
                el.addEventListener('click', function () {
                    showSecondary(el.getAttribute('data-category'));
                });
            });
        }

        function showSecondaryShell(title, bodyHtml) {
            primary.setAttribute('hidden', '');
            primary.classList.remove('open');
            secondary.removeAttribute('hidden');
            secondary.classList.add('open');
            secondary.innerHTML =
                '<div class="panel-filter-menu__head">' +
                '<button type="button" class="panel-filter-menu__back" aria-label="Назад">‹</button>' +
                '<span class="panel-filter-menu__head-title">' + escapeHtml(title) + '</span>' +
                '</div>' + bodyHtml;
            var back = secondary.querySelector('.panel-filter-menu__back');
            if (back) {
                back.addEventListener('click', function (e) {
                    e.stopPropagation();
                    applyPendingSelections();
                    if (!open) return;
                    secondary.setAttribute('hidden', '');
                    secondary.classList.remove('open');
                    secondary.innerHTML = '';
                    primary.removeAttribute('hidden');
                    primary.classList.add('open');
                    renderPrimary('');
                });
            }
        }

        function showSecondaryRange(key, m) {
            var u = new URL(window.location.href);
            var from = u.searchParams.get(m.param_from) || '';
            var to = u.searchParams.get(m.param_to) || '';
            var type = m.input_type === 'number' ? 'number' : 'date';
            showSecondaryShell(m.label || key,
                '<div class="panel-filter-menu__range">' +
                '<label class="panel-filter-menu__range-label">От' +
                '<input type="' + type + '" class="panel-filter-menu__range-input" data-role="from" value="' +
                escapeHtml(from) + '"></label>' +
                '<label class="panel-filter-menu__range-label">До' +
                '<input type="' + type + '" class="panel-filter-menu__range-input" data-role="to" value="' +
                escapeHtml(to) + '"></label>' +
                '<div class="panel-filter-menu__range-actions">' +
                '<button type="button" class="panel-btn panel-btn--small" data-role="apply">Применить</button>' +
                '<button type="button" class="panel-btn panel-btn--small panel-btn--ghost" data-role="clear">Очистить</button>' +
                '</div></div>');

            var fromEl = secondary.querySelector('[data-role="from"]');
            var toEl = secondary.querySelector('[data-role="to"]');

            function commit(clear) {
                var url = new URL(window.location.href);
                var vFrom = clear ? '' : (fromEl.value || '').trim();
                var vTo = clear ? '' : (toEl.value || '').trim();
                if (vFrom) url.searchParams.set(m.param_from, vFrom); else url.searchParams.delete(m.param_from);
                if (vTo) url.searchParams.set(m.param_to, vTo); else url.searchParams.delete(m.param_to);
                url.searchParams.delete('page');
                window.location.href = url.pathname + url.search;
            }

            secondary.querySelector('[data-role="apply"]').addEventListener('click', function (e) {
                e.stopPropagation();
                commit(false);
            });
            secondary.querySelector('[data-role="clear"]').addEventListener('click', function (e) {
                e.stopPropagation();
                commit(true);
            });
        }

        function showSecondary(key) {
            var m = meta[key];
            if (!m) return;
            if (m.kind === 'range') { showSecondaryRange(key, m); return; }

            var param = m.param || key;
            var list = items[key] || [];
            var current = paramValues(new URL(window.location.href), param);
            pendingParam = param;
            pendingSelections = current.slice();
            originalSelections = current.slice();

            showSecondaryShell(m.label || key,
                '<div class="panel-filter-menu__search">' +
                '<input type="search" class="panel-filter-menu__input" id="' + cfg.valSearchId +
                '" placeholder="Поиск значения" autocomplete="off" aria-label="Поиск значения">' +
                '</div>' +
                '<div class="panel-filter-menu__list panel-filter-menu__list--values" role="none"></div>');

            function renderValues(filterText) {
                var ft = (filterText || '').trim().toLowerCase();
                var rows = '';
                for (var i = 0; i < list.length; i++) {
                    var item = list[i];
                    var label = item.label || String(item.value);
                    var raw = String(item.value);
                    if (ft && label.toLowerCase().indexOf(ft) === -1) continue;
                    var selected = pendingSelections.indexOf(raw) !== -1;
                    rows +=
                        '<button type="button" class="panel-filter-menu__value' +
                        (selected ? ' is-selected' : '') +
                        '" role="menuitemcheckbox" aria-checked="' + (selected ? 'true' : 'false') +
                        '" data-value="' + escapeHtml(raw) + '">' +
                        '<span class="panel-filter-menu__value-label">' + escapeHtml(label) + '</span>' +
                        '<span class="panel-filter-menu__check" aria-hidden="true">✓</span>' +
                        '</button>';
                }
                if (!rows) rows = '<div class="panel-filter-menu__empty">Нет подходящих значений</div>';
                var listEl = secondary.querySelector('.panel-filter-menu__list--values');
                if (listEl) listEl.innerHTML = rows;
                secondary.querySelectorAll('.panel-filter-menu__value').forEach(function (el) {
                    el.addEventListener('click', function (e) {
                        e.stopPropagation();
                        var value = el.getAttribute('data-value');
                        var idx = pendingSelections.indexOf(value);
                        if (idx !== -1) pendingSelections.splice(idx, 1);
                        else pendingSelections.push(value);
                        var search = document.getElementById(cfg.valSearchId);
                        renderValues(search ? search.value : '');
                    });
                });
            }

            renderValues('');
            var search = document.getElementById(cfg.valSearchId);
            if (search) {
                search.focus();
                search.addEventListener('input', function () { renderValues(search.value); });
                search.addEventListener('click', function (e) { e.stopPropagation(); });
            }
        }

        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (open) closeMenus(); else openMenusPanel();
        });

        document.addEventListener('click', function (e) {
            if (!open) return;
            if (wrap.contains(e.target)) return;
            closeMenus();
        });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && open) {
                pendingParam = null;
                closeMenus();
            }
        });
    }

    function init() {
        initPanelFilterDropdown({
            catalogId: 'panel-filter-catalog',
            toggleId: 'panel-filter-toggle',
            menusId: 'panel-filter-menus',
            primaryId: 'panel-filter-menu-primary',
            secondaryId: 'panel-filter-menu-secondary',
            catSearchId: 'panel-filter-cat-search',
            valSearchId: 'panel-filter-val-search',
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
