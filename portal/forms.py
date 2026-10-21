from decimal import Decimal

from django import forms
from django.conf import settings

from restaurants.models import DeliveryZone, MenuCategory, MenuItem, Restaurant

MAX_IMAGE_BYTES = 4 * 1024 * 1024  # Vercel rejects request bodies over 4.5 MB


class CategoryForm(forms.ModelForm):
    class Meta:
        model = MenuCategory
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "e.g. Starters"})}

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Give the category a name.")
        return name


class MenuItemForm(forms.ModelForm):
    class Meta:
        model = MenuItem
        fields = ["category", "name", "description", "price", "image", "is_available"]
        labels = {"is_available": "Available to order"}
        help_texts = {"description": "Shown to customers and used by the assistant.", "image": "JPG or PNG, up to 4 MB."}
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, restaurant: Restaurant, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = restaurant.categories.all()
        self.fields["category"].empty_label = "No category"
        self.fields["price"].widget.attrs.update(min="0.01", step="0.01", inputmode="decimal")
        if not settings.PHOTO_UPLOADS_ENABLED:
            del self.fields["image"]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Give the item a name.")
        return name

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price is not None and price <= 0:
            raise forms.ValidationError("Price must be greater than 0.")
        return price

    def clean_image(self):
        image = self.cleaned_data.get("image")
        if image and getattr(image, "size", 0) > MAX_IMAGE_BYTES:
            raise forms.ValidationError("That photo is too large (max 4 MB).")
        return image


class DeliveryZoneForm(forms.ModelForm):
    class Meta:
        model = DeliveryZone
        fields = ["name", "fee", "active"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "e.g. Malabata"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["fee"].widget.attrs.update(min="0", step="0.01", inputmode="decimal")

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if not name:
            raise forms.ValidationError("Give the zone a name.")
        return name

    def clean_fee(self):
        fee = self.cleaned_data["fee"]
        if fee is not None and fee < 0:
            raise forms.ValidationError("Can't be negative.")
        return fee


class BaseZoneFormSet(forms.BaseModelFormSet):
    def clean(self):
        super().clean()
        seen = set()
        for form in self.forms:
            if not form.has_changed() and not form.instance.pk or self.can_delete and self._should_delete_form(form):
                continue
            name = (form.cleaned_data.get("name") or "").lower()
            if name in seen:
                form.add_error("name", "You already have a zone with this name.")
            seen.add(name)


ZoneFormSet = forms.modelformset_factory(
    DeliveryZone, form=DeliveryZoneForm, formset=BaseZoneFormSet, extra=1, can_delete=True
)


class RestaurantSettingsForm(forms.ModelForm):
    class Meta:
        model = Restaurant
        fields = [
            "name", "description", "opening_hours", "currency", "delivery_fee", "minimum_order",
            "accepts_delivery", "accepts_pickup", "is_open", "alert_phone",
        ]
        labels = {
            "accepts_delivery": "Offer delivery",
            "accepts_pickup": "Offer pickup",
            "is_open": "Open for orders",
            "alert_phone": "New-order alerts to (WhatsApp)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("delivery_fee", "minimum_order"):
            self.fields[name].widget.attrs.update(min="0", step="0.01", inputmode="decimal")

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("The restaurant needs a name.")
        return name

    def clean_currency(self):
        currency = self.cleaned_data["currency"].strip().upper()
        if not currency:
            raise forms.ValidationError("Enter a currency, e.g. MAD.")
        return currency

    def clean_alert_phone(self):
        phone = "".join(c for c in self.cleaned_data["alert_phone"] if c.isdigit())
        if phone.startswith("00"):
            phone = phone[2:]
        if phone and not 8 <= len(phone) <= 15:
            raise forms.ValidationError("Enter the full number with country code, e.g. 212612345678.")
        return phone

    def _non_negative(self, name):
        value = self.cleaned_data[name]
        if value is not None and value < Decimal("0"):
            raise forms.ValidationError("Can't be negative.")
        return value

    def clean_delivery_fee(self):
        return self._non_negative("delivery_fee")

    def clean_minimum_order(self):
        return self._non_negative("minimum_order")

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("accepts_delivery") and not cleaned.get("accepts_pickup"):
            raise forms.ValidationError("Offer at least delivery or pickup, or customers can't order.")
        return cleaned
