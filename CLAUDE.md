# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot that monitors a single Hyperliquid vault and a specific depositor's position in it, polling the Hyperliquid API and pushing formatted updates (fills, liquidation warnings, sell-coverage mismatches, on-demand `/status`) to a Telegram chat. `/account` is the one exception to the vault-scoped focus: it reports the depositor's whole Hyperliquid account (spot + perp + every vault they're in), not just their stake in this one vault.

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

Five modules, each with one job:

- **`src/hl_client.py`** — `HyperliquidClient` wraps the `hyperliquid` SDK's `Info` endpoint (mainnet, no websocket). All reads (margin summary, positions, fills, open orders, vault details, candles) go through here.
- **`src/state_tracker.py`** — pure functions and the `VaultState` dataclass. No I/O. This is where almost all logic lives: fill grouping, sell-coverage-gap detection, and every `format_*` function that turns raw API dicts into the monospace `<pre>` tables sent to Telegram. Edit here for message-content changes.
- **`src/chart.py`** — `render_candles` turns raw candle dicts into a dark-themed candlestick+volume PNG (`matplotlib`/`mplfinance`, `Agg` backend so no display is needed).
- **`src/notifier.py`** — thin wrapper around `telegram.Bot.send_message`/`send_photo`, tracks `last_sent_at` (used by the heartbeat check) and enforces quiet hours unless the caller passes `force=True`.
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

Sell fills omit "Distance" (and the fill count) entirely — `format_fill` instead reports either a full close (plus resulting equity) or the exposure remaining on a partial sell. Every fill message is prefixed with a bold "Buy order filled" / "Sell order filled" header so the type reads unambiguously even in a notification preview.

### Open-order numbering

`format_open_orders` only lists buy orders (the DCA ladder) - sell orders are shown in `format_position_status`'s "Sell price" row instead, not repeated here. Its "#" column numbers each coin's resting buy orders 1 (closest to market, next to fill) through N (deepest); reduce-only orders aren't numbered. Two problems had to be solved together, both in `assign_order_numbers`:

- **Numbers must survive fills.** They're assigned once (the first time a set of oids is seen) and then frozen in `state.order_numbers`, keyed by oid - not recomputed by position every cycle - since fills always remove the nearest-to-market (lowest-numbered) order first, and a plain re-sort would renumber every survivor each time that happens.
- **The nearest-to-market rung can fill before the bot ever sees it** (e.g. right after a restart), so a fresh assignment can't just start counting from 1 at whatever's currently open - it would then be permanently off by however many rungs already filled unseen. `assign_order_numbers` instead calibrates against `historical_order_sizes` (fill history since the position last opened from flat) to reconstruct the full rung sequence - the fill that first opened the position counts as #1, and every rung since (filled-and-gone or still open) gets the next number - so only currently-open orders end up displayed, but numbered as if every earlier rung were still visible. Real order sizes showed why this can't just use a stored count of past fills: a DCA ladder's size scales by a consistent multiplier per rung (~2.3x observed, estimated via median ratio to stay robust to one outlier pair), so a rung's absolute position is recoverable from its own size against that pattern - except the deepest rung, which is typically capped to whatever capital remains rather than continuing the multiplier, so it's simply given the number after the previous rung rather than fitted to the formula. The opening fill itself rarely fits that multiplier pattern (its ratio to itself is trivially 1), so each level is floored at "previous level + 1" - otherwise the rung right after the entry could round down onto the same number as the entry instead of following it.

Fetching fill history is only worth its cost when something is actually unnumbered (`needs_fresh_numbering`), so `_update_order_numbers` (`main.py`) gates the lookback fetch behind that check rather than doing it every poll cycle. Because the assignment is calibrated from real fill history rather than derived from whatever's currently open, it's also correct again immediately after a bot restart - unlike a naive "renumber from 1" fallback would be.

A filled order is already gone from `orders` by the time a fill shows up, so `_update_order_numbers` (which only keeps numbers for currently-open oids) would have already dropped it before the fill gets processed. `main.py`'s poll loop snapshots `state.order_numbers` into `prior_order_numbers` *before* calling `_update_order_numbers` each cycle, so a buy fill message can still look up which rung just filled and show it in the Symbol row (`format_fill`'s `order_number` param) - e.g. "CRVUSDC Buy #4".

`format_position_status` also takes `sell_price_by_coin` (built by `main.py`'s `_sell_price_by_coin`, the nearest-to-market resting sell price per coin) and shows it as "Sell price", ordered highest-to-lowest price: Sell price, Current price, Entry price.

### Loss-realizing sell detection

This vault runs [passivbot](https://github.com/enarjord/passivbot), whose ordinary closes only ever trigger above a profit threshold - so any sell fill with negative `closedPnl` is never a normal take-profit; it's most likely passivbot's auto-unstuck mechanism cutting an over-extended position. `format_fill` flags this in the header ("Sell order filled (loss)" vs the normal "Sell order filled").

For context on *why* it happened, `main.py`'s `_update_single_buy_order_tracking` maintains `state.single_buy_order_since` (per coin, when its buy ladder first dropped to exactly one resting order - `buy_order_counts`/`has_single_buy_order_left`'s underlying data). When a loss-realizing sell fill comes in, the poll loop looks up how long that coin had been sitting at one order left and passes it to `format_fill` as `stuck_hours`, shown as a "Stuck" row: a buy ladder that grows through several rungs, then stalls for hours with no further fills, then takes a loss on the next sell - matches the on-chain fingerprint of auto-unstuck triggering on a position whose grid ran out of room. Investigated by reconstructing full position lifecycles from `user_fills_by_time` (paginated past its 2000-record-per-call cap) and cross-referencing passivbot's `docs/config.bot.md`.

`state.unstuck_episode_fills` (per coin, a list of raw fill groups) starts recording once `is_loss_realizing_sell` first fires for that coin, and every subsequent fill for it (either side) gets appended - not just further losses, since the point is to show how the episode resolves (typically one more sell that closes the remainder, often back at a profit). Once there are 2+ entries, the fill message gets a second table appended via `format_unstuck_episode`: Time/Side/Price/Size/PnL for the whole episode so far. The record is cleared when the position returns to flat, same lifecycle as `first_entry_price`.

### Charts

Every buy fill message is sent as a photo (`notifier.send_photo`) with the usual table as its caption, rather than as plain text: `main.py`'s `_render_chart` fetches `CHART_LOOKBACK_MS` (12h) of `CHART_INTERVAL` (15m) candles via `client.get_candles` and renders them with `render_candles`. Sell fills are unaffected (still plain text) - the chart is meant to give a feel for what the market's been doing around a DCA entry, not general market commentary. Chart fetch/render is wrapped in try/except and falls back to a plain `notifier.send` on failure, so a candle-API hiccup or render error doesn't drop the fill notification.

`_render_chart` also overlays the ladder context onto the chart itself: recent buy fills (from a fresh `client.get_fills_since` scoped to the chart's own lookback window, so only fills actually visible on it are marked) as red upside-down triangles above each fill's candle (not at the exact fill price - reads as a "bought here" flag pointing down at the candle rather than sitting inside it). One triangle per resting order that filled, not per partial fill of it - `render_candles` first dedupes `buy_fills` by `oid` (keeping one representative per order) before plotting, so an order filled across several partials still shows as a single triangle; two-plus *different* orders filling in the same candle stack their triangles upward instead of overwriting each other, via a separate `addplot` scatter series per stack level. Each resting buy order gets a dashed red line labeled with its rung number (`state.order_numbers`, so it matches `format_open_orders`'s "#" column); resting sell order(s) get a dashed green line labeled "TP"; the last candle's close always gets a dotted orange "Now" line (`_CURRENT_PRICE_COLOR`) - dotted rather than dashed specifically to stay visually distinct from the order lines. Buy = red, sell = green throughout this overlay (`_BUY_COLOR`/`_SELL_COLOR` in `chart.py`) - deliberately the opposite of the candle body colors, which are about price direction, not order side. The visible y-axis is capped to the candle range plus 15% padding (`price_ax.set_ylim`, after `mpf.plot` - a resting order line far from the current price, e.g. a deep DCA rung or a distant take-profit, would otherwise stretch the axis and squash the candles into an unreadable band). Order lines outside that range just render off the edge rather than resizing the chart, and their "#N"/"TP"/"Now" labels are explicitly skipped when off-range too (matplotlib's `text()` isn't clipped to the axes by default, so an unfiltered label would otherwise float in blank space beyond the visible chart). Fill-triangle stack/base offsets are sized against the candle range alone (the same range the axis is capped to), not the older approach of including order-line prices, so stacking stays proportional to what's actually visible. The "Price"/"Volume" axis labels, the volume panel's numeric y-ticks (now also a smaller panel via `panel_ratios=(6, 1)`), and the x-axis date/time tick labels are all stripped after the plot is built (`price_ax`/`volume_ax` housekeeping in `render_candles`) - deliberately kept minimal; the price y-axis numeric ticks stay, since those are the actual reference needed to read the fill/order-line overlay, not decoration. This is the same chart used everywhere (buy fills, `/status`, heartbeat), so the overlay is identical regardless of which message it's attached to.

`/status` and the heartbeat use `_send_status` (`main.py`), which attaches the first open position's chart as that *same* message's photo caption - one message, not a message plus a trailing photo - falling back to text-only if there's no position or the chart fails to render. Both pass `caption_above=True`/`show_caption_above_media=True` so the text renders first and the chart lands at the bottom, unlike the buy-fill case (`notifier.send_photo`'s default `caption_above=False`) where the chart is the point and sits above the table. `_active_coins` finds which coin(s) currently have an open position; any position beyond the first gets its own caption-less follow-up photo, since a single Telegram message can only have one photo carry a caption. `/status` sends via `update.message.reply_photo`/`reply_text` directly (bypasses the notifier/quiet-hours, same reasoning as the text-only case used to); the heartbeat uses `notifier.send`/`send_photo` so it's held during quiet hours like the rest of the heartbeat.

### `/account`

Account-wide, not vault-specific, and plain text (no chart - deliberately, unlike `/status`/heartbeat/buy fills): account value, HYPE staked (plus its $ value), this depositor's equity in the watched vault, then PnL per period - no volume, that's `/account`'s one deliberate omission from what Hyperliquid's own Portfolio/Staking pages show. `HyperliquidClient.get_portfolio` calls the `portfolio` info endpoint for `USER_ADDRESS` (not the vault), returning value/PnL/volume history per period (`day`/`week`/`month`/`allTime`, plus `perpX` variants scoped to direct perp trading only - this account trades through the vault, so those read as ~$0 and `format_account_summary` skips them). Account value comes from the latest point of the `day` period's `accountValueHistory` (spot + perp + every vault this address is in, combined - the same figure Hyperliquid's UI shows); staked HYPE from `get_staking_summary`'s `delegated` (not `undelegated` or pending-withdrawal, which aren't actively staked), multiplied by `get_mid_prices()["HYPE"]` for its $ value; vault equity reuses `_user_equity(client.get_vault_details())`, the same figure `/status` shows. Each period's % is PnL over that period's *starting* value, omitted when the start is ~0 (`allTime`, since the account began at 0).

Non-dust spot balances (worth $3+) get one row each - just coin name and $ value, no separate table - appended directly after "Vault equity" in the same header block, sorted by value; dust is dropped so a wallet with many near-zero token balances doesn't clutter the summary. Valuing a spot token requires `get_spot_prices`, since `all_mids` only covers perps and raw `"@<index>"` pair names, not spot tokens by their own symbol: it cross-references `spot_meta_and_asset_ctxs`'s `universe` (spot pairs, each naming a base/quote token *index*) against its `tokens` list to find each token's USDC-quoted pair, then reads that pair's `midPx` from the accompanying asset contexts. Token list *position* doesn't reliably match a token's declared `index` (the index space is sparse - hundreds of tokens with indices up into the 800s), so this must go through an index -> name lookup rather than list indexing.

### Money math conventions

- "Exposure" / notional figures are relative to the **whole vault** (`vault_value` from margin summary).
- Dollar PnL and equity are **scaled to this depositor's share** (`equity / vault_value`, called `fraction` in `state_tracker.py`), since the vault pools multiple followers' capital.
- Telegram's `<pre>` blocks can't render bold text inline, so `format_table`/`format_grid` use fixed-width padding for alignment instead of markdown emphasis.

### Hang recovery

The event loop has previously frozen for hours with no crash or log output, because two lower-level clients had no bounded timeouts: the Hyperliquid SDK's `requests` session (synchronous, blocks the whole loop) and python-telegram-bot's own connection pool (observed getting stuck in `CLOSE_WAIT`, wedging both outgoing sends and the `/status` polling loop, since they share one `Bot`/connection pool). Three layers now guard against this recurring silently:

1. `HyperliquidClient` sets `REQUEST_TIMEOUT_SECONDS` (`src/hl_client.py`) on the SDK's session.
2. `TelegramNotifier.send` wraps the send in `asyncio.wait_for(..., SEND_TIMEOUT_SECONDS)` (`src/notifier.py`) as a backstop above the Bot's own per-request timeouts, since those didn't prevent the observed hang.
3. `main.py`'s `_sd_notify` kicks the systemd watchdog (`WATCHDOG=1`) once per completed poll cycle; `deploy/hl-vault-reportbot.service` sets `Type=notify` and `WatchdogSec=240`, so if a cycle ever truly hangs (no kick), systemd force-restarts the process. This is the layer that recovers even from hangs the two timeouts above don't anticipate, since a full process restart is the only way to fully reset the network client's internal state.
