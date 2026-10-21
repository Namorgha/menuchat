"""Tell the customer when their order status changes — on WhatsApp, or in the web demo chat."""
import logging

from bot.models import Conversation

from .models import Order

log = logging.getLogger(__name__)

MESSAGES = {
    Order.Status.CONFIRMED: "✅ Order #{n} confirmed! We're on it.",
    Order.Status.PREPARING: "👨‍🍳 Order #{n} is being prepared.",
    Order.Status.READY: "🛍️ Order #{n} is ready for pickup.",
    Order.Status.OUT_FOR_DELIVERY: "🛵 Order #{n} is on its way!",
    Order.Status.COMPLETED: "🎉 Order #{n} completed. Enjoy your meal — thanks for ordering from {r}!",
    Order.Status.CANCELLED: "❌ Order #{n} was cancelled. Reply here if you have any questions.",
}


def notify_status_change(order: Order) -> None:
    template = MESSAGES.get(order.status)
    if not template:
        return
    text = template.format(n=order.number, r=order.restaurant.name)
    convo, _ = Conversation.objects.get_or_create(customer=order.customer)

    sent = True
    if order.customer.channel == "whatsapp":
        if convo.in_service_window():
            from whatsapp.client import send_text

            sent = send_text(order.restaurant, order.customer.external_id, text)
        else:
            # Outside the 24-hour window WhatsApp only accepts approved templates; a free-form send would fail.
            log.info("Order %s status update not sent: customer's last message is over 24 hours old", order.pk)
            sent = False
    convo.add_message("assistant", text, sent=sent)
    convo.save(update_fields=["history", "updated_at"])
