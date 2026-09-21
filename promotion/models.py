from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager
import uuid
from django.utils import timezone


class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email обязателен')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)

        return self.create_user(email, password, **extra_fields)


class User(AbstractUser):
    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True,
        verbose_name='Публичный ID'
    )

    username = None
    email = models.EmailField(
        unique=True,
        verbose_name='Email'
    )

    phone = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        verbose_name='Телефон'
    )

    middle_name = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        verbose_name='Отчество'
    )

    birth_date = models.DateField(
        null=True,
        blank=True,
        verbose_name='Дата рождения'
    )

    address = models.TextField(
        blank=True,
        verbose_name='Адрес доставки'
    )

    city = models.CharField(
        null=True,
        blank=True,
        verbose_name='Город проживания',
        max_length=500
    )

    bank = models.CharField(
        null=True,
        blank=True,
        verbose_name='Банк',
        max_length=500
    )

    bank_bik = models.CharField(
        null=True,
        blank=True,
        verbose_name='БИК банка',
        max_length=20
    )

    is_subscribed_receipt_emails = models.BooleanField(
        default=False,
        verbose_name='Подписан на письма о статусе чека',
    )

    is_guaranteed_prize_sent = models.BooleanField(
        default=False,
        verbose_name='Письмо о гарантированном призе отправлено',
    )

    guaranteed_prize_opt_out = models.BooleanField(
        default=False,
        verbose_name='Отказ от гарантированного приза',
        help_text='Участник добровольно отказался от выплаты гарантированного приза. '
                  'Пока галочка стоит, выплата ему не отправляется: ни плановой пятничной '
                  'задачей, ни авто-ретраями, ни кнопкой «Оплатить» — см. '
                  'promotion/services/guaranteed_prize_payout.py.',
    )

    is_blocked = models.BooleanField(
        default=False,
        verbose_name='Заблокирован',
        help_text='Заблокированный участник не может загружать новые чеки.',
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='Дата регистрации'
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name']

    objects = CustomUserManager()

    class Meta:
        verbose_name = 'Участник'
        verbose_name_plural = 'Участники'
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['public_id']),
        ]

    def __str__(self):
        return f"{self.get_full_name()} ({self.email})"

    def get_full_name(self):
        full_name = super().get_full_name()
        return full_name or self.email


class Receipt(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'На проверке'
        CONFIRMED = 'confirmed', 'Подтвержден'
        REJECTED = 'rejected', 'Отклонен'
        WINNER = 'winner', 'Победный'
        FROZEN = 'frozen', 'Заморожен'

    class InputMethod(models.TextChoices):
        CAMERA = 'camera', 'Камера (QR-код)'
        PHOTO = 'photo', 'Фото чека'
        MANUAL = 'manual', 'Ручной ввод'

    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True
    )

    participant = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        verbose_name='Участник'
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        verbose_name='Статус'
    )

    is_participation = models.BooleanField(default=False, verbose_name='Участовал в розыгрыше')

    is_month_participation = models.BooleanField(
        default=False,
        verbose_name='Участвовал в месячном розыгрыше',
    )

    is_send_email = models.BooleanField(default=False, verbose_name='Отправлено на email',
                                        help_text='Если чек победный')
    email_sent_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Когда отправлено письмо с договором',
        help_text='Точка отсчёта срока на подпись договора (см. WinnerReplacementSettings) — '
                  'пока письмо не отправлено, срок не действует и победителя можно заменить свободно.',
    )

    link_oki_document = models.TextField(verbose_name='Ссылка на договор', null=True, blank=True)
    status_oki_document = models.CharField(max_length=250, verbose_name='Статус договора', null=True, blank=True)
    oki_document_issued_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Когда выставлен договор',
        help_text='Момент создания договора в OkiDoki — показывается в дашборде в колонке «Договор».',
    )
    link_oki_document_admin = models.TextField(verbose_name='Ссылка на договор для заказчика', null=True, blank=True,
                                               help_text='Если нужно заполнить обязательные поля в договоре')

    date_result_raffle = models.DateField(verbose_name='Дата подведения итогов', null=True, blank=True)

    message = models.TextField(verbose_name='Сообщение', null=True, blank=True)

    receipt_image = models.ImageField(
        upload_to='receipts/%Y/%m/%d/',
        verbose_name='Фото чека',
        null=True, blank=True
    )

    fn = models.CharField(max_length=16, blank=True, verbose_name='ФН', null=True)
    fd = models.CharField(max_length=10, blank=True, verbose_name='ФД', null=True)
    fp = models.CharField(max_length=10, blank=True, verbose_name='ФП', null=True)

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name='Сумма'
    )

    date = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Дата покупки'
    )

    store = models.CharField('Магазин', max_length=255, blank=True, null=True)
    address = models.CharField('Адрес', max_length=255, blank=True, null=True)
    inn = models.CharField('ИНН', max_length=20, blank=True, null=True)
    
    week = models.PositiveIntegerField(verbose_name='Неделя', null=True, blank=True)
    month = models.PositiveIntegerField(
        verbose_name='Месяц',
        null=True,
        blank=True,
        help_text='Номер месяца акции — проставляется автоматически при подтверждении чека.',
    )

    items = models.JSONField('Товары', default=dict, blank=True)
    
    promo_items = models.JSONField(
    'Акционные товары',
        default=list,
        blank=True,
        help_text='Позиции из чека, соответствующие условиям акции. '
                'Формат: [{name, price, quantity}, ...]',
    )

    qr_code_str = models.CharField(max_length=500, verbose_name='Строка QR-кода', null=True, blank=True)

    input_method = models.CharField(
        max_length=20,
        choices=InputMethod.choices,
        null=True,
        blank=True,
        verbose_name='Способ ввода',
    )

    fns_message_id = models.CharField(max_length=500, verbose_name='ID сообщения API ФНС', null=True, blank=True)
    
    system_message = models.TextField(
        verbose_name='Системное сообщение',
        null=True,
        blank=True
    )

    class AIRecommendation(models.TextChoices):
        ACCEPT = 'accept', 'Принять'
        REVIEW = 'review', 'Оставить на проверке'
        REJECT = 'reject', 'Отклонить'
        NO_ANSWER = 'no_answer', 'Нет ответа'

    # Разбор чека нейросетью. Поля заполняются всегда, когда нейросеть смотрела чек,
    # и в теневом режиме (OPENROUTER_SHADOW_MODE) остаются её единственным следом:
    # на статус, сообщение участнику и ключевые слова она тогда не влияет.
    ai_recommendation = models.CharField(
        max_length=20,
        choices=AIRecommendation.choices,
        blank=True,
        default='',
        db_index=True,
        verbose_name='Рекомендация нейросети',
        help_text='Что нейросеть предложила сделать с чеком. Отклонить чек по её ответу '
                  'система не может ни при каком режиме — это именно рекомендация.',
    )
    ai_review_note = models.TextField(
        blank=True,
        default='',
        verbose_name='Разбор чека нейросетью',
        help_text='Какие позиции признаны акционными, на какую сумму, что нейросеть '
                  'предлагает добавить в ключевые слова и почему.',
    )
    ai_reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name='Когда нейросеть смотрела чек',
        help_text='По этому полю удобно выбрать разбор за сутки.',
    )
    status_new_validate = models.CharField(
        max_length=250, 
        verbose_name='Статус новой проверки', 
        null=True, 
        blank=True
    )

    review_notified = models.BooleanField(
        default=False, 
        verbose_name='Уведомление о проверке отправлено'
    )

    retry_count = models.IntegerField(
        default=0,
        verbose_name='Количество попыток проверки'
    )

    fns_retry_pending = models.BooleanField(
        default=False,
        verbose_name='Ожидает повторного запроса в ФНС',
    )
    fns_retry_count = models.IntegerField(
        default=0,
        verbose_name='Количество повторных запросов в ФНС',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    moderated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Дата модерации',
    )

    class Meta:
        verbose_name = 'Чек'
        verbose_name_plural = 'Чеки'
        ordering = ['-created_at']

    def __str__(self):
        return f"Чек {self.public_id}"


class KeywordProduct(models.Model):
    keyword = models.CharField(max_length=250, verbose_name='Ключевое слово')

    class Meta:
        verbose_name = 'Ключевое слово для товара акции'
        verbose_name_plural = 'Ключевые слова для товаров акции'

    def __str__(self):
        return self.keyword
    

class ExcludingKeywordProduct(models.Model):
    keyword = models.CharField(max_length=250, verbose_name='Ключевое слово', null=True, blank=True)

    class Meta:
        verbose_name = 'Исключающее ключевое слово для товара акции'
        verbose_name_plural = 'Исключающие ключевые слова для товара акции'

    def __str__(self):
        return self.keyword



class PrizeKind(models.Model):
    """
    Вид приза — то, чем приз является, независимо от розыгрыша.

    Строка Prize описывает приз конкретного розыгрыша: «сертификат Ozon недели 3»,
    «сертификат Ozon недели 4» — это разные Prize, хотя приз один и тот же. Всё,
    что относится к призу как к вещи, а не к розыгрышу, вешается на вид: сейчас
    это файлы электронных призов (PrizeFile), которые бессмысленно привязывать
    к неделе — один сертификат подходит любому победителю этого же приза.

    Неделя для файлов не важна, а вот тип розыгрыша важен: один и тот же вид
    приза может разыгрываться и еженедельно, и ежемесячно, и под эти розыгрыши
    заводят разные партии сертификатов. Поэтому склад файлов — это пара
    (вид, тип розыгрыша), см. PrizeFile.draw_type.

    Вид подставляется автоматически по названию приза при его создании
    (см. Prize.save) — менеджеру про виды знать не нужно. Переименование приза
    уже созданный вид не меняет, иначе файлы отрывались бы от пула.
    """

    name = models.CharField(
        max_length=250,
        unique=True,
        verbose_name='Название вида',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата создания')

    class Meta:
        verbose_name = 'Вид приза'
        verbose_name_plural = 'Виды призов'
        ordering = ('name',)

    def __str__(self):
        return self.name

    @classmethod
    def for_name(cls, name: str) -> 'PrizeKind | None':
        """Вид для названия приза: находит существующий или заводит новый."""
        cleaned = (name or '').strip()
        if not cleaned:
            return None
        kind, _ = cls.objects.get_or_create(name=cleaned)
        return kind


class Prize(models.Model):
    class DrawPeriod(models.TextChoices):
        WEEKLY = 'weekly', 'Еженедельный'
        MONTHLY = 'monthly', 'Ежемесячный'
        INSTANT = 'instant', 'Моментальный'

    kind = models.ForeignKey(
        PrizeKind,
        on_delete=models.PROTECT,
        related_name='prizes',
        null=True,
        blank=True,
        verbose_name='Вид приза',
        help_text='Подставляется автоматически по названию приза. Призы одного вида '
                  'делят общий склад файлов электронных призов.',
    )
    name = models.CharField(max_length=250, verbose_name='Название приза', null=True, blank=True)
    description = models.TextField(blank=True, null=True, verbose_name='Описание')
    type_prize = models.CharField(max_length=250, verbose_name='Тип приза', null=True, blank=True)
    image = models.ImageField(upload_to='prizes/%Y/%m/%d/', verbose_name='Изображение', null=True, blank=True)
    count = models.IntegerField(verbose_name='Количество', null=True, blank=True)
    cost = models.FloatField(verbose_name='Стоимость', null=True, blank=True)
    ndfl = models.FloatField(verbose_name='НДФЛ', null=True, blank=True)
    is_main = models.BooleanField(verbose_name='Главный приз', default=False, null=True, blank=True)
    is_electronic = models.BooleanField(
        verbose_name='Электронный приз',
        default=False,
        help_text='Приз выдаётся файлом (сертификат, промокод и т.п.). Только для таких призов '
                  'можно загружать файлы призов и выдавать их победителям.',
    )
    is_active = models.BooleanField(verbose_name='Активен', default=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата создания')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Дата обновления')
    draw_period = models.CharField(
        max_length=10,
        choices=DrawPeriod.choices,
        default=DrawPeriod.WEEKLY,
        verbose_name='Периодичность розыгрыша',
        help_text='Не применяется к главному призу (см. «Главный приз»). «Моментальный» — '
                  'приз лотка яиц: по количеству и неделе для него генерируются призовые '
                  'моменты (см. InstantMoment), розыгрыш идёт непрерывно.',
    )
    week = models.PositiveIntegerField(
        verbose_name='Неделя',
        null=True,
        blank=True,
        help_text='Номер недели акции, в которую разыгрывается этот приз. Обязателен для '
                  'еженедельных и моментальных призов.',
    )
    month = models.PositiveIntegerField(
        verbose_name='Месяц',
        null=True,
        blank=True,
        help_text='Номер месяца акции, в который разыгрывается этот приз. Обязателен для ежемесячных призов.',
    )

    class Meta:
        verbose_name = 'Приз'
        verbose_name_plural = 'Призы'

    def save(self, *args, **kwargs):
        # Вид проставляется один раз — при создании приза. Переименование приза
        # вид не переносит: иначе уже загруженные файлы оторвались бы от пула.
        if self.kind_id is None:
            kind = PrizeKind.for_name(self.name)
            if kind is not None:
                self.kind = kind
                update_fields = kwargs.get('update_fields')
                if update_fields is not None and 'kind' not in update_fields:
                    kwargs['update_fields'] = [*update_fields, 'kind']
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class PrizeCountChange(models.Model):
    prize = models.ForeignKey(
        Prize,
        on_delete=models.CASCADE,
        related_name='count_changes',
        verbose_name='Приз',
    )
    changed_at = models.DateTimeField(auto_now_add=True, verbose_name='Когда')
    changed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Кто',
        help_text='Пусто — автоматический розыгрыш',
    )
    old_count = models.IntegerField(verbose_name='Было')
    new_count = models.IntegerField(verbose_name='Стало')
    reason = models.CharField(max_length=500, verbose_name='Причина')

    class Meta:
        verbose_name = 'Изменение количества приза'
        verbose_name_plural = 'Изменения количества призов'
        ordering = ('-changed_at',)

    def __str__(self):
        return f'{self.prize_id}: {self.old_count} → {self.new_count}'


class Store(models.Model):
    name = models.CharField('Название', max_length=255, help_text='Например, АКЦИОНЕРНОЕ ОБЩЕСТВО "ТАНДЕР"') 
    address = models.CharField(
        'Адрес', 
        max_length=255, 
        help_text='Поле больше не используется для валидации чеков! Пример значения: "630088, Новосибирская обл, Новосибирск г, Зорге ул, дом № 77а"', 
        null=True, blank=True
    ) 
    inn = models.CharField('ИНН', max_length=20, help_text='Например, "2310031475"')
    
    class Meta: 
        verbose_name = 'Магазин'
        verbose_name_plural = 'Магазины' 
        
    def __str__(self): 
        return f'{self.name} | {self.address if self.address else "без адреса"} | {self.inn}'


class PromotionDrawResult(models.Model):
    participant = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        verbose_name='Победитель',
    )
    prize = models.ForeignKey(
        Prize,
        on_delete=models.CASCADE,
        verbose_name='Приз',
    )
    receipt = models.ForeignKey(
        Receipt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Чек (для отметки)',
        related_name='draw_results'
    )
    
    week_num = models.PositiveIntegerField(
        verbose_name='Неделя розыгрыша',
        null=True,
        blank=True,
        db_index=True,
    )
    month_num = models.PositiveIntegerField(
        verbose_name='Месяц розыгрыша',
        null=True,
        blank=True,
        db_index=True,
    )
    is_published = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name='Опубликован',
        help_text='Победитель виден на сайте, чек переведён в статус «Победный»',
    )
    published_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Дата публикации',
    )
    is_reserve = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name='Резервный победитель',
        help_text='Определён одновременно с основным победителем (п. 8.4 Правил). '
                   'Не публикуется автоматически — используется для замены, если основной '
                   'победитель лишён приза/отказался.',
    )
    reserve_rank = models.PositiveSmallIntegerField(
        verbose_name='Очередь резерва',
        null=True,
        blank=True,
        help_text='1 — первый резервный победитель, 2 — второй и т.д.',
    )

    public_participant = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='public_draw_results',
        verbose_name='Победитель на лендинге',
        help_text='Заполняется автоматически при первой замене победителя: здесь остаётся тот, '
                  'кто был объявлен победителем изначально. Лендинг всегда показывает именно его — '
                  'замена победителя в дашборде не должна менять опубликованные итоги.',
    )

    is_instant = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name='Моментальный приз',
        help_text='Приз выигран в лотке яиц, а не в еженедельном/ежемесячном розыгрыше. '
                  'Такой итог создаётся сразу опубликованным: участник узнаёт о выигрыше '
                  'в момент игры, и держать победу неопубликованной нечего.',
    )
    instant_moment = models.OneToOneField(
        'InstantMoment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='draw_result',
        verbose_name='Призовой момент',
        help_text='Момент из расписания моментальных призов, который принёс эту победу.',
    )

    delivery_status = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        verbose_name='Доставлено',
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата розыгрыша')

    class Meta:
        verbose_name = 'Итог розыгрыша'
        verbose_name_plural = 'Итоги розыгрышей'

    def __str__(self):
        return f'{self.week_num} - {self.participant} → {self.prize}'


class PromotionDrawResultMainRaffle(models.Model):
    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        db_index=True
    )
    participant = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        verbose_name='Победитель',
    )
    prize = models.ForeignKey(
        Prize,
        on_delete=models.CASCADE,
        verbose_name='Приз',
    )
    receipt = models.ForeignKey(
        Receipt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name='Чек (билет)',
        related_name='main_draw_results',
        help_text='Чек, который принёс победу в Главном розыгрыше (п. 7.4 Правил).',
    )
    is_send_email = models.BooleanField(default=False, verbose_name='Отправлено на email',
                                        help_text='Если чек победный')
    email_sent_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Когда отправлено письмо с договором',
        help_text='Точка отсчёта срока на подпись договора (см. WinnerReplacementSettings) — '
                  'пока письмо не отправлено, срок не действует и победителя можно заменить свободно.',
    )

    link_oki_document = models.TextField(verbose_name='Ссылка на договор', null=True, blank=True)
    status_oki_document = models.CharField(max_length=250, verbose_name='Статус договора', null=True, blank=True)
    oki_document_issued_at = models.DateTimeField(
        null=True, blank=True, verbose_name='Когда выставлен договор',
        help_text='Момент создания договора в OkiDoki — показывается в дашборде в колонке «Договор».',
    )
    link_oki_document_admin = models.TextField(verbose_name='Ссылка на договор для заказчика', null=True, blank=True,
                                               help_text='Если нужно заполнить обязательные поля в договоре')
    is_reserve = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name='Резервный победитель',
        help_text='Определён одновременно с основным победителем (п. 8.4 Правил).',
    )
    reserve_rank = models.PositiveSmallIntegerField(
        verbose_name='Очередь резерва',
        null=True,
        blank=True,
        help_text='1 — первый резервный победитель, 2 — второй и т.д.',
    )

    public_participant = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='public_main_draw_results',
        verbose_name='Победитель на лендинге',
        help_text='Заполняется автоматически при первой замене победителя: здесь остаётся тот, '
                  'кто был объявлен победителем изначально. Публичные материалы показывают именно его.',
    )

    delivery_status = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        verbose_name='Доставлено',
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата розыгрыша')

    class Meta:
        verbose_name = 'Итог розыгрыша главного приза'
        verbose_name_plural = 'Итоги розыгрышей главного приза'

    def __str__(self):
        return f'{self.participant_id} → {self.prize_id}'


class WinnerReplacement(models.Model):
    """
    Замена победителя в уже разыгранном призовом слоте.

    Одна строка — одна замена. Итог розыгрыша можно заменять сколько угодно раз,
    вся цепочка сохраняется: кто был победителем, кто им стал, подходил ли новый
    победитель под условия розыгрыша, кто и когда произвёл замену.

    ФИО/email обоих участников дублируются строками-снимками: даже если участника
    удалят из базы, история замен остаётся читаемой.

    Замена НИКОГДА не меняет опубликованные итоги на лендинге — первоначальный
    победитель фиксируется в PromotionDrawResult.public_participant, и лендинг
    показывает именно его (см. main.services.get_winners_by_months).
    """

    class Mode(models.TextChoices):
        AUTO = 'auto', 'Автоматически'
        MANUAL = 'manual', 'Вручную'

    draw_result = models.ForeignKey(
        PromotionDrawResult,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='replacements',
        verbose_name='Итог розыгрыша',
    )
    main_draw_result = models.ForeignKey(
        PromotionDrawResultMainRaffle,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='replacements',
        verbose_name='Итог розыгрыша главного приза',
    )

    order = models.PositiveIntegerField(
        default=1,
        verbose_name='Номер замены',
        help_text='1 — первая замена в этом призовом слоте, 2 — вторая и т.д.',
    )
    mode = models.CharField(
        max_length=10,
        choices=Mode.choices,
        default=Mode.AUTO,
        verbose_name='Способ замены',
    )

    old_participant = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='winner_replacements_out',
        verbose_name='Кого заменили',
    )
    old_receipt = models.ForeignKey(
        Receipt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='winner_replacements_out',
        verbose_name='Чек прежнего победителя',
    )
    old_participant_label = models.CharField(
        max_length=300,
        blank=True,
        verbose_name='Прежний победитель (снимок)',
    )

    new_participant = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='winner_replacements_in',
        verbose_name='Кто стал победителем',
    )
    new_receipt = models.ForeignKey(
        Receipt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='winner_replacements_in',
        verbose_name='Чек нового победителя',
    )
    new_participant_label = models.CharField(
        max_length=300,
        blank=True,
        verbose_name='Новый победитель (снимок)',
    )

    was_eligible = models.BooleanField(
        default=True,
        verbose_name='Новый победитель подходил под условия',
        help_text='Чек нужного периода, нужное число чеков, чек ещё не приносил приз и т.д.',
    )
    eligibility_note = models.CharField(
        max_length=500,
        blank=True,
        verbose_name='Проверка условий',
        help_text='Почему новый победитель подходит или не подходит под условия розыгрыша.',
    )
    reason = models.TextField(
        blank=True,
        verbose_name='Причина замены',
    )

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='winner_replacements_made',
        verbose_name='Кто заменил',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Когда')

    class Meta:
        verbose_name = 'Замена победителя'
        verbose_name_plural = 'Замены победителей'
        ordering = ('-created_at', '-pk')
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(draw_result__isnull=False, main_draw_result__isnull=True)
                    | models.Q(draw_result__isnull=True, main_draw_result__isnull=False)
                ),
                name='winnerreplacement_single_owner',
            ),
        ]

    def __str__(self):
        return f'#{self.order}: {self.old_participant_label} → {self.new_participant_label}'

    @property
    def owner(self):
        return self.draw_result or self.main_draw_result

    @property
    def kind(self) -> str:
        return 'main' if self.main_draw_result_id else 'weekly'


class WinnerReplacementSettings(models.Model):
    """
    Синглтон-настройка замены победителя: срок на подпись договора.

    Пока письмо с договором не отправлено, срок не действует — победителя можно
    заменить в любой момент (нет смысла ждать подпись документа, которого
    победитель ещё не видел). Как только письмо отправлено, отсчитывается
    `sign_deadline_days` дней: в течение этого срока замена заблокирована
    (см. staff_panel.services.winner_replacement.can_replace), чтобы не заменить
    победителя, который ещё успевает подписать договор. 0 — срок отключён,
    замена возможна сразу после отправки письма (до подписи).
    """

    sign_deadline_days = models.PositiveSmallIntegerField(
        default=5,
        verbose_name='Срок на подпись договора, дней',
        help_text='Пока это время не прошло с момента отправки письма с договором, заменить '
                  'победителя нельзя (если письмо ещё не отправлено — замена не ограничена). 0 — без ограничения.',
    )
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Замена победителя · Настройки'
        verbose_name_plural = 'Замена победителя · Настройки'

    def __str__(self):
        return 'Настройки замены победителя'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls) -> 'WinnerReplacementSettings':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


def prize_file_upload_to(instance, filename):
    return f'prize_files/{instance.kind_id}/{uuid.uuid4().hex}/{filename}'


class PrizeFile(models.Model):
    """
    Файл электронного приза (сертификат, промокод и т.п.).

    Имя файла — уникальный идентификатор внутри всей акции: загрузить два файла
    с одинаковым именем нельзя. Файлы загружаются партиями под ВИД приза
    (PrizeKind) и тип розыгрыша, а не под приз конкретной недели: сертификат Ozon
    на 4 000 ₽ одинаково подходит победителю первой и девятой недели. А вот
    еженедельный и ежемесячный розыгрыши одного и того же вида — разные склады:
    под них закупают разные партии. Загруженные файлы лежат «свободными» и
    выдаются победителю вручную через колонку «Файл приза» на вкладке
    «Победители» либо пачкой через авто-раздачу.

    Привязка к итогу розыгрыша — FK с on_delete=SET_NULL: при удалении итога
    розыгрыша файл НЕ удаляется, а освобождается и снова доступен для выдачи.
    """

    class DrawType(models.TextChoices):
        WEEKLY = 'weekly', 'Еженедельный'
        MONTHLY = 'monthly', 'Ежемесячный'
        MAIN = 'main', 'Главный'
        INSTANT = 'instant', 'Моментальный'

    kind = models.ForeignKey(
        PrizeKind,
        on_delete=models.PROTECT,
        related_name='prize_files',
        verbose_name='Вид приза',
    )
    draw_type = models.CharField(
        max_length=10,
        choices=DrawType.choices,
        default=DrawType.WEEKLY,
        db_index=True,
        verbose_name='Тип розыгрыша',
        help_text='Партии под еженедельные и ежемесячные розыгрыши одного и того же приза '
                  'не смешиваются: это разные склады.',
    )
    name = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        verbose_name='Имя файла',
        help_text='Уникальный идентификатор файла приза.',
    )
    file = models.FileField(
        upload_to=prize_file_upload_to,
        verbose_name='Файл',
    )
    size = models.PositiveIntegerField(default=0, verbose_name='Размер, байт')
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name='Загружен')
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='uploaded_prize_files',
        verbose_name='Кто загрузил',
    )

    draw_result = models.ForeignKey(
        PromotionDrawResult,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prize_files',
        verbose_name='Итог розыгрыша',
    )
    main_draw_result = models.ForeignKey(
        PromotionDrawResultMainRaffle,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='prize_files',
        verbose_name='Итог розыгрыша главного приза',
    )
    assigned_at = models.DateTimeField(null=True, blank=True, verbose_name='Выдан')

    class Meta:
        verbose_name = 'Файл электронного приза'
        verbose_name_plural = 'Файлы электронных призов'
        ordering = ('kind_id', 'draw_type', 'name')
        constraints = [
            models.UniqueConstraint(
                fields=['draw_result'],
                condition=models.Q(draw_result__isnull=False),
                name='prizefile_one_per_draw_result',
            ),
            models.UniqueConstraint(
                fields=['main_draw_result'],
                condition=models.Q(main_draw_result__isnull=False),
                name='prizefile_one_per_main_draw_result',
            ),
            models.CheckConstraint(
                condition=models.Q(draw_result__isnull=True) | models.Q(main_draw_result__isnull=True),
                name='prizefile_single_owner',
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_assigned(self) -> bool:
        return bool(self.draw_result_id or self.main_draw_result_id)


class Raffle(models.Model):
    class WeekDay(models.IntegerChoices):
        MONDAY = 0, 'Понедельник'
        TUESDAY = 1, 'Вторник'
        WEDNESDAY = 2, 'Среда'
        THURSDAY = 3, 'Четверг'
        FRIDAY = 4, 'Пятница'
        SATURDAY = 5, 'Суббота'
        SUNDAY = 6, 'Воскресенье'

    start_date = models.DateTimeField(verbose_name='Дата начала')
    first_week_end_date = models.DateField(
        verbose_name='Последний день первого недельного периода',
        null=True,
        blank=True,
        help_text='Первый период регистрации чеков может быть длиннее или короче календарной '
                  'недели. Для этой Акции — 11.10.2026: старт 01.10 (четверг), первый розыгрыш '
                  '14.10 (среда), всего 14 недельных периодов. Пусто — период заканчивается '
                  'ближайшим воскресеньем после старта.',
    )
    week_day = models.IntegerField(verbose_name='День недели розыгрыша', choices=WeekDay.choices)
    month_day = models.PositiveIntegerField(
        verbose_name='День месяца ежемесячного розыгрыша',
        null=True,
        blank=True,
        help_text='Число месяца (1-28), в которое автоматически запускается ежемесячный розыгрыш. '
                   'Пусто — ежемесячный розыгрыш не запускается автоматически.',
    )
    main_raffle_date = models.DateTimeField(verbose_name='Дата главного розыгрыша')
    end_date = models.DateTimeField(verbose_name='Дата окончания')
    is_active = models.BooleanField(verbose_name='Активен', default=True)
    key_last_raffle = models.CharField(max_length=250, verbose_name='Ключ последнего розыгрыша', null=True, blank=True)
    key_last_monthly_raffle = models.CharField(
        max_length=250,
        verbose_name='Ключ последнего ежемесячного розыгрыша',
        null=True,
        blank=True,
    )
    key_reminder_weekly = models.CharField(
        max_length=250,
        verbose_name='Ключ напоминания о еженедельном розыгрыше',
        null=True,
        blank=True,
    )
    key_reminder_monthly = models.CharField(
        max_length=250,
        verbose_name='Ключ напоминания о ежемесячном розыгрыше',
        null=True,
        blank=True,
    )
    key_reminder_main = models.CharField(
        max_length=250,
        verbose_name='Ключ напоминания о главном розыгрыше',
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = 'Розыгрыш'
        verbose_name_plural = 'Розыгрыши'

    def __str__(self):
        return f'Розыгрыш'

    @classmethod
    def get_default_analytics_range(cls):
        """
        Диапазон дат акции (начало/окончание) для дефолтного периода аналитики.
        Берётся из активного (или первого) розыгрыша; None, если розыгрышей нет.
        """
        raffle = cls.objects.filter(is_active=True).first() or cls.objects.first()
        if raffle is None:
            return None, None
        return timezone.localtime(raffle.start_date).date(), timezone.localtime(raffle.end_date).date()


# ============================================================================ #
# Моментальные призы — «лоток яиц».
#
# Механика (бриф, п. 6): за каждый принятый чек участник получает попытку.
# В модалке показывается лоток из десяти яиц; участник открывает одно — внутри
# либо приз, либо пусто. Чем больше принятых чеков, тем больше попыток.
#
# Выигрыш определяется НЕ вероятностью на каждый клик, а расписанием призовых
# моментов (InstantMoment): на каждую единицу моментального приза заранее
# создаётся момент времени, и эти моменты равномерно размазаны по всем дням
# Акции. Первый, кто открывает яйцо после наступления момента, его и забирает.
# Так призовой фонд уходит ровно по графику — примерно поровну каждый день —
# и не зависит ни от трафика, ни от везения генератора случайных чисел.
#
# Подробности алгоритма — promotion/services/instant_prizes.py.
# ============================================================================ #
class InstantPrizeSettings(models.Model):
    """Синглтон-настройки лотка яиц: правит менеджер, не разработчик.

    Значения по умолчанию берутся из settings.INSTANT_PRIZES — так новый стенд
    поднимается без ручной настройки, а боевые значения при этом остаются в БД
    и переживают деплой.
    """

    is_enabled = models.BooleanField(
        'Моментальные призы включены',
        default=True,
        help_text='Выключатель на случай паузы. Уже начисленные попытки сохраняются, '
                  'но сыграть ими нельзя, пока механика выключена.',
    )
    eggs_per_tray = models.PositiveSmallIntegerField(
        'Яиц в лотке',
        default=10,
        help_text='Сколько яиц видит участник. Выбор яйца — оформление: исход попытки '
                  'определяется на сервере в момент клика, «спрятать» приз заранее в '
                  'конкретную ячейку невозможно.',
    )
    attempts_per_receipt = models.PositiveSmallIntegerField(
        'Попыток за один принятый чек',
        default=1,
        help_text='Чем больше принятых чеков, тем больше попыток у участника.',
    )
    day_start_hour = models.PositiveSmallIntegerField(
        'Начало игрового окна, час',
        default=8,
        help_text='Внутри этого окна раскладываются призовые моменты по каждому дню.',
    )
    day_end_hour = models.PositiveSmallIntegerField(
        'Конец игрового окна, час',
        default=23,
    )
    moment_ttl_hours = models.PositiveSmallIntegerField(
        'Срок жизни призового момента, часов',
        default=0,
        help_text='Через сколько часов непойманный момент сгорает. 0 — не сгорает никогда: '
                  'призовой фонд должен быть разыгран полностью.',
    )
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Моментальные призы · Настройки'
        verbose_name_plural = 'Моментальные призы · Настройки'

    def __str__(self):
        return 'Настройки моментальных призов'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls) -> 'InstantPrizeSettings':
        from django.conf import settings as django_settings

        defaults = getattr(django_settings, 'INSTANT_PRIZES', {})
        obj, _ = cls.objects.get_or_create(pk=1, defaults={
            'eggs_per_tray': defaults.get('EGGS_PER_TRAY', 10),
            'attempts_per_receipt': defaults.get('ATTEMPTS_PER_RECEIPT', 1),
            'day_start_hour': defaults.get('DAY_START_HOUR', 8),
            'day_end_hour': defaults.get('DAY_END_HOUR', 23),
            'moment_ttl_hours': defaults.get('MOMENT_TTL_HOURS', 0),
        })
        return obj


class InstantMoment(models.Model):
    """Призовой момент: «в 14:37 такого-то дня разыгрывается вот этот приз».

    Один момент — одна единица приза. Моменты генерируются заранее на всю Акцию
    (команда ``generate_instant_moments`` или кнопка в панели) по призам с
    периодичностью «Моментальный»: у приза указаны неделя и количество, дальше
    количество раскладывается поровну по дням этой недели, а внутри дня — по
    игровому окну. Отсюда и берётся требование «каждый день уходит примерно
    равное количество моментальных призов».

    Момент, до которого никто не доиграл, не сгорает (если не задан ttl) —
    он достаётся первому же участнику, открывшему яйцо после его наступления.
    """

    prize = models.ForeignKey(
        Prize,
        on_delete=models.CASCADE,
        related_name='instant_moments',
        verbose_name='Приз',
    )
    week = models.PositiveIntegerField(
        'Неделя Акции',
        null=True,
        blank=True,
        db_index=True,
        help_text='Неделя, из призов которой сгенерирован момент.',
    )
    scheduled_at = models.DateTimeField(
        'Момент розыгрыша',
        db_index=True,
        help_text='С этого времени приз можно выиграть.',
    )
    is_claimed = models.BooleanField('Разыгран', default=False, db_index=True)
    claimed_at = models.DateTimeField('Когда разыгран', null=True, blank=True)
    participant = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='instant_moments',
        verbose_name='Кому достался',
    )

    class Meta:
        verbose_name = 'Моментальные призы · Призовой момент'
        verbose_name_plural = 'Моментальные призы · Призовые моменты'
        ordering = ('scheduled_at', 'pk')
        indexes = [
            models.Index(fields=['is_claimed', 'scheduled_at']),
        ]

    def __str__(self):
        return f'{self.scheduled_at:%d.%m.%Y %H:%M} — {self.prize_id}'


class InstantAttempt(models.Model):
    """Попытка открыть яйцо: начисляется за принятый чек, тратится в момент клика."""

    participant = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='instant_attempts',
        verbose_name='Участник',
    )
    receipt = models.ForeignKey(
        Receipt,
        on_delete=models.CASCADE,
        related_name='instant_attempts',
        null=True,
        blank=True,
        verbose_name='Чек, давший попытку',
    )
    sequence = models.PositiveSmallIntegerField(
        'Номер попытки по этому чеку',
        default=1,
        help_text='Если за чек даётся несколько попыток — их порядковые номера.',
    )
    created_at = models.DateTimeField('Начислена', auto_now_add=True)

    played_at = models.DateTimeField('Сыграна', null=True, blank=True, db_index=True)
    chosen_egg = models.PositiveSmallIntegerField(
        'Выбранное яйцо', null=True, blank=True,
        help_text='Номер яйца, которое открыл участник (с 1). Только для отчётности: '
                  'на исход попытки выбор не влияет.',
    )
    is_win = models.BooleanField('Выигрышная', default=False, db_index=True)
    moment = models.OneToOneField(
        InstantMoment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='attempt',
        verbose_name='Пойманный призовой момент',
    )

    class Meta:
        verbose_name = 'Моментальные призы · Попытка'
        verbose_name_plural = 'Моментальные призы · Попытки'
        ordering = ('-created_at', '-pk')
        constraints = [
            models.UniqueConstraint(
                fields=['receipt', 'sequence'],
                condition=models.Q(receipt__isnull=False),
                name='instantattempt_unique_per_receipt_sequence',
            ),
        ]
        indexes = [
            models.Index(fields=['participant', 'played_at']),
        ]

    def __str__(self):
        state = 'сыграна' if self.played_at else 'не сыграна'
        return f'Попытка участника {self.participant_id} ({state})'

    @property
    def is_played(self) -> bool:
        return self.played_at is not None


class EmailVerificationCode(models.Model):
    class Purpose(models.TextChoices):
        REGISTRATION = 'registration', 'Регистрация'
        PASSWORD_RESET = 'password_reset', 'Сброс пароля'

    email = models.EmailField(verbose_name='Email', db_index=True)
    code = models.CharField(max_length=6, verbose_name='Код')
    purpose = models.CharField(max_length=32, choices=Purpose.choices, verbose_name='Назначение')
    is_used = models.BooleanField(default=False, verbose_name='Использован')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создан')
    expires_at = models.DateTimeField(verbose_name='Действует до')

    class Meta:
        verbose_name = 'Код подтверждения email'
        verbose_name_plural = 'Коды подтверждения email'
        indexes = [
            models.Index(fields=['email', 'purpose', 'is_used']),
        ]

    def __str__(self):
        return f'{self.email} — {self.get_purpose_display()}'


class AnalyticsDashboard(models.Model):
    """Виртуальная модель — раздел скачивания отчётов в админке (без таблицы в БД)."""

    class Meta:
        managed = False
        verbose_name = 'Аналитика'
        verbose_name_plural = 'Аналитика'
        default_permissions = ()



class SentEmail(models.Model): 
    name = models.CharField('Тема', max_length=255)
    message = models.TextField('Текст', max_length=7000) 
    receipt = models.ForeignKey(verbose_name='Чек', to=Receipt, on_delete=models.CASCADE, related_name='emails') 
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self): 
        return f'{self.receipt} | {self.name}'
    
    class Meta: 
        verbose_name = 'Отправленный Email'
        verbose_name_plural = 'Отправленные email'
        
        
class GuaranteedPrizeSent(models.Model):
    participant = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='guaranteed_prize',
        verbose_name='Участник',
    )
    receipt = models.ForeignKey(
        Receipt,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='guaranteed_prize',
        verbose_name='Чек',
    )
    sent_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата отправки')

    class Meta:
        verbose_name = 'Гарантированный приз'
        verbose_name_plural = 'Гарантированные призы'

    def __str__(self):
        return f'{self.participant} — {self.sent_at:%d.%m.%Y}'


class GuaranteedPrizeEmailTemplate(models.Model):
    """Редактируемые в админке тексты писем о гарантированном призе.

    Плейсхолдеры в фигурных скобках подставляются автоматически при отправке
    (см. promotion/services/guaranteed_prize_email.py): {recipient_name} — имя
    участника, {prize_amount} — сумма приза (например, «50 рублей»). Плейсхолдер,
    которого нет в этом списке, останется в письме как есть — так опечатка в
    названии переменной не помешает подставиться остальным.
    """

    class Code(models.TextChoices):
        REGISTERED = 'registered', 'После регистрации (приз будет отправлен позже)'
        SENT = 'sent', 'Приз фактически отправлен'
        ERROR_FIO = 'error_fio', 'Ошибка отправки — вероятно, из-за ФИО/банка'
        ERROR_BANK_DECLINED = 'error_bank_declined', 'Ошибка отправки — банк отклонил платёж (не ФИО)'

    code = models.CharField(
        max_length=32,
        choices=Code.choices,
        unique=True,
        verbose_name='Тип письма',
    )
    subject = models.CharField(max_length=255, verbose_name='Тема письма')
    text = models.TextField(
        max_length=3000,
        verbose_name='Текст письма',
        help_text='Доступные плейсхолдеры: {recipient_name}, {prize_amount}.',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Текст письма о гарантированном призе'
        verbose_name_plural = 'Тексты писем о гарантированном призе'
        ordering = ['code']

    def __str__(self):
        return self.get_code_display()


class OkiDokiTemplate(models.Model):
    class Kind(models.TextChoices):
        """
        Категория приза, для которой используется шаблон. Шаблон подбирается
        автоматически (staff_panel.services.oki_templates.resolve_template_for_row):
        главный приз — свой шаблон, остальные — по стоимости приза относительно
        порога НДФЛ в 4000 ₽.
        """
        SMALL = 'small', 'Приз до 4 000 ₽ включительно'
        LARGE = 'large', 'Приз дороже 4 000 ₽ (с НДФЛ)'
        MAIN = 'main', 'Главный приз'

    kind = models.CharField(
        max_length=16,
        choices=Kind.choices,
        default=Kind.SMALL,
        db_index=True,
        verbose_name='Для какой категории приза',
        help_text='По этому полю шаблон подбирается автоматически. На каждую категорию '
                  'должен быть ровно один активный шаблон.',
    )
    name = models.CharField(max_length=255, verbose_name='Название шаблона')
    oki_template_id = models.CharField(
        max_length=255,
        verbose_name='ID шаблона в OkiDoki',
        help_text='Например, 69897f66bb27c395d9bb0bae',
    )
    url = models.URLField(max_length=1000, verbose_name='Ссылка на шаблон')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Шаблон договора OkiDoki'
        verbose_name_plural = 'Шаблоны договоров OkiDoki'
        ordering = ['name']

    def __str__(self):
        return self.name
    
    
class ReceiptMessageTemplate(models.Model):
    class Code(models.TextChoices):
        ACCEPTED = 'accepted', 'Принят к участию'
        WINNER = 'winner', 'Победный чек'
        PENDING = 'pending', 'На проверке'
        REJECTED_ITEMS_MISMATCH = 'rejected_items_mismatch', 'Отклонён: товары не соответствуют условиям акции'
        REJECTED_DATE_INVALID = 'rejected_date_invalid', 'Отклонён: дата покупки вне периода акции'
        REJECTED_QR_DECODE_FAILED = 'rejected_qr_decode_failed', 'Отклонён: не удалось распознать QR-код'
        REJECTED_FNS_NOT_CONFIRMED = 'rejected_fns_not_confirmed', 'Отклонён: ФНС не подтвердил чек'
        REJECTED_DUPLICATE = 'rejected_duplicate', 'Отклонён: чек уже зарегистрирован'
        REJECTED_STORE_NOT_FOUND = 'rejected_store_not_found', 'Отклонён: магазин не участвует в акции'
        REJECTED_PROMO_SUM_TOO_LOW = 'rejected_promo_sum_too_low', 'Отклонён: совокупная стоимость акционных товаров в чеке ниже порога'
        INSUFFICIENT_DATA = 'insufficient_data', 'Недостаточно данных'

    code = models.CharField(
        max_length=64,
        choices=Code.choices,
        unique=True,
        verbose_name='Код события',
    )
    text = models.TextField(
        verbose_name='Текст сообщения',
        max_length=500,
        help_text=(
            'Можно использовать плейсхолдеры в фигурных скобках, например {week_num} или {prize_name} — '
            'они подставляются автоматически при формировании сообщения в коде.'
        ),
    )

    class Meta:
        verbose_name = 'Шаблон сообщения чека'
        verbose_name_plural = 'Шаблоны сообщений чеков'
        ordering = ['code']

    def __str__(self):
        return self.get_code_display()


class PrizeShippingSoonMessage(models.Model):
    """Синглтон: текст письма «приз скоро будет отправлен»."""

    text = models.TextField(
        verbose_name='Текст письма',
        max_length=3000,
        default='Здравствуйте! Ваш приз скоро будет отправлен.',
        # help_text=(
        #     'Можно использовать плейсхолдеры в фигурных скобках, например {prize_name} — '
        #     'подставляется автоматически.'
        # ),
    )
    subject = models.CharField(
        max_length=255,
        verbose_name='Тема письма',
        default='Ваш приз скоро будет отправлен',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Текст письма «Приз скоро будет отправлен»'
        verbose_name_plural = 'Текст письма «Приз скоро будет отправлен»'

    def __str__(self):
        return 'Настройки письма о скорой отправке приза'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls) -> 'PrizeShippingSoonMessage':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class AccountBlockedMessage(models.Model):
    """Синглтон: текст модального окна, которое видит заблокированный участник при попытке загрузить чек."""

    text = models.TextField(
        verbose_name='Текст сообщения',
        max_length=1000,
        default=(
            'Мы приостановили загрузку новых чеков на этом аккаунте — наша система отметила '
            'подозрительную активность. Чтобы разобраться и снять ограничение, напишите в поддержку.'
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Текст сообщения о блокировке аккаунта'
        verbose_name_plural = 'Текст сообщения о блокировке аккаунта'

    def __str__(self):
        return 'Настройки сообщения о блокировке аккаунта'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls) -> 'AccountBlockedMessage':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class UTMVisit(models.Model):
    utm_medium = models.CharField(max_length=255, blank=True, default='', verbose_name='utm_medium')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Дата визита')

    class Meta:
        verbose_name = 'UTM визит'
        verbose_name_plural = 'UTM визиты'

    def __str__(self):
        return f'{self.utm_medium or "(без метки)"} — {self.created_at}'


class SbpBank(models.Model):
    """Справочник банков-участников СБП (метод Cyclops list_bank_sbp).

    Нужен, чтобы по БИК банка из профиля участника (`User.bank_bik`) получить
    идентификатор банка в СБП (`bank_sbp_id`) для выплаты гарантированного приза.
    """

    sbp_id = models.CharField('ID банка в СБП', max_length=32, unique=True)
    bank_code = models.CharField('БИК', max_length=9, db_index=True)
    name = models.CharField('Название (лат.)', max_length=255, blank=True)
    name_rus = models.CharField('Наименование', max_length=255, blank=True)
    effective_date = models.CharField('Действует на дату', max_length=32, blank=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Банк СБП'
        verbose_name_plural = 'Банки СБП'
        ordering = ['name_rus']

    def __str__(self):
        return f'{self.name_rus or self.name} (БИК {self.bank_code} / {self.sbp_id})'

    @classmethod
    def resolve_sbp_id(cls, bank_code: str):
        """Вернуть bank_sbp_id по БИК или None, если банк не найден в справочнике."""
        if not bank_code:
            return None
        bank = cls.objects.filter(bank_code=str(bank_code).strip()).first()
        return bank.sbp_id if bank else None


class PayoutRegistry(models.Model):
    """Реестр перечисления денежных средств Победителям Акции — Excel-документ
    по форме, согласованной с ООО «Хелиос» (docs/Форма_Реестра_выплат_Excel.xlsx).

    Один реестр может покрывать многих участников (например, всех, кто
    накопился за неделю) — им всем при выплате прикладывается один и тот же
    файл как service_agreement. См. promotion/services/cyclops_registry.py.
    """

    registry_number = models.CharField('Номер реестра', max_length=50, blank=True)
    file = models.FileField('Файл реестра (xlsx)', upload_to='cyclops_registries/')
    created_at = models.DateTimeField('Создан', auto_now_add=True)

    class Meta:
        verbose_name = 'Cyclops · Реестр выплат'
        verbose_name_plural = 'Cyclops · Реестры выплат'
        ordering = ['-created_at']

    def __str__(self):
        return f'Реестр №{self.registry_number or self.pk} от {self.created_at:%d.%m.%Y}'


class GuaranteedPrizePayout(models.Model):
    """Трекер автоматической выплаты гарантированного приза 50₽ через СБП.

    Одна запись на участника (гарантия однократной выплаты). Управляет
    статусами и авто-ретраями в Celery. См. docs/cyclops-integration-plan.md
    """

    STATUS_HOLD = 'hold'
    STATUS_NEW = 'new'
    STATUS_PROCESSING = 'processing'
    STATUS_EXECUTING = 'executing'
    STATUS_PAID = 'paid'
    STATUS_FAILED = 'failed'
    STATUS_RETRYABLE = 'retryable'
    STATUS_RETRY_LIMIT = 'retry_limit'
    # Технические статусы движка выплат. Для людей (панель персонала) они
    # огрублены и переименованы: один STATUS_HOLD означает сразу и «ещё ни разу
    # не отправляли», и «банк отклонил, повторим в пятницу» — поэтому дашборд
    # показывает не их, а производные «этапы» (см. staff_panel/services/payouts.py).
    STATUS_CHOICES = [
        (STATUS_HOLD, 'В очереди на выплату (ближайшая пятница)'),
        (STATUS_NEW, 'Новая'),
        (STATUS_PROCESSING, 'В обработке'),
        (STATUS_EXECUTING, 'Отправлена в банк'),
        (STATUS_PAID, 'Выплачена'),
        (STATUS_FAILED, 'Не выплачена — нужна проверка'),
        (STATUS_RETRYABLE, 'Техническая ошибка — авто-повтор'),
        (STATUS_RETRY_LIMIT, 'Исчерпан лимит авто-повторов'),
    ]

    # Статусы, при которых задача авто-ретраев берёт запись в повторную обработку.
    # STATUS_HOLD сюда намеренно не входит: выплата поставлена в очередь, но ждёт
    # ручного/планового запуска (см. ensure_guaranteed_prize_payout) — авто-ретраи
    # её не подхватывают, пока кто-то не переведёт её в STATUS_RETRYABLE вручную
    # (например, действием «Повторить выплату» в админке).
    # STATUS_FAILED — терминальный (повтор только вручную из админки или при
    # пере-сохранении профиля участником).
    # STATUS_RETRY_LIMIT — тоже терминальный: авто-повторов израсходовано
    # CYCLOPS_MAX_PAYOUT_RETRIES подряд, дальше система сама не пробует, чтобы
    # не долбиться в банк бесконечно. Ручной запуск обнуляет счётчик попыток.
    RETRIABLE_STATUSES = (STATUS_NEW, STATUS_RETRYABLE)
    # Статусы «в работе/завершено» — при них новую выплату не запускаем
    IN_FLIGHT_STATUSES = (STATUS_PAID, STATUS_PROCESSING, STATUS_EXECUTING)
    # Статусы, из которых выплату может вытащить только человек (кнопка
    # «Повторить» в панели/админке) — авто-ретраи их не подхватывают.
    MANUAL_ONLY_STATUSES = (STATUS_FAILED, STATUS_RETRY_LIMIT)

    participant = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='guaranteed_prize_payout',
        verbose_name='Участник',
    )
    amount = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        verbose_name='Сумма',
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_NEW,
        db_index=True,
        verbose_name='Статус',
    )
    deal_id = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        db_index=True,
        verbose_name='ID сделки в Cyclops',
    )
    document = models.ForeignKey(
        'CyclopsDocument',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Документ-основание (сгенерирован по шаблону)',
    )
    registry = models.ForeignKey(
        'PayoutRegistry',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payouts',
        verbose_name='Реестр выплат',
    )
    bank_sbp_id = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        verbose_name='ID банка в СБП',
    )
    phone_number = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        verbose_name='Телефон (нормализованный)',
    )
    cnt_retry = models.IntegerField(
        default=0,
        verbose_name='Количество попыток',
    )
    error_reason = models.TextField(
        null=True,
        blank=True,
        verbose_name='Причина ошибки',
    )
    paid_email_sent = models.BooleanField(
        default=False,
        verbose_name='Письмо «приз отправлен» отправлено',
        help_text='Отправляется автоматически, как только сделка подтверждена банком '
                   '(status=paid) — фоновым опросом или ретраем. Также можно отправить '
                   'вручную со страницы этой выплаты (см. GuaranteedPrizePayoutAdmin).',
    )
    error_email_sent = models.BooleanField(
        default=False,
        verbose_name='Письмо об ошибке отправки отправлено',
        help_text='Автоматически отправляется только для выплат, запущенных плановой '
                   'фоновой задачей — см. sent_manually. Для выплат, запущенных вручную, '
                   'письмо шлётся только вручную со страницы этой выплаты.',
    )
    error_bank_email_sent = models.BooleanField(
        default=False,
        verbose_name='Письмо «банк отклонил платёж» отправлено',
        help_text='Отправляется автоматически при отказе банка НЕ по причине ФИО/реквизитов '
                   '(см. guaranteed_prize_payout.apply_deal_status) — только для выплат, '
                   'запущенных плановой фоновой задачей, см. sent_manually. Для выплат, '
                   'запущенных вручную, письмо шлётся только вручную со страницы этой выплаты.',
    )
    sent_manually = models.BooleanField(
        default=False,
        verbose_name='Запущена вручную',
        help_text='Ставится, когда выплату запускает кнопка «Провести N выплат» в '
                   'админке (а не плановая фоновая задача). Для таких выплат письма об '
                   'итоге (успех/ошибка ФИО) НЕ отправляются автоматически — см. '
                   'guaranteed_prize_payout.apply_deal_status — их шлют вручную со '
                   'страницы выплаты.',
    )
    api_log = models.TextField(
        'Лог обращений в Точку', blank=True, default='',
        help_text='Что именно отправлялось и что ответила Точка — для разбора постфактум '
                   '(см. ControlPayout.api_log — тот же формат).',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создана')
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name='Отправлена в банк')
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name='Подтверждена выплата')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='Обновлена')

    class Meta:
        verbose_name = 'Выплата гарантированного приза'
        verbose_name_plural = 'Выплаты гарантированного приза'
        ordering = ['-created_at']

    def __str__(self):
        return f'Выплата {self.amount}₽ участнику {self.participant_id} — {self.get_status_display()}'

    def log(self, message):
        """Дописать строку в журнал обращений, не затирая предыдущие."""
        stamp = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
        self.api_log = f'{self.api_log}[{stamp}] {message}\n'


class GuaranteedPrizePayoutEmail(models.Model):
    """Письмо, отправленное по конкретной выплате гарантированного приза.

    Вся привязка писем к выплатам делается именно к этой записи (не к
    участнику и не к чеку) — история переотправок видна прямо в карточке
    выплаты в админке (см. GuaranteedPrizePayoutAdmin).
    """

    payout = models.ForeignKey(
        GuaranteedPrizePayout,
        on_delete=models.CASCADE,
        related_name='emails',
        verbose_name='Выплата',
    )
    subject = models.CharField('Тема', max_length=255)
    message = models.TextField('Текст', max_length=7000, blank=True)
    sent_at = models.DateTimeField('Отправлено', auto_now_add=True)

    class Meta:
        verbose_name = 'Письмо по выплате гарантированного приза'
        verbose_name_plural = 'Письма по выплатам гарантированного приза'
        ordering = ['-sent_at']

    def __str__(self):
        return f'{self.payout} | {self.subject}'


# ============================================================================ #
# Cyclops (Точка Банк): бенефициары, счета, платежи, документы, настройки.
# Данные интерфейса управления сервисом хранятся в БД. См. staff_panel.
# ============================================================================ #
class CyclopsBeneficiary(models.Model):
    """Бенефициар (владелец денег на номинальном счёте)."""

    TYPE_UL = 'ul'
    TYPE_IP = 'ip'
    TYPE_CHOICES = [
        (TYPE_UL, 'Юридическое лицо'),
        (TYPE_IP, 'ИП / Физлицо'),
    ]

    beneficiary_id = models.CharField('ID в Cyclops', max_length=255, unique=True, null=True, blank=True)
    beneficiary_type = models.CharField('Тип', max_length=2, choices=TYPE_CHOICES, default=TYPE_UL)
    legal_type = models.CharField('Тип клиента (API)', max_length=2, null=True, blank=True,
                                  help_text='F — физлицо, I — ИП, J — юрлицо')
    name = models.CharField('Наименование', max_length=255)
    inn = models.CharField('ИНН', max_length=12)
    kpp = models.CharField('КПП', max_length=9, null=True, blank=True)
    ogrn = models.CharField('ОГРН', max_length=15, null=True, blank=True)
    first_name = models.CharField('Имя', max_length=250, null=True, blank=True)
    last_name = models.CharField('Фамилия', max_length=250, null=True, blank=True)
    middle_name = models.CharField('Отчество', max_length=250, null=True, blank=True)
    nominal_account_code = models.CharField('Номинальный счёт', max_length=30, null=True, blank=True)
    nominal_account_bic = models.CharField('БИК номинального счёта', max_length=9, null=True, blank=True)
    permission = models.BooleanField('Разрешены операции', null=True, blank=True)
    permission_description = models.TextField('Причина запрета операций', null=True, blank=True)
    is_added_to_ms = models.BooleanField('Добавлен в мастер-систему банка', default=False)
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлён', auto_now=True)

    # --- Реквизиты для автогенерации Договора присоединения к Оферте (contract_offer) ---
    # Заполняются перед регистрацией, если документ должен быть сгенерирован
    # автоматически, а не загружен вручную уже подписанным. См.
    # promotion/services/cyclops_documents.py::generate_contract_offer_document
    legal_address = models.CharField('Юридический адрес', max_length=500, null=True, blank=True)
    bank_account = models.CharField('Р/с Заказчика', max_length=20, null=True, blank=True)
    bank_name = models.CharField('Банк Заказчика', max_length=255, null=True, blank=True)
    bank_bic = models.CharField('БИК банка Заказчика', max_length=9, null=True, blank=True)
    bank_corr_account = models.CharField('К/с банка Заказчика', max_length=20, null=True, blank=True)
    contact_email = models.EmailField('Email для документов', max_length=255, null=True, blank=True)
    signatory_name = models.CharField(
        'ФИО подписанта', max_length=255, null=True, blank=True,
        help_text='В родительном падеже — как в договоре: «в лице Иванова Ивана Ивановича»',
    )
    signatory_basis = models.CharField(
        'Основание полномочий подписанта', max_length=255, null=True, blank=True,
        default='Устава', help_text='Например: Устава, Доверенности № ... от ...',
    )
    # Вознаграждение платформы (ООО «Хелиос») по Договору присоединения — заполняется
    # ровно одно из двух: либо процент (с необязательным минимумом), либо фиксированная сумма.
    commission_percent = models.DecimalField(
        'Вознаграждение, %', max_digits=5, decimal_places=2, null=True, blank=True,
    )
    commission_min_amount = models.DecimalField(
        'Минимальное вознаграждение за акцию, ₽', max_digits=12, decimal_places=2, null=True, blank=True,
    )
    commission_fixed_amount = models.DecimalField(
        'Фиксированное вознаграждение, ₽', max_digits=12, decimal_places=2, null=True, blank=True,
    )

    class Meta:
        verbose_name = 'Cyclops · Бенефициар'
        verbose_name_plural = 'Cyclops · Бенефициары'
        ordering = ['name']

    def __str__(self):
        return f'{self.name} (ИНН {self.inn})'

    @property
    def has_contract_offer_details(self):
        """Достаточно ли данных, чтобы сгенерировать Договор присоединения
        автоматически (без ручной загрузки уже подписанного файла)."""
        has_commission = bool(self.commission_fixed_amount or self.commission_percent)
        return bool(
            self.name and self.inn and self.kpp and self.legal_address
            and self.bank_account and self.bank_name and self.bank_bic and self.bank_corr_account
            and self.signatory_name and has_commission
        )


class CyclopsVirtualAccount(models.Model):
    """Виртуальный счёт бенефициара (баланс средств)."""

    beneficiary = models.ForeignKey(
        CyclopsBeneficiary, on_delete=models.CASCADE,
        related_name='virtual_accounts', null=True, blank=True,
        verbose_name='Бенефициар',
    )
    virtual_account_id = models.CharField('ID счёта', max_length=255, unique=True)
    account_name = models.CharField('Название', max_length=500, null=True, blank=True)
    account_type = models.CharField('Тип счёта', max_length=20, null=True, blank=True,
                                    help_text='standard / for_ndfl')
    available_balance = models.DecimalField('Доступно', max_digits=15, decimal_places=2, default=0)
    blocked_balance = models.DecimalField('Заблокировано', max_digits=15, decimal_places=2, default=0)
    is_active = models.BooleanField('Активен', default=True)
    updated_at = models.DateTimeField('Обновлён', auto_now=True)

    class Meta:
        verbose_name = 'Cyclops · Виртуальный счёт'
        verbose_name_plural = 'Cyclops · Виртуальные счета'

    def __str__(self):
        return f'Счёт {self.virtual_account_id} ({self.available_balance}₽)'


class CyclopsPayment(models.Model):
    """Входящий платёж на номинальный счёт."""

    payment_id = models.CharField('ID платежа', max_length=100, unique=True)
    amount = models.DecimalField('Сумма', max_digits=15, decimal_places=2, null=True, blank=True)
    status = models.CharField('Статус', max_length=100, default='new')
    purpose = models.TextField('Назначение', null=True, blank=True)
    type = models.CharField('Тип', max_length=200, null=True, blank=True)
    identify = models.BooleanField('Идентифицирован', default=False)
    deal_id = models.CharField('ID сделки', max_length=500, null=True, blank=True)
    payer_name = models.CharField('Плательщик', max_length=250, null=True, blank=True)
    payer_inn = models.CharField('ИНН плательщика', max_length=100, null=True, blank=True)
    payer_account = models.CharField('Счёт плательщика', max_length=200, null=True, blank=True)
    payer_bic = models.CharField('БИК плательщика', max_length=100, null=True, blank=True)
    beneficiary = models.ForeignKey(
        CyclopsBeneficiary, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='payments', verbose_name='Идентифицирован на бенефициара',
    )
    created_at = models.DateTimeField('Дата платежа', null=True, blank=True)
    synced_at = models.DateTimeField('Синхронизирован', auto_now=True)

    class Meta:
        verbose_name = 'Cyclops · Платёж'
        verbose_name_plural = 'Cyclops · Платежи'
        ordering = ['-created_at']

    def __str__(self):
        return f'Платёж {self.payment_id} — {self.amount}₽'


class CyclopsDocument(models.Model):
    """Документ, загруженный в Cyclops (по бенефициару или по сделке)."""

    TYPE_CONTRACT_OFFER = 'contract_offer'
    TYPE_SERVICE_AGREEMENT = 'service_agreement'
    TYPE_CHOICES = [
        (TYPE_CONTRACT_OFFER, 'Договор оферты (бенефициар)'),
        (TYPE_SERVICE_AGREEMENT, 'Договор оказания услуг (сделка)'),
    ]

    beneficiary = models.ForeignKey(
        CyclopsBeneficiary, on_delete=models.CASCADE, null=True, blank=True,
        related_name='documents', verbose_name='Бенефициар',
    )
    deal_id = models.CharField('ID сделки', max_length=100, null=True, blank=True)
    document_type = models.CharField('Тип документа', max_length=32, choices=TYPE_CHOICES)
    file = models.FileField('Файл', upload_to='cyclops_documents/')
    document_id = models.CharField('ID документа в Cyclops', max_length=255, null=True, blank=True)
    document_number = models.CharField('Номер документа', max_length=50, default='001')
    document_date = models.DateField('Дата документа', null=True, blank=True)
    uploaded = models.BooleanField('Загружен в Cyclops', default=False)
    created_at = models.DateTimeField('Создан', auto_now_add=True)

    class Meta:
        verbose_name = 'Cyclops · Документ'
        verbose_name_plural = 'Cyclops · Документы'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_document_type_display()} — {self.file.name}'


class CyclopsSettings(models.Model):
    """Синглтон-настройки Cyclops: активный плательщик призового фонда и шаблон документа.

    Хранится в БД (не в env). Приложение работает и без заполнения — выплаты
    просто не будут выполняться, пока плательщик не настроен.
    """

    payout_beneficiary = models.ForeignKey(
        CyclopsBeneficiary, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='Бенефициар-плательщик (призовой фонд)',
    )
    payout_virtual_account = models.ForeignKey(
        CyclopsVirtualAccount, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='Виртуальный счёт для выплат',
    )
    service_agreement = models.ForeignKey(
        CyclopsDocument, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='Шаблон «Договор оказания услуг» для выплат',
    )
    payout_enabled = models.BooleanField(
        'Автоматические выплаты гарантированного приза включены', default=True,
        help_text='Выключатель на случай паузы (проверка документов, нехватка фонда и т.п.) — '
                   'без удаления настроек плательщика. Уже поставленные в очередь выплаты '
                   'дождутся включения и уйдут сами.',
    )
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        verbose_name = 'Cyclops · Настройки'
        verbose_name_plural = 'Cyclops · Настройки'

    def __str__(self):
        return 'Настройки Cyclops'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass

    @classmethod
    def load(cls) -> 'CyclopsSettings':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def is_payout_configured(self) -> bool:
        return bool(
            self.payout_virtual_account_id
            and self.payout_beneficiary_id
        )

class ControlPayout(models.Model):
    """Контрольная (тестовая) выплата по реестру, подготовленному вручную.

    Отдельная сущность от `GuaranteedPrizePayout`: получатели здесь — не
    участники акции, а конкретные люди из заранее согласованного файла реестра,
    и штатный флоу выплат гарантированного приза эти записи не видит и не
    трогает. Нужна, чтобы после прогона команды было видно, что произошло с
    каждой выплатой: какая сделка создана, дошли ли деньги, что ответила Точка.
    """

    STATUS_NEW = 'new'
    STATUS_EXECUTING = 'executing'
    STATUS_PAID = 'paid'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_NEW, 'Создана (не отправлена)'),
        (STATUS_EXECUTING, 'Отправлена в банк'),
        (STATUS_PAID, 'Выплачена'),
        (STATUS_FAILED, 'Ошибка'),
    ]

    batch = models.CharField(
        'Прогон', max_length=64, db_index=True,
        help_text='Метка одного запуска команды — все выплаты одного реестра имеют общую метку.',
    )
    row_number = models.PositiveIntegerField('№ строки в реестре', default=0)

    last_name = models.CharField('Фамилия', max_length=150)
    first_name = models.CharField('Имя', max_length=150)
    middle_name = models.CharField('Отчество', max_length=150, blank=True)
    phone = models.CharField('Телефон (нормализованный)', max_length=20)

    bank_name_source = models.CharField('Банк (как указан в реестре)', max_length=255)
    bank_name = models.CharField('Банк (из справочника СБП)', max_length=255, blank=True)
    bank_bik = models.CharField('БИК', max_length=9, blank=True)
    bank_sbp_id = models.CharField('ID банка в СБП', max_length=32, blank=True)

    amount = models.DecimalField('Сумма', max_digits=15, decimal_places=2)
    purpose = models.CharField('Назначение платежа', max_length=255)

    registry_file = models.FileField(
        'Файл реестра', upload_to='cyclops_control_registries/', null=True, blank=True,
        help_text='Тот самый файл, который приложен к сделке как service_agreement.',
    )
    beneficiary_id = models.CharField('Бенефициар-плательщик', max_length=100, blank=True)
    virtual_account = models.CharField('Виртуальный счёт плательщика', max_length=100, blank=True)

    deal_id = models.CharField('ID сделки в Cyclops', max_length=100, null=True, blank=True, db_index=True)
    document_id = models.CharField('ID документа в Cyclops', max_length=255, null=True, blank=True)
    status = models.CharField('Статус', max_length=20, choices=STATUS_CHOICES,
                              default=STATUS_NEW, db_index=True)
    error_reason = models.TextField('Причина ошибки', null=True, blank=True)
    api_log = models.TextField(
        'Лог обращений в Точку', blank=True, default='',
        help_text='Что именно отправлялось и что ответила Точка — для разбора постфактум.',
    )

    created_at = models.DateTimeField('Создана', auto_now_add=True)
    sent_at = models.DateTimeField('Отправлена в банк', null=True, blank=True)
    paid_at = models.DateTimeField('Подтверждена выплата', null=True, blank=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        verbose_name = 'Cyclops · Контрольная выплата'
        verbose_name_plural = 'Cyclops · Контрольные выплаты'
        ordering = ['-created_at', 'row_number']

    def __str__(self):
        return f'{self.fio} — {self.amount}₽ ({self.get_status_display()})'

    @property
    def fio(self):
        return ' '.join(p for p in (self.last_name, self.first_name, self.middle_name) if p)

    def log(self, message):
        """Дописать строку в журнал обращений, не затирая предыдущие."""
        stamp = timezone.now().strftime('%Y-%m-%d %H:%M:%S')
        self.api_log = f'{self.api_log}[{stamp}] {message}\n'
