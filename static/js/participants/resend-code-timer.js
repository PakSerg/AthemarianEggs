document.addEventListener('DOMContentLoaded', function () {
    const timerEl = document.getElementById('resend-code-timer');
    const btn = document.getElementById('resend-code-btn');

    if (!timerEl || !btn) {
        return;
    }

    let secondsLeft = parseInt(timerEl.dataset.secondsLeft || '0', 10);

    function formatTime(totalSeconds) {
        const minutes = Math.floor(totalSeconds / 60);
        const seconds = totalSeconds % 60;
        return `${minutes}:${String(seconds).padStart(2, '0')}`;
    }

    function updateTimer() {
        if (secondsLeft > 0) {
            btn.disabled = true;
            btn.classList.add('resend-code-btn--disabled');
            timerEl.textContent = `Повторная отправка через ${formatTime(secondsLeft)}`;
            timerEl.hidden = false;
            secondsLeft -= 1;
            return;
        }

        btn.disabled = false;
        btn.classList.remove('resend-code-btn--disabled');
        timerEl.hidden = true;
        clearInterval(intervalId);
    }

    updateTimer();
    const intervalId = setInterval(updateTimer, 1000);
});
