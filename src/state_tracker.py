"""Tracks vault state between polls and formats fill notifications."""

from dataclasses import dataclass, field


@dataclass
class VaultState:
    seen_fill_hashes: set[str] = field(default_factory=set)
    liquidation_warned: bool = False


def _pair(coin: str) -> str:
    return f"{coin}USDC"


def format_fill(fill: dict, vault_value: float | None) -> str:
    coin = fill.get("coin", "?")
    action = "Buy" if fill.get("side") == "B" else "Sell"
    price = float(fill.get("px", 0))
    size = float(fill.get("sz", 0))
    start_pos = float(fill.get("startPosition", 0))
    end_pos = start_pos + size if fill.get("side") == "B" else start_pos - size

    exposure = ""
    if vault_value:
        before_pct = abs(start_pos) * price / vault_value * 100
        after_pct = abs(end_pos) * price / vault_value * 100
        exposure = f" · {after_pct - before_pct:+.2f}% (now {after_pct:.2f}%)"

    pnl = float(fill.get("closedPnl") or 0)
    pnl_str = ""
    if abs(pnl) > 1e-9:
        sign = "+" if pnl >= 0 else "-"
        pnl_str = f" ({sign}${abs(pnl):,.2f})"

    return f"<b>{_pair(coin)}</b> {action} @ ${price:,.4f}{exposure}{pnl_str}"


def format_position_status(asset_positions: list[dict], vault_value: float | None) -> str:
    lines = []
    for ap in asset_positions:
        pos = ap.get("position", {})
        coin = pos.get("coin")
        size = float(pos.get("szi", 0) or 0)
        if not coin or size == 0:
            continue
        side = "Long" if size > 0 else "Short"
        notional = float(pos.get("positionValue", 0) or 0)
        exposure_pct = f" ({notional / vault_value * 100:.2f}%)" if vault_value else ""
        pnl = float(pos.get("unrealizedPnl", 0) or 0)
        sign = "+" if pnl >= 0 else "-"
        lines.append(
            f"{_pair(coin)} {side}\n"
            f"Exposure: ${notional:,.2f}{exposure_pct}\n"
            f"PnL: {sign}${abs(pnl):,.2f}"
        )

    body = "\n\n".join(lines) if lines else "No open positions"
    if vault_value:
        body += f"\n\nVault value: ${vault_value:,.2f}"
    return body
