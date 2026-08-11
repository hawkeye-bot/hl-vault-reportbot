import asyncio
import logging
import os
import socket
import time

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
from src.hl_client import HyperliquidClient
from src.notifier import TelegramNotifier
from src.state_tracker import (
    VaultState,
    assign_order_numbers,
    find_position_open_price,
    find_sell_coverage_gap,
    format_fill,
    format_open_orders,
    format_position_status,
    format_sell_coverage_gap,
    format_table,
    group_fills_by_order,
    has_single_buy_order_left,
    historical_order_sizes,
    needs_fresh_numbering,
    position_after_fills,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

STATUS_KEYBOARD = ReplyKeyboardMarkup([["/status"]], resize_keyboard=True, is_persistent=True)
FIRST_ENTRY_LOOKBACK_MS = 30 * 24 * 60 * 60 * 1000  # 30 days
SELL_COVERAGE_GAP_STREAK_THRESHOLD = 3  # consecutive cycles before warning


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


def _update_order_numbers(client: HyperliquidClient, state: VaultState, orders: list[dict]) -> None:
    """Keep state.order_numbers current, fetching fill history to calibrate
    a fresh assignment only when some order isn't numbered yet (rare - see
    needs_fresh_numbering) rather than every cycle.
    """
    historical_sizes = {}
    if needs_fresh_numbering(orders, state.order_numbers):
        lookback_fills = client.get_fills_since(int(time.time() * 1000) - FIRST_ENTRY_LOOKBACK_MS)
        coins = {
            o.get("coin") for o in orders if o.get("side") == "B" and not o.get("reduceOnly")
        }
        historical_sizes = {coin: historical_order_sizes(lookback_fills, coin) for coin in coins}
    state.order_numbers = assign_order_numbers(orders, state.order_numbers, historical_sizes)


def _entry_price_by_coin(positions: list[dict]) -> dict[str, float]:
    return {
        ap["position"]["coin"]: float(ap["position"].get("entryPx", 0) or 0)
        for ap in positions
        if ap.get("position", {}).get("coin")
    }


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


def _status_message(title: str, client: HyperliquidClient, state: VaultState) -> str:
    vault_value = _vault_value(client.get_margin_summary())
    equity = _user_equity(client.get_vault_details())
    positions = client.get_open_positions()
    orders = client.get_open_orders()
    _update_order_numbers(client, state, orders)
    return (
        f"<b>{title}</b>\n"
        f"{format_position_status(positions, vault_value, equity, _sell_price_by_coin(orders))}\n\n"
        f"<b>Open orders</b>\n"
        f"{format_open_orders(orders, _entry_price_by_coin(positions), state.first_entry_price, state.order_numbers)}"
    )


async def handle_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    client: HyperliquidClient = context.bot_data["client"]
    state: VaultState = context.bot_data["state"]
    await update.message.reply_text(
        _status_message("Status", client, state), parse_mode="HTML", reply_markup=STATUS_KEYBOARD
    )


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
    startup_historical_sizes = {
        coin: historical_order_sizes(lookback_fills, coin) for coin in startup_coins
    }
    state.order_numbers = assign_order_numbers(
        startup_open_orders, state.order_numbers, startup_historical_sizes
    )
    startup_orders = format_open_orders(
        startup_open_orders,
        _entry_price_by_coin(startup_positions),
        state.first_entry_price,
        state.order_numbers,
    )
    await notifier.send(
        f"<b>Monitor started</b>\n{tracking_table}\n\n"
        f"{format_position_status(startup_positions, startup_vault_value, startup_equity, _sell_price_by_coin(startup_open_orders))}\n\n"
        f"<b>Open orders</b>\n{startup_orders}",
        reply_markup=STATUS_KEYBOARD,
    )
    _sd_notify("READY=1")

    while True:
        try:
            margin = client.get_margin_summary()
            vault_value = _vault_value(margin)
            equity = _user_equity(client.get_vault_details())
            orders = client.get_open_orders()
            positions = client.get_open_positions()
            _update_order_numbers(client, state, orders)

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

                msg = format_fill(
                    group,
                    vault_value,
                    equity,
                    entry_price_by_coin.get(coin),
                    state.first_entry_price.get(coin),
                )
                log.info("Fill: %s", msg)
                await notifier.send(msg)

                if position_after_fills(group) == 0:
                    state.first_entry_price.pop(coin, None)
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
                await notifier.send(
                    f"<b>Heartbeat</b>\n"
                    f"{format_position_status(positions, vault_value, equity, _sell_price_by_coin(orders))}\n\n"
                    f"<b>Open orders</b>\n"
                    f"{format_open_orders(orders, entry_price_by_coin, state.first_entry_price, state.order_numbers)}"
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
