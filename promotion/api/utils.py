import logging
import os
import cv2
from pyzbar.pyzbar import decode
import zxingcpp
from .api import FNSApiClient
from .config import CONFIG
import qrcode
import numpy as np
from .decrypt import send_check_request, decrypt_response
import json

logger = logging.getLogger(__name__)

PASSWORD_CRYPTO = "38s91f65nm"

QR_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qr_models')

_wechat_qr_detector = None
_wechat_qr_unavailable = False


def _get_wechat_qr_detector():
    """
    Ленивая загрузка WeChatQRCode — детектора и супер-резолюции на базе нейросетей
    (модуль cv2.wechat_qrcode из opencv-contrib), который заметно надёжнее
    pyzbar/zxing-cpp на смазанных/мелких QR-кодах с реальных фото.
    """
    global _wechat_qr_detector, _wechat_qr_unavailable

    if _wechat_qr_detector is not None or _wechat_qr_unavailable:
        return _wechat_qr_detector

    try:
        _wechat_qr_detector = cv2.wechat_qrcode_WeChatQRCode(
            os.path.join(QR_MODELS_DIR, 'detect.prototxt'),
            os.path.join(QR_MODELS_DIR, 'detect.caffemodel'),
            os.path.join(QR_MODELS_DIR, 'sr.prototxt'),
            os.path.join(QR_MODELS_DIR, 'sr.caffemodel'),
        )
    except Exception:
        logger.exception('Не удалось инициализировать WeChatQRCode, движок отключён')
        _wechat_qr_unavailable = True

    return _wechat_qr_detector


def process_receipt_from_image(image_path, api_client, raw_data=False, raw_result=None, tg_data=False):
    if raw_data:
        fns_data = raw_result
    else:
        fns_data = decode_qr_fns(image_path)

    if not fns_data:
        return {
            'status': 'error',
            'message': 'Не удалось распознать QR-код на изображении'
        }

    params = {}

    for item in fns_data.split('&'):
        if '=' in item:
            key, value = item.split('=', 1)

            params[key.lower()] = value

    required_params = ['fn', 'i', 'fp', 't', 's']
    missing_params = [param for param in required_params if param not in params]

    if missing_params:
        return {
            'status': 'error',
            'message': f'QR-код не содержит обязательных параметров чека ФНС.',
            'data_error': params
        }

    if tg_data:
        return {
            'data_error': params
        }

    ticket_info = api_client.get_ticket(fns_data)

    if ticket_info:
        ticket_info['fns_data'] = fns_data
        return ticket_info
    else:
        return {
            'status': 'error',
            'message': 'Не удалось получить данные чека от ФНС'
        }


def decode_qr_fns(image_path):
    image = cv2.imread(image_path)

    if image is None:
        return None

    fns_qr_data = decode_fns_qr_with_wechat(image)
    if fns_qr_data:
        return fns_qr_data

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # Уменьшенные копии исходного (не бинаризованного) фото — пробуем всеми
    # движками, включая WeChatQRCode: крупные фото со смартфонов (обычно
    # >8 Мп) содержат QR лишь на небольшой части кадра, и уменьшение нередко
    # помогает там, где полное разрешение — нет.
    h, w = gray.shape[:2]
    if max(h, w) > 1600:
        for max_dim in (1600, 1200, 900):
            scale = max_dim / max(h, w)
            resized = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            fns_qr_data = decode_fns_qr_with_wechat(resized)
            if fns_qr_data:
                return fns_qr_data

    # Дальше — варианты с ручной бинаризацией/фильтрацией. Их проверяем без
    # WeChatQRCode: на грубо бинаризованном шумном изображении в полном
    # разрешении его сеть-детектор может зависать на несколько минут (см.
    # docstring decode_fns_qr_any_engine), а pyzbar/zxing-cpp дешёвы и
    # безопасны на любом входе.
    processed_images = [('grayscale', gray)]

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    contrast_enhanced = clahe.apply(gray)
    processed_images.append(('contrast', contrast_enhanced))

    _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    processed_images.append(('binary', binary))

    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    processed_images.append(('otsu', otsu))

    adaptive_binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                            cv2.THRESH_BINARY, 11, 2)
    processed_images.append(('adaptive_binary', adaptive_binary))

    denoised = cv2.medianBlur(gray, 3)
    processed_images.append(('denoised', denoised))

    kernel = np.ones((2, 2), np.uint8)
    morph = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    processed_images.append(('morph', morph))

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_white = np.array([0, 0, 200])
    upper_white = np.array([180, 30, 255])
    white_mask = cv2.inRange(hsv, lower_white, upper_white)

    glare_removed = cv2.bitwise_and(gray, gray, mask=~white_mask)
    processed_images.append(('glare_removed', glare_removed))

    for method_name, processed_img in processed_images:
        fns_qr_data = decode_fns_qr_any_engine(processed_img)

        if fns_qr_data:
            return fns_qr_data

    response_send_check_req = send_check_request(image_path)

    if response_send_check_req:
        decrypted = decrypt_response(response_send_check_req, PASSWORD_CRYPTO)

        if decrypted:
            try:
                decrypted_str = decrypted.decode("utf-8")
                decrypted_json = json.loads(decrypted_str)
                qrraw = decrypted_json['request']['qrraw']

                pairs = qrraw.split('&')

                fixed_pairs = []
                for pair in pairs:
                    if pair.startswith('t='):
                        key, value = pair.split('=')
                        fixed_value = value.replace('t', 'T', 1)
                        fixed_pairs.append(f"{key}={fixed_value}")
                    else:
                        fixed_pairs.append(pair)

                fixed_string = '&'.join(fixed_pairs)

                return fixed_string

            except Exception:
                return None

    return None


def check_fns_qr(decoded_objects):
    for obj in decoded_objects:
        if obj.type == 'QRCODE':
            try:
                data = obj.data.decode('utf-8')

                if _is_fns_qr_text(data):
                    return data
            except:
                continue
    return None


def _is_fns_qr_text(data):
    return any(key in data.lower() for key in ['fn=', 'fp=', 't=', 's=', 'i='])


def decode_fns_qr_any_engine(image):
    """
    Пытается распознать QR-код чека ФНС через pyzbar (zbar) и zxing-cpp — они
    по-разному справляются с нечёткими/шумными фото, каждый находит коды,
    которые пропускает другой. Оба безопасны на любом входе (в т.ч. на грубо
    бинаризованных вручную картинках).

    WeChatQRCode сюда намеренно не включён: его сеть-детектор на полноразмерном
    вручную бинаризованном (adaptiveThreshold и т.п.) шумном изображении может
    зависать на 2-3 минуты (проверено на реальном фото) — см. decode_fns_qr_with_wechat.
    """
    decoded_objects = decode(image)
    fns_qr_data = check_fns_qr(decoded_objects)
    if fns_qr_data:
        return fns_qr_data

    try:
        results = zxingcpp.read_barcodes(image, formats=zxingcpp.QRCode)
    except Exception:
        results = []

    for result in results:
        text = result.text
        if text and _is_fns_qr_text(text):
            return text

    return None


WECHAT_QR_MAX_DIM = 2200


def decode_fns_qr_with_wechat(image):
    """
    То же самое, что decode_fns_qr_any_engine, плюс WeChatQRCode. Использовать
    только на "естественных" изображениях (исходное цветное/серое фото или его
    уменьшенная копия) — НЕ на результатах ручной бинаризации/адаптивного
    порога: на шумной бинарной картинке в полном разрешении детектор
    WeChatQRCode может зависать на несколько минут (см. предупреждение выше).
    """
    fns_qr_data = decode_fns_qr_any_engine(image)
    if fns_qr_data:
        return fns_qr_data

    detector = _get_wechat_qr_detector()
    if detector is None:
        return None

    h, w = image.shape[:2]
    if max(h, w) > WECHAT_QR_MAX_DIM:
        scale = WECHAT_QR_MAX_DIM / max(h, w)
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    try:
        texts, _points = detector.detectAndDecode(image)
    except Exception:
        texts = []

    for text in texts:
        if text and _is_fns_qr_text(text):
            return text

    return None


def create_api_client():
    api_client = FNSApiClient(
        base_url=CONFIG['base_url'],
        master_token=CONFIG['master_token'],
        openapi_token=CONFIG['openapi_token'],
        openapi_user_token=CONFIG['openapi_user_token']
    )
    return api_client


def create_qr(receipt_data):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(receipt_data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    return img
