import asyncio
import logging
import time
from datetime import datetime, timezone

from src.config import (
    DAILY_SUMMARY_HOUR,
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
    diff_positions,
    format_fill,
    parse_positions,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def _equity_footer(client: HyperliquidClient) -> str:
    equity = client.get_vault_equity()
    if equity is None:
        return ""
    return f"\n\n<i>Your vault equity: ${equity:,.2f}</i>"


async def send_daily_summary(client: HyperliquidClient, notifier: TelegramNotifier) -> None:
    equity = client.get_vault_equity()
    all_time_pnl = client.get_vault_all_time_pnl()
    margin = client.get_margin_summary()

    lines = ["<b>Daily vault summary</b>"]
    if equity is not None:
        lines.append(f"Your equity: <b>${equity:,.2f}</b>")
    if all_time_pnl is not None:
        sign = "+" if all_time_pnl >= 0 else ""
        lines.append(f"Vault all-time PnL: <b>{sign}${all_time_pnl:,.2f}</b>")
    account_value = margin.get("accountValue")
    if account_value:
        lines.append(f"Vault account value: ${float(account_value):,.2f}")

    await notifier.send("\n".join(lines))


async def poll_loop(client: HyperliquidClient, notifier: TelegramNotifier) -> None:
    state = VaultState()
    last_fill_time_ms = int(time.time() * 1000)
    last_summary_day: int | None = None

    log.info("Starting vault monitor for %s", VAULT_ADDRESS)
    await notifier.send(
        f"Vault monitor started\n"
        f"Vault: <code>{VAULT_ADDRESS}</code>\n"
        f"User:  <code>{USER_ADDRESS}</code>"
    )

    while True:
        try:
            now_utc = datetime.now(timezone.utc)

            # Daily summary
            if now_utc.hour == DAILY_SUMMARY_HOUR and now_utc.day != last_summary_day:
                await send_daily_summary(client, notifier)
                last_summary_day = now_utc.day

            # Position diffs
            raw_positions = client.get_open_positions()
            new_positions = parse_positions(raw_positions)
            events = diff_positions(state.positions, new_positions)
            footer = _equity_footer(client)
            for event in events:
                log.info("Position event: %s", event)
                await notifier.send(f"<b>Vault update</b>\n{event}{footer}")
            state.positions = new_positions

            # Liquidation warning
            margin = client.get_margin_summary()
            margin_ratio_str = margin.get("marginRatio")
            if margin_ratio_str is not None:
                ratio = float(margin_ratio_str)
                if ratio < LIQUIDATION_WARN_THRESHOLD and not state.liquidation_warned:
                    await notifier.send(
                        f"<b>Liquidation warning</b>\n"
                        f"Vault margin ratio is critically low: {ratio:.1%}\n"
                        f"Threshold: {LIQUIDATION_WARN_THRESHOLD:.1%}{footer}"
                    )
                    state.liquidation_warned = True
                elif ratio >= LIQUIDATION_WARN_THRESHOLD:
                    state.liquidation_warned = False

            # New fills
            new_fills = client.get_fills_since(last_fill_time_ms)
            unseen = [f for f in new_fills if f.get("hash") not in state.seen_fill_hashes]
            for fill in unseen:
                state.seen_fill_hashes.add(fill.get("hash"))
                msg = format_fill(fill)
                log.info("Fill: %s", msg)
                await notifier.send(f"<b>Vault fill</b>\n{msg}{footer}")
            if unseen:
                last_fill_time_ms = int(time.time() * 1000)

        except Exception as exc:
            log.error("Poll error: %s", exc)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    client = HyperliquidClient(VAULT_ADDRESS, USER_ADDRESS)
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    asyncio.run(poll_loop(client, notifier))


if __name__ == "__main__":
    main()
