document.addEventListener('DOMContentLoaded', function() {
    const swiper = new Swiper(".prizesSwiper", {
        slidesPerView: 'auto',
        spaceBetween: 20,
        navigation: {
            nextEl: '.btn-next',
            prevEl: '.btn-prev',
            disabledClass: 'btn-slide-no-active',
        }
    });
});

document.addEventListener('DOMContentLoaded', function() {
    const swiper = new Swiper(".tastesSwiper", {
        slidesPerView: 'auto',
        spaceBetween: 20,
    });
});

document.addEventListener('DOMContentLoaded', function() {
    initFaq();
    initChecksAjax();
    initPrizesSlider();
});

function initPrizesSlider() {
    const sliderElement = document.querySelector('[data-prizes-slider]');
    if (!sliderElement) return;

    const btnPrev = document.querySelector('[data-prizes-slider-prev]');
    const btnNext = document.querySelector('[data-prizes-slider-next]');

    const swiper = new Swiper(sliderElement, {
        slidesPerView: 'auto',
        spaceBetween: 8,
        grabCursor: true,
        watchOverflow: true,
        resizeObserver: true,
        breakpoints: {
            1024: { spaceBetween: 20 }
        }
    });

    btnPrev?.addEventListener('click', () => swiper.slidePrev());
    btnNext?.addEventListener('click', () => swiper.slideNext());
}

function initFaq() {
    document.querySelectorAll('.card-faq').forEach(function(card) {
        const toggle = card.querySelector('.show-answer-faq');
        if (!toggle) return;

        function toggleCard(open) {
            const isOpen = open !== undefined ? open : !card.classList.contains('is-open');
            card.classList.toggle('is-open', isOpen);
            toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
            toggle.setAttribute('aria-label', isOpen ? 'Скрыть ответ' : 'Показать ответ');
        }

        toggle.addEventListener('click', function(e) {
            e.stopPropagation();
            toggleCard();
        });

        card.addEventListener('click', function(e) {
            if (e.target.closest('a, button, input, select, textarea, [role="button"]')) return;
            toggleCard();
        });
    });
}

function initChecksAjax() {
    const tableContainer = document.getElementById('checks-table-container');
    const card = tableContainer && tableContainer.closest('.card-history-checks');
    if (!card) return;

    function loadChecks(url) {
        tableContainer.style.opacity = '0.5';
        history.pushState(null, '', url);

        fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
            .then(function(r) { return r.text(); })
            .then(function(html) {
                const doc = new DOMParser().parseFromString(html, 'text/html');

                const newTable = doc.getElementById('checks-table-container');
                if (newTable) tableContainer.replaceWith(newTable);

                const oldPagination = document.getElementById('checks-pagination');
                const newPagination = doc.getElementById('checks-pagination');
                if (oldPagination) oldPagination.remove();
                if (newPagination) {
                    document.getElementById('checks-table-container').insertAdjacentElement('afterend', newPagination);
                }

                initChecksAjax();
            });
    }

    card.addEventListener('click', function(e) {
        const link = e.target.closest('a[href]');
        if (!link) return;

        const isSortLink = link.closest('.header-table-history-checks');
        const isPaginationLink = link.closest('#checks-pagination');
        if (!isSortLink && !isPaginationLink) return;

        e.preventDefault();
        loadChecks(link.href);
    });
}