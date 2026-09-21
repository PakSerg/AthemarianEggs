import logging

from celery import shared_task

from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name='promotion.tasks.process_participant_receipt')
def process_participant_receipt(receipt_id: int):

    from .models import Receipt
    from .services.receipt_fns import process_receipt_by_id

    try:
        process_receipt_by_id(int(receipt_id))
    except Exception:
        logger.exception('process_participant_receipt failed receipt_id=%s', receipt_id)
        try:
            Receipt.objects.filter(pk=int(receipt_id), status=Receipt.Status.PENDING).update(
                status=Receipt.Status.REJECTED,
                message='Ошибка при проверке чека. Попробуйте отправить снова или обратитесь в поддержку.',
            )
        except Exception:
            logger.exception('could not mark receipt failed receipt_id=%s', receipt_id)
        raise


@shared_task(name='promotion.tasks.notify_upcoming_raffles')
def notify_upcoming_raffles():
    """За сутки до розыгрыша — напоминание в Telegram (еженедельный и/или главный)."""
    from .services.raffle_reminder import process_upcoming_raffle_reminders

    try:
        result = process_upcoming_raffle_reminders()
        logger.info('notify_upcoming_raffles: %s', result)
        return result
    except Exception:
        logger.exception('notify_upcoming_raffles failed')
        raise


@shared_task(name='promotion.tasks.monitor_oki_documents')
def monitor_oki_documents():
    """Проверка договоров OkiDoki у победителей — напоминания в Telegram."""
    from .services.oki_document_monitor import process_oki_document_reminders

    try:
        result = process_oki_document_reminders()
        logger.info('monitor_oki_documents: %s', result)
        return result
    except Exception:
        logger.exception('monitor_oki_documents failed')
        raise


@shared_task(name='promotion.tasks.sync_oki_document_statuses')
def sync_oki_document_statuses():
    """Ежедневная сверка статусов договоров с OkiDoki — страховка на случай,
    если callback до нас не дошёл (см. promotion/services/oki_status_sync.py)."""
    from .services.oki_status_sync import sync_oki_document_statuses as _sync

    try:
        result = _sync()
        logger.info('sync_oki_document_statuses: %s', result)
        return result
    except Exception:
        logger.exception('sync_oki_document_statuses failed')
        raise


@shared_task(name='promotion.tasks.notify_pending_receipts_manual_check')
def notify_pending_receipts_manual_check():
    """Ежедневная сводка pending-чеков (ручная проверка) в Telegram."""
    from .models import Receipt
    from .services.receipt_fns import notify_pending_receipts_manual_check as notify_pending

    try:
        pending_receipts = list(
            Receipt.objects
            .filter(status=Receipt.Status.PENDING)
            .order_by('id')
            .only('id')
        )
        notify_pending(pending_receipts)
        result = {'pending_count': len(pending_receipts)}
        logger.info('notify_pending_receipts_manual_check: %s', result)
        return result
    except Exception:
        logger.exception('notify_pending_receipts_manual_check failed')
        raise


@shared_task(name='promotion.tasks.repeat_process_receipts')
def repeat_process_receipts():
    from .services.receipt_fns import repeat_process_receipts

    try:
        repeat_process_receipts()
    except Exception:
        logger.exception('repeat_process_receipts failed')
        
        
@shared_task(name='promotion.tasks.sync_sbp_banks')
def sync_sbp_banks():
    """Обновление справочника банков-участников СБП (Cyclops list_bank_sbp)."""
    from .services.cyclops_sbp import sync_sbp_banks as _sync

    try:
        return _sync()
    except Exception:
        logger.exception('sync_sbp_banks failed')


@shared_task(name='promotion.tasks.pay_guaranteed_prize')
def pay_guaranteed_prize(participant_id: int):
    """Выполнить выплату гарантированного приза одному участнику."""
    from .services.guaranteed_prize_payout import perform_payout

    try:
        perform_payout(participant_id)
    except Exception:
        logger.exception('pay_guaranteed_prize failed participant_id=%s', participant_id)


@shared_task(name='promotion.tasks.poll_prize_deals')
def poll_prize_deals():
    """Опрос статусов отправленных выплат (get_deal → paid/failed)."""
    from .services.guaranteed_prize_payout import poll_prize_deals as _poll

    try:
        _poll()
    except Exception:
        logger.exception('poll_prize_deals failed')


@shared_task(name='promotion.tasks.retry_failed_prizes')
def retry_failed_prizes():
    """Авто-ретраи выплат, которые ещё не отправлены (new/retryable)."""
    from .services.guaranteed_prize_payout import retry_failed_prizes as _retry

    try:
        _retry()
    except Exception:
        logger.exception('retry_failed_prizes failed')


@shared_task(name='promotion.tasks.process_raffle')
def process_raffle():
    from .models import Raffle, Receipt
    from .services.draw_result import _execute_draw_result, get_week_num_raffle

    try:
        raffle = Raffle.objects.filter(is_active=True).first()

        if not raffle:
            return False

        start_date = timezone.localtime(raffle.start_date)
        end_date = timezone.localtime(raffle.end_date)
        week_day = raffle.week_day
        key_last_raffle = raffle.key_last_raffle

        new_key_last_raffle = f'{week_day}_{timezone.localtime().date()}'

        week_num = get_week_num_raffle(raffle)

        if (
            start_date <= timezone.localtime() < end_date
            and timezone.localtime().weekday() == week_day
            and new_key_last_raffle != key_last_raffle
            and week_num != 1
        ) or (timezone.localtime().date() == end_date.date() and new_key_last_raffle != key_last_raffle):
            result = _execute_draw_result(raffle)
            if not result.get('ok'):
                logger.warning(
                    'process_raffle: draw not ok status=%s run_id=%s winners=%s has_errors=%s message=%s',
                    result.get('status'),
                    result.get('run_id'),
                    result.get('winners_count'),
                    result.get('has_errors'),
                    result.get('message'),
                )
                return False

            raffle.key_last_raffle = new_key_last_raffle
            raffle.save(update_fields=['key_last_raffle'])
            Receipt.objects.filter(is_participation=False, status__in=[Receipt.Status.CONFIRMED, Receipt.Status.WINNER]).update(is_participation=True)
            logger.info(
                'process_raffle: draw ok status=%s run_id=%s winners=%s has_errors=%s key=%s',
                result.get('status'),
                result.get('run_id'),
                result.get('winners_count'),
                result.get('has_errors'),
                new_key_last_raffle,
            )
            return True

        return False
    except Exception:
        logger.exception('process_raffle failed')
        return False


@shared_task(name='promotion.tasks.process_raffle_main')
def process_raffle_main():
    """Главный розыгрыш — запускается один раз, начиная с даты Raffle.main_raffle_date."""
    from .models import PromotionDrawResultMainRaffle, Raffle
    from .services.draw_result import _execute_draw_result_main_raffle

    try:
        raffle = Raffle.objects.filter(is_active=True).first()

        if not raffle or not raffle.main_raffle_date:
            return False

        if PromotionDrawResultMainRaffle.objects.filter(is_reserve=False).exists():
            return False

        main_date = timezone.localtime(raffle.main_raffle_date).date()
        if timezone.localtime().date() < main_date:
            return False

        result = _execute_draw_result_main_raffle(raffle)
        if not result.get('ok'):
            logger.warning(
                'process_raffle_main: draw not ok status=%s run_id=%s winners=%s has_errors=%s message=%s',
                result.get('status'),
                result.get('run_id'),
                result.get('winners_count'),
                result.get('has_errors'),
                result.get('message'),
            )
            return False

        logger.info(
            'process_raffle_main: draw ok status=%s run_id=%s winners=%s has_errors=%s',
            result.get('status'),
            result.get('run_id'),
            result.get('winners_count'),
            result.get('has_errors'),
        )
        return True
    except Exception:
        logger.exception('process_raffle_main failed')
        return False


@shared_task(name='promotion.tasks.process_raffle_monthly')
def process_raffle_monthly():
    from .models import Raffle, Receipt
    from .services.draw_result import _execute_draw_result_monthly, get_month_num_raffle

    try:
        raffle = Raffle.objects.filter(is_active=True).first()

        if not raffle or not raffle.month_day:
            return False

        start_date = timezone.localtime(raffle.start_date)
        end_date = timezone.localtime(raffle.end_date)
        month_day = raffle.month_day
        key_last_monthly_raffle = raffle.key_last_monthly_raffle

        month_num = get_month_num_raffle(raffle)
        new_key_last_monthly_raffle = f'month_{month_num}_{timezone.localtime().date()}'

        if not (
            start_date <= timezone.localtime() < end_date
            and timezone.localtime().day == month_day
            and new_key_last_monthly_raffle != key_last_monthly_raffle
            and month_num != 1
        ):
            return False

        result = _execute_draw_result_monthly(raffle)
        if not result.get('ok'):
            logger.warning(
                'process_raffle_monthly: draw not ok status=%s run_id=%s winners=%s has_errors=%s message=%s',
                result.get('status'),
                result.get('run_id'),
                result.get('winners_count'),
                result.get('has_errors'),
                result.get('message'),
            )
            return False

        raffle.key_last_monthly_raffle = new_key_last_monthly_raffle
        raffle.save(update_fields=['key_last_monthly_raffle'])
        Receipt.objects.filter(
            month=month_num - 1,
            is_month_participation=False,
            status__in=[Receipt.Status.CONFIRMED, Receipt.Status.WINNER],
        ).update(is_month_participation=True)
        logger.info(
            'process_raffle_monthly: draw ok status=%s run_id=%s winners=%s has_errors=%s key=%s',
            result.get('status'),
            result.get('run_id'),
            result.get('winners_count'),
            result.get('has_errors'),
            new_key_last_monthly_raffle,
        )
        return True
    except Exception:
        logger.exception('process_raffle_monthly failed')
        return False


# Плановая пакетная выплата гарантированного приза (раз в неделю, пятница):
# собрать всех накопившихся в статусе «Отложена» участников, сформировать на
# них один Реестр выплат и поставить всем выплату в очередь.
#
# Задача намеренно не запускается сама: расписание регистрируется в Celery beat
# только при CYCLOPS_SCHEDULED_PAYOUTS_ENABLED=True (см. config/celery.py), и
# тот же флаг проверяется здесь ещё раз — чтобы задача, оставшаяся в таблице
# django_celery_beat от прошлых запусков, не смогла сработать сама по себе.
@shared_task(name='promotion.tasks.process_scheduled_guaranteed_prize_payouts')
def process_scheduled_guaranteed_prize_payouts():
    """Вторник и пятница: собрать реестр из накопившихся выплат и запустить их."""
    from django.conf import settings

    from .services.guaranteed_prize_payout import (
        process_scheduled_guaranteed_prize_payouts as _process,
    )

    if not settings.CYCLOPS_CONFIG.get('SCHEDULED_PAYOUTS_ENABLED'):
        logger.info(
            'process_scheduled_guaranteed_prize_payouts: плановые выплаты выключены '
            '(CYCLOPS_SCHEDULED_PAYOUTS_ENABLED), пропуск'
        )
        return

    try:
        _process()
    except Exception:
        logger.exception('process_scheduled_guaranteed_prize_payouts failed')


@shared_task(name='promotion.tasks.notify_instant_prizes_state')
def notify_instant_prizes_state():
    """Ежедневная сводка по моментальным призам в Telegram.

    Лоток яиц может «сломаться» тихо: расписание не сгенерировано или призы
    уходят медленнее графика — ошибок в логах при этом нет. См.
    promotion/services/instant_prizes_monitor.py.
    """
    from .services.instant_prizes_monitor import process_instant_prizes_report

    try:
        result = process_instant_prizes_report()
        logger.info('notify_instant_prizes_state: %s', result)
        return result
    except Exception:
        logger.exception('notify_instant_prizes_state failed')
        raise
