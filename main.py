import asyncio
import functools
import logging
import os
import socket
import time

import requests
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from src.config import (
    HEARTBEAT_FAST_INTERVAL_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    LIQUIDATION_WARN_THRESHOLD,
    POLL_INTERVAL_SECONDS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    USER_ADDRESS,
    VAULT_ADDRESS,
)
from src.chart import render_candles
from src.hl_client import HyperliquidClient
from src.notifier import TelegramNotifier
from src.state_tracker import (
    VaultState,
    assign_order_numbers,
    buy_order_counts,
    fills_since_position_opened,
    find_position_open_price,
    find_sell_coverage_gap,
    format_fill,
    format_open_orders,
    format_account_summary,
    format_position_status,
    format_sell_coverage_gap,
    format_table,
    format_unstuck_episode,
    group_fills_by_order,
    has_single_buy_order_left,
    historical_order_sizes,
    is_loss_realizing_sell,
    needs_fresh_numbering,
    position_after_fills,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

BOT_KEYBOARD = ReplyKeyboardMarkup(
    [["/status", "/account"]], resize_keyboard=True, is_persistent=True
)
FIRST_ENTRY_LOOKBACK_MS = 30 * 24 * 60 * 60 * 1000  # 30 days
SELL_COVERAGE_GAP_STREAK_THRESHOLD = 3  # consecutive cycles before warning
CHART_INTERVAL = "15m"
CHART_LOOKBACK_MS = 12 * 60 * 60 * 1000  # 12 hours of 15m candles on buy fill charts
EUR_RATE_TIMEOUT_SECONDS = 10


def _sd_notify(message: str) -> None:
    """Minimal systemd sd_notify client (no extra dependency). No-op outside
    a systemd unit with Type=notify and a watchdog configured - see
    hl-vault-reportbot.service. Used to signal readiness and to kick the
    watchdog every poll cycle, so systemd force-restarts the process if the
    event loop ever wedges (e.g. a hung network connection) instead of it
    silently going unresponsive forever.
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(addr)
        sock.sendall(message.encode())


def _vault_value(margin: dict) -> float | None:
    account_value = margin.get("accountValue")
    return float(account_value) if account_value else None


def _user_equity(details: dict) -> float | None:
    equity_str = (details.get("followerState") or {}).get("vaultEquity")
    return float(equity_str) if equity_str is not None else None


def _vault_all_time_pnl(details: dict) -> float | None:
    """This depositor's cumulative profit in the vault since joining it -
    "allTimePnl" in followerState, not to be confused with "pnl" (a more
    recent-window figure the API also returns alongside it).
    """
    pnl_str = (details.get("followerState") or {}).get("allTimePnl")
    return float(pnl_str) if pnl_str is not None else None


def _update_order_numbers(
    client: HyperliquidClient, state: VaultState, orders: list[dict], positions: list[dict]
) -> None:
    """Keep state.order_numbers current, fetching fill history to calibrate
    a fresh assignment only when some order isn't numbered yet (rare - see
    needs_fresh_numbering) rather than every cycle.

    A coin with no open position right now (flat) gets an empty history
    even if it has a resting order - passivbot places a fresh re-entry
    order the moment a position closes, and fill history alone can't tell
    "flat now, about to start a new cycle" from "still mid-cycle", since
    both just look like fills since the last startPosition==0 fill. Without
    this, a flat coin's brand-new re-entry order would inherit the *previous*,
    already-closed cycle's history and get numbered as a continuation of it
    (e.g. #8) instead of starting the new cycle at #1.
    """
    historical_sizes = {}
    if needs_fresh_numbering(orders, state.order_numbers):
        active_coins = set(_active_coins(positions))
        lookback_fills = client.get_fills_since(int(time.time() * 1000) - FIRST_ENTRY_LOOKBACK_MS)
        coins = {
            o.get("coin") for o in orders if o.get("side") == "B" and not o.get("reduceOnly")
        }
        historical_sizes = {
            coin: historical_order_sizes(lookback_fills, coin) if coin in active_coins else []
            for coin in coins
        }
    state.order_numbers = assign_order_numbers(orders, state.order_numbers, historical_sizes)


def _entry_price_by_coin(positions: list[dict]) -> dict[str, float]:
    return {
        ap["position"]["coin"]: float(ap["position"].get("entryPx", 0) or 0)
        for ap in positions
        if ap.get("position", {}).get("coin")
    }


def _update_single_buy_order_tracking(state: VaultState, orders: list[dict]) -> None:
    """Track, per coin, how long its DCA ladder has been down to exactly one
    resting buy order - the state a coin is in right before passivbot's
    auto-unstuck mechanism would act on an over-extended position. Consulted
    by format_fill when a loss-realizing sell fill comes in, to show how long
    the ladder had been stalled beforehand.
    """
    counts = buy_order_counts(orders)
    now = time.monotonic()
    for coin in list(state.single_buy_order_since):
        if counts.get(coin) != 1:
            state.single_buy_order_since.pop(coin, None)
    for coin, count in counts.items():
        if count == 1:
            state.single_buy_order_since.setdefault(coin, now)


def _sell_price_by_coin(orders: list[dict]) -> dict[str, float]:
    """The nearest-to-market resting sell price per coin (lowest, since a
    sell fills as price rises toward it), for the position status message.
    """
    prices: dict[str, float] = {}
    for o in orders:
        if o.get("side") != "A":
            continue
        coin = o.get("coin")
        price = float(o.get("limitPx", 0) or 0)
        if coin and (coin not in prices or price < prices[coin]):
            prices[coin] = price
    return prices


def _active_coins(positions: list[dict]) -> list[str]:
    coins = []
    for ap in positions:
        pos = ap.get("position", {})
        coin = pos.get("coin")
        if coin and float(pos.get("szi", 0) or 0) != 0:
            coins.append(coin)
    return coins


def _pending_entry_coin(positions: list[dict], orders: list[dict]) -> str | None:
    """The coin of a resting, non-reduce-only buy order for a coin with no
    open position right now - the lone re-entry attempt passivbot places
    the moment a position closes. In practice at most one such order/coin
    exists at a time (this vault only ever trades one coin at once), so the
    first match is enough - used to fill format_position_status's Symbol
    row when otherwise blank.
    """
    active = set(_active_coins(positions))
    for o in orders:
        if o.get("side") == "B" and not o.get("reduceOnly") and o.get("coin") not in active:
            return o.get("coin")
    return None


async def _render_chart(
    client: HyperliquidClient,
    coin: str,
    orders: list[dict],
    order_numbers: dict[int, int],
    entry_price_by_coin: dict[str, float] | None = None,
) -> bytes | None:
    """Chart for `coin` with fills, resting buy rungs, resting sell(s), and
    the position's entry price overlaid - see render_candles. Buy fills
    are limited to the chart's own lookback window, matching what's
    actually visible on it - and further scoped to the position that's
    still open (fills_since_position_opened), since a coin can close and
    reopen more than once within that window and only the fills from the
    current, still-open cycle should get a "bought here" marker.
    """
    try:
        candles = client.get_candles(coin, CHART_INTERVAL, CHART_LOOKBACK_MS)
        recent_fills = client.get_fills_since(int(time.time() * 1000) - CHART_LOOKBACK_MS)
        current_cycle_fills = fills_since_position_opened(recent_fills, coin)
        buy_fills = [f for f in current_cycle_fills if f.get("side") == "B"]
        open_buy_prices = {
            order_numbers[o.get("oid")]: float(o.get("limitPx", 0) or 0)
            for o in orders
            if o.get("coin") == coin
            and o.get("side") == "B"
            and not o.get("reduceOnly")
            and o.get("oid") in order_numbers
        }
        open_sell_prices = [
            float(o.get("limitPx", 0) or 0)
            for o in orders
            if o.get("coin") == coin and o.get("side") == "A"
        ]
        entry_price = (entry_price_by_coin or {}).get(coin)
        return render_candles(
            candles,
            coin,
            CHART_INTERVAL,
            buy_fills,
            open_buy_prices,
            open_sell_prices,
            entry_price,
        )
    except Exception as exc:
        log.warning("Chart render failed for %s: %s", coin, exc)
        return None


async def _render_spot_chart(client: HyperliquidClient, token_name: str) -> bytes | None:
    """Plain candlestick+volume chart (render_candles, no order/fill overlay)
    for a spot token's USDC market - same look as every other chart in this
    bot, just without the vault-position context those have, since this
    isn't about a position the vault holds. Spot markets aren't addressable
    by the token's own symbol in candleSnapshot, only by their internal pair
    name (get_spot_pair_name), which is why this can't just reuse
    _render_chart directly - that ties one `coin` to both the candle-fetch
    identifier and the chart's title, which don't match here ("@107" vs
    "HYPE").
    """
    try:
        pair_name = client.get_spot_pair_name(token_name)
        if not pair_name:
            return None
        candles = client.get_candles(pair_name, CHART_INTERVAL, CHART_LOOKBACK_MS)
        return render_candles(candles, token_name, CHART_INTERVAL)
    except Exception as exc:
        log.warning("Spot chart render failed for %s: %s", token_name, exc)
        return None


async def _send_status(
    client: HyperliquidClient,
    text: str,
    positions: list[dict],
    orders: list[dict],
    order_numbers: dict[int, int],
    send_text,
    send_photo,
) -> None:
    """Send a status/heartbeat-style message. If there's an open position,
    its chart is attached to the *same* message (as the photo's caption,
    matching how buy fills work) rather than as a trailing separate
    message; falls back to text-only if there's no position or the chart
    fails to render. Any further positions beyond the first each get their
    own, caption-less follow-up chart, since only one photo can carry the
    caption in a single message.
    """
    coins = _active_coins(positions)
    entry_price_by_coin = _entry_price_by_coin(positions)
    chart = (
        await _render_chart(client, coins[0], orders, order_numbers, entry_price_by_coin)
        if coins
        else None
    )
    if chart:
        await send_photo(chart, text)
    else:
        await send_text(text)
    for coin in coins[1:]:
        extra = await _render_chart(client, coin, orders, order_numbers, entry_price_by_coin)
        if extra:
            await send_photo(extra, "")


def _status_message(
    title: str, client: HyperliquidClient, state: VaultState
) -> tuple[str, list[dict], list[dict]]:
    vault_value = _vault_value(client.get_margin_summary())
    equity = _user_equity(client.get_vault_details())
    positions = client.get_open_positions()
    orders = client.get_open_orders()
    _update_order_numbers(client, state, orders, positions)
    text = (
        f"<b>{title}</b>\n"
        f"{format_position_status(positions, vault_value, equity, _sell_price_by_coin(orders), _pending_entry_coin(positions, orders))}\n\n"
        f"<b>Open orders</b>\n"
        f"{format_open_orders(orders, state.first_entry_price, state.order_numbers, vault_value, equity)}"
    )
    return text, positions, orders


async def handle_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client: HyperliquidClient = context.bot_data["client"]
    state: VaultState = context.bot_data["state"]
    text, positions, orders = _status_message("Status", client, state)

    async def send_text(t: str) -> None:
        await update.message.reply_text(t, parse_mode="HTML", reply_markup=BOT_KEYBOARD)

    async def send_photo(photo: bytes, caption: str) -> None:
        await update.message.reply_photo(
            photo,
            caption=caption,
            parse_mode="HTML",
            reply_markup=BOT_KEYBOARD,
            show_caption_above_media=True,
        )

    await _send_status(
        client, text, positions, orders, state.order_numbers, send_text, send_photo
    )


def _usd_to_eur_rate() -> float | None:
    """Current USD->EUR rate (ECB reference rate via the free, no-key-needed
    Frankfurter API). Hyperliquid itself has no usable EUR feed - its "EUR"
    spot token is listed but has no trading pairs - so converting the
    account equity to EUR needs an external forex source. Returns None
    (letting the caller skip that row) rather than raising, so a forex-API
    hiccup doesn't take down the rest of the account summary.
    """
    try:
        resp = requests.get(
            "https://api.frankfurter.dev/v1/latest",
            params={"base": "USD", "symbols": "EUR"},
            timeout=EUR_RATE_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return float(resp.json()["rates"]["EUR"])
    except Exception as exc:
        log.warning("USD/EUR rate fetch failed: %s", exc)
        return None


async def handle_account_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Account-wide summary (spot + perp + every vault this address is in),
    not scoped to the one vault the rest of this bot watches: account
    equity (plus its EUR equivalent), HYPE staked (its equity and current
    price) and BTC's current price, this depositor's equity and all-time
    profit in the watched vault, net Hyperliquid "Earn" (lending) value,
    non-dust spot balances (each just their $ value), and PnL per period -
    plus a HYPE/USDC spot chart attached as the message's photo, text above
    per the same convention /status and the heartbeat use (unlike a buy
    fill's chart, this isn't the point of the message, just context).
    """
    client: HyperliquidClient = context.bot_data["client"]
    portfolio = client.get_portfolio()
    staked_hype = float(client.get_staking_summary().get("delegated", 0) or 0)
    mid_prices = client.get_mid_prices()
    hype_price = float(mid_prices.get("HYPE", 0) or 0)
    btc_price = float(mid_prices.get("BTC", 0) or 0)
    staked_value = staked_hype * hype_price if hype_price else None
    vault_details = client.get_vault_details()
    vault_equity = _user_equity(vault_details)
    vault_all_time_pnl = _vault_all_time_pnl(vault_details)
    earn_value = client.get_earn_value()
    eur_rate = _usd_to_eur_rate()
    summary = format_account_summary(
        portfolio,
        staked_hype,
        staked_value,
        vault_equity,
        earn_value,
        client.get_spot_balances(),
        client.get_spot_prices(),
        hype_price=hype_price,
        vault_all_time_pnl=vault_all_time_pnl,
        btc_price=btc_price,
        eur_rate=eur_rate,
    )
    text = f"<b>Account</b>\n{summary}"
    chart = await _render_spot_chart(client, "HYPE")
    if chart:
        await update.message.reply_photo(
            chart,
            caption=text,
            parse_mode="HTML",
            reply_markup=BOT_KEYBOARD,
            show_caption_above_media=True,
        )
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=BOT_KEYBOARD)


async def poll_loop(client: HyperliquidClient, notifier: TelegramNotifier, state: VaultState) -> None:
    last_fill_time_ms = int(time.time() * 1000)

    log.info("Starting vault monitor for %s", VAULT_ADDRESS)
    startup_positions = client.get_open_positions()
    startup_vault_value = _vault_value(client.get_margin_summary())
    startup_equity = _user_equity(client.get_vault_details())

    # Seed the true first-fill price for positions already open when the bot
    # starts (e.g. after downtime) from fill history, so "Distance" is right
    # even though we didn't watch this position open live. Falls back to the
    # current blended entry price if the opening fill is older than the
    # lookback window.
    lookback_fills = client.get_fills_since(int(time.time() * 1000) - FIRST_ENTRY_LOOKBACK_MS)
    for ap in startup_positions:
        pos = ap.get("position", {})
        coin = pos.get("coin")
        if not coin or float(pos.get("szi", 0) or 0) == 0:
            continue
        open_price = find_position_open_price(lookback_fills, coin)
        state.first_entry_price[coin] = (
            open_price if open_price is not None else float(pos.get("entryPx", 0) or 0)
        )

    tracking_table = format_table([("Tracking", VAULT_ADDRESS), ("User", USER_ADDRESS)])
    startup_open_orders = client.get_open_orders()
    # Reuses the lookback_fills already fetched above for first_entry_price,
    # rather than _update_order_numbers's own fetch, to avoid a duplicate
    # API call at startup.
    startup_coins = {
        o.get("coin")
        for o in startup_open_orders
        if o.get("side") == "B" and not o.get("reduceOnly")
    }
    startup_active_coins = set(_active_coins(startup_positions))
    startup_historical_sizes = {
        coin: historical_order_sizes(lookback_fills, coin) if coin in startup_active_coins else []
        for coin in startup_coins
    }
    state.order_numbers = assign_order_numbers(
        startup_open_orders, state.order_numbers, startup_historical_sizes
    )
    startup_orders = format_open_orders(
        startup_open_orders,
        state.first_entry_price,
        state.order_numbers,
        startup_vault_value,
        startup_equity,
    )
    await notifier.send(
        f"<b>Monitor started</b>\n{tracking_table}\n\n"
        f"{format_position_status(startup_positions, startup_vault_value, startup_equity, _sell_price_by_coin(startup_open_orders), _pending_entry_coin(startup_positions, startup_open_orders))}\n\n"
        f"<b>Open orders</b>\n{startup_orders}",
        reply_markup=BOT_KEYBOARD,
    )
    _sd_notify("READY=1")

    while True:
        try:
            margin = client.get_margin_summary()
            vault_value = _vault_value(margin)
            equity = _user_equity(client.get_vault_details())
            orders = client.get_open_orders()
            positions = client.get_open_positions()
            # A filled order is already gone from `orders` by the time we see
            # it, so it won't be in the numbers _update_order_numbers is about
            # to (re)compute - snapshot the prior numbering first so a fill
            # message can still report which rung just filled.
            prior_order_numbers = dict(state.order_numbers)
            _update_order_numbers(client, state, orders, positions)
            _update_single_buy_order_tracking(state, orders)

            # Liquidation warning
            margin_ratio_str = margin.get("marginRatio")
            if margin_ratio_str is not None:
                ratio = float(margin_ratio_str)
                if ratio < LIQUIDATION_WARN_THRESHOLD and not state.liquidation_warned:
                    rows = [
                        ("Margin ratio", f"{ratio:.1%}"),
                        ("Threshold", f"{LIQUIDATION_WARN_THRESHOLD:.1%}"),
                    ]
                    if equity is not None:
                        rows.append(("Equity", f"${equity:,.2f}"))
                    await notifier.send(
                        f"<b>Liquidation warning</b>\n{format_table(rows)}", force=True
                    )
                    state.liquidation_warned = True
                elif ratio >= LIQUIDATION_WARN_THRESHOLD:
                    state.liquidation_warned = False

            # New fills, oldest first so a burst between polls posts in order;
            # partial fills of the same order are grouped into one message
            entry_price_by_coin = _entry_price_by_coin(positions)
            new_fills = client.get_fills_since(last_fill_time_ms)
            unseen = [f for f in new_fills if f.get("hash") not in state.seen_fill_hashes]
            unseen.sort(key=lambda f: f.get("time", 0))
            for group in group_fills_by_order(unseen):
                for fill in group:
                    state.seen_fill_hashes.add(fill.get("hash"))
                coin = group[0].get("coin")

                # Remember the price that opened this position from flat, so a
                # buy's "Distance" can reference the original entry rather than
                # the blended average that shifts with every DCA
                if group[0].get("side") == "B" and float(group[0].get("startPosition", 0) or 0) == 0:
                    state.first_entry_price[coin] = float(group[0].get("px", 0) or 0)

                stuck_since = state.single_buy_order_since.get(coin)
                stuck_hours = (time.monotonic() - stuck_since) / 3600 if stuck_since else None
                order_number = prior_order_numbers.get(group[0].get("oid"))

                # Once a coin's first loss-realizing sell shows up, keep every
                # fill since (any side) so later messages can show the full
                # episode, not just this one fill
                if is_loss_realizing_sell(group) and coin not in state.unstuck_episode_fills:
                    state.unstuck_episode_fills[coin] = []
                if coin in state.unstuck_episode_fills:
                    state.unstuck_episode_fills[coin].append(group)

                msg = format_fill(
                    group,
                    vault_value,
                    equity,
                    entry_price_by_coin.get(coin),
                    state.first_entry_price.get(coin),
                    stuck_hours,
                    order_number,
                )
                episode = state.unstuck_episode_fills.get(coin)
                if episode and len(episode) > 1:
                    msg += f"\n\n<b>Since unstuck began</b>\n{format_unstuck_episode(episode)}"

                # Buy fills also get the same "Open orders" table shown in
                # /status/heartbeat, so it's clear what's still left in the
                # ladder right after one of its rungs just filled - `orders`
                # already reflects the post-fill state (fetched fresh this
                # cycle, after the fill happened on the exchange).
                if group[0].get("side") == "B":
                    msg += (
                        f"\n\n<b>Open orders</b>\n"
                        f"{format_open_orders(orders, state.first_entry_price, state.order_numbers, vault_value, equity)}"
                    )

                # A chart gives a feel for what the market's been doing
                # without opening a separate app - only worth it for buys,
                # since that's what a DCA ladder filling is really about
                chart = None
                if group[0].get("side") == "B":
                    chart = await _render_chart(
                        client, coin, orders, state.order_numbers, entry_price_by_coin
                    )

                log.info("Fill: %s", msg)
                if chart:
                    await notifier.send_photo(chart, caption=msg, force=True)
                else:
                    await notifier.send(msg, force=True)

                if position_after_fills(group) == 0:
                    state.first_entry_price.pop(coin, None)
                    state.unstuck_episode_fills.pop(coin, None)
            if unseen:
                last_fill_time_ms = int(time.time() * 1000)

            # A long position whose resting sell orders don't add up to its full
            # size can mean the vault's trading bot is offline or stuck (e.g. a
            # DCA fill grew the position but the old sell order was never
            # resized); skip for the cycle any fill (buy or sell) happens, since
            # the trading bot needs a moment to resize/replace its sell order
            # after moving the position, and the positions/orders endpoints can
            # also be transiently out of sync with each other right after a fill.
            # Beyond that, require the gap to persist for several consecutive
            # cycles before warning, since it's often resolved by the next
            # cycle on its own.
            gap = find_sell_coverage_gap(positions, orders)
            should_warn = gap is not None and not unseen
            if should_warn:
                state.sell_coverage_gap_streak += 1
            else:
                state.sell_coverage_gap_streak = 0
                state.sell_coverage_warned = False

            if (
                should_warn
                and state.sell_coverage_gap_streak >= SELL_COVERAGE_GAP_STREAK_THRESHOLD
                and not state.sell_coverage_warned
            ):
                await notifier.send(
                    f"🚨 <b>Sell order mismatch</b>\n{format_sell_coverage_gap(gap)}\n"
                    f"<i>The bot trading this vault may be offline.</i>",
                    force=True,
                )
                state.sell_coverage_warned = True

            # Heartbeat: prove we're still alive if nothing else has posted in a
            # while. Runs on a shorter interval whenever some coin's DCA ladder
            # is down to its last buy order, for closer monitoring near the end
            # of a ladder.
            heartbeat_interval = (
                HEARTBEAT_FAST_INTERVAL_SECONDS
                if has_single_buy_order_left(orders)
                else HEARTBEAT_INTERVAL_SECONDS
            )
            silence = time.monotonic() - notifier.last_sent_at
            if silence >= heartbeat_interval:
                heartbeat_text = (
                    f"<b>Heartbeat</b>\n"
                    f"{format_position_status(positions, vault_value, equity, _sell_price_by_coin(orders), _pending_entry_coin(positions, orders))}\n\n"
                    f"<b>Open orders</b>\n"
                    f"{format_open_orders(orders, state.first_entry_price, state.order_numbers, vault_value, equity)}"
                )
                await _send_status(
                    client,
                    heartbeat_text,
                    positions,
                    orders,
                    state.order_numbers,
                    notifier.send,
                    functools.partial(notifier.send_photo, caption_above=True),
                )

        except Exception as exc:
            log.error("Poll error: %s", exc)

        # Reaching here means the cycle above didn't hang - a hard freeze
        # (e.g. a wedged network connection) means no kick, so systemd's
        # watchdog eventually force-restarts the process.
        _sd_notify("WATCHDOG=1")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def run() -> None:
    client = HyperliquidClient(VAULT_ADDRESS, USER_ADDRESS)
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    state = VaultState()

    application = Application.builder().bot(notifier.bot).build()
    application.bot_data["client"] = client
    application.bot_data["state"] = state
    application.add_handler(
        CommandHandler(
            "status", handle_status_command, filters=filters.Chat(int(TELEGRAM_CHAT_ID))
        )
    )
    application.add_handler(
        CommandHandler(
            "account", handle_account_command, filters=filters.Chat(int(TELEGRAM_CHAT_ID))
        )
    )

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    try:
        await poll_loop(client, notifier, state)
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
