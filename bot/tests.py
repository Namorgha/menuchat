import hashlib
import hmac
import json
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from restaurants.models import Customer, MenuItem, Order, Restaurant

from . import ordering
from .engine import TOOLS, OpenAIChatModel, handle_message, retry_delay
from .models import Conversation, ProcessedMessage


def make_restaurant(**kw):
    r = Restaurant.objects.create(
        name="Test Grill", slug=kw.pop("slug", "test"), currency="MAD",
        delivery_fee=Decimal("10"), minimum_order=Decimal("40"), **kw,
    )
    tagine = MenuItem.objects.create(restaurant=r, name="Tagine", price=Decimal("65"))
    juice = MenuItem.objects.create(restaurant=r, name="Orange juice", price=Decimal("18"))
    return r, tagine, juice


def convo_for(r, external_id="2126000000"):
    c = Customer.objects.create(restaurant=r, external_id=external_id, name="Sara")
    return Conversation.objects.create(customer=c)


class OrderingTests(TestCase):
    def setUp(self):
        self.r, self.tagine, self.juice = make_restaurant()
        self.convo = convo_for(self.r)

    def test_totals_come_from_the_menu_not_the_caller(self):
        ordering.add_to_cart(self.convo, self.tagine.id, 2)
        ordering.add_to_cart(self.convo, self.juice.id, 1)
        summary = ordering.set_fulfillment(self.convo, "delivery", "12 Rue de Fes, Tangier")
        self.assertEqual(summary["subtotal"], "148.00 MAD")
        self.assertEqual(summary["delivery_fee"], "10.00 MAD")
        self.assertEqual(summary["total"], "158.00 MAD")

    def test_adding_same_item_merges_quantities(self):
        ordering.add_to_cart(self.convo, self.tagine.id, 1)
        ordering.add_to_cart(self.convo, self.tagine.id, 2)
        self.assertEqual(self.convo.cart, [{"item_id": self.tagine.id, "quantity": 3, "note": ""}])

    def test_rejects_items_from_another_restaurant_and_unavailable_items(self):
        other, other_item, _ = make_restaurant(slug="other")
        with self.assertRaises(ordering.OrderingError):
            ordering.add_to_cart(self.convo, other_item.id)
        self.juice.is_available = False
        self.juice.save()
        with self.assertRaises(ordering.OrderingError):
            ordering.add_to_cart(self.convo, self.juice.id)

    def test_item_sold_out_after_adding_is_dropped_from_cart(self):
        ordering.add_to_cart(self.convo, self.juice.id)
        self.juice.is_available = False
        self.juice.save()
        summary = ordering.cart_summary(self.convo)
        self.assertEqual(summary["lines"], [])
        self.assertEqual(summary["removed_unavailable_items"], [self.juice.id])

    def test_place_order_requires_address_minimum_and_open_restaurant(self):
        ordering.add_to_cart(self.convo, self.juice.id)  # 18 MAD < 40 minimum
        ordering.set_fulfillment(self.convo, "delivery")
        with self.assertRaisesMessage(ordering.OrderingError, "address"):
            ordering.place_order(self.convo)
        ordering.set_fulfillment(self.convo, "delivery", "12 Rue de Fes, Tangier")
        with self.assertRaisesMessage(ordering.OrderingError, "minimum"):
            ordering.place_order(self.convo)
        ordering.add_to_cart(self.convo, self.tagine.id)
        self.r.is_open = False
        self.r.save()
        with self.assertRaisesMessage(ordering.OrderingError, "closed"):
            ordering.place_order(self.convo)

    def test_place_order_snapshots_items_numbers_orders_and_clears_cart(self):
        ordering.add_to_cart(self.convo, self.tagine.id, 1, "no olives")
        ordering.set_fulfillment(self.convo, "pickup")
        order = ordering.place_order(self.convo)
        self.assertEqual(order.number, 1001)
        self.assertEqual(order.total, Decimal("65"))
        self.assertEqual(order.delivery_fee, Decimal("0"))
        item = order.items.get()
        self.assertEqual((item.name, item.note, item.unit_price), ("Tagine", "no olives", Decimal("65")))
        self.assertEqual(self.convo.cart, [])
        # later menu price changes don't touch the placed order
        self.tagine.price = Decimal("99")
        self.tagine.save()
        self.assertEqual(order.items.get().unit_price, Decimal("65"))

        ordering.add_to_cart(self.convo, self.tagine.id)
        self.assertEqual(ordering.place_order(self.convo).number, 1002)


class FakeModel:
    """Scripted stand-in for the OpenAI model: returns queued responses in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append(messages)
        return self.responses.pop(0)


def tool(name, **args):
    return {"id": f"call_{name}", "name": name, "arguments": json.dumps(args)}


class EngineTests(TestCase):
    def setUp(self):
        self.r, self.tagine, self.juice = make_restaurant()

    def test_full_order_through_tool_calls(self):
        model = FakeModel(
            {"content": None, "tool_calls": [tool("add_to_cart", item_id=self.tagine.id, quantity=2)]},
            {"content": None, "tool_calls": [tool("set_fulfillment", method="delivery", address="5 Av. Pasteur, Tangier")]},
            {"content": None, "tool_calls": [tool("place_order")]},
            {"content": "Order #1001 placed! Total 140.00 MAD.", "tool_calls": []},
        )
        reply = handle_message(self.r, "212611111111", "Youssef", "2 tagine, deliver to 5 Av Pasteur, yes confirm", model)
        self.assertIn("#1001", reply)
        order = Order.objects.get()
        self.assertEqual(order.total, Decimal("140"))  # 2×65 + 10 delivery, computed server-side
        self.assertEqual(order.customer.name, "Youssef")
        convo = Conversation.objects.get()
        self.assertEqual([m["role"] for m in convo.history], ["user", "assistant"])
        # the model saw each tool result
        self.assertEqual(model.calls[-1][-1]["role"], "tool")
        self.assertTrue(json.loads(model.calls[-1][-1]["content"])["placed"])

    def test_tool_errors_are_returned_to_the_model_not_raised(self):
        model = FakeModel(
            {"content": None, "tool_calls": [tool("place_order")]},
            {"content": "Your cart is empty — what would you like?", "tool_calls": []},
        )
        reply = handle_message(self.r, "212622222222", "Amina", "order now", model)
        self.assertIn("empty", reply)
        self.assertIn("error", json.loads(model.calls[1][-1]["content"]))
        self.assertFalse(Order.objects.exists())

    def test_menu_is_in_the_system_prompt_and_show_menu_is_gone(self):
        self.juice.is_available = False
        self.juice.save()
        model = FakeModel({"content": "Salam!", "tool_calls": []})
        handle_message(self.r, "212644444444", "Nora", "menu?", model)
        prompt = model.calls[0][0]["content"]
        self.assertIn(f"id {self.tagine.id}: Tagine", prompt)
        self.assertIn("65.00 MAD", prompt)
        self.assertIn(f"id {self.juice.id}: Orange juice", prompt)
        self.assertIn("SOLD OUT", prompt)
        self.assertNotIn("show_menu", [t["function"]["name"] for t in TOOLS])

    def test_saved_cart_is_in_the_prompt_on_the_next_message(self):
        # History keeps only text, so without this the model re-adds items when the customer says "yes".
        handle_message(self.r, "212677777777", "Sami", "2 tagines", FakeModel(
            {"content": None, "tool_calls": [tool("add_to_cart", item_id=self.tagine.id, quantity=2)]},
            {"content": "Added. Confirm?", "tool_calls": []},
        ))
        model = FakeModel({"content": "Delivery or pickup?", "tool_calls": []})
        handle_message(self.r, "212677777777", "Sami", "yes", model)
        prompt = model.calls[0][0]["content"]
        self.assertIn('"quantity": 2', prompt)
        self.assertIn('"subtotal": "130.00 MAD"', prompt)

    def test_several_tool_calls_in_one_response_take_one_round(self):
        model = FakeModel(
            {"content": None, "tool_calls": [
                dict(tool("add_to_cart", item_id=self.tagine.id, quantity=2), id="c1"),
                dict(tool("add_to_cart", item_id=self.juice.id, quantity=1), id="c2"),
                dict(tool("set_fulfillment", method="pickup"), id="c3"),
            ]},
            {"content": "2 tagines + 1 juice, pickup. Total 148.00 MAD. Confirm?", "tool_calls": []},
        )
        handle_message(self.r, "212655555555", "Ali", "2 tagine + 1 juice, pickup", model)
        self.assertEqual(len(model.calls), 2)
        tool_msgs = [m for m in model.calls[1] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ["c1", "c2", "c3"])
        convo = Conversation.objects.get()
        self.assertEqual(ordering.cart_summary(convo)["total"], "148.00 MAD")

    def test_model_failure_gives_a_friendly_reply(self):
        class Broken:
            def complete(self, *a):
                raise TimeoutError("upstream timeout")

        with self.assertLogs("bot.engine", level="ERROR"):
            reply = handle_message(self.r, "212633333333", "Omar", "salam", Broken())
        self.assertIn("trouble", reply)


class RateLimited(Exception):
    """Shaped like an openai.RateLimitError from Gemini: no Retry-After header, delay in the body."""

    status_code = 429

    def __init__(self, seconds):
        super().__init__(f"Quota exceeded. Please retry in {seconds}s.")
        self.response = mock.Mock(headers={})


@override_settings(OPENAI_API_KEY="test-key")
class RateLimitRetryTests(TestCase):
    def setUp(self):
        self.model = OpenAIChatModel()
        self.ok = mock.Mock(choices=[mock.Mock(message=mock.Mock(content="Salam!", tool_calls=None))])

    def test_retries_once_after_the_suggested_delay(self):
        with mock.patch.object(self.model, "_create", side_effect=[RateLimited(3.5), self.ok]) as create, \
                mock.patch("bot.engine.time.sleep") as sleep:
            result = self.model.complete([], TOOLS)
        self.assertEqual(result["content"], "Salam!")
        self.assertEqual(create.call_count, 2)
        sleep.assert_called_once_with(3.5)

    def test_wait_is_capped_and_a_second_429_gives_the_friendly_reply(self):
        r, _, _ = make_restaurant()
        with mock.patch.object(self.model, "_create", side_effect=[RateLimited(35.1), RateLimited(25)]) as create, \
                mock.patch("bot.engine.time.sleep") as sleep, self.assertLogs("bot.engine", level="ERROR"):
            reply = handle_message(r, "212666666666", "Hind", "salam", self.model)
        self.assertIn("trouble", reply)
        self.assertEqual(create.call_count, 2)
        sleep.assert_called_once_with(10)

    def test_other_errors_are_not_retried(self):
        with mock.patch.object(self.model, "_create", side_effect=TimeoutError()) as create, \
                self.assertRaises(TimeoutError):
            self.model.complete([], TOOLS)
        self.assertEqual(create.call_count, 1)

    def test_retry_after_header_is_preferred(self):
        error = RateLimited(30)
        error.response.headers = {"retry-after": "2"}
        self.assertEqual(retry_delay(error), 2)


@override_settings(WHATSAPP_APP_SECRET="app-secret", WHATSAPP_VERIFY_TOKEN="verify-me", WHATSAPP_ACCESS_TOKEN="tok")
class WebhookTests(TestCase):
    def setUp(self):
        self.r, self.tagine, _ = make_restaurant(whatsapp_phone_number_id="PNID1")

    def payload(self, msg_id="wamid.1", text="salam"):
        return {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": "PNID1"},
                "contacts": [{"wa_id": "212644444444", "profile": {"name": "Karim"}}],
                "messages": [{"id": msg_id, "from": "212644444444", "type": "text", "text": {"body": text}}],
            }}]}],
        }

    def post(self, data, secret="app-secret"):
        body = json.dumps(data).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post("/webhooks/whatsapp/", body, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig)

    def test_verification_handshake(self):
        ok = self.client.get("/webhooks/whatsapp/", {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
        self.assertEqual((ok.status_code, ok.content), (200, b"42"))
        bad = self.client.get("/webhooks/whatsapp/", {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
        self.assertEqual(bad.status_code, 403)

    def test_rejects_bad_signature(self):
        with mock.patch("whatsapp.views.handle_message") as handle:
            self.assertEqual(self.post(self.payload(), secret="wrong").status_code, 403)
            handle.assert_not_called()

    def test_routes_to_restaurant_replies_and_ignores_duplicates(self):
        with mock.patch("whatsapp.views.handle_message", return_value="Ahlan Karim!") as handle, \
             mock.patch("whatsapp.views.send_text") as send:
            self.assertEqual(self.post(self.payload()).status_code, 200)
            self.assertEqual(self.post(self.payload()).status_code, 200)  # Meta retry, same message id
        handle.assert_called_once_with(self.r, "212644444444", "Karim", "salam")
        send.assert_called_once_with(self.r, "212644444444", "Ahlan Karim!")
        self.assertEqual(ProcessedMessage.objects.count(), 1)

    def test_non_text_messages_get_a_polite_reply(self):
        data = self.payload()
        data["entry"][0]["changes"][0]["value"]["messages"][0] = {"id": "wamid.2", "from": "212644444444", "type": "audio"}
        with mock.patch("whatsapp.views.handle_message") as handle, mock.patch("whatsapp.views.send_text") as send:
            self.post(data)
        handle.assert_not_called()
        self.assertIn("text", send.call_args.args[2])


class DashboardTests(TestCase):
    def setUp(self):
        self.r, self.tagine, _ = make_restaurant()
        convo = convo_for(self.r, "212655555555")
        ordering.add_to_cart(convo, self.tagine.id)
        ordering.set_fulfillment(convo, "pickup")
        self.order = ordering.place_order(convo)
        self.staff = get_user_model().objects.create_user("chef", password="pw-123456")
        self.r.staff.add(self.staff)

    def set_status(self, status):
        return self.client.post(f"/dashboard/{self.r.slug}/orders/{self.order.id}/status/", {"status": status})

    def test_staff_moves_order_and_customer_is_notified(self):
        self.client.force_login(self.staff)
        with mock.patch("whatsapp.client.send_text") as send:
            self.assertEqual(self.set_status("confirmed").status_code, 200)
        send.assert_called_once()
        self.assertIn("#1001 confirmed", send.call_args.args[2])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")
        self.assertIn("confirmed", Conversation.objects.get().history[-1]["content"])

    def test_invalid_transitions_are_refused(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.set_status("completed").status_code, 400)  # can't skip from new to completed

    def test_other_restaurants_staff_cannot_touch_orders(self):
        outsider = get_user_model().objects.create_user("other", password="pw-123456")
        other, _, _ = make_restaurant(slug="other")
        other.staff.add(outsider)
        self.client.force_login(outsider)
        self.assertEqual(self.set_status("confirmed").status_code, 404)
        self.assertEqual(self.client.get(f"/dashboard/{self.r.slug}/data/").status_code, 404)

    def test_board_requires_login(self):
        self.assertEqual(self.client.get("/dashboard/").status_code, 302)

    def test_superuser_sees_every_restaurant_without_being_staff(self):
        admin = get_user_model().objects.create_superuser("admin", "admin@example.com", "pw-123456")
        self.client.force_login(admin)
        page = self.client.get(f"/dashboard/{self.r.slug}/")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["restaurant"], self.r)
        self.assertEqual(self.client.get(f"/dashboard/{self.r.slug}/data/").status_code, 200)
        with mock.patch("whatsapp.client.send_text"):
            self.assertEqual(self.set_status("confirmed").status_code, 200)


class DeploySetupTests(TestCase):
    """The Docker CMD runs seed_demo and createsuperuser --noinput on every start."""

    def test_seed_demo_is_idempotent_and_keeps_admin_edits(self):
        call_command("seed_demo", stdout=StringIO())
        r = Restaurant.objects.get(slug="dar-tangier")
        r.delivery_fee = Decimal("15")
        r.save()
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Restaurant.objects.count(), 1)
        self.assertEqual(Restaurant.objects.get().delivery_fee, Decimal("15"))
        self.assertEqual(MenuItem.objects.filter(restaurant=r).count(), 15)

    @mock.patch.dict("os.environ", {
        "DJANGO_SUPERUSER_USERNAME": "boss", "DJANGO_SUPERUSER_EMAIL": "boss@example.com",
        "DJANGO_SUPERUSER_PASSWORD": "a-long-deploy-password",
    })
    def test_createsuperuser_from_env_then_fails_cleanly_when_it_exists(self):
        call_command("createsuperuser", interactive=False, stdout=StringIO())
        user = get_user_model().objects.get(username="boss")
        self.assertTrue(user.is_superuser and user.check_password("a-long-deploy-password"))
        # second start: a CommandError, which `|| true` in the Dockerfile swallows
        with self.assertRaises(CommandError):
            call_command("createsuperuser", interactive=False, stdout=StringIO(), stderr=StringIO())


class DemoChatTests(TestCase):
    @override_settings(DEMO_RESTAURANT_SLUG="test", DEMO_MESSAGES_PER_SESSION=2)
    def test_demo_chat_uses_session_customer_and_rate_limits(self):
        make_restaurant()
        self.client.get("/")
        with mock.patch("dashboard.views.handle_message", return_value="Salam!") as handle:
            for _ in range(2):
                self.assertEqual(self.client.post("/demo/send/", {"text": "hi"}).json(), {"reply": "Salam!"})
            self.assertEqual(self.client.post("/demo/send/", {"text": "hi"}).status_code, 429)
        self.assertTrue(handle.call_args.args[1].startswith("web:"))
