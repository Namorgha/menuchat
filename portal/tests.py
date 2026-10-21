from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from bot import ordering
from bot.models import Conversation
from restaurants.models import Customer, MenuItem, Order, Restaurant

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
