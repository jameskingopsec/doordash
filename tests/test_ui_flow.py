import asyncio
import unittest
import base64
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import time

import discord

from src.api import WoolixApiError
from src.bot import (
    CartMismatchPanel,
    DashboardPanel,
    DraftPanel,
    OrderPlacedPanel,
    RetryOrderPanel,
    UnverifiedPanel,
    WoolixBot,
    card_failure_message,
    parse_discord_user_id,
    parse_start_line,
    parse_whitelist_duration,
)
from src.models import OrderSession
from src.tracking import configure_tracking


class PrefixFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_input_is_one_line(self):
        group_link, address = parse_start_line(
            "https://doordash.com/group/abc, 123 Main St, Chicago, IL 60601"
        )
        self.assertEqual(group_link, "https://doordash.com/group/abc")
        self.assertEqual(address, "123 Main St, Chicago, IL 60601, USA")
        with self.assertRaises(ValueError):
            parse_start_line("https://doordash.com/group/abc")

    async def test_start_input_accepts_common_separators(self):
        expected = ("https://drd.sh/cart/abc/", "4200 W Billy Ct Dr, Lincoln, NE 68524, USA")
        for value in (
            "https://drd.sh/cart/abc/,4200 W Billy ct drive Lincoln, NE 68524",
            "https://drd.sh/cart/abc/ 4200 W Billy ct drive Lincoln, NE 68524",
            "<https://drd.sh/cart/abc/>\n4200 W Billy ct drive Lincoln, NE 68524",
            "https://drd.sh/cart/abc/|4200 W Billy ct drive Lincoln, NE 68524",
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_start_line(value), expected)

    async def test_whitelist_accepts_ids_and_mentions(self):
        self.assertEqual(parse_discord_user_id("123456789012345678"), 123456789012345678)
        self.assertEqual(parse_discord_user_id("<@!123456789012345678>"), 123456789012345678)
        with self.assertRaises(ValueError):
            parse_discord_user_id("not-a-user")

    async def test_whitelist_duration_supports_common_units(self):
        self.assertEqual(parse_whitelist_duration("30m"), 1800)
        self.assertEqual(parse_whitelist_duration("12 hours"), 43200)
        self.assertEqual(parse_whitelist_duration("7d"), 604800)
        self.assertEqual(parse_whitelist_duration("4w"), 2419200)
        self.assertIsNone(parse_whitelist_duration("permanent"))
        self.assertIsNone(parse_whitelist_duration(""))
        with self.assertRaises(ValueError):
            parse_whitelist_duration("tomorrow")

    async def test_start_requires_owner_or_whitelisted_user(self):
        fake_bot = SimpleNamespace(
            settings=SimpleNamespace(
                allowed_user_ids={222, 333},
                allowed_user_expirations={222: int(time.time()) + 3600, 333: int(time.time()) - 1},
                save_runtime=Mock(),
            ),
            is_owner=lambda user_id: user_id == 111,
            queue_error_log=Mock(),
        )
        self.assertTrue(WoolixBot.can_start(fake_bot, 111))
        self.assertTrue(WoolixBot.can_start(fake_bot, 222))
        self.assertFalse(WoolixBot.can_start(fake_bot, 333))
        self.assertNotIn(333, fake_bot.settings.allowed_user_ids)
        self.assertNotIn(333, fake_bot.settings.allowed_user_expirations)
        fake_bot.settings.save_runtime.assert_called_once()

    async def test_revoke_removes_timed_access_and_persists(self):
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(author=SimpleNamespace(id=111), channel=channel)
        fake_bot = SimpleNamespace(
            settings=SimpleNamespace(
                allowed_user_ids={222},
                allowed_user_expirations={222: int(time.time()) + 3600},
                save_runtime=Mock(),
            ),
            is_owner=lambda user_id: user_id == 111,
            clean_input_message=AsyncMock(),
            queue_error_log=Mock(),
            queue_event_log=Mock(),
        )

        await WoolixBot.handle_revoke(fake_bot, message, "222")

        self.assertNotIn(222, fake_bot.settings.allowed_user_ids)
        self.assertNotIn(222, fake_bot.settings.allowed_user_expirations)
        fake_bot.settings.save_runtime.assert_called_once()
        channel.send.assert_awaited_once()

    async def test_revoke_routes_with_any_whitespace(self):
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            content="!revoke\t139850600283242496",
        )
        fake_bot = SimpleNamespace(
            handle_whitelist=AsyncMock(),
            handle_revoke=AsyncMock(),
        )

        await WoolixBot.on_message(fake_bot, message)

        fake_bot.handle_revoke.assert_awaited_once_with(message, "139850600283242496")

    async def test_payments_balance_routes_and_renders(self):
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(
            author=SimpleNamespace(id=111, bot=False),
            channel=channel,
            content="!payments balance 552276900261789718",
        )
        fake_bot = SimpleNamespace(
            is_owner=lambda user_id: user_id == 111,
            clean_input_message=AsyncMock(),
            billing_store=SimpleNamespace(balance_summary=lambda user_id: {
                "user_id": user_id,
                "count": 2,
                "total_cents": 700,
                "records": [],
            }),
            queue_event_log=Mock(),
        )

        await WoolixBot.handle_payments(fake_bot, message, "balance 552276900261789718")

        channel.send.assert_awaited_once()
        panel = channel.send.await_args.kwargs["view"]
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )
        self.assertIn("Outstanding Balance", rendered)
        self.assertIn("$7.00", rendered)
        self.assertIn("552276900261789718", rendered)

    async def test_payments_command_routes_from_message(self):
        message = SimpleNamespace(
            author=SimpleNamespace(bot=False),
            content="!payments balance 552276900261789718",
        )
        fake_bot = SimpleNamespace(
            handle_whitelist=AsyncMock(),
            handle_revoke=AsyncMock(),
            handle_payments=AsyncMock(),
        )

        await WoolixBot.on_message(fake_bot, message)

        fake_bot.handle_payments.assert_awaited_once_with(message, "balance 552276900261789718")

    async def test_payments_clear_marks_balance_paid_and_renders_success(self):
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(
            author=SimpleNamespace(id=111, bot=False),
            channel=channel,
            content="!payments clear 1536043737069650020",
        )
        billing_store = SimpleNamespace(clear_balance=Mock(return_value={
            "user_id": 1536043737069650020,
            "count": 2,
            "total_cents": 700,
            "job_ids": ["one", "two"],
        }))
        fake_bot = SimpleNamespace(
            is_owner=lambda user_id: user_id == 111,
            clean_input_message=AsyncMock(),
            billing_store=billing_store,
            queue_event_log=Mock(),
        )

        await WoolixBot.handle_payments(fake_bot, message, "clear 1536043737069650020")

        billing_store.clear_balance.assert_called_once_with(1536043737069650020)
        panel = channel.send.await_args.kwargs["view"]
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )
        self.assertIn("Balance Cleared", rendered)
        self.assertIn("$7.00", rendered)

    async def test_dashboard_command_routes_from_message(self):
        message = SimpleNamespace(author=SimpleNamespace(bot=False), content="!dashboard")
        fake_bot = SimpleNamespace(
            handle_whitelist=AsyncMock(),
            handle_revoke=AsyncMock(),
            handle_payments=AsyncMock(),
            handle_dashboard=AsyncMock(),
        )

        await WoolixBot.on_message(fake_bot, message)

        fake_bot.handle_dashboard.assert_awaited_once_with(message, "")

    async def test_dashboard_masks_key_and_shows_controls(self):
        raw_key = "1234567890abcdef1234567890"
        panel = DashboardPanel(
            object(),
            123,
            masked_key="1234••••••7890",
            balance="$4.25",
        )
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )
        labels = [item.label for item in panel.walk_children() if isinstance(item, discord.ui.Button)]

        self.assertIn("Account Dashboard", rendered)
        self.assertIn("$4.25", rendered)
        self.assertIn("1234••••••7890", rendered)
        self.assertNotIn(raw_key, rendered)
        self.assertIn("Update GetAText", labels)
        self.assertIn("Remove", labels)

    async def test_hidden_duplicate_panel_has_no_place_order_button(self):
        job = {
            "status": "draft_ready",
            "input": {"tip": 0},
            "cart": {
                "subtotal_cents": 7197,
                "promo_discount_cents": 1000,
                "tax_and_fees_cents": 1107,
                "client_total_cents": 7304,
                "items": [
                    {"name": "Flavor Lineup Wings", "quantity": 1, "unit_price_cents": 2999},
                    {"name": "3 pc Crispy Tender Combo", "quantity": 1, "unit_price_cents": 1199},
                ],
            },
        }
        panel = CartMismatchPanel(object(), OrderSession(user_id=1, job_id="job_1"), job)
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )
        labels = [item.label for item in panel.walk_children() if isinstance(item, discord.ui.Button)]

        self.assertIn("Cart Total Changed", rendered)
        self.assertIn("extra **$29.99**", rendered)
        self.assertNotIn("Place Order", labels)
        self.assertIn("Use Fresh Cart", labels)

    async def test_hidden_duplicate_is_rebuilt_once_automatically(self):
        bad_job = {
            "status": "draft_ready",
            "cart": {
                "subtotal_cents": 7197,
                "items": [
                    {"name": "Wings", "quantity": 1, "unit_price_cents": 2999},
                    {"name": "Tenders", "quantity": 1, "unit_price_cents": 1199},
                ],
            },
        }
        repaired_job = {
            "status": "draft_ready",
            "cart": {
                "subtotal_cents": 4198,
                "items": bad_job["cart"]["items"],
            },
        }
        session = OrderSession(user_id=1, job_id="old_job", group_cart="https://example.com/cart")
        api = SimpleNamespace(
            rebuild_job=AsyncMock(return_value={"new_job_id": "new_job"}),
            wait_for_draft=AsyncMock(return_value=repaired_job),
        )
        fake_bot = SimpleNamespace(
            api=api,
            settings=SimpleNamespace(draft_timeout_seconds=30, poll_interval_seconds=0.1),
            set_session=Mock(),
            queue_event_log=Mock(),
            queue_error_log=Mock(),
        )
        update = AsyncMock()

        result = await WoolixBot.repair_suspected_duplicate(
            fake_bot,
            session,
            bad_job,
            on_update=update,
        )

        self.assertIs(result, repaired_job)
        self.assertTrue(session.integrity_rebuild_attempted)
        self.assertEqual(session.job_id, "new_job")
        api.rebuild_job.assert_awaited_once_with("old_job", "https://example.com/cart")
        api.wait_for_draft.assert_awaited_once()

    async def test_place_order_blocks_hidden_duplicate_before_proceed(self):
        job = {
            "status": "draft_ready",
            "cart": {
                "subtotal_cents": 7197,
                "items": [
                    {"name": "Wings", "quantity": 1, "unit_price_cents": 2999},
                    {"name": "Tenders", "quantity": 1, "unit_price_cents": 1199},
                ],
            },
        }
        session = OrderSession(user_id=1, job_id="job_1", state="draft_ready")
        response = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        interaction = SimpleNamespace(response=response, edit_original_response=AsyncMock())
        api = SimpleNamespace(get_job=AsyncMock(return_value=job), proceed_job=AsyncMock())
        lock = asyncio.Lock()
        fake_bot = SimpleNamespace(
            sessions={1: session},
            api=api,
            lock_for=lambda _user_id: lock,
            remember_main_interaction=Mock(),
            set_session=Mock(),
            queue_event_log=Mock(),
            queue_error_log=Mock(),
        )

        await WoolixBot.place_order(fake_bot, interaction, session)

        api.proceed_job.assert_not_awaited()
        blocked_panel = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertIsInstance(blocked_panel, CartMismatchPanel)

    async def test_place_order_rejects_stale_panel(self):
        old = OrderSession(user_id=1, job_id="old_job", state="draft_ready")
        active = OrderSession(user_id=1, job_id="current_job", state="draft_ready")
        response = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        interaction = SimpleNamespace(response=response)
        fake_bot = SimpleNamespace(sessions={1: active})

        await WoolixBot.place_order(fake_bot, interaction, old)

        response.send_message.assert_awaited_once()
        self.assertIn("old checkout panel", response.send_message.await_args.args[0])

    async def test_cancel_clears_active_session_so_start_can_begin_again(self):
        session = OrderSession(user_id=1, job_id="job_1", state="draft_ready")
        response = SimpleNamespace(
            is_done=Mock(return_value=False),
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            response=response,
            message=object(),
            edit_original_response=AsyncMock(),
        )
        fake_bot = SimpleNamespace(
            sessions={1: session},
            api=SimpleNamespace(cancel_job=AsyncMock(return_value={}), get_job=AsyncMock()),
            clear_session=Mock(return_value=True),
            queue_event_log=Mock(),
            queue_error_log=Mock(),
        )

        await WoolixBot.cancel_session(fake_bot, interaction, session)

        fake_bot.api.cancel_job.assert_awaited_once_with("job_1")
        fake_bot.clear_session.assert_called_once_with(1, expected_job_id="job_1")
        rendered = interaction.edit_original_response.await_args.kwargs["view"]
        text = " ".join(
            child.content for child in rendered.container.children if hasattr(child, "content")
        )
        self.assertIn("Cancelled", text)

    async def test_cancel_conflict_discards_non_committed_job_instead_of_showing_409(self):
        session = OrderSession(user_id=1, job_id="job_1", state="draft_ready")
        response = SimpleNamespace(
            is_done=Mock(return_value=False),
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            response=response,
            message=object(),
            edit_original_response=AsyncMock(),
        )
        api = SimpleNamespace(
            cancel_job=AsyncMock(side_effect=WoolixApiError(409, {"error": "already updated"})),
            get_job=AsyncMock(return_value={"status": "failed", "payment_status": "declined"}),
        )
        fake_bot = SimpleNamespace(
            sessions={1: session},
            api=api,
            clear_session=Mock(return_value=True),
            queue_event_log=Mock(),
            queue_error_log=Mock(),
        )

        await WoolixBot.cancel_session(fake_bot, interaction, session)

        api.get_job.assert_awaited_once_with("job_1")
        fake_bot.clear_session.assert_called_once_with(1, expected_job_id="job_1")
        text = " ".join(
            child.content
            for child in interaction.edit_original_response.await_args.kwargs["view"].container.children
            if hasattr(child, "content")
        )
        self.assertIn("Cancelled", text)
        self.assertNotIn("Order Already Updated", text)

    async def test_cancel_conflict_preserves_order_that_is_already_placing(self):
        session = OrderSession(user_id=1, job_id="job_1", state="draft_ready")
        response = SimpleNamespace(
            is_done=Mock(return_value=False),
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            response=response,
            message=object(),
            edit_original_response=AsyncMock(),
        )
        latest = {"status": "PLACING", "payment_status": "verifying"}
        active_panel = Mock()
        fake_bot = SimpleNamespace(
            sessions={1: session},
            api=SimpleNamespace(
                cancel_job=AsyncMock(side_effect=WoolixApiError(409, {"error": "already updated"})),
                get_job=AsyncMock(return_value=latest),
            ),
            clear_session=Mock(return_value=True),
            panel_for_job=Mock(return_value=active_panel),
            set_session=Mock(),
            queue_event_log=Mock(),
            queue_error_log=Mock(),
        )

        await WoolixBot.cancel_session(fake_bot, interaction, session)

        fake_bot.clear_session.assert_not_called()
        fake_bot.set_session.assert_called_once_with(session)
        fake_bot.panel_for_job.assert_called_once_with(session, latest)
        interaction.edit_original_response.assert_awaited_with(view=active_panel)

    async def test_clear_session_persists_removal_and_protects_a_newer_order(self):
        current = OrderSession(user_id=1, job_id="current_job", state="draft_ready")
        store = SimpleNamespace(save=Mock())
        fake_bot = SimpleNamespace(
            sessions={1: current},
            logged_session_states={1: ("draft_ready", "current_job")},
            session_store=store,
        )

        removed = WoolixBot.clear_session(fake_bot, 1, expected_job_id="old_job")

        self.assertFalse(removed)
        self.assertIs(fake_bot.sessions[1], current)
        store.save.assert_not_called()

        removed = WoolixBot.clear_session(fake_bot, 1, expected_job_id="current_job")

        self.assertTrue(removed)
        self.assertNotIn(1, fake_bot.sessions)
        self.assertNotIn(1, fake_bot.logged_session_states)
        store.save.assert_called_once()
        self.assertEqual(list(store.save.call_args.args[0]), [])

    async def test_pending_confirmation_hides_technical_order_details(self):
        job = {
            "order_uuid": "private-order-id",
            "account_email": "private@example.com",
            "account_password": "private-password",
            "account_phone": "5555555555",
            "tracking_url": "https://example.com/track/private",
        }
        panel = UnverifiedPanel(object(), OrderSession(user_id=1), job)
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )
        labels = [item.label for item in panel.walk_children() if isinstance(item, discord.ui.Button)]

        self.assertIn("Finishing Your Order", rendered)
        self.assertNotIn("Final Outcome Unknown", rendered)
        self.assertNotIn("private-order-id", rendered)
        self.assertNotIn("private@example.com", rendered)
        self.assertNotIn("private-password", rendered)
        self.assertNotIn("Track Order", labels)

    async def test_placed_panel_uses_encrypted_ovio_tracker_link(self):
        secret = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
        configure_tracking("https://food-order-tracker.up.railway.app/", secret)
        order_id = "d8172e5d-ed0c-417f-823e-ad255ff28132"
        panel = OrderPlacedPanel(
            object(),
            OrderSession(user_id=1, job_id="job_1"),
            {
                "order_uuid": order_id,
                "account_email": "customer@example.com",
                "account_password": "secret-password",
                "account_phone": "5555555555",
                "payment_status": "succeeded",
                "cart": {
                    "store_name": "Test Store",
                    "client_total_cents": 1000,
                    "items": [{"name": "Private item", "quantity": 1, "unit_price_cents": 1000}],
                },
            },
        )
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )
        tracker_buttons = [
            item for item in panel.walk_children()
            if isinstance(item, discord.ui.Button) and item.label == "Track Order"
        ]
        self.assertEqual(len(tracker_buttons), 1)
        self.assertTrue(tracker_buttons[0].url.startswith("https://food-order-tracker.up.railway.app/?order=v1."))
        self.assertNotIn(order_id, tracker_buttons[0].url)
        self.assertIn("customer@example.com", rendered)
        self.assertIn("$10.00", rendered)
        self.assertNotIn(order_id, rendered)
        self.assertNotIn("secret-password", rendered)
        self.assertNotIn("5555555555", rendered)
        self.assertNotIn("Private item", rendered)

    async def test_draft_falls_back_to_add_card_when_missing(self):
        job = {"status": "awaiting_config", "cart": {"items": []}}
        without_card = DraftPanel(object(), OrderSession(user_id=1), job)
        labels = [item.label for item in without_card.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertIn("Add Card", labels)
        self.assertNotIn("Place Order", labels)

        with_card = DraftPanel(object(), OrderSession(user_id=1, card_last4="1111"), job)
        labels = [item.label for item in with_card.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertIn("Place Order", labels)
        self.assertNotIn("Add Card", labels)

    async def test_draft_shows_detailed_delivery_and_payment_summary(self):
        session = OrderSession(
            user_id=1,
            name="Customer",
            address="123 Main St, Chicago, IL 60601, USA",
            unit="Apt 4",
            delivery_note="Leave at door",
            card_last4="1111",
        )
        job = {
            "status": "draft_ready",
            "cart": {
                "store_name": "Test Store",
                "subtotal_cents": 1000,
                "promo_discount_cents": 200,
                "tax_and_fees_cents": 150,
                "client_total_cents": 950,
                "items": [{"name": "Test Item", "quantity": 1, "unit_price_cents": 1000}],
            },
        }

        panel = DraftPanel(object(), session, job)
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )

        self.assertIn("Customer", rendered)
        self.assertIn("123 Main St", rendered)
        self.assertIn("Apt 4", rendered)
        self.assertIn("Leave at door", rendered)
        self.assertIn("1111", rendered)
        self.assertIn("Test Item", rendered)
        self.assertIn("Subtotal", rendered)
        self.assertIn("Checkout fee", rendered)

    async def test_capacity_failure_keeps_setup_visible_then_shows_neutral_error(self):
        session = OrderSession(user_id=1, group_cart="https://example.com/cart", address="123 Main St")
        main = SimpleNamespace(edit=AsyncMock())
        lock = asyncio.Lock()
        fake_bot = SimpleNamespace(
            api=SimpleNamespace(
                create_job=AsyncMock(side_effect=WoolixApiError(402, {"error": "no credits"})),
            ),
            lock_for=lambda _user_id: lock,
            set_session=Mock(),
            queue_error_log=Mock(),
        )

        with patch("src.bot.asyncio.sleep", new_callable=AsyncMock) as delay:
            await WoolixBot.build_draft_from_message(fake_bot, main, session)

        self.assertEqual(main.edit.await_count, 2)
        delay.assert_awaited_once()
        first_panel = main.edit.await_args_list[0].kwargs["view"]
        final_panel = main.edit.await_args_list[-1].kwargs["view"]
        first_text = " ".join(
            child.content for child in first_panel.container.children if hasattr(child, "content")
        )
        final_text = " ".join(
            child.content for child in final_panel.container.children if hasattr(child, "content")
        )
        self.assertIn("Setting Up Order", first_text)
        self.assertIn("Connection Setup Failed", final_text)
        self.assertIn("Contact the developer", final_text)
        self.assertNotIn("credit", final_text.lower())
        self.assertNotIn("wool", final_text.lower())

    async def test_success_post_is_not_blocked_by_slow_billing_storage(self):
        session = OrderSession(user_id=1, job_id="job_1")
        job = {
            "payment_status": "succeeded",
            "cart": {"store_name": "Test Store", "client_total_cents": 1000},
        }
        release_billing = threading.Event()
        events: list[str] = []

        def record_checkout(_user_id, _job_id):
            events.append("billing-start")
            release_billing.wait(timeout=1)
            events.append("billing-end")
            return True

        async def send_success(_session, _job):
            events.append("post")
            release_billing.set()

        fake_bot = SimpleNamespace(
            announcement_lock_for=lambda _user_id: asyncio.Lock(),
            notifier=SimpleNamespace(send=AsyncMock(side_effect=send_success)),
            billing_store=SimpleNamespace(record_checkout=Mock(side_effect=record_checkout)),
            set_session=Mock(),
            queue_event_log=Mock(),
            queue_error_log=Mock(),
        )

        await WoolixBot.maybe_announce_success(fake_bot, session, job)

        self.assertIn("post", events)
        self.assertIn("billing-end", events)
        self.assertLess(events.index("post"), events.index("billing-end"))
        self.assertTrue(session.success_announced)
        fake_bot.notifier.send.assert_awaited_once_with(session, job)

    async def test_draft_hides_promo_and_cart_reuse_callouts(self):
        job = {
            "status": "awaiting_config",
            "cart": {
                "items": [],
                "applied_promos": ["45% off"],
                "custom_promo_note": "$0 delivery fee",
            },
            "decline_risk": {"message": "Success rate: 34% — this cart link was used before."},
        }
        panel = DraftPanel(object(), OrderSession(user_id=1), job)
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )
        self.assertNotIn("Applied:", rendered)
        self.assertNotIn("45% off", rendered)
        self.assertNotIn("Success rate", rendered)
        self.assertNotIn("cart link was used", rendered)

    async def test_nested_card_response_is_customer_friendly(self):
        error = WoolixApiError(400, {
            "message": "Error calling payment service",
            "response": {"error_code": "card_declined", "grpcStatus": "INVALID_ARGUMENT"},
        })
        self.assertEqual(card_failure_message(error), "Your card was declined.")

    async def test_declined_payment_panel_shows_payment_reason(self):
        job = {
            "status": "failed",
            "payment_status": "declined",
            "payment_error": {"decline_reason": "insufficient_funds"},
        }
        panel = RetryOrderPanel(object(), OrderSession(user_id=1), job)
        rendered = " ".join(
            child.content for child in panel.container.children if hasattr(child, "content")
        )
        self.assertIn("Payment Declined", rendered)
        self.assertIn("insufficient funds", rendered)
        self.assertNotIn("fresh group cart", rendered)


if __name__ == "__main__":
    unittest.main()
