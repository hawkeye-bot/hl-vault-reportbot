import asyncio
import logging
import time
from datetime import datetime, timezone

from src.config import (
    DAILY_SUMMARY_HOUR,
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
    find_sell_coverage_gap,
    format_fill,
    format_open_orders,
    format_position_status,
    format_sell_coverage_gap,
    format_table,
    group_fills_by_order,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def _vault_value(margin: dict) -> float | None:
    account_value = margin.get("accountValue")
    return float(account_value) if account_value else None


def _user_equity(details: dict) -> float | None:
    equity_str = (details.get("followerState") or {}).get("vaultEquity")
    return float(equity_str) if equity_str is not None else None


def _all_time_pnl(details: dict) -> float | None:
    pnl_str = details.get("allTimePnl")
    return float(pnl_str) if pnl_str is not None else None


async def send_daily_summary(client: HyperliquidClient, notifier: TelegramNotifier) -> None:
    details = client.get_vault_details()
    equity = _user_equity(details)
    all_time_pnl = _all_time_pnl(details)

    rows = []
    if all_time_pnl is not None:
        sign = "+" if all_time_pnl >= 0 else ""
        rows.append(("All-time PnL", f"{sign}${all_time_pnl:,.2f}"))
    if equity is not None:
        rows.append(("Your equity", f"${equity:,.2f}"))

    message = "<b>Daily summary</b>"
    if rows:
        message += f"\n{format_table(rows)}"
    await notifier.send(message)


async def poll_loop(client: HyperliquidClient, notifier: TelegramNotifier) -> None:
    state = VaultState()
    last_fill_time_ms = int(time.time() * 1000)
    last_summary_day: int | None = None

    log.info("Starting vault monitor for %s", VAULT_ADDRESS)
    startup_vault_value = _vault_value(client.get_margin_summary())
    startup_equity = _user_equity(client.get_vault_details())
    tracking_table = format_table([("Tracking", VAULT_ADDRESS), ("User", USER_ADDRESS)])
    await notifier.send(
        f"<b>Monitor started</b>\n{tracking_table}\n\n"
        f"{format_position_status(client.get_open_positions(), startup_vault_value, startup_equity)}"
    )

    while True:
        try:
            now_utc = datetime.now(timezone.utc)

            # Daily summary
            if now_utc.hour == DAILY_SUMMARY_HOUR and now_utc.day != last_summary_day:
                await send_daily_summary(client, notifier)
                last_summary_day = now_utc.day

            margin = client.get_margin_summary()
            vault_value = _vault_value(margin)
            equity = _user_equity(client.get_vault_details())
            orders = client.get_open_orders()
            positions = client.get_open_positions()

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
                        rows.append(("Your equity", f"${equity:,.2f}"))
                    await notifier.send(f"<b>Liquidation warning</b>\n{format_table(rows)}")
                    state.liquidation_warned = True
                elif ratio >= LIQUIDATION_WARN_THRESHOLD:
                    state.liquidation_warned = False

            # New fills, oldest first so a burst between polls posts in order;
            # partial fills of the same order are grouped into one message
            new_fills = client.get_fills_since(last_fill_time_ms)
            unseen = [f for f in new_fills if f.get("hash") not in state.seen_fill_hashes]
            unseen.sort(key=lambda f: f.get("time", 0))
            sell_filled_this_cycle = any(f.get("side") == "A" for f in unseen)
            for group in group_fills_by_order(unseen):
                for fill in group:
                    state.seen_fill_hashes.add(fill.get("hash"))
                msg = format_fill(group, vault_value, equity)
                log.info("Fill: %s", msg)
                await notifier.send(msg)
            if unseen:
                last_fill_time_ms = int(time.time() * 1000)

            # A long position whose resting sell orders don't add up to its full
            # size can mean the vault's trading bot is offline or stuck (e.g. a
            # DCA fill grew the position but the old sell order was never
            # resized); skip right after a sell just filled, since a transient
            # mismatch between the positions and orders endpoints is expected
            gap = find_sell_coverage_gap(positions, orders)
            should_warn = gap is not None and not sell_filled_this_cycle
            if should_warn and not state.sell_coverage_warned:
                await notifier.send(
                    f"<b>Sell order mismatch</b>\n{format_sell_coverage_gap(gap)}\n"
                    f"<i>The bot trading this vault may be offline.</i>"
                )
                state.sell_coverage_warned = True
            elif not should_warn:
                state.sell_coverage_warned = False

            # Heartbeat: prove we're still alive if nothing else has posted in a while
            silence = time.monotonic() - notifier.last_sent_at
            if silence >= HEARTBEAT_INTERVAL_SECONDS:
                await notifier.send(
                    f"<b>Heartbeat</b>\n"
                    f"{format_position_status(positions, vault_value, equity)}\n\n"
                    f"<b>Open orders</b>\n{format_open_orders(orders)}"
                )

        except Exception as exc:
            log.error("Poll error: %s", exc)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    client = HyperliquidClient(VAULT_ADDRESS, USER_ADDRESS)
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    asyncio.run(poll_loop(client, notifier))


if __name__ == "__main__":
    main()
