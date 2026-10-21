"""Tell the customer when their order status changes — on WhatsApp, or in the web demo chat."""
import logging

from django.conf import settings

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


def _destination(order: Order) -> str:
    if order.fulfillment == Order.Fulfillment.DELIVERY:
        zone = f" ({order.delivery_zone})" if order.delivery_zone else ""
        return f"Delivery: {order.delivery_address}{zone}"
    return "Pickup"


def new_order_alert_text(order: Order) -> str:
    currency = order.restaurant.currency
    lines = [f"🔔 New order #{order.number}"]
    lines += [f"{i.quantity}× {i.name}" + (f" ({i.note})" if i.note else "") for i in order.items.all()]
    lines.append(f"Total: {order.total:.2f} {currency} · {order.payment_label}")
    lines.append(f"📍 {_destination(order)}")
    customer = order.customer
    lines.append(f"👤 {customer.name or 'Customer'}" + (f" · {customer.phone}" if customer.phone else ""))
    if order.notes:
        lines.append(f"📝 {order.notes}")
    return "\n".join(lines)


def alert_new_order(order: Order) -> None:
    """Message the restaurant's alert number about a new order.

    With WHATSAPP_ALERT_TEMPLATE set, an approved template is used and arrives at any time. Otherwise it's a
    free-form message, which WhatsApp only delivers within 24 hours of the alert number's last message to the
    restaurant's number. Failures are logged.
    """
    to = order.restaurant.alert_phone
    if not to:
        return
    from whatsapp.client import send_template, send_text

    if settings.WHATSAPP_ALERT_TEMPLATE:
        items = ", ".join(f"{i.quantity}× {i.name}" for i in order.items.all())
        total = f"{order.total:.2f} {order.restaurant.currency} ({order.payment_label})"
        sent = send_template(
            order.restaurant, to, settings.WHATSAPP_ALERT_TEMPLATE, settings.WHATSAPP_ALERT_TEMPLATE_LANGUAGE,
            [f"#{order.number}", items, total, _destination(order)],
        )
    else:
        sent = send_text(order.restaurant, to, new_order_alert_text(order))
    if not sent:
        log.warning("New-order alert for order %s wasn't delivered to the alert number", order.pk)


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
