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

In production this runs as the systemd unit in `deploy/hl-vault-reportbot.service` (installed at `/etc/systemd/system/`). After editing that file, reinstall with `sudo cp deploy/hl-vault-reportbot.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart hl-vault-reportbot`. Code changes also require a `systemctl restart` to take effect - there's no auto-reload.

## Configuration

All config is env vars loaded via `.env` (see `.env.example`), read once in `src/config.py`:

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — bot credentials and destination chat.
- `VAULT_ADDRESS` — the Hyperliquid vault being watched.
- `USER_ADDRESS` — the depositor whose share of the vault is reported (equity, scaled PnL).
- `POLL_INTERVAL_SECONDS`, `LIQUIDATION_WARN_THRESHOLD`, `HEARTBEAT_INTERVAL_SECONDS`, `HEARTBEAT_FAST_INTERVAL_SECONDS` — tunables, all with defaults.

Quiet hours (23:00–06:00 local time, hardcoded in `src/notifier.py`) hold all non-critical sends. Liquidation warnings and sell-coverage-mismatch alerts pass `force=True` to `TelegramNotifier.send` to bypass this; the `/status` command replies directly via `update.message.reply_text` rather than through the notifier, so it's unaffected either way.

## Architecture

Four modules, each with one job:

- **`src/hl_client.py`** — `HyperliquidClient` wraps the `hyperliquid` SDK's `Info` endpoint (mainnet, no websocket). All reads (margin summary, positions, fills, open orders, vault details) go through here.
- **`src/state_tracker.py`** — pure functions and the `VaultState` dataclass. No I/O. This is where almost all logic lives: fill grouping, sell-coverage-gap detection, and every `format_*` function that turns raw API dicts into the monospace `<pre>` tables sent to Telegram. Edit here for message-content changes.
- **`src/notifier.py`** — thin wrapper around `telegram.Bot.send_message`, tracks `last_sent_at` (used by the heartbeat check) and enforces quiet hours unless the caller passes `force=True`.
- **`main.py`** — wires the above together: `run()` builds the client/notifier/state and a `python-telegram-bot` `Application` (for the `/status` command handler), then runs `poll_loop()` forever alongside it.

### Poll loop (`main.py:poll_loop`)

Runs every `POLL_INTERVAL_SECONDS` and, per cycle:

1. Checks margin ratio against `LIQUIDATION_WARN_THRESHOLD`, sends a one-shot warning (latched by `state.liquidation_warned` until margin recovers).
2. Fetches fills since the last-seen timestamp, dedupes against `state.seen_fill_hashes`, groups partial fills of the same order (`group_fills_by_order`) into a single message.
3. Checks for a long position whose resting sell orders don't cover its full size (`find_sell_coverage_gap`) — skipped for a cycle in which any fill just happened, since the trading bot needs a moment to resize orders and the positions/orders endpoints can be transiently out of sync. Only warns once the gap has been seen on `SELL_COVERAGE_GAP_STREAK_THRESHOLD` (3) consecutive cycles (`state.sell_coverage_gap_streak`), since it's often resolved by the next cycle on its own; any cycle without the gap resets the streak. Latched similarly to the liquidation warning.
4. Sends a heartbeat status message if nothing has been sent in `HEARTBEAT_INTERVAL_SECONDS` — or in `HEARTBEAT_FAST_INTERVAL_SECONDS` if any coin currently has exactly one resting buy order (`has_single_buy_order_left`), signaling its DCA ladder is down to its last rung and worth watching more closely.

### "Distance" and first-entry-price tracking

Buy-side "Distance" is measured from the price of the fill that *originally opened* the position from flat — not the blended average entry price, which shifts with every DCA buy. This price is tracked in `state.first_entry_price[coin]`:

- Seeded at startup from fill history (`find_position_open_price`, 30-day lookback via `FIRST_ENTRY_LOOKBACK_MS`) so it's correct even if the bot was offline when the position opened.
- Updated live whenever a fill opens a position from flat (`startPosition == 0`).
- Cleared when a position returns to flat.

Sell fills omit "Distance" (and the fill count) entirely — `format_fill` instead reports either a full close (plus resulting equity) or the exposure remaining on a partial sell, prefixed with a bold "Sell" header so it reads unambiguously even in a notification preview.

### Open-order numbering

`format_open_orders`'s "#" column numbers each coin's resting buy orders 1 (closest to market, next to fill) through N (deepest); sells and reduce-only orders aren't numbered. Two problems had to be solved together, both in `assign_order_numbers`:

- **Numbers must survive fills.** They're assigned once (the first time a set of oids is seen) and then frozen in `state.order_numbers`, keyed by oid - not recomputed by position every cycle - since fills always remove the nearest-to-market (lowest-numbered) order first, and a plain re-sort would renumber every survivor each time that happens.
- **The nearest-to-market rung can fill before the bot ever sees it** (e.g. right after a restart), so a fresh assignment can't just start counting from 1 at whatever's currently open - it would then be permanently off by however many rungs already filled unseen. `assign_order_numbers` instead calibrates against `historical_order_sizes` (fill history since the position last opened from flat) to reconstruct the full rung sequence - the fill that first opened the position counts as #1, and every rung since (filled-and-gone or still open) gets the next number - so only currently-open orders end up displayed, but numbered as if every earlier rung were still visible. Real order sizes showed why this can't just use a stored count of past fills: a DCA ladder's size scales by a consistent multiplier per rung (~2.3x observed, estimated via median ratio to stay robust to one outlier pair), so a rung's absolute position is recoverable from its own size against that pattern - except the deepest rung, which is typically capped to whatever capital remains rather than continuing the multiplier, so it's simply given the number after the previous rung rather than fitted to the formula. The opening fill itself rarely fits that multiplier pattern (its ratio to itself is trivially 1), so each level is floored at "previous level + 1" - otherwise the rung right after the entry could round down onto the same number as the entry instead of following it.

Fetching fill history is only worth its cost when something is actually unnumbered (`needs_fresh_numbering`), so `_update_order_numbers` (`main.py`) gates the lookback fetch behind that check rather than doing it every poll cycle. Because the assignment is calibrated from real fill history rather than derived from whatever's currently open, it's also correct again immediately after a bot restart - unlike a naive "renumber from 1" fallback would be.

`format_position_status` also takes `sell_price_by_coin` (built by `main.py`'s `_sell_price_by_coin`, the nearest-to-market resting sell price per coin) and shows it as "Sell price", ordered highest-to-lowest price: Sell price, Current price, Entry price.

### Money math conventions

- "Exposure" / notional figures are relative to the **whole vault** (`vault_value` from margin summary).
- Dollar PnL and equity are **scaled to this depositor's share** (`equity / vault_value`, called `fraction` in `state_tracker.py`), since the vault pools multiple followers' capital.
- Telegram's `<pre>` blocks can't render bold text inline, so `format_table`/`format_grid` use fixed-width padding for alignment instead of markdown emphasis.

### Hang recovery

The event loop has previously frozen for hours with no crash or log output, because two lower-level clients had no bounded timeouts: the Hyperliquid SDK's `requests` session (synchronous, blocks the whole loop) and python-telegram-bot's own connection pool (observed getting stuck in `CLOSE_WAIT`, wedging both outgoing sends and the `/status` polling loop, since they share one `Bot`/connection pool). Three layers now guard against this recurring silently:

1. `HyperliquidClient` sets `REQUEST_TIMEOUT_SECONDS` (`src/hl_client.py`) on the SDK's session.
2. `TelegramNotifier.send` wraps the send in `asyncio.wait_for(..., SEND_TIMEOUT_SECONDS)` (`src/notifier.py`) as a backstop above the Bot's own per-request timeouts, since those didn't prevent the observed hang.
3. `main.py`'s `_sd_notify` kicks the systemd watchdog (`WATCHDOG=1`) once per completed poll cycle; `deploy/hl-vault-reportbot.service` sets `Type=notify` and `WatchdogSec=240`, so if a cycle ever truly hangs (no kick), systemd force-restarts the process. This is the layer that recovers even from hangs the two timeouts above don't anticipate, since a full process restart is the only way to fully reset the network client's internal state.
