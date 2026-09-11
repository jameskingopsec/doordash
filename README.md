# OVIO DD

A Discord Components V2 auto-checkout interface for the [Woolix Order API](https://woolixdoc.com/#tag/Orders/operation/getJob). The visual system is intentionally matched to `direct-aco-new`: one red accent container per screen, the same typography hierarchy, dividers, button language, warning/success colors, progress panels, and the complete application-emoji set.

## Flow

1. Send `!start` and reply on one line with `GROUP LINK, FULL ADDRESS`.
   Comma-separated, space-separated, pipe-separated, and pasted multiline US addresses are normalized automatically.
2. The same message becomes the live order panel while the cart is prepared and priced.
3. Review the items, discounts, fees, tip, and final total. The order is still uncharged.
4. Press **Add Card** on the priced draft and enter the payment details there.
5. Optionally change the note, tip, dropoff preference, fulfillment method, or group cart.
6. **Place Order** shows a final warning panel before submitting the real order.
7. The bot handles successful, declined, failed, transient, and unverified payment outcomes without blindly placing the order twice.

The bot only exposes a user's own in-memory/persisted job mapping. Full card details are held just long enough to send the create/configure request and are never logged or written to `data/sessions.json`.

## Setup

Requires Python 3.11+ and a Discord application with a bot token. Enable the **Message Content Intent** in the Discord Developer Portal because the bot is intentionally slash-commandless.

```bash
cd /Users/christianobora/Documents/Projects/SnkrDevWork/woolix-aco
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Runtime configuration is stored in the tracked `config.json` file (no environment variables). Its Discord token, guild/owner IDs, Woolix key, and public success webhook are already configured for the private deployment repository. Use `config.example.json` as the field reference.

Checkout fees and per-user GetAText settings are stored in the configured Postgres database. Both tables are created automatically, legacy checkout-fee JSON records are imported without duplicating job IDs, and GetAText keys are encrypted before storage.

Tracking buttons use AES-GCM authenticated Relay slugs. The raw order UUID never appears in the customer URL; only the Relay server can recover it using the shared key in the tracked tracking configuration.

The private Discord channel configured by `discord.log_channel_id` receives sanitized startup, command, access-control, order-state, checkout, billing, and nightly-DM activity. Failures include a deduplicated diagnostic attachment with the redacted traceback and response details. Console output and the rotating `data/ovio-dd.log` file use the same credential and payment-data redaction; each local log is capped at 2 MB with three backups.

Only configured owners and whitelisted users can start orders. An owner can use `!whitelist USER_ID 7d`; durations support minutes (`30m`), hours (`12h`), days (`7d`), and weeks (`4w`). Omitting the duration—or using `permanent`—grants permanent access. IDs and expiration times persist across restarts.

Run it with:

```bash
.venv/bin/python main.py
```

## Emoji upload

The 37 image assets copied from `direct-aco-new/data/emoji_assets` live in `data/emoji_assets`. On startup the bot:

- lists the Discord application's existing emojis;
- reuses every `aco_<asset-name>` match;
- uploads only missing PNG/GIF assets;
- swaps the matching Unicode fallback everywhere in the UI;
- skips files over Discord's 256 KB application-emoji limit.

This makes the same assets work in DMs and every guild where the application is installed. The bot token must belong to the Discord application that should own those emojis.

## Commands

- `!start` — begin or reopen an order
- `!dashboard` — add, update, remove, and check the balance of your GetAText connection
- `!whitelist USER_ID [duration]` — grant permanent or timed access (owner only)
- `!revoke USER_ID` — revoke permanent or timed access (owner only)
- `!payments balance USER_ID` — show a user's outstanding checkout-fee balance (owner only)
- `!payments clear USER_ID` — mark all of a user's outstanding checkout fees paid (owner only)

No slash commands are registered.

Successful orders are watched in the background and automatically post a compact green **Order Placed** embed with only the user, total, store, and timestamp. The bot records a **$3.50 fee per successful checkout** and DMs each user a payment demand after the **11 PM America/Los_Angeles** cutoff. Missed deployment windows are caught up automatically, and unpaid balances are demanded again nightly until cleared. Tracking remains private inside the user's order panel; no card, account credentials, or tracking link is included in the public webhook.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q main.py src tests
```
