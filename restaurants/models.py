from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class Restaurant(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    description = models.CharField(max_length=255, blank=True, help_text="One line the bot uses to introduce you.")
    currency = models.CharField(max_length=8, default="MAD")
    opening_hours = models.CharField(max_length=255, blank=True, help_text="e.g. Every day 12:00–23:30")
    delivery_fee = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    minimum_order = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    accepts_delivery = models.BooleanField(default=True)
    accepts_pickup = models.BooleanField(default=True)
    is_open = models.BooleanField(default=True, help_text="Turn off to stop taking orders.")

    # WhatsApp Cloud API — incoming messages are routed to the restaurant by phone_number_id.
    whatsapp_phone_number_id = models.CharField(max_length=64, blank=True, unique=True, null=True)
    whatsapp_access_token = models.TextField(blank=True, help_text="Leave empty to use the server default token.")

    staff = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="restaurants")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class MenuCategory(models.Model):
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=80)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]
        verbose_name_plural = "menu categories"

    def __str__(self):
        return f"{self.restaurant.name} · {self.name}"


class MenuItem(models.Model):
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name="items")
    category = models.ForeignKey(MenuCategory, on_delete=models.SET_NULL, null=True, blank=True, related_name="items")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    image = models.ImageField(upload_to="menu/%Y/%m/", blank=True)
    is_available = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["category__position", "position", "id"]

    def __str__(self):
        return f"{self.name} ({self.price})"


class Customer(models.Model):
    """Someone ordering — identified by WhatsApp ID, or a web-demo session ID."""
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name="customers")
    external_id = models.CharField(max_length=64)  # WhatsApp wa_id or "web:<session>"
    name = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("restaurant", "external_id")]

    @property
    def channel(self) -> str:
        return "web" if self.external_id.startswith("web:") else "whatsapp"

    @property
    def phone(self) -> str:
        return f"+{self.external_id}" if self.channel == "whatsapp" else ""

    def __str__(self):
        return self.name or self.external_id


class Order(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        CONFIRMED = "confirmed", "Confirmed"
        PREPARING = "preparing", "Preparing"
        READY = "ready", "Ready"
        OUT_FOR_DELIVERY = "out_for_delivery", "Out for delivery"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    class Fulfillment(models.TextChoices):
        DELIVERY = "delivery", "Delivery"
        PICKUP = "pickup", "Pickup"

    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name="orders")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    number = models.PositiveIntegerField(help_text="Per-restaurant order number shown to customers.")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    fulfillment = models.CharField(max_length=10, choices=Fulfillment.choices, default=Fulfillment.DELIVERY)
    delivery_address = models.CharField(max_length=255, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    delivery_fee = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    total = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("restaurant", "number")]

    def __str__(self):
        return f"#{self.number} · {self.restaurant.name}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    menu_item = models.ForeignKey(MenuItem, on_delete=models.SET_NULL, null=True)
    name = models.CharField(max_length=120)  # snapshot, so later menu edits don't change past orders
    unit_price = models.DecimalField(max_digits=8, decimal_places=2)
    quantity = models.PositiveIntegerField()
    note = models.CharField(max_length=120, blank=True)

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity

    def __str__(self):
        return f"{self.quantity}× {self.name}"
