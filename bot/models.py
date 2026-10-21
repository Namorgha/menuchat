from datetime import timedelta

from django.db import models
from django.utils import timezone

from restaurants.models import Customer


class Conversation(models.Model):
    """Per-customer chat state: recent history (the AI's context), the cart being built, and who's answering."""
    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name="conversation")
    history = models.JSONField(default=list, blank=True)  # [{"role": "user"|"assistant", "content": str, "ts": iso}]
    cart = models.JSONField(default=list, blank=True)  # [{"item_id": int, "quantity": int, "note": str}]
    fulfillment = models.CharField(max_length=10, blank=True)  # "delivery" | "pickup"
    delivery_address = models.CharField(max_length=255, blank=True)
    # Staff took over from the portal: the bot stays quiet until they hand back.
    taken_over = models.BooleanField(default=False)
    # The owner answered from the WhatsApp Business app: the bot stays quiet until then.
    bot_paused_until = models.DateTimeField(null=True, blank=True)
    # WhatsApp only allows free-form messages within 24 hours of the customer's last message.
    last_customer_message_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    HISTORY_LIMIT = 30
    SERVICE_WINDOW = timedelta(hours=24)

    def add_message(self, role: str, content: str, sender: str | None = None, sent: bool = True) -> "Message":
        """Add to the AI's history (caller saves) and to the permanent log shown in the inbox (saved now)."""
        now = timezone.now()
        self.history = (self.history + [{"role": role, "content": content, "ts": now.isoformat()}])[-self.HISTORY_LIMIT :]
        if role == "user":
            self.last_customer_message_at = now
        sender = sender or (Message.Sender.CUSTOMER if role == "user" else Message.Sender.BOT)
        return Message.objects.create(conversation=self, sender=sender, text=content, sent=sent, created_at=now)

    @property
    def bot_paused(self) -> bool:
        return self.taken_over or bool(self.bot_paused_until and self.bot_paused_until > timezone.now())

    def in_service_window(self) -> bool:
        return bool(self.last_customer_message_at and timezone.now() - self.last_customer_message_at < self.SERVICE_WINDOW)

    def __str__(self):
        return f"Conversation with {self.customer}"


class Message(models.Model):
    """Every message in a conversation, kept in full for the inbox (history above is trimmed for the AI)."""

    class Sender(models.TextChoices):
        CUSTOMER = "customer", "Customer"
        BOT = "bot", "Bot"
        STAFF = "staff", "Staff (portal)"
        OWNER = "owner", "Owner (WhatsApp app)"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.CharField(max_length=10, choices=Sender.choices)
    text = models.TextField()
    sent = models.BooleanField(default=True, help_text="False if it couldn't be delivered to the customer.")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.get_sender_display()}: {self.text[:40]}"


class ProcessedMessage(models.Model):
    """WhatsApp retries webhooks; remember message IDs so each is handled once."""
    message_id = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
