"""
Файл настроек для локальной разработки.
Подключить: DJANGO_SETTINGS_MODULE=config.settings_local

Отключает Email, OkiDoki — чтобы случайно не отправить
сообщения реальным пользователям во время разработки.
Также добавляет поддержку прокси для CRPT API при необходимости.
"""
from .settings import * 

ALLOWED_HOSTS = ['*']
CSRF_TRUSTED_ORIGINS = ['http://localhost', 'http://127.0.0.1', 'http://localhost:8080', 'http://127.0.0.1:8080']

# ── Отключение внешних отправок ──────────────────────────────────────────────

# Блокирует все письма через sendemail.space (send_mail / send_html_mail).
EMAIL_DISABLED = False

# Блокирует создание договоров в OkiDoki (send_link_oki_doki).
OKIDOKI_DISABLED = False

# Вместо реального запроса в ФНС возвращает тестовый ответ из debug_config.py.
FNS_DISABLED = True

# ── Удобства для разработки ───────────────────────────────────────────────────
DEBUG = True

# ── БД ───────────────────────────────────────────────────────────────────────
# При DEBUG=True основные настройки выбирают SQLite.
# Переопределяем на PostgreSQL из docker-compose.local.yml.
# При запуске вне Docker можно сменить HOST на localhost.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'atemar_local',
        'USER': 'postgres',
        'PASSWORD': 'postgres',
        'HOST': 'db',
        'PORT': '5432',
    }
}

# ── Redis ─────────────────────────────────────────────────────────────────────
# При DEBUG=True settings.py переключается на 127.0.0.1:6389 (локальный Redis).
# В Docker-окружении Redis доступен по имени сервиса.
CELERY_BROKER_URL = 'redis://redis:6379/0'
CELERY_RESULT_BACKEND = 'redis://redis:6379/0'

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': 'redis://redis:6379/1',
    },
}
