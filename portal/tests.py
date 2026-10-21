import shutil
import tempfile
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image

from bot import ordering
from bot.models import Conversation
from restaurants.models import Customer, MenuCategory, MenuItem, Order, Restaurant

User = get_user_model()


def make_restaurant(slug="test", name="Test Grill", **kw):
    return Restaurant.objects.create(
        name=name, slug=slug, currency="MAD", delivery_fee=Decimal("10"), minimum_order=Decimal("40"), **kw
    )


def make_order(restaurant, number, status=Order.Status.NEW, customer_name="Sara", external_id=None):
    customer, _ = Customer.objects.get_or_create(
        restaurant=restaurant, external_id=external_id or f"2126{number:08d}", defaults={"name": customer_name}
    )
    return Order.objects.create(
        restaurant=restaurant, customer=customer, number=number, status=status,
        subtotal=Decimal("50"), total=Decimal("50"),
    )


class PortalTestCase(TestCase):
    """Restaurant A with staff user `chef`, restaurant B with staff user `other`."""

    def setUp(self):
        self.a = make_restaurant("a", "Restaurant A")
        self.b = make_restaurant("b", "Restaurant B")
        self.chef = User.objects.create_user("chef", password="pw-123456")
        self.other = User.objects.create_user("other", password="pw-123456")
        self.a.staff.add(self.chef)
        self.b.staff.add(self.other)


class LoginTests(PortalTestCase):
    def test_portal_pages_require_login(self):
        for url in ["/portal/orders/", "/portal/orders/history/", "/portal/orders/data/"]:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response["Location"].startswith("/login/?next="), url)

    def test_login_lands_on_orders_and_logout_returns_to_login(self):
        page = self.client.get("/login/")
        self.assertContains(page, "Restaurant sign in")
        response = self.client.post("/login/", {"username": "chef", "password": "pw-123456"})
        self.assertRedirects(response, "/portal/orders/")
        self.assertRedirects(self.client.post("/logout/"), "/login/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/portal/orders/").status_code, 302)

    def test_wrong_password_shows_an_error(self):
        response = self.client.post("/login/", {"username": "chef", "password": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "correct username and password")

    def test_user_without_a_restaurant_is_told_so(self):
        User.objects.create_user("nobody", password="pw-123456")
        self.client.login(username="nobody", password="pw-123456")
        response = self.client.get("/portal/orders/")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "isn't linked to a restaurant", status_code=403)

    def test_old_dashboard_urls_redirect_to_the_portal(self):
        for url in ["/dashboard/", "/dashboard/a/", "/dashboard/a/data/"]:
            self.assertRedirects(self.client.get(url), "/portal/orders/", fetch_redirect_response=False)


class OrdersTests(PortalTestCase):
    def setUp(self):
        super().setUp()
        tagine = MenuItem.objects.create(restaurant=self.a, name="Tagine", price=Decimal("65"))
        convo = Conversation.objects.create(
            customer=Customer.objects.create(restaurant=self.a, external_id="212655555555", name="Sara")
        )
        ordering.add_to_cart(convo, tagine.id)
        ordering.set_fulfillment(convo, "pickup")
        self.order = ordering.place_order(convo)

    def set_status(self, status):
        return self.client.post(f"/portal/orders/{self.order.id}/status/", {"status": status})

    def test_board_shows_own_orders(self):
        self.client.force_login(self.chef)
        page = self.client.get("/portal/orders/")
        self.assertContains(page, "Restaurant A")
        data = self.client.get("/portal/orders/data/").json()
        self.assertEqual([o["number"] for o in data["columns"][0]["orders"]], [self.order.number])

    def test_staff_moves_order_and_customer_is_notified(self):
        self.client.force_login(self.chef)
        with mock.patch("whatsapp.client.send_text") as send:
            self.assertEqual(self.set_status("confirmed").status_code, 200)
        send.assert_called_once()
        self.assertIn("#1001 confirmed", send.call_args.args[2])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")
        self.assertIn("confirmed", Conversation.objects.get().history[-1]["content"])

    def test_invalid_transitions_are_refused(self):
        self.client.force_login(self.chef)
        self.assertEqual(self.set_status("completed").status_code, 400)  # can't skip from new to completed

    def test_other_restaurants_staff_cannot_see_or_touch_orders(self):
        self.client.force_login(self.other)
        self.assertEqual(self.set_status("confirmed").status_code, 404)
        data = self.client.get("/portal/orders/data/").json()
        self.assertEqual(sum(len(c["orders"]) for c in data["columns"]), 0)
        history = self.client.get("/portal/orders/history/")
        self.assertNotContains(history, "Sara")
        self.assertEqual(history.context["restaurant"], self.b)

    def test_cannot_switch_to_a_restaurant_you_are_not_staff_of(self):
        self.client.force_login(self.other)
        self.client.post("/portal/switch/", {"restaurant": "a"})
        self.assertEqual(self.client.get("/portal/orders/").context["restaurant"], self.b)

    def test_staff_of_several_restaurants_can_switch(self):
        self.b.staff.add(self.chef)
        self.client.force_login(self.chef)
        self.assertEqual(self.client.get("/portal/orders/").context["restaurant"], self.a)
        self.client.post("/portal/switch/", {"restaurant": "b"})
        self.assertEqual(self.client.get("/portal/orders/").context["restaurant"], self.b)

    def test_superuser_sees_every_restaurant_without_being_staff(self):
        admin = User.objects.create_superuser("admin", "admin@example.com", "pw-123456")
        self.client.force_login(admin)
        self.assertEqual(self.client.get("/portal/orders/").status_code, 200)
        with mock.patch("whatsapp.client.send_text"):
            self.assertEqual(self.set_status("confirmed").status_code, 200)

    def test_toggle_open_from_the_header(self):
        self.client.force_login(self.chef)
        response = self.client.post("/portal/toggle-open/", HTTP_ACCEPT="application/json")
        self.assertEqual(response.json(), {"on": False, "label": "Closed"})
        self.a.refresh_from_db()
        self.assertFalse(self.a.is_open)
        self.b.refresh_from_db()
        self.assertTrue(self.b.is_open)
        # Without JS it redirects back to the page it came from.
        response = self.client.post("/portal/toggle-open/", {"next": "/portal/orders/history/"})
        self.assertRedirects(response, "/portal/orders/history/")
        self.a.refresh_from_db()
        self.assertTrue(self.a.is_open)


class OrderHistoryTests(PortalTestCase):
    def setUp(self):
        super().setUp()
        for n in range(1, 26):
            make_order(self.a, n, status=Order.Status.COMPLETED, customer_name=f"Guest {n}")
        make_order(self.a, 100, status=Order.Status.CANCELLED, customer_name="Youssef")
        make_order(self.b, 500, customer_name="Secret Customer")
        self.client.force_login(self.chef)

    def numbers(self, response):
        return [o.number for o in response.context["page_obj"]]

    def test_paginates_newest_first(self):
        first = self.client.get("/portal/orders/history/")
        self.assertEqual(len(self.numbers(first)), 20)
        self.assertEqual(first.context["page_obj"].paginator.count, 26)
        self.assertNotContains(first, "Secret Customer")
        second = self.client.get("/portal/orders/history/?page=2")
        self.assertEqual(len(self.numbers(second)), 6)

    def test_status_filter(self):
        response = self.client.get("/portal/orders/history/?status=cancelled")
        self.assertEqual(self.numbers(response), [100])

    def test_search_by_number_or_customer(self):
        self.assertEqual(self.numbers(self.client.get("/portal/orders/history/?q=%237")), [7])
        self.assertEqual(self.numbers(self.client.get("/portal/orders/history/?q=youss")), [100])
        self.assertEqual(self.numbers(self.client.get("/portal/orders/history/?q=500")), [])  # restaurant B's order


def png(name="photo.png"):
    buffer = BytesIO()
    Image.new("RGB", (4, 4), "orange").save(buffer, "PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class MenuTests(PortalTestCase):
    def setUp(self):
        super().setUp()
        self.mains = MenuCategory.objects.create(restaurant=self.a, name="Mains", position=0)
        self.drinks = MenuCategory.objects.create(restaurant=self.a, name="Drinks", position=1)
        self.tagine = MenuItem.objects.create(restaurant=self.a, category=self.mains, name="Tagine", price=Decimal("65"), position=0)
        self.couscous = MenuItem.objects.create(restaurant=self.a, category=self.mains, name="Couscous", price=Decimal("70"), position=1)
        self.b_cat = MenuCategory.objects.create(restaurant=self.b, name="B secret category")
        self.b_item = MenuItem.objects.create(restaurant=self.b, category=self.b_cat, name="B secret dish", price=Decimal("9"))
        self.client.force_login(self.chef)

    def item_data(self, **overrides):
        data = {"name": "Harira", "description": "Lentil soup", "price": "15", "category": self.mains.pk, "is_available": "on"}
        data.update(overrides)
        return data

    def test_menu_lists_own_categories_and_items_only(self):
        page = self.client.get("/portal/menu/")
        self.assertContains(page, "Mains")
        self.assertContains(page, "Couscous")
        self.assertNotContains(page, "B secret")

    def test_create_category(self):
        response = self.client.post("/portal/menu/categories/new/", {"name": "  Desserts "}, follow=True)
        self.assertContains(response, "Category “Desserts” added.")
        desserts = self.a.categories.get(name="Desserts")
        self.assertEqual(desserts.position, 2)

    def test_blank_category_name_shows_an_error(self):
        response = self.client.post("/portal/menu/categories/new/", {"name": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors["name"])
        self.assertEqual(self.a.categories.count(), 2)

    def test_rename_and_delete_category_keeps_its_items(self):
        self.client.post(f"/portal/menu/categories/{self.mains.pk}/edit/", {"name": "Plats"})
        self.mains.refresh_from_db()
        self.assertEqual(self.mains.name, "Plats")
        confirm = self.client.get(f"/portal/menu/categories/{self.mains.pk}/delete/")
        self.assertContains(confirm, "2 items will stay on the menu")
        self.client.post(f"/portal/menu/categories/{self.mains.pk}/delete/")
        self.assertFalse(MenuCategory.objects.filter(pk=self.mains.pk).exists())
        self.tagine.refresh_from_db()
        self.assertIsNone(self.tagine.category)
        self.assertContains(self.client.get("/portal/menu/"), "No category")

    def test_create_item(self):
        response = self.client.post("/portal/menu/items/new/", self.item_data(), follow=True)
        self.assertContains(response, "“Harira” added to the menu.")
        harira = self.a.items.get(name="Harira")
        self.assertEqual((harira.price, harira.category, harira.position, harira.is_available), (Decimal("15"), self.mains, 2, True))

    def test_new_item_form_preselects_category(self):
        response = self.client.get(f"/portal/menu/items/new/?category={self.drinks.pk}")
        self.assertEqual(response.context["form"].initial["category"], self.drinks)
        self.assertEqual(self.client.get("/portal/menu/items/new/?category=abc").status_code, 200)

    def test_item_validation_errors_are_shown_inline(self):
        for price in ["0", "-5", ""]:
            response = self.client.post("/portal/menu/items/new/", self.item_data(price=price))
            self.assertEqual(response.status_code, 200)
            self.assertIn("price", response.context["form"].errors)
        response = self.client.post("/portal/menu/items/new/", self.item_data(name=""))
        self.assertContains(response, 'class="error"')
        self.assertFalse(self.a.items.filter(name="Harira").exists())

    def test_cannot_put_an_item_in_another_restaurants_category(self):
        response = self.client.post("/portal/menu/items/new/", self.item_data(category=self.b_cat.pk))
        self.assertIn("category", response.context["form"].errors)

    def test_edit_item(self):
        data = self.item_data(name="Tagine royal", price="80.50", category=self.drinks.pk)
        del data["is_available"]
        response = self.client.post(f"/portal/menu/items/{self.tagine.pk}/edit/", data)
        self.assertRedirects(response, f"/portal/menu/#item-{self.tagine.pk}")
        self.tagine.refresh_from_db()
        self.assertEqual(self.tagine.name, "Tagine royal")
        self.assertEqual(self.tagine.price, Decimal("80.50"))
        self.assertEqual(self.tagine.category, self.drinks)
        self.assertFalse(self.tagine.is_available)

    def test_delete_item(self):
        self.assertContains(self.client.get(f"/portal/menu/items/{self.tagine.pk}/delete/"), "Delete “Tagine”?")
        self.client.post(f"/portal/menu/items/{self.tagine.pk}/delete/")
        self.assertFalse(MenuItem.objects.filter(pk=self.tagine.pk).exists())

    def test_reorder_items_and_categories(self):
        self.client.post(f"/portal/menu/items/{self.couscous.pk}/move/", {"direction": "up"})
        self.assertEqual(list(self.a.items.filter(category=self.mains).order_by("position").values_list("name", flat=True)), ["Couscous", "Tagine"])
        self.client.post(f"/portal/menu/items/{self.couscous.pk}/move/", {"direction": "up"})  # already first: no change
        self.assertEqual(list(self.a.items.filter(category=self.mains).order_by("position").values_list("name", flat=True)), ["Couscous", "Tagine"])
        self.client.post(f"/portal/menu/categories/{self.drinks.pk}/move/", {"direction": "up"})
        self.assertEqual(list(self.a.categories.values_list("name", flat=True)), ["Drinks", "Mains"])

    def test_sold_out_toggle_answers_json_for_instant_updates(self):
        url = f"/portal/menu/items/{self.tagine.pk}/availability/"
        self.assertEqual(self.client.post(url, HTTP_ACCEPT="application/json").json(), {"on": False, "label": "Sold out"})
        self.tagine.refresh_from_db()
        self.assertFalse(self.tagine.is_available)
        self.assertRedirects(self.client.post(url), f"/portal/menu/#item-{self.tagine.pk}")
        self.tagine.refresh_from_db()
        self.assertTrue(self.tagine.is_available)

    def test_image_upload(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        with override_settings(MEDIA_ROOT=media):
            self.client.post("/portal/menu/items/new/", self.item_data(image=png()))
            harira = self.a.items.get(name="Harira")
            self.assertTrue(harira.image.name.startswith("menu/"))
            self.assertContains(self.client.get("/portal/menu/"), harira.image.url)
            not_an_image = SimpleUploadedFile("menu.png", b"not an image", content_type="image/png")
            response = self.client.post("/portal/menu/items/new/", self.item_data(name="Bad", image=not_an_image))
            self.assertIn("image", response.context["form"].errors)

    def test_staff_cannot_touch_another_restaurants_menu(self):
        cat, item = self.b_cat.pk, self.b_item.pk
        requests = [
            ("get", f"/portal/menu/items/{item}/edit/", {}),
            ("post", f"/portal/menu/items/{item}/edit/", self.item_data()),
            ("post", f"/portal/menu/items/{item}/delete/", {}),
            ("post", f"/portal/menu/items/{item}/move/", {"direction": "up"}),
            ("post", f"/portal/menu/items/{item}/availability/", {}),
            ("post", f"/portal/menu/categories/{cat}/edit/", {"name": "Hacked"}),
            ("post", f"/portal/menu/categories/{cat}/delete/", {}),
            ("post", f"/portal/menu/categories/{cat}/move/", {"direction": "down"}),
        ]
        for method, url, data in requests:
            self.assertEqual(getattr(self.client, method)(url, data).status_code, 404, url)
        self.b_item.refresh_from_db()
        self.b_cat.refresh_from_db()
        self.assertEqual((self.b_item.name, self.b_item.is_available, self.b_cat.name), ("B secret dish", True, "B secret category"))


class SettingsTests(PortalTestCase):
    def data(self, **overrides):
        data = {
            "name": "Restaurant A+", "description": "Fresh food", "opening_hours": "12:00–23:00", "currency": "eur",
            "delivery_fee": "12.50", "minimum_order": "30", "accepts_delivery": "on", "accepts_pickup": "on",
        }
        data.update(overrides)
        return data

    def test_settings_save(self):
        self.client.force_login(self.chef)
        response = self.client.post("/portal/settings/", self.data(), follow=True)
        self.assertRedirects(response, "/portal/settings/")
        self.assertContains(response, "Settings saved.")
        self.a.refresh_from_db()
        self.assertEqual(
            (self.a.name, self.a.description, self.a.opening_hours, self.a.currency, self.a.delivery_fee, self.a.minimum_order),
            ("Restaurant A+", "Fresh food", "12:00–23:00", "EUR", Decimal("12.50"), Decimal("30")),
        )
        self.assertFalse(self.a.is_open)  # unchecked box closes the restaurant
        self.assertTrue(self.a.accepts_delivery and self.a.accepts_pickup)

    def test_invalid_settings_show_errors_and_change_nothing(self):
        self.client.force_login(self.chef)
        response = self.client.post("/portal/settings/", self.data(name="", delivery_fee="-1"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.context["form"].errors), {"name", "delivery_fee"})
        self.assertContains(response, "Can&#x27;t be negative.")
        response = self.client.post("/portal/settings/", self.data(accepts_delivery="", accepts_pickup=""))
        self.assertContains(response, "Offer at least delivery or pickup")
        self.a.refresh_from_db()
        self.assertEqual(self.a.name, "Restaurant A")

    def test_settings_only_edit_your_own_restaurant(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/portal/settings/").context["form"].instance, self.b)
        self.client.post("/portal/settings/", self.data(name="Renamed"))
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual((self.a.name, self.b.name), ("Restaurant A", "Renamed"))
