from django.db import models

from restaurants.models import Customer


class Conversation(models.Model):
    """Per-customer chat state: recent history and the cart being built."""
    customer = models.OneToOneField(Customer, on_delete=models.CASCADE, related_name="conversation")
    history = models.JSONField(default=list, blank=True)  # [{"role": "user"|"assistant", "content": str, "ts": iso}]
    cart = models.JSONField(default=list, blank=True)  # [{"item_id": int, "quantity": int, "note": str}]
    fulfillment = models.CharField(max_length=10, blank=True)  # "delivery" | "pickup"
    delivery_address = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    HISTORY_LIMIT = 30

    def add_message(self, role: str, content: str) -> None:
        from django.utils import timezone

        self.history = (self.history + [{"role": role, "content": content, "ts": timezone.now().isoformat()}])[
            -self.HISTORY_LIMIT :
        ]

    def __str__(self):
        return f"Conversation with {self.customer}"


class ProcessedMessage(models.Model):
    """WhatsApp retries webhooks; remember message IDs so each is handled once."""
    message_id = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
