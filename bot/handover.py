"""People answering instead of the bot: staff from the portal inbox, or the owner from the WhatsApp Business app."""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import Conversation, Message

log = logging.getLogger(__name__)


class HandoverError(Exception):
    """Why a staff reply can't be sent. The message is shown to staff."""


def take_over(convo: Conversation) -> None:
    convo.taken_over = True
    convo.save(update_fields=["taken_over", "updated_at"])


def hand_back(convo: Conversation) -> None:
    """Let the bot answer again, including after an owner's reply from the WhatsApp app."""
    convo.taken_over = False
    convo.bot_paused_until = None
    convo.save(update_fields=["taken_over", "bot_paused_until", "updated_at"])


def send_staff_reply(convo: Conversation, text: str) -> Message:
    """Send a reply typed in the portal. On WhatsApp it goes out from the restaurant's number."""
    text = text.strip()[:4096]
    if not text:
        raise HandoverError("Write a message first.")
    if not convo.bot_paused:
        raise HandoverError("Take over the conversation before replying, so the bot doesn't answer too.")
    customer = convo.customer
    sent = True
    if customer.channel == "whatsapp":
        if not convo.in_service_window():
            raise HandoverError(
                "The customer last wrote more than 24 hours ago. WhatsApp only allows approved template "
                "messages after that, so this reply can't be sent."
            )
        from whatsapp.client import send_text

        sent = send_text(customer.restaurant, customer.external_id, text)
    message = convo.add_message("assistant", text, sender=Message.Sender.STAFF, sent=sent)
    convo.save(update_fields=["history", "updated_at"])
    if not sent:
        raise HandoverError("WhatsApp didn't accept the message. It's saved here; try again in a moment.")
    return message


def owner_replied(convo: Conversation, text: str) -> None:
    """The owner answered from the WhatsApp Business app (Coexistence echo): log it and let them talk for a while."""
    convo.add_message("assistant", text, sender=Message.Sender.OWNER)
    convo.bot_paused_until = timezone.now() + timedelta(minutes=settings.OWNER_REPLY_PAUSE_MINUTES)
    convo.save(update_fields=["history", "bot_paused_until", "updated_at"])
