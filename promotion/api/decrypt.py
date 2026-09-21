import hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import os
import requests


def decrypt_response(data, password):
    try:
        key = hashlib.sha256(password.encode()).digest()

        iv = data[-12:]
        encrypted = data[:-12]
        ciphertext = encrypted[:-16]
        tag = encrypted[-16:]

        cipher = Cipher(algorithms.AES(key), modes.GCM(iv, tag), backend=default_backend())
        decryptor = cipher.decryptor()

        decrypted = decryptor.update(ciphertext) + decryptor.finalize()
        return decrypted
    except Exception:
        return None


def send_check_request(image_path):

    if not os.path.exists(image_path):
        return None

    url = "https://proverkacheka.com/api/v1/check/get"

    headers = {
        "Host": "proverkacheka.com",
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Origin": "https://proverkacheka.com",
        "Referer": "https://proverkacheka.com/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Sec-Ch-Ua": '"Chromium";v="143", "Not A(Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "same-origin",
        "Sec-Fetch-Dest": "empty",
    }

    cookies = {
        "ENGID": "1.1",
        "_ym_uid": "1771078977218916358",
        "_ym_d": "1771078977",
        "_ym_isad": "2",
        "_ym_visorc": "w"
    }

    try:
        with open(image_path, 'rb') as f:
            file_content = f.read()

        files = {
            'qrfile': (
                os.path.basename(image_path),
                file_content,
                'image/jpeg'
            )
        }

        data = {
            'qr': '2',
            'status': '0',
            'token': '0.19'
        }

        response = requests.post(
            url,
            headers=headers,
            cookies=cookies,
            files=files,
            data=data,
            timeout=30
        )

        if response.status_code == 200:
            return response.content
        else:
            return None

    except requests.exceptions.Timeout:
        return None
    except requests.exceptions.RequestException as e:
        return None
    except Exception as e:
        return None


