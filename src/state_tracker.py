"""Tracks vault state between polls and formats fill notifications."""

from dataclasses import dataclass, field


@dataclass
class VaultState:
    seen_fill_hashes: set[str] = field(default_factory=set)
    liquidation_warned: bool = False


def _pair(coin: str) -> str:
    return f"{coin}USDC"


def _format_price(price: float) -> str:
    """Show a market price at full precision, without rounding away small-token decimals."""
    if price == 0:
        return "0"
    if abs(price) >= 1:
        return f"{price:,.2f}"
    return f"{price:.10f}".rstrip("0").rstrip(".")


def _fraction(vault_value: float | None, equity: float | None) -> float | None:
    """This user's share of the vault (their equity / total vault value)."""
    if not vault_value or equity is None:
        return None
    return equity / vault_value


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


def format_fill(fills: list[dict], vault_value: float | None, equity: float | None) -> str:
    """Format one or more partial fills of the same order as a single message.

    Exposure % is relative to the whole vault; the dollar PnL is scaled down to
    this user's share of it, since the vault pools multiple followers.
    """
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

    exposure_line = ""
    if vault_value:
        before_pct = abs(start_pos) * vwap / vault_value * 100
        after_pct = abs(end_pos) * vwap / vault_value * 100
        exposure_line = f"{after_pct - before_pct:+.2f}% (now {after_pct:.2f}%)"

    pnl_line = ""
    fraction = _fraction(vault_value, equity)
    if fraction is not None:
        pnl = sum(float(f.get("closedPnl") or 0) for f in fills) * fraction
        if abs(pnl) > 1e-9:
            sign = "+" if pnl >= 0 else "-"
            pnl_line = f"{sign}${abs(pnl):,.2f}"

    fills_note = f" ({len(fills)} fills)" if len(fills) > 1 else ""
    detail = f"{_pair(coin)} {action} @ ${_format_price(vwap)}{fills_note}"

    header = " · ".join(p for p in (exposure_line, pnl_line) if p)
    return f"<b>{header}</b>\n{detail}" if header else detail


def format_position_status(
    asset_positions: list[dict], vault_value: float | None, equity: float | None
) -> str:
    """List open positions: exposure % is relative to the whole vault, dollar
    value and PnL are scaled down to this user's share of it.
    """
    fraction = _fraction(vault_value, equity)
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
        value_str = f"${notional * fraction:,.2f}" if fraction is not None else "n/a"

        pnl_line = ""
        if fraction is not None:
            pnl = float(pos.get("unrealizedPnl", 0) or 0) * fraction
            sign = "+" if pnl >= 0 else "-"
            pnl_line = f"\nPnL: {sign}${abs(pnl):,.2f}"

        current_price = notional / abs(size) if size else 0
        entry_price = float(pos.get("entryPx", 0) or 0)
        price_line = (
            f"\nCurrent price: ${_format_price(current_price)}"
            f"\nEntry price: ${_format_price(entry_price)}"
        )

        lines.append(
            f"Exposure: {value_str}{exposure_pct}{pnl_line}\n{_pair(coin)} {side}{price_line}"
        )

    body = "\n\n".join(lines) if lines else "No open positions"
    if equity is not None:
        body += f"\n\nYour equity: ${equity:,.2f}"
    return body
