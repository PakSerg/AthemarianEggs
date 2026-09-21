document.addEventListener('DOMContentLoaded', function () {
    const birthInput = document.getElementById('id_birth_date');
    if (birthInput) {
        // Ручной ввод разрешён с любыми разделителями (. , : / пробел и т.д.) —
        // приводим их к дефису перед разбором, чтобы дата бралась в формате ДД-ММ-ГГГГ.
        function parseFlexibleDate(dateStr) {
            const normalized = dateStr.trim().replace(/[^\d]+/g, '-').replace(/^-+|-+$/g, '');
            const match = normalized.match(/^(\d{1,2})-(\d{1,2})-(\d{4})$/);
            if (!match) return undefined;

            const day = parseInt(match[1], 10);
            const month = parseInt(match[2], 10) - 1;
            const year = parseInt(match[3], 10);
            const date = new Date(year, month, day);
            if (date.getFullYear() !== year || date.getMonth() !== month || date.getDate() !== day) {
                return undefined;
            }
            return date;
        }

        flatpickr(birthInput, {
            locale: "ru",
            dateFormat: "d-m-Y",
            allowInput: true,
            // Оставляем календарь flatpickr и на мобильных устройствах вместо нативного
            // пикера ОС, чтобы поле можно было и печатать вручную, и выбирать через календарь.
            disableMobile: true,
            appendTo: birthInput.closest('.profile-field') || document.body,
            positionElement: birthInput,
            parseDate: function (datestr) {
                return parseFlexibleDate(datestr);
            },
        });
    }
});


document.addEventListener('DOMContentLoaded', function () {
    const navButtons = document.querySelectorAll('.btn-change-forms-profile');
    const formIds = { main: 'form-main', sucuri: 'form-sucuri', notifications: 'form-notifications' };

    function showForm(formType) {
        Object.entries(formIds).forEach(([key, id]) => {
            const el = document.getElementById(id);
            if (el) el.style.display = key === formType ? 'block' : 'none';
        });
        navButtons.forEach(btn => {
            btn.classList.toggle('active', btn.getAttribute('data-menu-form') === formType);
        });
    }

    navButtons.forEach(btn => {
        btn.addEventListener('click', function (e) {
            e.preventDefault();
            showForm(this.getAttribute('data-menu-form'));
        });
    });

    const params = new URLSearchParams(window.location.search);
    const tabFromUrl = params.get('tab');
    const reloadFormType = document.querySelector('[name="reload_form_type"]');
    const initialTab = tabFromUrl || (reloadFormType ? reloadFormType.value : 'main');
    showForm(initialTab);
});

document.addEventListener('DOMContentLoaded', function () {
    $(document).ready(function () {
        $('input[type="tel"]').inputmask({
            mask: "+7 (S99) 999-99-99",
            definitions: {
                'S': {
                    validator: "[0-69]",
                    cardinality: 1
                }
            }
        });
    });
});

document.addEventListener('DOMContentLoaded', function () {
    const middleNameInput = document.getElementById('id_middle_name');
    const noMiddleNameCheckbox = document.getElementById('id_no_middle_name');
    const middleNameStar = document.getElementById('middle-name-star');
    if (!middleNameInput || !noMiddleNameCheckbox || !middleNameStar) return;

    function applyMiddleNameState() {
        const noMiddleName = noMiddleNameCheckbox.checked;
        middleNameInput.required = !noMiddleName;
        middleNameInput.disabled = noMiddleName;
        middleNameInput.classList.toggle('is-disabled-field', noMiddleName);
        middleNameStar.hidden = noMiddleName;
        if (noMiddleName) middleNameInput.value = '';
    }

    // Поле обязательно по умолчанию — становится необязательным только после
    // отметки чекбокса «Отчество отсутствует».
    noMiddleNameCheckbox.checked = false;
    applyMiddleNameState();

    noMiddleNameCheckbox.addEventListener('change', applyMiddleNameState);
});

document.addEventListener('DOMContentLoaded', function () {
    const toggleButtons = document.querySelectorAll('.toggle-password');

    toggleButtons.forEach(button => {
        button.addEventListener('click', function () {
            const targetId = this.getAttribute('data-target');
            const input = document.getElementById(targetId);

            if (input) {
                const type = input.getAttribute('type') === 'password' ? 'text' : 'password';
                input.setAttribute('type', type);

                this.classList.toggle('fa-eye');
                this.classList.toggle('fa-eye-slash');
            }
        });
    });
});

document.addEventListener('DOMContentLoaded', function () {
    const token = window.DADATA_TOKEN;
    const cityInput = document.getElementById('id_city');
    const suggestionsBox = document.getElementById('city-suggestions');
    const profileForm = document.querySelector('.form-profile[data-form="main"]');
    const cityFieldError = document.getElementById('city-field-error');
    if (!token || !cityInput || !suggestionsBox || !profileForm || !cityFieldError) return;

    const CITY_SELECT_ERROR = 'Выберите город из списка подсказок';

    const SELECT_EVENT = window.PointerEvent ? 'pointerdown' : 'mousedown';

    let debounceTimer = null;
    let requestSeq = 0;
    let hasSuggestionsForCurrentValue = false;
    let selectedFromSuggestions = Boolean(cityInput.value.trim());
    let currentItems = [];
    let activeIndex = -1;

    function setCityValidationState() {
        const showError = hasSuggestionsForCurrentValue && !selectedFromSuggestions;
        cityInput.classList.toggle('city-required-invalid', showError);
        if (showError) {
            cityFieldError.textContent = CITY_SELECT_ERROR;
            cityFieldError.hidden = false;
        } else {
            cityFieldError.textContent = '';
            cityFieldError.hidden = true;
        }
    }

    function hideSuggestions() {
        suggestionsBox.style.display = 'none';
        suggestionsBox.innerHTML = '';
        currentItems = [];
        activeIndex = -1;
    }

    function applySuggestion(item) {
        // Отменяем отложенный запрос и обесцениваем запросы в полёте,
        // чтобы их ответы не перерисовали список поверх сделанного выбора.
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = null;
        requestSeq += 1;

        cityInput.value = item;
        selectedFromSuggestions = true;
        hasSuggestionsForCurrentValue = false;
        hideSuggestions();
        setCityValidationState();
    }

    function setActiveIndex(index) {
        activeIndex = index;
        Array.prototype.forEach.call(suggestionsBox.children, function (child, i) {
            child.classList.toggle('is-active', i === index);
        });
    }

    function renderSuggestions(items) {
        suggestionsBox.innerHTML = '';
        currentItems = items;
        activeIndex = -1;
        hasSuggestionsForCurrentValue = items.length > 0;
        if (!items.length) {
            hideSuggestions();
            setCityValidationState();
            return;
        }

        items.forEach((item) => {
            const option = document.createElement('div');
            option.className = 'city-suggestion-item';
            option.textContent = item;
            // Выбор по pointerdown/mousedown, а не по click: click срабатывает уже
            // после blur, и подсказку успевало убрать из DOM — список закрывался,
            // а значение не подставлялось. preventDefault удерживает фокус в поле.
            option.addEventListener(SELECT_EVENT, function (e) {
                e.preventDefault();
                applySuggestion(item);
            });
            suggestionsBox.appendChild(option);
        });

        suggestionsBox.style.display = 'block';
        setCityValidationState();
    }

    function resetSuggestions() {
        hasSuggestionsForCurrentValue = false;
        hideSuggestions();
        setCityValidationState();
    }

    async function fetchCitySuggestions(query) {
        const seq = ++requestSeq;
        try {
            const response = await fetch('https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                    'Authorization': `Token ${token}`,
                },
                body: JSON.stringify({
                    query: query,
                    count: 7,
                    from_bound: { value: 'city' },
                    to_bound: { value: 'city' },
                }),
            });

            // Ответ на устаревший запрос (или пришедший после выбора) игнорируем.
            if (seq !== requestSeq) return;

            if (!response.ok) {
                resetSuggestions();
                return;
            }

            const data = await response.json();
            if (seq !== requestSeq) return;

            const seen = new Set();
            const cities = [];
            (data.suggestions || []).forEach((suggestion) => {
                const city = (suggestion.data && (suggestion.data.city_with_type || suggestion.data.city)) || '';
                if (city && !seen.has(city)) {
                    seen.add(city);
                    cities.push(city);
                }
            });
            renderSuggestions(cities);
        } catch (e) {
            if (seq !== requestSeq) return;
            resetSuggestions();
        }
    }

    cityInput.addEventListener('input', function () {
        const value = cityInput.value.trim();
        selectedFromSuggestions = false;
        if (debounceTimer) clearTimeout(debounceTimer);
        if (value.length < 2) {
            requestSeq += 1;
            resetSuggestions();
            return;
        }
        debounceTimer = setTimeout(function () {
            fetchCitySuggestions(value);
        }, 250);
    });

    cityInput.addEventListener('keydown', function (e) {
        if (!currentItems.length || suggestionsBox.style.display !== 'block') return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            setActiveIndex((activeIndex + 1) % currentItems.length);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActiveIndex((activeIndex - 1 + currentItems.length) % currentItems.length);
        } else if (e.key === 'Enter' && activeIndex >= 0) {
            e.preventDefault();
            applySuggestion(currentItems[activeIndex]);
        } else if (e.key === 'Escape') {
            hideSuggestions();
        }
    });

    // Клик по подсказке больше не снимает фокус (preventDefault выше),
    // поэтому blur означает реальный уход из поля — прячем список сразу.
    cityInput.addEventListener('blur', function () {
        hideSuggestions();
    });

    profileForm.addEventListener('submit', function (e) {
        setCityValidationState();
        if (hasSuggestionsForCurrentValue && !selectedFromSuggestions) {
            e.preventDefault();
            cityInput.focus();
        }
    });

    setCityValidationState();
});

document.addEventListener('DOMContentLoaded', function () {
    const sbpBanksUrl = window.SBP_BANKS_URL;
    const bankInput = document.getElementById('id_bank');
    const bankBikInput = document.getElementById('id_bank_bik');
    const suggestionsBox = document.getElementById('bank-suggestions');
    const profileForm = document.querySelector('.form-profile[data-form="main"]');
    const bankFieldError = document.getElementById('bank-field-error');
    if (!sbpBanksUrl || !bankInput || !bankBikInput || !suggestionsBox || !profileForm || !bankFieldError) return;

    const BANK_SELECT_ERROR = 'Выберите банк из списка подсказок';

    const SELECT_EVENT = window.PointerEvent ? 'pointerdown' : 'mousedown';

    let debounceTimer = null;
    let requestSeq = 0;
    let hasSuggestionsForCurrentValue = false;
    let selectedFromSuggestions = Boolean(bankInput.value.trim() && bankBikInput.value.trim());
    let currentItems = [];
    let activeIndex = -1;

    function setBankValidationState() {
        const showError = !bankInput.value.trim() || !selectedFromSuggestions;
        bankInput.classList.toggle('bank-required-invalid', showError && hasInteracted);
        if (showError && hasInteracted) {
            bankFieldError.textContent = BANK_SELECT_ERROR;
            bankFieldError.hidden = false;
        } else {
            bankFieldError.textContent = '';
            bankFieldError.hidden = true;
        }
    }

    let hasInteracted = false;

    function hideSuggestions() {
        suggestionsBox.style.display = 'none';
        suggestionsBox.innerHTML = '';
        currentItems = [];
        activeIndex = -1;
    }

    function applySuggestion(item) {
        // Отменяем отложенный запрос и обесцениваем запросы в полёте,
        // чтобы их ответы не перерисовали список поверх сделанного выбора.
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = null;
        requestSeq += 1;

        bankInput.value = item.name;
        bankBikInput.value = item.bic || '';
        selectedFromSuggestions = true;
        hasSuggestionsForCurrentValue = false;
        hideSuggestions();
        setBankValidationState();
    }

    function setActiveIndex(index) {
        activeIndex = index;
        Array.prototype.forEach.call(suggestionsBox.children, function (child, i) {
            child.classList.toggle('is-active', i === index);
        });
    }

    function renderSuggestions(items) {
        suggestionsBox.innerHTML = '';
        currentItems = items;
        activeIndex = -1;
        hasSuggestionsForCurrentValue = items.length > 0;
        if (!items.length) {
            hideSuggestions();
            return;
        }

        items.forEach((item) => {
            const option = document.createElement('div');
            option.className = 'bank-suggestion-item';

            const name = document.createElement('div');
            name.textContent = item.name;
            option.appendChild(name);

            // БИК намеренно не показываем в интерфейсе — только название банка,
            // сам БИК по-прежнему сохраняется в скрытое поле для СБП/Cyclops.

            // Выбор по pointerdown/mousedown, а не по click: click срабатывает уже
            // после blur, и подсказку успевало убрать из DOM — список закрывался,
            // а значение не подставлялось. preventDefault удерживает фокус в поле.
            option.addEventListener(SELECT_EVENT, function (e) {
                e.preventDefault();
                applySuggestion(item);
            });
            suggestionsBox.appendChild(option);
        });

        suggestionsBox.style.display = 'block';
    }

    function resetSuggestions() {
        hasSuggestionsForCurrentValue = false;
        hideSuggestions();
    }

    async function fetchBankSuggestions(query) {
        const seq = ++requestSeq;
        try {
            const url = query ? `${sbpBanksUrl}?q=${encodeURIComponent(query)}` : sbpBanksUrl;
            const response = await fetch(url, {
                headers: { 'Accept': 'application/json' },
            });

            // Ответ на устаревший запрос (или пришедший после выбора) игнорируем.
            if (seq !== requestSeq) return;

            if (!response.ok) {
                resetSuggestions();
                return;
            }

            const data = await response.json();
            if (seq !== requestSeq) return;

            const banks = (data.suggestions || [])
                .filter((item) => item && item.name && item.bic)
                .map((item) => ({ name: item.name, bic: item.bic }));
            renderSuggestions(banks);
        } catch (e) {
            if (seq !== requestSeq) return;
            resetSuggestions();
        }
    }

    bankInput.addEventListener('input', function () {
        hasInteracted = true;
        const value = bankInput.value.trim();
        selectedFromSuggestions = false;
        bankBikInput.value = '';
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(function () {
            fetchBankSuggestions(value);
        }, 250);
    });

    bankInput.addEventListener('focus', function () {
        if (!bankInput.value.trim()) {
            fetchBankSuggestions('');
        }
    });

    bankInput.addEventListener('keydown', function (e) {
        if (!currentItems.length || suggestionsBox.style.display !== 'block') return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            setActiveIndex((activeIndex + 1) % currentItems.length);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setActiveIndex((activeIndex - 1 + currentItems.length) % currentItems.length);
        } else if (e.key === 'Enter' && activeIndex >= 0) {
            e.preventDefault();
            applySuggestion(currentItems[activeIndex]);
        } else if (e.key === 'Escape') {
            hideSuggestions();
        }
    });

    // Клик по подсказке больше не снимает фокус (preventDefault выше),
    // поэтому blur означает реальный уход из поля — прячем список сразу.
    bankInput.addEventListener('blur', function () {
        hasInteracted = true;
        hideSuggestions();
        setBankValidationState();
    });

    profileForm.addEventListener('submit', function (e) {
        hasInteracted = true;
        setBankValidationState();
        if (!bankInput.value.trim() || !selectedFromSuggestions) {
            e.preventDefault();
            bankInput.focus();
        }
    });
});
