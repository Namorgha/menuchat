from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import Order
from .notifications import notify_status_change


@receiver(pre_save, sender=Order)
def remember_previous_status(sender, instance: Order, **kwargs):
    instance._previous_status = (
        Order.objects.filter(pk=instance.pk).values_list("status", flat=True).first() if instance.pk else None
    )


@receiver(post_save, sender=Order)
def status_changed(sender, instance: Order, created: bool, **kwargs):
    if not created and instance._previous_status != instance.status:
        notify_status_change(instance)
