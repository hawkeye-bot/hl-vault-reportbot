# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot that monitors a single Hyperliquid vault and a specific depositor's position in it, polling the Hyperliquid API and pushing formatted updates (fills, liquidation warnings, sell-coverage mismatches, on-demand `/status`) to a Telegram chat.

## Running

```bash
python -m venv .venv && source .venv/bin/activate   # first time
pip install -r requirements.txt
cp .env.example .env   # then fill in real values
python main.py
```

There are no tests, linter, or build step configured in this repo.

## Configuration

All config is env vars loaded via `.env` (see `.env.example`), read once in `src/config.py`:

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — bot credentials and destination chat.
- `VAULT_ADDRESS` — the Hyperliquid vault being watched.
- `USER_ADDRESS` — the depositor whose share of the vault is reported (equity, scaled PnL).
- `POLL_INTERVAL_SECONDS`, `LIQUIDATION_WARN_THRESHOLD`, `HEARTBEAT_INTERVAL_SECONDS` — tunables, all with defaults.

## Architecture

Four modules, each with one job:

- **`src/hl_client.py`** — `HyperliquidClient` wraps the `hyperliquid` SDK's `Info` endpoint (mainnet, no websocket). All reads (margin summary, positions, fills, open orders, vault details) go through here.
- **`src/state_tracker.py`** — pure functions and the `VaultState` dataclass. No I/O. This is where almost all logic lives: fill grouping, sell-coverage-gap detection, and every `format_*` function that turns raw API dicts into the monospace `<pre>` tables sent to Telegram. Edit here for message-content changes.
- **`src/notifier.py`** — thin wrapper around `telegram.Bot.send_message`, tracks `last_sent_at` (used by the heartbeat check).
- **`main.py`** — wires the above together: `run()` builds the client/notifier/state and a `python-telegram-bot` `Application` (for the `/status` command handler), then runs `poll_loop()` forever alongside it.

### Poll loop (`main.py:poll_loop`)

Runs every `POLL_INTERVAL_SECONDS` and, per cycle:

1. Checks margin ratio against `LIQUIDATION_WARN_THRESHOLD`, sends a one-shot warning (latched by `state.liquidation_warned` until margin recovers).
2. Fetches fills since the last-seen timestamp, dedupes against `state.seen_fill_hashes`, groups partial fills of the same order (`group_fills_by_order`) into a single message.
3. Checks for a long position whose resting sell orders don't cover its full size (`find_sell_coverage_gap`) — skipped for a cycle in which any fill just happened, since the trading bot needs a moment to resize orders and the positions/orders endpoints can be transiently out of sync. Latched similarly to the liquidation warning.
4. Sends a heartbeat status message if nothing has been sent in `HEARTBEAT_INTERVAL_SECONDS`.

### "Distance" and first-entry-price tracking

Buy-side "Distance" is measured from the price of the fill that *originally opened* the position from flat — not the blended average entry price, which shifts with every DCA buy. This price is tracked in `state.first_entry_price[coin]`:

- Seeded at startup from fill history (`find_position_open_price`, 30-day lookback via `FIRST_ENTRY_LOOKBACK_MS`) so it's correct even if the bot was offline when the position opened.
- Updated live whenever a fill opens a position from flat (`startPosition == 0`).
- Cleared when a position returns to flat.

Sell-side "Distance" instead uses the position's current blended entry price.

### Money math conventions

- "Exposure" / notional figures are relative to the **whole vault** (`vault_value` from margin summary).
- Dollar PnL and equity are **scaled to this depositor's share** (`equity / vault_value`, called `fraction` in `state_tracker.py`), since the vault pools multiple followers' capital.
- Telegram's `<pre>` blocks can't render bold text inline, so `format_table`/`format_grid` use fixed-width padding for alignment instead of markdown emphasis.
