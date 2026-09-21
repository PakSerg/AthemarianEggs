import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import uuid
import json


class FNSApiClient:
    def __init__(self, base_url, master_token=None, openapi_token=None, openapi_user_token=None, use_proxy=True, proxy_url='http://185.154.194.106:3128'):
        self.base_url = base_url.rstrip('/')
        self.master_token = master_token
        self.openapi_token = openapi_token
        self.openapi_user_token = openapi_user_token
        self.session_token = None
        self.token_expire_time = None
        self.proxy_url = proxy_url

        self.session = requests.Session()
        if use_proxy:
            self.session.proxies = {
                'http': proxy_url,
                'https': proxy_url
            }

    def _get_auth_headers(self):
        headers = {
            'Accept-Encoding': 'gzip,deflate',
            'Content-Type': 'text/xml;charset=UTF-8',
            'User-Agent': 'FNS-Client/1.0'
        }

        if self.session_token:
            headers['FNS-OpenApi-Token'] = self.openapi_token
            headers['FNS-OpenApi-UserToken'] = self.openapi_user_token

        return headers

    def _create_soap_envelope(self, body_xml):
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
    <soapenv:Header/>
    <soapenv:Body>
        {body_xml}
    </soapenv:Body>
</soapenv:Envelope>'''

    def _make_request(self, url, headers, data):
        response = self.session.post(
            url,
            headers=headers,
            data=data,
            verify=False,
            timeout=60
        )
        return response

    def authorize(self):

        auth_xml = f'''<ns:GetMessageRequest xmlns:ns="urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService/types/1.0">
    <ns:Message>
        <tns:AuthRequest xmlns:tns="urn://x-artefacts-gnivc-ru/ais3/kkt/AuthService/types/1.0">
            <tns:AuthAppInfo>
                <tns:MasterToken>{self.master_token}</tns:MasterToken>
            </tns:AuthAppInfo>
        </tns:AuthRequest>
    </ns:Message>
</ns:GetMessageRequest>'''

        soap_envelope = self._create_soap_envelope(auth_xml)

        headers = self._get_auth_headers()
        headers['SOAPAction'] = 'urn:GetMessageRequest'

        response = self._make_request(
            f'{self.base_url}/open-api/AuthService/0.1',
            headers=headers,
            data=soap_envelope
        )

        if response.status_code == 200:
            root = ET.fromstring(response.content)

            namespace = {
                'soap': 'http://schemas.xmlsoap.org/soap/envelope/',
                'ns': 'urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiMessageConsumerService/types/1.0',
                'ns2': 'urn://x-artefacts-gnivc-ru/ais3/kkt/AuthService/types/1.0'
            }

            token_element = root.find('.//ns2:Token', namespace)
            expire_element = root.find('.//ns2:ExpireTime', namespace)

            if token_element is not None:
                self.session_token = token_element.text
                self.openapi_token = token_element.text
                if expire_element is not None:
                    self.token_expire_time = datetime.fromisoformat(expire_element.text.replace('Z', '+00:00'))
                print(f"Авторизация успешна. Токен действителен до: {self.token_expire_time}")
                return True

        print(f"Ошибка авторизации: {response.status_code}")
        print(response.text)
        return False

    def is_token_valid(self):
        if not self.session_token or not self.token_expire_time:
            return False

        if self.token_expire_time.tzinfo:
            now = datetime.now(self.token_expire_time.tzinfo)
        else:
            now = datetime.now(timezone.utc)

        return now < self.token_expire_time - timedelta(minutes=5)

    def ensure_auth(self):
        if not self.is_token_valid():
            return self.authorize()
        return True

    def parse_qr_data(self, qr_string):
        params = {}

        if qr_string.startswith('http'):
            import urllib.parse
            parsed_url = urllib.parse.urlparse(qr_string)
            qr_string = parsed_url.query

        for item in qr_string.split('&'):
            if '=' in item:
                key, value = item.split('=', 1)

                if key.lower() == 's':
                    params[key.lower()] = int(round(float(value) * 100))
                else:
                    params[key.lower()] = value

        return params

    def get_ticket_request_xml(self, qr_params):

        def normalize_date(t):
            if len(t) == 13:
                dt = datetime.strptime(t, "%Y%m%dT%H%M")
                return dt.strftime("%Y-%m-%dT%H:%M:00")
            elif len(t) == 15:
                dt = datetime.strptime(t, "%Y%m%dT%H%M%S")
                return dt.strftime("%Y-%m-%dT%H:%M:%S")
            else:
                return t

        print(qr_params)

        return f'''<ns0:SendMessageRequest xmlns:ns0="urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0">
    <ns0:Message>
        <tns:GetTicketRequest xmlns:tns="urn://x-artefacts-gnivc-ru/ais3/kkt/KktTicketService/types/1.0">
            <tns:GetTicketInfo>
                <tns:Sum>{qr_params.get('s', 0)}</tns:Sum>
                <tns:Date>{normalize_date(qr_params['t'])}</tns:Date>
                <tns:Fn>{qr_params.get('fn', '')}</tns:Fn>
                <tns:TypeOperation>1</tns:TypeOperation>
                <tns:FiscalDocumentId>{qr_params.get('i', '')}</tns:FiscalDocumentId>
                <tns:FiscalSign>{qr_params.get('fp', '')}</tns:FiscalSign>
                <tns:RawData>true</tns:RawData>
            </tns:GetTicketInfo>
        </tns:GetTicketRequest>
    </ns0:Message>
</ns0:SendMessageRequest>'''

    def get_ticket(self, qr_data):
        if not self.ensure_auth():
            print("Не удалось авторизоваться")
            return None

        if isinstance(qr_data, str):
            qr_params = self.parse_qr_data(qr_data)
        else:
            qr_params = qr_data

        request_xml = self.get_ticket_request_xml(qr_params)
        soap_envelope = self._create_soap_envelope(request_xml)

        headers = self._get_auth_headers()
        headers['SOAPAction'] = 'urn:SendMessageRequest'

        try:
            response = self._make_request(
                f'{self.base_url}/open-api/ais3/KktService/0.1',
                headers=headers,
                data=soap_envelope
            )

            print(f"Статус ответа SendMessageRequest: {response.status_code}")

            if response.status_code == 200:
                root = ET.fromstring(response.content)
                message_id_element = root.find(
                    './/{urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0}MessageId')

                if message_id_element is not None:
                    message_id = message_id_element.text
                    print(f"Получен MessageId: {message_id}")

                    return self.get_message_result(message_id)
                else:
                    print("MessageId не найден в ответе")
            else:
                print(f"Ошибка SendMessageRequest: {response.text}")

        except Exception as e:
            print(f"Ошибка при отправке запроса: {e}")

        return None

    def get_message_result(self, message_id, max_attempts=20, delay_seconds=3):
        import time

        if not self.ensure_auth():
            print("Не удалось авторизоваться")
            return {
                'status': 'error',
                'raw_response': 'Не удалось авторизоваться',
            }

        for attempt in range(max_attempts):
            print(f"Попытка {attempt + 1} получения результата для MessageId: {message_id}")

            status_xml = f'''<ns:GetMessageRequest xmlns:ns="urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0">
    <ns:MessageId>{message_id}</ns:MessageId>
</ns:GetMessageRequest>'''

            soap_envelope = self._create_soap_envelope(status_xml)

            headers = self._get_auth_headers()
            headers['SOAPAction'] = 'urn:GetMessageRequest'

            try:
                response = self._make_request(
                    f'{self.base_url}/open-api/ais3/KktService/0.1',
                    headers=headers,
                    data=soap_envelope
                )

                print(response.content)

                if response.status_code == 200:
                    root = ET.fromstring(response.content)

                    namespace = {
                        'ns': 'urn://x-artefacts-gnivc-ru/inplat/servin/OpenApiAsyncMessageConsumerService/types/1.0'
                    }

                    status_element = root.find('.//ns:ProcessingStatus', namespace)

                    if status_element is not None:
                        status = status_element.text

                        if status == 'COMPLETED':
                            ticket_element = root.find(
                                './/{urn://x-artefacts-gnivc-ru/ais3/kkt/KktTicketService/types/1.0}Ticket')

                            if ticket_element is not None:
                                try:
                                    ticket_data = json.loads(ticket_element.text)
                                    print(f"Чек получен успешно!")
                                    return {
                                        'status': 'success',
                                        'ticket_data': ticket_data,
                                        'raw_response': response.text,
                                        'message_id': message_id
                                    }

                                except json.JSONDecodeError:
                                    print(f"Ошибка парсинга JSON чека")
                                    return {
                                        'status': 'error',
                                        'message': 'Ошибка получения данных чека на стороне ФНС.',
                                        'raw_response': response.text,
                                        'message_id': message_id
                                    }
                            else:
                                print(root)
                                return {
                                    'status': 'error',
                                    'message': 'Ошибка получения чека от ФНС.',
                                    'raw_response': response.text,
                                    'message_id': message_id
                                }

                        elif status == 'PROCESSING':
                            print(f"Чек еще обрабатывается, ждем...")
                            time.sleep(delay_seconds)
                            continue

                        else:
                            print(f"Неизвестный статус: {status}")
                            time.sleep(delay_seconds)
                            continue
                    else:
                        return {
                            'status': 'error',
                            'message': 'Не удалось получить статус чека от ФНС.',
                            'raw_response': response.text,
                            'message_id': message_id
                        }

                else:
                    print(f"Ошибка GetMessageRequest: {response.status_code}")
                    print(response.text)
                    return {
                        'status': 'error',
                        'message': 'Ошибка на стороне ФНС при обработке чека.',
                        'raw_response': response.text,
                        'message_id': message_id
                    }

            except Exception as e:
                print(f"Ошибка при запросе статуса: {e}")
                return {
                    'status': 'error',
                    'message': 'Ошибка на стороне ФНС при обработке чека.',
                    'raw_response': '',
                    'message_id': message_id
                }

        print(f"Не удалось получить чек после {max_attempts} попыток")
        return {
            'status': 'error',
            'message': 'Не удалось получить чек от ФНС',
            'raw_response': '',
            'message_id': message_id
        }
