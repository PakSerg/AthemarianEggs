let html5QrCode;

document.getElementById('checkModal').addEventListener('shown.bs.modal', function () {
  html5QrCode = new Html5Qrcode("qr-reader");

  const enableManualBtn = () => {
    document.getElementById('load-check-manual').disabled = false;
  };

  // Фоллбэк: если камера не запустилась за 3 сек — всё равно включаем кнопку
  const fallbackTimer = setTimeout(() => {
    enableManualBtn();
    document.getElementById('qr-result').innerHTML = `
      <div class="alert alert-danger">
        Ошибка доступа к камере. Вы можете ввести код вручную.
      </div>
    `;
  }, 3000);

  const qrCodeSuccessCallback = (decodedText, decodedResult) => {
    html5QrCode.stop();
    processCheckCode(decodedText);
  };

  const config = {
    fps: 10,
    qrbox: { width: 250, height: 250 },
    aspectRatio: 1.0
  };

  html5QrCode.start(
    { facingMode: "environment" },
    config,
    qrCodeSuccessCallback
  ).then(() => {
    clearTimeout(fallbackTimer);
    enableManualBtn();
  }).catch(err => {
    clearTimeout(fallbackTimer);
    enableManualBtn();
    document.getElementById('qr-result').innerHTML = `
      <div class="alert alert-danger">
        Ошибка доступа к камере. Вы можете ввести код вручную.
      </div>
    `;
  });
});

document.getElementById('checkModal').addEventListener('hidden.bs.modal', function () {
  if (html5QrCode) {
    html5QrCode.stop().then(() => {
      html5QrCode.clear();
    });
  }
  document.getElementById('load-check-manual').disabled = true;
  document.getElementById('qr-result').innerHTML = '';
});


function processCheckCode(code) {

  fetch('/participants/api/upload-check-qr/', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': document.querySelector("[name=csrfmiddlewaretoken]").value,
    },
    body: JSON.stringify({ code: code })
  })
  .then(response => response.json())
  .then(data => {
        const checkModal = bootstrap.Modal.getInstance(document.getElementById('checkModal'));
        if (checkModal) {
            checkModal.hide();
        } else {
            const modal = new bootstrap.Modal(document.getElementById('checkModal'));
            modal.hide();
        }

        const successModal = new bootstrap.Modal(document.getElementById('successModal'));
        successModal.show();

        setTimeout(() => {
            location.reload();
        }, 3000);
  })
  .catch(error => {
    location.reload();
  });
}


document.getElementById('load-check-image').addEventListener('click', function() {
    const fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = 'image/*';
    fileInput.capture = 'environment';

    fileInput.onchange = async function(e) {
        const file = e.target.files[0];
        if (!file) return;

        const btn = document.getElementById('load-check-image');
        const originalText = btn.innerHTML;
        btn.innerHTML = 'Отправка...';
        btn.disabled = true;

        try {
            const formData = new FormData();
            formData.append('check_photo', file);

            const response = await fetch('/participants/api/upload-check-image/', {
                method: 'POST',
                headers: {
                    'X-CSRFToken': document.querySelector("[name=csrfmiddlewaretoken]").value,
                },
                body: formData,
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const result = await response.json();

            const checkModal = bootstrap.Modal.getInstance(document.getElementById('checkModal'));
            if (checkModal) {
                checkModal.hide();
            } else {
                const modal = new bootstrap.Modal(document.getElementById('checkModal'));
                modal.hide();
            }

            const successModal = new bootstrap.Modal(document.getElementById('successModal'));
            successModal.show();

            setTimeout(() => {
                location.reload();
            }, 3000);

        } catch (error) {
            console.error('Ошибка загрузки:', error);
            location.reload();
        } finally {
            btn.innerHTML = originalText;
            btn.disabled = false;
        }
    };

    fileInput.click();
});


document.addEventListener('DOMContentLoaded', function() {
    document.getElementById('load-check-manual').addEventListener('click', function() {
        var checkModal = bootstrap.Modal.getInstance(document.getElementById('checkModal'));

        if (checkModal) {
            checkModal.hide();

            document.getElementById('load-check-manual').disabled = true;

            if (html5QrCode) {
                if (typeof html5QrCode.isScanning !== 'undefined') {
                    if (html5QrCode.isScanning) {
                        html5QrCode.stop().then(() => {
                            html5QrCode.clear();
                        }).catch(error => {
                            console.log('Ошибка при остановке сканера:', error);
                        });
                    }
                } else {
                    html5QrCode.stop().then(() => {
                        html5QrCode.clear();
                    }).catch(error => {
                        console.log('Сканер не был запущен или уже остановлен');
                    });
                }
            }
        } else {
            checkModal = new bootstrap.Modal(document.getElementById('checkModal'));
            checkModal.hide();

            document.getElementById('load-check-manual').disabled = true;

            if (html5QrCode) {
                html5QrCode.stop().then(() => {
                    html5QrCode.clear();
                }).catch(error => {
                    console.log('Сканер не был запущен');
                });
            }
        }

        var manualModal = new bootstrap.Modal(document.getElementById('manualModal'));
        manualModal.show();
    });
});

document.addEventListener('DOMContentLoaded', function () {
    const watchModal = document.getElementById('watchCheckModal');
    const watchImage = document.getElementById('watch-check-modal-image');
    if (!watchModal || !watchImage) return;

    watchModal.addEventListener('show.bs.modal', function (event) {
        const trigger = event.relatedTarget;
        const imageUrl = trigger ? trigger.getAttribute('data-check-image') : null;
        if (imageUrl) {
            watchImage.src = imageUrl;
        }
    });
});