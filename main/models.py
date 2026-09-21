"""Контент публичной части Акции «Купи и выиграй с Атемарской».

Лендинг собирается из готовых секций (templates/partials/sections/), а тексты
этих секций лежат здесь — в БД, а не в вёрстке. Устройство то же, что в
PromoTemplate: ContentBlock хранит заголовок/подзаголовок/текст одной секции,
ParticipationStep — шаги механики, FAQ — вопросы и ответы, витрины призов и
товаров — отдельные модели. Ни одного текста конкретной акции в шаблонах нет:
менеджер правит всё в админке, не трогая код.
"""

from django.db import models


class SectionCode(models.TextChoices):
    """Секции главной страницы. Код секции = имя файла в templates/partials/sections/."""

    HERO = 'hero', 'Первый экран'
    HOW_TO_PARTICIPATE = 'how_to_participate', 'Как участвовать'
    INSTANT_PRIZES = 'instant_prizes', 'Моментальные призы'
    WEEKLY_PRIZES = 'weekly_prizes', 'Еженедельные призы'
    MONTHLY_PRIZES = 'monthly_prizes', 'Ежемесячные призы'
    MAIN_PRIZE = 'main_prize', 'Главный приз'
    PRODUCTS = 'products', 'Товары акции'
    WINNERS = 'winners', 'Победители'
    RULES = 'rules', 'Условия участия'
    FAQ = 'faq', 'Вопросы и ответы'
    DOCUMENTS = 'documents', 'Документы'
    CONTACTS = 'contacts', 'Контакты'


class ContentBlock(models.Model):
    """Тексты одной секции главной страницы."""

    code = models.CharField(
        max_length=50,
        choices=SectionCode.choices,
        unique=True,
        verbose_name='Секция',
    )
    title = models.CharField(max_length=255, blank=True, verbose_name='Заголовок')
    subtitle = models.CharField(max_length=500, blank=True, verbose_name='Подзаголовок')
    body = models.TextField(blank=True, verbose_name='Текст')
    image = models.ImageField(
        upload_to='content/', null=True, blank=True, verbose_name='Изображение',
    )
    is_visible = models.BooleanField(
        default=True,
        verbose_name='Показывать',
        help_text='Снятая галочка убирает секцию с лендинга — удалять её из шаблона не нужно.',
    )
    order = models.PositiveIntegerField(default=0, verbose_name='Порядок')

    class Meta:
        verbose_name = 'Блок главной страницы'
        verbose_name_plural = 'Блоки главной страницы'
        ordering = ['order', 'code']

    def __str__(self):
        return self.get_code_display()


class ParticipationStep(models.Model):
    """Шаг механики в секции «Как участвовать»."""

    number = models.PositiveIntegerField(verbose_name='Номер шага')
    title = models.CharField(max_length=255, verbose_name='Заголовок')
    text = models.TextField(blank=True, verbose_name='Описание')
    icon = models.ImageField(
        upload_to='content/steps/', null=True, blank=True, verbose_name='Иконка',
    )

    class Meta:
        verbose_name = 'Шаг участия'
        verbose_name_plural = 'Шаги участия'
        ordering = ['number']

    def __str__(self):
        return f'{self.number}. {self.title}'


class FAQ(models.Model):
    question = models.CharField(
        max_length=200,
        verbose_name="Вопрос",
    )

    answer = models.TextField(
        max_length=1000,
        verbose_name="Ответ",
    )

    order = models.PositiveIntegerField(
        default=1,
        verbose_name="Порядок",
        help_text="Порядок отображения"
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата создания"
    )

    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Дата обновления"
    )

    class Meta:
        verbose_name = "Вопрос-ответ"
        verbose_name_plural = "Вопросы-ответы"
        ordering = ['order']

    def __str__(self):
        return self.question[:50] + "..." if len(self.question) > 50 else self.question


class CompanyInfo(models.Model):
    organization_name = models.CharField('Название организации', max_length=255, default='ООО «ЧНГ»')
    customer_name = models.CharField(
        'Заказчик работ', max_length=255, default='АО «Птицефабрика «Атемарская»',
        help_text='Если отличается от организатора акции (бриф, п. 1).',
    )
    brand_name = models.CharField(
        'Торговая марка', max_length=255, default='Атемарская Ферма',
        help_text='Бренд, от имени которого идёт акция.',
    )
    promotion_name = models.CharField(
        'Название акции', max_length=255, default='Купи и выиграй с Атемарской',
    )
    landing_url = models.CharField(
        'Адрес лендинга', max_length=255, default='https://af-promo.ru',
        help_text='Лендинг живёт на отдельном домене, личный кабинет ссылается на него.',
    )
    cheqly_link = models.CharField('Ссылка на CheqLy', max_length=255, default='cheqly.ru')
    footer_text = models.TextField(
        'Текст в футере',
        default=(
            'Срок проведения акции с 1 октября 2026 года по 31 января 2027 года. '
            'Период совершения покупки и регистрации чеков — с 1 октября 2026 года '
            'по 10 января 2027 года. Внешний вид призов может отличаться от изображений '
            'в рекламных материалах. Информацию об Организаторе акции, о правилах её '
            'проведения, количестве призов, сроке, месте и порядке их получения можно '
            'узнать на сайте af-promo.ru'
        ),
    )
    privacy_policy = models.FileField('Обработка персональных данных', upload_to='personal_data/', null=True, blank=True)
    personal_data_consent = models.FileField('Согласие на обработку персональных данных', upload_to='personal_data_consent/', null=True, blank=True)
    rules = models.FileField('Правила акции', upload_to='rules/', null=True, blank=True)
    products_list = models.FileField('Файл со всеми товарами', null=True, blank=True)
    email = models.EmailField('Email', max_length=50, default='af@cheqly.ru', help_text='Для обратной связи от клиентов')

    # Реквизиты Организатора (ООО «ЧНГ»). Точные значения заполняет менеджер в
    # админке до старта Акции — в коде их нет намеренно, чтобы не расходиться
    # с уставными документами.
    legal_address = models.CharField('Юридический адрес', max_length=255, blank=True, default='')
    inn = models.CharField('ИНН', max_length=20, blank=True, default='')
    kpp = models.CharField('КПП', max_length=20, blank=True, default='')
    settlement_account = models.CharField('Расчётный счёт (р/с)', max_length=30, blank=True, default='')
    correspondent_account = models.CharField('Корреспондентский счёт (к/с)', max_length=30, blank=True, default='')
    bik = models.CharField('БИК', max_length=20, blank=True, default='')
    bank_name = models.CharField('Наименование банка', max_length=255, blank=True, default='')
    ogrn = models.CharField('ОГРН', max_length=20, blank=True, default='')

    seo_production_mode = models.BooleanField(
        'Боевой режим SEO (robots.txt и sitemap.xml)',
        default=False,
        help_text='Выключено — сайт ещё не индексируется: sitemap.xml недоступен, '
                   'ссылка на него в robots.txt закомментирована (состояние "до начала акции"). '
                   'Включите перед стартом акции — sitemap.xml заработает, а в robots.txt '
                   'раскомментируется ссылка на него.'
    )

    def __str__(self):
        return 'Информация о компании'

    class Meta:
        verbose_name = 'Информация о компании'
        verbose_name_plural = 'Информация о компании'

    @classmethod
    def get_instance(cls) -> "CompanyInfo":
        instance, created = cls.objects.get_or_create(id=1)
        return instance


    def save(self, *args, **kwargs):
        if self.__class__.objects.count():
            self.pk = self.__class__.objects.first().pk
        super().save(*args, **kwargs)


class SEOSettings(models.Model):
    title = models.CharField(
        'Заголовок по умолчанию (title)',
        max_length=255,
        default='Купи и выиграй с Атемарской',
        help_text='Используется, если у страницы не задан свой заголовок'
    )
    description = models.TextField(
        'Описание по умолчанию (description)',
        default='Покупайте продукцию «Атемарской Фермы», регистрируйте чеки и выигрывайте призы: сертификаты Ozon, аэрогрили и холодильники',
        help_text='Используется, если у страницы не задано своё описание'
    )
    og_image = models.ImageField(
        'Изображение для соцсетей (og:image)',
        upload_to='seo/',
        null=True,
        blank=True,
        help_text='Если не задано — используется картинка по умолчанию из статики'
    )

    def __str__(self):
        return 'SEO-настройки'

    class Meta:
        verbose_name = 'SEO-настройки'
        verbose_name_plural = 'SEO-настройки'

    @classmethod
    def get_instance(cls) -> "SEOSettings":
        instance, created = cls.objects.get_or_create(id=1)
        return instance

    def save(self, *args, **kwargs):
        if self.__class__.objects.count():
            self.pk = self.__class__.objects.first().pk
        super().save(*args, **kwargs)


class WeekPrizes(models.Model):
    image = models.ImageField(
        upload_to='week_prizes/',
        verbose_name='Изображение',
        help_text='Загрузите изображение приза недели'
    )
    name = models.CharField(
        max_length=100,
        verbose_name='Название приза',
        help_text='Краткое название приза'
    )
    caption = models.CharField(
        max_length=255,
        verbose_name='Подпись',
        help_text='Дополнительный текст под изображением'
    )
    # Опционально: порядок отображения
    order = models.PositiveIntegerField(
        default=0,
        verbose_name='Порядок',
        help_text='Чем меньше число, тем выше в списке'
    )

    class Meta:
        verbose_name = 'Еженедельный приз'
        verbose_name_plural = 'Еженедельные призы'
        ordering = ['order']

    def __str__(self):
        return self.name


class MonthPrizes(models.Model):
    image = models.ImageField(
        upload_to='month_prizes/',
        verbose_name='Изображение',
        help_text='Загрузите изображение приза месяца'
    )
    name = models.CharField(
        max_length=100,
        verbose_name='Название приза',
        help_text='Краткое название приза'
    )
    caption = models.CharField(
        max_length=255,
        verbose_name='Подпись',
        help_text='Дополнительный текст под изображением'
    )
    # Опционально: порядок отображения
    order = models.PositiveIntegerField(
        default=0,
        verbose_name='Порядок',
        help_text='Чем меньше число, тем выше в списке'
    )

    class Meta:
        verbose_name = 'Ежемесячный приз'
        verbose_name_plural = 'Ежемесячные призы'
        ordering = ['order']

    def __str__(self):
        return self.name


class MomentPrizes(models.Model):
    """Витрина моментальных призов на лендинге (сам розыгрыш — в promotion)."""

    image = models.ImageField(
        upload_to='moment_prizes/',
        verbose_name='Изображение',
        help_text='Загрузите изображение моментального приза',
    )
    name = models.CharField(
        max_length=100,
        verbose_name='Название приза',
        help_text='Краткое название приза',
    )
    caption = models.CharField(
        max_length=255,
        verbose_name='Подпись',
        help_text='Дополнительный текст под изображением',
    )
    order = models.PositiveIntegerField(
        default=0,
        verbose_name='Порядок',
        help_text='Чем меньше число, тем выше в списке',
    )

    class Meta:
        verbose_name = 'Моментальный приз'
        verbose_name_plural = 'Моментальные призы'
        ordering = ['order']

    def __str__(self):
        return self.name


class SpecialPrizes(models.Model):
    image = models.ImageField(
        upload_to='special_prizes/',
        verbose_name='Изображение',
        help_text='Загрузите изображение специального приза'
    )
    name = models.CharField(
        max_length=100,
        verbose_name='Название приза',
        help_text='Краткое название приза'
    )
    caption = models.CharField(
        max_length=255,
        verbose_name='Подпись',
        help_text='Дополнительный текст под изображением'
    )
    # Опционально: порядок отображения
    order = models.PositiveIntegerField(
        default=0,
        verbose_name='Порядок',
        help_text='Чем меньше число, тем выше в списке'
    )

    class Meta:
        verbose_name = 'Специальный приз'
        verbose_name_plural = 'Специальные призы'
        ordering = ['order']

    def __str__(self):
        return self.name


class ActionProducts(models.Model):
    image = models.ImageField(
        upload_to='action_products/',
        verbose_name='Изображение товара',
        help_text='Загрузите изображение товара акции'
    )
    name = models.CharField(
        max_length=200,
        verbose_name='Название товара'
    )
    description = models.TextField(
        verbose_name='Описание товара',
        help_text='Подробное описание товара-участника акции'
    )
    link = models.URLField(
        verbose_name='Ссылка на товар',
        blank=True,
        null=True,
        help_text='Опциональная ссылка на страницу товара'
    )
    show_in_products_block = models.BooleanField(
        default=False,
        verbose_name='Показывать в блоке «Товары-участники» (десктоп)',
        help_text='Товар попадёт в статичный блок из 8 карточек на десктопе'
    )
    products_block_order = models.PositiveIntegerField(
        default=0,
        verbose_name='Порядок в блоке «Товары-участники»',
        help_text='Чем меньше число, тем раньше товар показывается в блоке на десктопе'
    )
    # Поля для сортировки/времени при необходимости
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='Дата создания'
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name='Дата обновления'
    )

    class Meta:
        verbose_name = 'Акционный товар'
        verbose_name_plural = 'Акционные товары'
        ordering = ['-created_at']

    def __str__(self):
        return self.name
    

class City(models.Model): 
    name = models.CharField('Название', max_length=60, unique=True, help_text='Например, "Белгород"')

    class Meta: 
        verbose_name = 'Город'
        verbose_name_plural = 'Города' 

    def __str__(self): 
        return f'{self.name}'
    

class Address(models.Model): 
    name = models.CharField('Название', max_length=120, unique=True, help_text='Например, "Корочанская 84"') 
    city = models.ForeignKey(verbose_name='Город', to='City', on_delete=models.CASCADE, related_name='addresses')

    class Meta: 
        verbose_name = 'Адрес'
        verbose_name_plural = 'Адреса' 

    def __str__(self): 
        return f'{self.name}'
