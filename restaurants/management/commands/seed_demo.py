from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from restaurants.models import MenuCategory, MenuItem, Restaurant

MENU = {
    "Tagines": [
        ("Chicken tagine with preserved lemon", "Olives, preserved lemon, saffron", "65"),
        ("Beef tagine with prunes", "Slow-cooked, almonds and sesame", "80"),
        ("Kefta tagine with eggs", "Spiced meatballs in tomato sauce", "55"),
    ],
    "Grill & Sandwiches": [
        ("Mixed grill plate", "Kefta, chicken brochettes, fries, salad", "90"),
        ("Chicken shawarma sandwich", "Garlic sauce, pickles, fries inside", "35"),
        ("Merguez sandwich", "Harissa, onions, fries", "30"),
    ],
    "Pizza": [
        ("Margherita pizza", "Tomato, mozzarella, basil", "50"),
        ("Pizza fruits de mer", "Shrimp, calamari, mozzarella", "75"),
    ],
    "Sides": [
        ("Fries", "", "15"),
        ("Moroccan salad", "Tomato, cucumber, onion", "20"),
        ("Harira soup", "Traditional lentil and chickpea soup", "15"),
    ],
    "Drinks": [
        ("Fresh orange juice", "", "18"),
        ("Avocado smoothie", "With almonds and dates", "25"),
        ("Mint tea (pot)", "", "20"),
        ("Soft drink", "Coca-Cola, Fanta or Sprite", "12"),
    ],
}


class Command(BaseCommand):
    help = "Create the demo restaurant with a menu (safe to run more than once)."

    def add_arguments(self, parser):
        parser.add_argument("--staff-username", help="Create/attach a staff login for the dashboard")
        parser.add_argument("--staff-password")

    def handle(self, *args, **opts):
        # get_or_create, not update_or_create: this runs on every deploy and must keep admin edits.
        r, created = Restaurant.objects.get_or_create(
            slug=settings.DEMO_RESTAURANT_SLUG,
            defaults=dict(
                name="Dar Tangier",
                description="Moroccan home cooking, grills and pizza in the heart of Tangier.",
                currency="MAD",
                opening_hours="Every day 12:00–23:30",
                delivery_fee=Decimal("10"),
                minimum_order=Decimal("40"),
            ),
        )
        if created or not r.items.exists():
            for pos, (cat_name, items) in enumerate(MENU.items()):
                cat = MenuCategory.objects.create(restaurant=r, name=cat_name, position=pos)
                for ipos, (name, desc, price) in enumerate(items):
                    MenuItem.objects.create(restaurant=r, category=cat, name=name, description=desc, price=Decimal(price), position=ipos)
        if opts.get("staff_username"):
            User = get_user_model()
            user, _ = User.objects.get_or_create(username=opts["staff_username"], defaults={"is_staff": True})
            if opts.get("staff_password"):
                user.set_password(opts["staff_password"])
            user.is_staff = True
            user.save()
            r.staff.add(user)
        self.stdout.write(self.style.SUCCESS(f"Demo restaurant ready: {r.name} ({r.items.count()} menu items)"))
