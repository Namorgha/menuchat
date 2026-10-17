from django.contrib import admin

from .models import Customer, MenuCategory, MenuItem, Order, OrderItem, Restaurant


class MenuCategoryInline(admin.TabularInline):
    model = MenuCategory
    extra = 0


@admin.register(Restaurant)
class RestaurantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_open", "currency", "whatsapp_phone_number_id")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("staff",)
    inlines = [MenuCategoryInline]
    fieldsets = (
        (None, {"fields": ("name", "slug", "description", "opening_hours", "is_open")}),
        ("Ordering", {"fields": ("currency", "delivery_fee", "minimum_order", "accepts_delivery", "accepts_pickup")}),
        ("WhatsApp", {"fields": ("whatsapp_phone_number_id", "whatsapp_access_token")}),
        ("Team", {"fields": ("staff",)}),
    )


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ("name", "restaurant", "category", "price", "is_available")
    list_editable = ("price", "is_available")
    list_filter = ("restaurant", "category", "is_available")
    search_fields = ("name",)


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("name", "unit_price", "quantity", "note")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("number", "restaurant", "customer", "status", "fulfillment", "total", "created_at")
    list_filter = ("restaurant", "status", "fulfillment")
    readonly_fields = ("subtotal", "delivery_fee", "total", "created_at")
    inlines = [OrderItemInline]


admin.site.register(Customer)
admin.site.site_header = "MenuChat admin"
