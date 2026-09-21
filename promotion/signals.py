from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from .models import (
    PrizeFile,
    PromotionDrawResult,
    PromotionDrawResultMainRaffle,
    Receipt,
    User,
)


@receiver(pre_save, sender=User)
def freeze_or_unfreeze_receipts_on_block_change(sender, instance: User, **kwargs):
    """
    При блокировке участника его чеки «На проверке», по которым уже пришли данные
    от ФНС (см. Receipt.items), замораживаются — а не остаются в очереди на модерацию,
    как будто ничего не произошло. При разблокировке — возвращаются на проверку.
    """
    if instance.pk is None:
        return

    try:
        previous = User.objects.only('is_blocked').get(pk=instance.pk)
    except User.DoesNotExist:
        return

    if previous.is_blocked == instance.is_blocked:
        return

    if instance.is_blocked:
        (
            Receipt.objects
            .filter(participant_id=instance.pk, status=Receipt.Status.PENDING)
            .exclude(items={})
            .update(status=Receipt.Status.FROZEN)
        )
    else:
        (
            Receipt.objects
            .filter(participant_id=instance.pk, status=Receipt.Status.FROZEN)
            .update(status=Receipt.Status.PENDING)
        )


@receiver(post_save, sender=Receipt)
def ensure_guaranteed_prize_payout_on_receipt_confirmed(sender, instance: Receipt, **kwargs):
    """
    Право на гарантированный приз (п. 4.9/6.2 Правил) возникает при подтверждении
    первого чека участника — ставим выплату в очередь тем же путём, что и при
    сохранении профиля. Срабатывает при любом сохранении чека со статусом
    «Подтверждён»: автоматическая модерация (receipt_fns), ручное подтверждение
    в админке (ReceiptAdmin.save_model) и в дашборде (staff_panel — set_status) —
    все три пути сохраняют чек через .save(), поэтому один сигнал покрывает их все.
    ensure_guaranteed_prize_payout сама идемпотентна и ничего не делает, если
    профиль не заполнен или запись уже существует.
    """
    if instance.status != Receipt.Status.CONFIRMED:
        return

    from .services.guaranteed_prize_payout import ensure_guaranteed_prize_payout
    ensure_guaranteed_prize_payout(instance.participant)


@receiver(pre_delete, sender=PromotionDrawResult)
@receiver(pre_delete, sender=PromotionDrawResultMainRaffle)
def release_prize_files_on_draw_result_delete(sender, instance, **kwargs):
    """
    Удаление итога розыгрыша НИКОГДА не удаляет файл электронного приза: файл
    только отвязывается и снова становится свободным для выдачи.

    Сама ссылка обнуляется через on_delete=SET_NULL, но пометку о выдаче
    (assigned_at) нужно снять явно — иначе останется «выданный ничей» файл.
    """
    field = 'draw_result' if sender is PromotionDrawResult else 'main_draw_result'
    PrizeFile.objects.filter(**{field: instance}).update(**{field: None, 'assigned_at': None})
