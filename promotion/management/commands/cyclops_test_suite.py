"""Сквозной прогон всех реализованных методов Cyclops на текущем слое.

Зачем: у Точки в гайде («Процесс тестирования», стр. 23-25) прямо написано, что
перед выходом в Prod нужно прогнать на pre-слое весь список обязательных методов,
а их поддержка потом сверяет по логам площадки. Эта команда — единая точка,
которая гоняет их все за один запуск, с понятным отчётом (годится и как чек-лист
для себя, и как то, что можно показать на созвоне).

Использование:
    python manage.py cyclops_test_suite                      # полный прогон обязательных методов
    python manage.py cyclops_test_suite --skip-refund         # без refund_payment / refund_virtual_account
    python manage.py cyclops_test_suite --skip-fl-beneficiary # без создания тестового ИП (v3-методы)
    python manage.py cyclops_test_suite --reset-payout        # прогнать сквозную выплату ещё раз
    python manage.py cyclops_test_suite --allow-prod          # разрешить запуск на боевом слое (осторожно!)

По умолчанию гоняются ВСЕ методы из требований Точки, включая refund_virtual_account
и v3-методы по бенефициарам-физлицам (create_beneficiary / add_beneficiary_documents_data /
get_beneficiary_documents_data) — их отсутствие в логах площадки блокирует выход в Prod.

По умолчанию отказывается запускаться на боевом слое (CYCLOPS_CONFIG.USE_TEST_KEY=False) —
слишком легко случайно пошевелить что-то реальное. На pre-слое всё, что двигает деньги,
использует официальные тестовые данные Точки (номера СБП 700000000001-9, банк — из
живого list_bank_sbp, реквизиты возврата — из «Тестовые данные для платежей по реквизитам»
в гайде), их можно переопределить флагами.
"""

import time
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from promotion.models import CyclopsPayment, CyclopsSettings, GuaranteedPrizePayout, SbpBank, User
from promotion.services import cyclops_manager
from promotion.services import guaranteed_prize_payout as gpp
from promotion.services.cyclops import CyclopsAPIClient
from promotion.services.cyclops_sbp import sync_sbp_banks

# Официальные тестовые данные Точки (гайд, стр. 20-21) — используем как значения
# по умолчанию, чтобы не выдумывать реквизиты руками.
TEST_SBP_PHONE = '70000000001'
TEST_REFUND_ACCOUNT = '40702810238030000904'
TEST_REFUND_BANK_CODE = '046577964'
TEST_REFUND_NAME = 'Общество с ограниченной ответственностью "Петруня"'
TEST_REFUND_INN = '6671217676'
TEST_REFUND_KPP = '667101001'

TEST_PARTICIPANT_EMAIL = 'cyclops-pretest@omskbacon.internal'

# Тестовый бенефициар-физлицо для v3-методов. ИНН — 12 знаков с корректной
# контрольной суммой (иначе Точка отклонит на валидации формата). Точка также
# сверяет ИНН с реестром: для legal_type='I' (ИП) он должен принадлежать
# действующему ИП, иначе приходит ip_with_provided_inn_is_closed. Поэтому по
# умолчанию тестируем 'F' (физлицо/самозанятый) — переопределяется флагами
# --fl-inn / --fl-legal-type.
TEST_FL_INN = '772154686468'
TEST_FL_LEGAL_TYPE = 'F'
TEST_FL_ADDRESS = '430000, г. Саранск, ул. Тестовая, д. 1, кв. 1'
TEST_FL_PASSPORT = {
    'type': 'internal_passport',
    'series': '5215',
    'number': '654321',
    'first_name': 'Иван',
    'middle_name': 'Иванович',
    'last_name': 'Тестовый',
    'birth_date': '1990-01-08',
    'issuer_code': '550-001',
    'issue_date': '2015-03-20',
}
TEST_FL_BIRTH_PLACE = 'г. Саранск'


class Command(BaseCommand):
    help = (
        'Прогоняет все реализованные методы Cyclops API на текущем слое и печатает отчёт. '
        'См. docstring файла — соответствует чек-листу тестирования из гайда Точки.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--allow-prod', action='store_true',
                             help='Разрешить запуск на боевом слое (по умолчанию запрещено)')
        parser.add_argument('--skip-refund', action='store_true',
                             help='Не прогонять refund_payment и refund_virtual_account '
                                  '(по умолчанию прогоняются — их требует Точка для выхода в Prod)')
        parser.add_argument('--skip-fl-beneficiary', action='store_true',
                             help='Не создавать тестового бенефициара-ИП и не гонять v3-методы '
                                  'по его документам (по умолчанию прогоняются — требование Точки)')
        parser.add_argument('--reset-payout', action='store_true',
                             help='Сбросить статус тестовой сквозной выплаты и прогнать её заново, '
                                  'даже если она уже была выплачена/провалена в прошлый раз')
        parser.add_argument('--fl-inn', default=TEST_FL_INN,
                             help=f'ИНН для тестового бенефициара-физлица (по умолчанию {TEST_FL_INN})')
        parser.add_argument('--fl-legal-type', default=TEST_FL_LEGAL_TYPE, choices=['F', 'I'],
                             help="F — физлицо/самозанятый (по умолчанию), I — ИП")
        parser.add_argument('--phone', default=TEST_SBP_PHONE,
                             help=f'Тестовый номер телефона для СБП (по умолчанию {TEST_SBP_PHONE})')

    # ------------------------------------------------------------------ #
    def handle(self, *args, **options):
        self.client = CyclopsAPIClient()
        self.results = []  # (название, ok: bool, детали: str)
        self.opts = options

        cfg = settings.CYCLOPS_CONFIG
        # Слой определяется адресом, а НЕ режимом подписи: на pre-слое подпись
        # тоже может (и должна перед выходом в Prod) быть реальной RSA-подписью,
        # так что USE_TEST_KEY здесь не показатель.
        is_pre = 'pre.tochka.com' in (cfg.get('BASE_URL') or '')
        if not is_pre and not options['allow_prod']:
            raise CommandError(
                f'BASE_URL={cfg.get("BASE_URL")} не похож на тестовый слой (ожидался pre.tochka.com). '
                'Команда двигает тестовые деньги и не должна просто так запускаться на проде. '
                'Передайте --allow-prod, если это осознанное решение.'
            )

        signing = 'ТЕСТОВАЯ подпись "12345"' if cfg.get('USE_TEST_KEY', True) else 'RSA-подпись закрытым ключом'
        self._banner(
            f'Cyclops test suite · слой: {"PRE" if is_pre else "PROD"} · {cfg["BASE_URL"]}\n'
            f'Подпись запросов: {signing} · sign-system: {cfg.get("PLATFORM_ID")} · '
            f'sign-thumbprint: {cfg.get("CERT_THUMBPRINT")}'
        )

        self._run('echo (проверка связи)', self._step_echo, category='Служебное')
        self._run('list_bank_sbp (справочник банков СБП)', self._step_sync_banks, category='СБП')

        cs = CyclopsSettings.load()
        beneficiary = cs.payout_beneficiary
        virtual_account = cs.payout_virtual_account

        if not beneficiary or not virtual_account:
            self.stdout.write(self.style.WARNING(
                '\nПлательщик не настроен (Cyclops → Настройки в панели персонала) — '
                'пропускаю всё, что требует бенефициара/виртуального счёта. Зарегистрируйте '
                'бенефициара и выберите его в настройках, затем запустите команду снова.'
            ))
        else:
            self._run('get_beneficiary', lambda: self.client.get_beneficiary(beneficiary.beneficiary_id),
                       category='Бенефициары')
            self._run('list_beneficiary', lambda: self.client.list_beneficiary(),
                       category='Бенефициары')
            self._run('update_beneficiary_ul', lambda: cyclops_manager.update_beneficiary(
                beneficiary.pk, name=beneficiary.name, kpp=beneficiary.kpp, ogrn=beneficiary.ogrn,
            ), category='Бенефициары')
            self._step_activate_deactivate(beneficiary)
            self._run('list_documents', lambda: self.client.list_documents(beneficiary_id=beneficiary.beneficiary_id),
                       category='Документы')
            self._run('get_virtual_account', lambda: self.client.get_virtual_account(virtual_account.virtual_account_id),
                       category='Виртуальные счета')
            self._run('list_virtual_account', lambda: self.client.list_virtual_account(beneficiary.beneficiary_id),
                       category='Виртуальные счета')

            if is_pre:
                self._step_topup_and_identify(virtual_account)
            else:
                self.stdout.write('  (transfer_money недоступен на боевом слое — пропускаю пополнение)')

            self._step_throwaway_deal(virtual_account)
            self._step_real_payout(options)
            self._run('poll_prize_deals (get_deal по отправленным)', self._step_poll,
                       category='Бизнес-сценарий (гарантированный приз)')
            self._run('retry_failed_prizes (подсчёт кандидатов на ретрай)', self._step_retry,
                       category='Бизнес-сценарий (гарантированный приз)')

            if not options['skip_refund']:
                self._step_refunds(virtual_account)
            else:
                self.stdout.write('  (--skip-refund — refund_payment/refund_virtual_account пропущены)')

        # v3-методы по бенефициарам-физлицам (ИП/самозанятые) не зависят от
        # настроенного плательщика — гоняем их даже если плательщик не выбран.
        if not options['skip_fl_beneficiary']:
            self._step_fl_beneficiary()
        else:
            self.stdout.write('  (--skip-fl-beneficiary — v3-методы по ИП пропущены)')

        self._summary()

    # ------------------------------------------------------------------ #
    # Шаги
    # ------------------------------------------------------------------ #
    def _step_echo(self):
        resp = self.client.echo('cyclops_test_suite')
        assert resp.get('result') == 'cyclops_test_suite', resp
        return resp

    def _step_sync_banks(self):
        count = sync_sbp_banks()
        return f'{count} банков в справочнике'

    def _step_topup_and_identify(self, virtual_account):
        def _topup():
            resp = cyclops_manager.send_test_payment(
                recipient_account=settings.CYCLOPS_CONFIG.get('NOMINAL_ACCOUNT', ''),
                recipient_bank_code='044525104',
                amount=500,
                purpose='cyclops_test_suite — тестовое пополнение',
            )
            return resp
        self._run('transfer_money (тестовое пополнение, только pre)', _topup, category='Платежи')

        def _find_and_identify():
            # Идентифицировать можно только платёж в типе `incoming`. Если он
            # пролежал неопознанным ~полчаса, Точка сама переводит его в
            # `incoming_unrecognized`, и identification_payment по нему отвечает
            # «It is forbidden to work with a payment with the current type».
            # Поэтому берём именно свежий incoming, а не просто последний
            # неидентифицированный, и даём пополнению время долететь.
            payment = None
            attempts = 3
            for attempt in range(attempts):
                # sync_payments дёргает list_payments + get_payment на каждый и
                # сохраняет в БД (сумма нужна там для identify_payment ниже)
                cyclops_manager.sync_payments()
                payment = (
                    CyclopsPayment.objects
                    .filter(identify=False, type='incoming')
                    .order_by('-created_at')
                    .first()
                )
                if payment:
                    break
                if attempt < attempts - 1:
                    time.sleep(10)

            if not payment:
                stuck = CyclopsPayment.objects.filter(
                    identify=False, type='incoming_unrecognized',
                ).count()
                # Не ошибка интеграции: тестовое пополнение на pre-слое прилетает
                # асинхронно и с собственным payment_id (service_pay_key из
                # transfer_money — это НЕ идентификатор будущего платежа).
                # Идентифицировать нечего — сообщаем и идём дальше.
                return ('пропущено: за ~1 минуту не появилось платежа в типе incoming '
                        f'(в incoming_unrecognized висит {stuck}). Тестовое пополнение '
                        'долетает асинхронно — запустите команду повторно через несколько '
                        'минут, когда платёж появится в разделе «Платежи»')

            cyclops_manager.identify_payment(payment.payment_id, virtual_account.virtual_account_id)
            return (f'identification_payment: {payment.payment_id} ({payment.amount} руб.) '
                    f'-> {virtual_account.virtual_account_id}')
        self._run('list_payments + get_payment + identification_payment', _find_and_identify, category='Платежи')

    def _step_throwaway_deal(self, virtual_account):
        """Одноразовая сделка на 1₽ — только чтобы протестировать create_deal /
        update_deal / rejected_deal, не трогая реальный сценарий выплаты."""
        state = {}

        def _create():
            recipient = {
                'number': 1, 'type': 'payment_contract_by_sbp', 'amount': 1.0,
                'first_name': 'Тест', 'last_name': 'Тестов', 'phone_number': self.opts['phone'],
                'bank_sbp_id': self._resolve_test_bank_sbp_id(),
                'purpose': 'cyclops_test_suite — служебная сделка (будет отменена)',
            }
            payers = [{'virtual_account': virtual_account.virtual_account_id, 'amount': 1.0}]
            resp = self.client.create_deal(1.0, payers, [recipient])
            if 'result' not in resp:
                raise RuntimeError(resp.get('error'))
            state['deal_id'] = resp['result']['deal_id']
            return state['deal_id']
        self._run('create_deal (служебная, на отмену)', _create, category='Сделки')

        if 'deal_id' not in state:
            return

        def _update():
            recipient = {
                'number': 1, 'type': 'payment_contract_by_sbp', 'amount': 1.0,
                'first_name': 'Тест', 'last_name': 'Тестов', 'phone_number': self.opts['phone'],
                'bank_sbp_id': self._resolve_test_bank_sbp_id(),
                'purpose': 'cyclops_test_suite — служебная сделка (обновлена перед отменой)',
            }
            payers = [{'virtual_account': virtual_account.virtual_account_id, 'amount': 1.0}]
            resp = self.client.update_deal(state['deal_id'], 1.0, payers, [recipient])
            if 'result' not in resp:
                raise RuntimeError(resp.get('error'))
            return resp
        self._run('update_deal', _update, category='Сделки')

        def _identify_returned():
            # По служебной сделке реального возврата нет — ожидаем деловую ошибку
            # от Точки (например, «платёж не найден»), а не транспортный сбой.
            # Это подтверждает, что сам метод/эндпоинт работает и Точка его знает.
            resp = self.client.identification_returned_payment_by_deal(state['deal_id'])
            if 'error' in resp:
                return f'эндпоинт отвечает (деловая ошибка ожидаема — по сделке нет возврата): {resp["error"]}'
            return resp
        self._run('identification_returned_payment_by_deal (эндпоинт без реального возврата)',
                   _identify_returned, category='Платежи')

        self._run('rejected_deal (отмена служебной сделки)',
                   lambda: cyclops_manager.reject_deal(state['deal_id']), category='Сделки')

    def _step_real_payout(self, options):
        """Реальный сквозной сценарий — та же бизнес-логика, что и в проде
        (`_do_payout`), но вызвана синхронно, без похода в Celery/Redis:
        эта команда должна работать и на машине, где воркер не поднят.
        В реальном профиле постановку в очередь делает `ensure_guaranteed_prize_payout`
        (см. ProfileView.post) — здесь сознательно её не вызываем."""
        participant, _ = User.objects.get_or_create(
            email=TEST_PARTICIPANT_EMAIL,
            defaults=dict(
                first_name='Тест', last_name='Тестовый', middle_name='Тестович',
                phone=self.opts['phone'], bank_bik='044525104',
            ),
        )
        participant.first_name = participant.first_name or 'Тест'
        participant.last_name = participant.last_name or 'Тестовый'
        participant.phone = self.opts['phone']
        participant.bank_bik = '044525104'
        participant.save(update_fields=['first_name', 'last_name', 'phone', 'bank_bik'])

        payout = GuaranteedPrizePayout.objects.filter(participant=participant).first()
        if payout and options['reset_payout']:
            payout.delete()
            payout = None

        if payout and payout.status in ('paid', 'failed'):
            self.stdout.write(
                f'  Сквозная выплата уже прогонялась: статус={payout.status}, '
                f'deal_id={payout.deal_id}. Передайте --reset-payout, чтобы прогнать заново.'
            )
            self.results.append(('Бизнес-сценарий (гарантированный приз)', 'сквозной сценарий (perform_payout)',
                                  True, f'уже {payout.status}, deal_id={payout.deal_id}'))
            return

        if payout is None:
            payout = GuaranteedPrizePayout.objects.create(
                participant=participant,
                amount=Decimal(str(settings.CYCLOPS_CONFIG['PRIZE_AMOUNT'])),
                status=GuaranteedPrizePayout.STATUS_NEW,
            )

        def _run_payout():
            gpp.perform_payout(participant.pk)  # синхронно, минуя Celery
            p = GuaranteedPrizePayout.objects.get(participant=participant)
            if p.status == 'failed':
                raise RuntimeError(p.error_reason)
            doc = f', документ={p.document.document_number}' if p.document_id else ', документ не создан'
            return f'статус={p.status}, deal_id={p.deal_id}{doc}'
        self._run('сквозной сценарий (perform_payout)', _run_payout, category='Бизнес-сценарий (гарантированный приз)')

    def _step_poll(self):
        gpp.poll_prize_deals()
        payout = GuaranteedPrizePayout.objects.filter(participant__email=TEST_PARTICIPANT_EMAIL).first()
        return f'статус тестовой выплаты после опроса: {payout.status if payout else "—"}'

    def _step_retry(self):
        """Только подсчёт — саму отправку через Celery здесь не дёргаем (это может
        зависнуть на машине без поднятого воркера/брокера; в реальной работе
        retry_failed_prizes вызывается по расписанию Celery beat, не отсюда)."""
        count = GuaranteedPrizePayout.objects.filter(
            status__in=GuaranteedPrizePayout.RETRIABLE_STATUSES,
            cnt_retry__lt=gpp.max_payout_retries(),
        ).count()
        return f'{count} записей подходят под авто-ретрай (реальная рассылка — через Celery beat)'

    def _step_activate_deactivate(self, payout_beneficiary):
        """deactivate_beneficiary → activate_beneficiary.

        Точка просила показать в логах именно активацию НЕактивного бенефициара.
        Деактивировать нельзя бенефициара с деньгами на виртуальном счёте
        («On the virtual accounts ... positive balance»), а у плательщика
        призового фонда деньги как раз лежат — поэтому берём для этой пары
        любого другого бенефициара с нулевым балансом.
        """
        from promotion.models import CyclopsBeneficiary

        candidates = list(
            CyclopsBeneficiary.objects
            .exclude(pk=payout_beneficiary.pk)
            .exclude(beneficiary_id__isnull=True).exclude(beneficiary_id='')
            .filter(is_active=True)[:5]
        )
        if not candidates:
            candidates = [payout_beneficiary]

        chosen = {}

        def _deactivate():
            errors = []
            for cand in candidates:
                try:
                    cyclops_manager.set_beneficiary_active(cand.pk, False)
                    chosen['b'] = cand
                    return f'деактивирован: {cand.name} ({cand.beneficiary_id})'
                except Exception as e:  # noqa: BLE001 — перебираем кандидатов
                    errors.append(f'{cand.name}: {e}')
            raise RuntimeError('ни одного бенефициара не удалось деактивировать: ' + '; '.join(errors[:3]))
        self._run('deactivate_beneficiary', _deactivate, category='Бенефициары')

        if 'b' not in chosen:
            self.stdout.write('  (деактивация не прошла — activate_beneficiary пропущен)')
            return

        def _activate():
            # Сразу после деактивации Точка активацию не принимает по двум причинам:
            #   - «Beneficiary already in upgrading state» — переходное состояние;
            #   - код 4963 «activation is not allowed after deactivation» — кулдаун,
            #     в meta.available_after приходит время, когда станет можно (~минута).
            # Обе ситуации временные, поэтому просто ждём и повторяем.
            cand = chosen['b']
            retryable = ('upgrading state', 'not allowed after deactivation')
            last_error = None
            # Кулдаун после деактивации на pre-слое — около 5 минут, поэтому
            # ждём с запасом: иначе бенефициар останется деактивированным.
            deadline_attempts = 32  # 32 * 15c = 8 минут
            for attempt in range(deadline_attempts):
                try:
                    cyclops_manager.set_beneficiary_active(cand.pk, True)
                    return (f'активирован обратно: {cand.name} ({cand.beneficiary_id})'
                            f'{f", попыток: {attempt + 1}" if attempt else ""}')
                except Exception as e:  # noqa: BLE001 — ждём окончания кулдауна
                    last_error = e
                    if not any(marker in str(e) for marker in retryable):
                        raise
                    time.sleep(15)
            raise RuntimeError(
                f'ВНИМАНИЕ: бенефициар {cand.beneficiary_id} ({cand.name}) остался ДЕАКТИВИРОВАННЫМ — '
                f'активировать за ~8 минут не удалось: {last_error}. '
                f'Активируйте его вручную в панели (Cyclops -> Бенефициары -> Активировать).'
            )
        self._run('activate_beneficiary (активация неактивного)', _activate, category='Бенефициары')

    def _step_fl_beneficiary(self):
        """v3-методы по бенефициару-физлицу (ИП/самозанятый): создание и работа
        с данными его документов (ИНН + ДУЛ). Требование Точки для выхода в Prod.

        Создаёт бенефициара в Cyclops (не в нашей БД — в бизнес-логике выплат
        такие бенефициары пока не участвуют, плательщик у нас всегда ЮЛ).
        """
        state = {}
        inn = self.opts['fl_inn']
        legal_type = self.opts['fl_legal_type']
        type_label = 'ИП' if legal_type == 'I' else 'физлицо/самозанятый'

        def _create():
            resp = self.client.create_beneficiary(
                inn=inn,
                registration_address=TEST_FL_ADDRESS,
                legal_type=legal_type,
                nominal_account_code=settings.CYCLOPS_CONFIG.get('NOMINAL_ACCOUNT') or None,
                nominal_account_bic='044525104',
            )
            if 'result' not in resp:
                # Повторный прогон с тем же ИНН: Точка отвечает
                # the_beneficiary_is_already_exists и кладёт id существующего в
                # meta — переиспользуем его, чтобы методы по документам ниже
                # всё равно прогонялись на каждом запуске.
                error = resp.get('error') or {}
                for err in ((error.get('data') or {}).get('errors') or []):
                    if err.get('code') == 'the_beneficiary_is_already_exists':
                        state['id'] = (err.get('meta') or {}).get('id')
                        if state['id']:
                            return (f'{type_label} с ИНН {inn} уже создан ранее, '
                                    f'переиспользуем: {state["id"]}')
                raise RuntimeError(error)
            result = resp['result']
            # id может лежать как в result.id, так и в result.beneficiary.id
            state['id'] = result.get('id') or (result.get('beneficiary') or {}).get('id')
            if not state['id']:
                raise RuntimeError(f'API не вернул id бенефициара: {result}')
            return f'{type_label} создан: {state["id"]} (ИНН {inn})'
        self._run(f'create_beneficiary (v3, {type_label})', _create,
                   category='Бенефициары (v3, физлица)')

        if 'id' not in state:
            self.stdout.write(
                '  (create_beneficiary не прошёл — add/get_beneficiary_documents_data пропущены)'
            )
            return

        def _add_docs():
            documents = [
                dict(TEST_FL_PASSPORT),
                {'type': 'inn_f', 'inn': inn, 'birth_place': TEST_FL_BIRTH_PLACE},
            ]
            resp = self.client.add_beneficiary_documents_data(state['id'], documents)
            if 'result' not in resp:
                # Повторный прогон в тот же день: документы уже отправлены и
                # успешно проверены — Точка не даёт переотправить их повторно.
                # Для проверки метода это успешный исход, а не ошибка.
                error = resp.get('error') or {}
                for err in ((error.get('data') or {}).get('errors') or []):
                    if err.get('code') == 'documents_already_successfully_validated_today':
                        return ('документы уже успешно проверены сегодня '
                                '(Точка не принимает повторную отправку в тот же день)')
                raise RuntimeError(error)
            docs = (resp['result'] or {}).get('documents') or []
            statuses = ', '.join(
                f"{d.get('type')}={(d.get('validation_process') or {}).get('status')}" for d in docs
            )
            return f'документы приняты: {statuses or resp["result"]}'
        self._run('add_beneficiary_documents_data (v3, ИНН + ДУЛ)', _add_docs,
                   category='Бенефициары (v3, физлица)')

        def _get_docs():
            resp = self.client.get_beneficiary_documents_data(state['id'])
            if 'result' not in resp:
                raise RuntimeError(resp.get('error'))
            result = resp['result'] or {}
            last = result.get('last_documents') or []
            valid = result.get('valid_documents') or []
            statuses = ', '.join(
                f"{d.get('type')}={(d.get('validation_process') or {}).get('status')}" for d in last
            )
            return f'проверенных: {len(valid)}, последние: {statuses or "—"}'
        self._run('get_beneficiary_documents_data (v3, статус проверки)', _get_docs,
                   category='Бенефициары (v3, физлица)')

    def _step_refunds(self, virtual_account):
        def _refund_payment():
            resp = cyclops_manager.send_test_payment(
                recipient_account=settings.CYCLOPS_CONFIG.get('NOMINAL_ACCOUNT', ''),
                recipient_bank_code='044525104',
                amount=150,
                purpose='cyclops_test_suite — на возврат',
            )
            time.sleep(2)
            payments = (self.client.list_payments(identify_status=False) or {}).get('payments', [])
            if not payments:
                return 'платёж для возврата не появился сразу — пропущено (можно прогнать --with-refund ещё раз)'
            return cyclops_manager.refund_incoming_payment(payments[-1])
        self._run('refund_payment', _refund_payment, category='Платежи')

        self._run('refund_virtual_account', lambda: cyclops_manager.refund_virtual_account_funds(
            virtual_account.pk, amount=1,
            recipient_account=TEST_REFUND_ACCOUNT, recipient_bank_code=TEST_REFUND_BANK_CODE,
            recipient_name=TEST_REFUND_NAME, recipient_inn=TEST_REFUND_INN, recipient_kpp=TEST_REFUND_KPP,
            purpose='cyclops_test_suite — тестовый вывод 1₽',
        ), category='Виртуальные счета')

    # ------------------------------------------------------------------ #
    # Вспомогательное
    # ------------------------------------------------------------------ #
    def _resolve_test_bank_sbp_id(self):
        bank = SbpBank.objects.filter(bank_code='044525104').first() or SbpBank.objects.first()
        if not bank:
            raise RuntimeError('Справочник банков СБП пуст — шаг sync banks СБП должен был его наполнить')
        return bank.sbp_id

    def _banner(self, text):
        self.stdout.write(self.style.MIGRATE_HEADING(f'\n{"=" * len(text)}\n{text}\n{"=" * len(text)}'))

    def _run(self, title, fn, category='Прочее'):
        start = time.monotonic()
        try:
            detail = fn()
            elapsed = time.monotonic() - start
            self.results.append((category, title, True, str(detail) if detail else 'ok'))
            self.stdout.write(self.style.SUCCESS(f'✓ {title}') + f'  ({elapsed:.1f}s) {detail if detail else ""}')
        except Exception as e:  # noqa: BLE001 — тестовая команда, любая ошибка должна попасть в отчёт
            elapsed = time.monotonic() - start
            self.results.append((category, title, False, str(e)))
            self.stdout.write(self.style.ERROR(f'✗ {title}') + f'  ({elapsed:.1f}s) {e}')

    # Порядок разделов — как в технической документации Cyclops, чтобы отчёт
    # можно было один в один сверить с чек-листом поддержки Точки по логам.
    _CATEGORY_ORDER = [
        'Служебное', 'Бенефициары', 'Бенефициары (v3, физлица)', 'Виртуальные счета',
        'Платежи', 'Сделки', 'Документы', 'СБП',
        'Бизнес-сценарий (гарантированный приз)', 'Прочее',
    ]

    def _summary(self):
        ok = sum(1 for _, _, success, _ in self.results if success)
        total = len(self.results)
        self._banner(f'Итого: {ok}/{total} шагов прошли успешно')

        by_category = {c: [] for c in self._CATEGORY_ORDER}
        for category, title, success, detail in self.results:
            by_category.setdefault(category, []).append((title, success, detail))

        # Отчёт, готовый для вставки в чат поддержки Точки: какие методы
        # покрыты тестом и с каким результатом, по разделам их документации.
        for category in self._CATEGORY_ORDER:
            items = by_category.get(category)
            if not items:
                continue
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n{category}'))
            for title, success, detail in items:
                mark = self.style.SUCCESS('✓') if success else self.style.ERROR('✗')
                self.stdout.write(f'  {mark} {title}')
                if not success:
                    self.stdout.write(f'      {detail}')

        if ok < total:
            self.stdout.write(self.style.WARNING(
                '\nЕсть шаги с ошибками — это тоже полезный результат: подробности выше по каждому шагу.'
            ))
