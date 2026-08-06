"""Tracks vault state between polls and formats fill notifications."""

from dataclasses import dataclass, field


@dataclass
class VaultState:
    seen_fill_hashes: set[str] = field(default_factory=set)
    liquidation_warned: bool = False


def _pair(coin: str) -> str:
    return f"{coin}USDC"


def group_fills_by_order(fills: list[dict]) -> list[list[dict]]:
    """Group fills (already time-sorted) by order id, preserving first-seen order."""
    groups: dict[int, list[dict]] = {}
    order: list[int] = []
    for f in fills:
        oid = f.get("oid")
        if oid not in groups:
            groups[oid] = []
            order.append(oid)
        groups[oid].append(f)
    return [groups[oid] for oid in order]


def format_fill(fills: list[dict], vault_value: float | None) -> str:
    """Format one or more partial fills of the same order as a single message."""
    first, last = fills[0], fills[-1]
    coin = first.get("coin", "?")
    action = "Buy" if first.get("side") == "B" else "Sell"

    total_sz = sum(float(f.get("sz", 0)) for f in fills)
    vwap = (
        sum(float(f.get("px", 0)) * float(f.get("sz", 0)) for f in fills) / total_sz
        if total_sz
        else 0
    )

    last_sz = float(last.get("sz", 0))
    start_pos = float(first.get("startPosition", 0))
    end_pos = (
        float(last.get("startPosition", 0)) + last_sz
        if last.get("side") == "B"
        else float(last.get("startPosition", 0)) - last_sz
    )

    exposure = ""
    if vault_value:
        before_pct = abs(start_pos) * vwap / vault_value * 100
        after_pct = abs(end_pos) * vwap / vault_value * 100
        exposure = f" · {after_pct - before_pct:+.2f}% (now {after_pct:.2f}%)"

    pnl = sum(float(f.get("closedPnl") or 0) for f in fills)
    pnl_str = ""
    if abs(pnl) > 1e-9:
        sign = "+" if pnl >= 0 else "-"
        pnl_str = f" ({sign}${abs(pnl):,.2f})"

    fills_note = f" ({len(fills)} fills)" if len(fills) > 1 else ""

    return f"<b>{_pair(coin)}</b> {action} @ ${vwap:,.4f}{fills_note}{exposure}{pnl_str}"


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
