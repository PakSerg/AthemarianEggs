import os
from pathlib import Path

from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Настройки Django здесь ещё не загружены (config/__init__.py импортирует этот
# модуль при импорте пакета), а расписание ниже зависит от переменной из .env —
# поэтому читаем .env сами. Повторный вызов load_dotenv в settings.py безвреден.
load_dotenv(Path(__file__).resolve().parent.parent / '.env')

app = Celery("config")
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

app.conf.beat_schedule = {
    'process_raffle': {
        'task': 'promotion.tasks.process_raffle',
        'schedule': crontab(minute=0),
    },
    'process_raffle_monthly': {
        'task': 'promotion.tasks.process_raffle_monthly',
        'schedule': crontab(minute=0),
    },
    'process_raffle_main': {
        'task': 'promotion.tasks.process_raffle_main',
        'schedule': crontab(minute=0),
    },
    'notify_upcoming_raffles': {
        'task': 'promotion.tasks.notify_upcoming_raffles',
        'schedule': crontab(hour=10, minute=0),
    },
    'monitor_oki_documents': {
        'task': 'promotion.tasks.monitor_oki_documents',
        'schedule': crontab(minute=0, hour='*/3'),
    },
    # Сверка на 08:45 стоит намеренно перед 09:00 — ближайшим запуском
    # monitor_oki_documents: напоминания «победитель не подписал договор»
    # уходят уже по актуальным статусам.
    'sync_oki_document_statuses': {
        'task': 'promotion.tasks.sync_oki_document_statuses',
        'schedule': crontab(hour=8, minute=45),
    },
    'notify_pending_receipts_manual_check': {
        'task': 'promotion.tasks.notify_pending_receipts_manual_check',
        'schedule': crontab(hour=21, minute=0),
    },
    # Сводка по лотку яиц — вечером, вместе с остальной суточной отчётностью.
    'notify_instant_prizes_state': {
        'task': 'promotion.tasks.notify_instant_prizes_state',
        'schedule': crontab(hour=21, minute=5),
    },
    'repeat-process-receipts': {
        'task': 'promotion.tasks.repeat_process_receipts',
        'schedule': crontab(minute=0, hour='*/4'),
    },
    'sync_sbp_banks': {
        'task': 'promotion.tasks.sync_sbp_banks',
        'schedule': crontab(day_of_month=1, hour=4, minute=30),
    },
    'poll_prize_deals': {
        'task': 'promotion.tasks.poll_prize_deals',
        'schedule': crontab(minute='*/10'),
    },
    'retry_failed_prizes': {
        'task': 'promotion.tasks.retry_failed_prizes',
        'schedule': crontab(minute='*/15'),
    },
}

# Плановая пакетная выплата гарантированного приза — раз в неделю по пятницам,
# в 07:35 МСК (14:35 по Владивостоку) — один Реестр на всех накопившихся
# участников. Расписание регистрируется
# только при CYCLOPS_SCHEDULED_PAYOUTS_ENABLED=True: пока флаг выключен, beat
# вообще не знает об этой задаче, и выплаты идут только вручную из панели или
# админки. Тот же флаг проверяется внутри самой задачи (promotion/tasks.py) —
# чтобы запись, оставшаяся в django_celery_beat от прошлых запусков, не могла
# сработать после выключения флага.
#
# Флаг читается напрямую из окружения, а не через django.conf.settings:
# config/__init__.py импортирует этот модуль при импорте пакета, то есть
# ещё до того, как настройки Django загружены, и обращение к settings здесь
# уронило бы старт приложения.
if os.getenv('CYCLOPS_SCHEDULED_PAYOUTS_ENABLED', 'False') == 'True':
    app.conf.beat_schedule['process_scheduled_guaranteed_prize_payouts'] = {
        'task': 'promotion.tasks.process_scheduled_guaranteed_prize_payouts',
        'schedule': crontab(day_of_week='fri', hour=7, minute=35),
    }