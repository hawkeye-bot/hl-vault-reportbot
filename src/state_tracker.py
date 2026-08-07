"""Tracks vault state between polls and formats fill notifications."""

from dataclasses import dataclass, field


@dataclass
class VaultState:
    seen_fill_hashes: set[str] = field(default_factory=set)
    liquidation_warned: bool = False
    sell_coverage_warned: bool = False


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


def format_table(rows: list[tuple[str, str]]) -> str:
    """Render label/value rows as a monospace table (Telegram can't bold text
    inside a <pre> block, so this trades bold labels for column alignment).
    """
    label_width = max(len(label) for label, _ in rows)
    body = "\n".join(f"{label.ljust(label_width)} {value}" for label, value in rows)
    return f"<pre>{body}</pre>"


def format_grid(headers: list[str], rows: list[list[str]]) -> str:
    """Render a multi-column monospace table with a header row, for listing
    several items (as opposed to format_table's one label/value per row)."""
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]

    def fmt(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths))

    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines.extend(fmt(r) for r in rows)
    return f"<pre>{chr(10).join(lines)}</pre>"


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


def find_sell_coverage_gap(
    positions: list[dict], orders: list[dict], tolerance: float = 0.001
) -> dict | None:
    """Find a long position whose resting sell orders don't add up to its full
    size (e.g. a DCA fill grew the position but the old sell order was never
    resized). Returns details for the first gap found, or None if every long
    position is fully covered. Short positions aren't checked since this vault
    only takes long positions.
    """
    sell_size_by_coin: dict[str, float] = {}
    for o in orders:
        if o.get("side") == "A":
            coin = o.get("coin")
            sell_size_by_coin[coin] = sell_size_by_coin.get(coin, 0.0) + float(o.get("sz", 0) or 0)

    for ap in positions:
        pos = ap.get("position", {})
        coin = pos.get("coin")
        size = float(pos.get("szi", 0) or 0)
        if not coin or size <= 0:
            continue
        sell_size = sell_size_by_coin.get(coin, 0.0)
        if abs(sell_size - size) > max(size * tolerance, 1e-9):
            position_value = float(pos.get("positionValue", 0) or 0)
            price = position_value / size
            return {
                "coin": coin,
                "position_value": position_value,
                "sell_value": sell_size * price,
            }
    return None


def format_sell_coverage_gap(gap: dict) -> str:
    rows = [
        ("Symbol", _pair(gap["coin"])),
        ("Position value", f"${gap['position_value']:,.2f}"),
        ("Sell orders value", f"${gap['sell_value']:,.2f}"),
    ]
    return format_table(rows)


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

    fraction = _fraction(vault_value, equity)

    rows = []
    if vault_value:
        before_pct = abs(start_pos) * vwap / vault_value * 100
        after_pct = abs(end_pos) * vwap / vault_value * 100

        added_str = f"{after_pct - before_pct:+.2f}%"
        now_str = f"{after_pct:.2f}%"
        if fraction is not None:
            before_value = abs(start_pos) * vwap * fraction
            after_value = abs(end_pos) * vwap * fraction
            added_value = after_value - before_value
            added_sign = "+" if added_value >= 0 else "-"
            added_str += f" ({added_sign}${abs(added_value):,.2f})"
            now_str += f" (${after_value:,.2f})"

        rows.append(("Exposure added", added_str))
        rows.append(("Exposure now", now_str))

    if fraction is not None:
        raw_pnl = sum(float(f.get("closedPnl") or 0) for f in fills)
        pnl = raw_pnl * fraction
        if abs(pnl) > 1e-9:
            sign = "+" if pnl >= 0 else "-"
            closed_notional = vwap * total_sz
            pct_str = f" ({raw_pnl / closed_notional * 100:+.2f}%)" if closed_notional else ""
            rows.append(("PnL", f"{sign}${abs(pnl):,.2f}{pct_str}"))

    rows.append(("Symbol", f"{_pair(coin)} {action}"))
    rows.append(("Price", f"${_format_price(vwap)}"))
    if len(fills) > 1:
        rows.append(("Fills", str(len(fills))))

    return format_table(rows)


def format_position_status(
    asset_positions: list[dict], vault_value: float | None, equity: float | None
) -> str:
    """List open positions: exposure % is relative to the whole vault, dollar
    value and PnL are scaled down to this user's share of it.
    """
    fraction = _fraction(vault_value, equity)
    tables = []
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

        rows = []
        if equity is not None:
            rows.append(("Your equity", f"${equity:,.2f}"))
        rows.append(("Exposure", f"{value_str}{exposure_pct}"))
        if fraction is not None:
            raw_pnl = float(pos.get("unrealizedPnl", 0) or 0)
            pnl = raw_pnl * fraction
            sign = "+" if pnl >= 0 else "-"
            pct_str = f" ({raw_pnl / notional * 100:+.2f}%)" if notional else ""
            rows.append(("PnL", f"{sign}${abs(pnl):,.2f}{pct_str}"))

        current_price = notional / abs(size) if size else 0
        entry_price = float(pos.get("entryPx", 0) or 0)
        rows.append(("Symbol", f"{_pair(coin)} {side}"))
        rows.append(("Current price", f"${_format_price(current_price)}"))
        rows.append(("Entry price", f"${_format_price(entry_price)}"))

        tables.append(format_table(rows))

    if tables:
        return "\n\n".join(tables)
    if equity is not None:
        return f"No open positions\n\n{format_table([('Your equity', f'${equity:,.2f}')])}"
    return "No open positions"


def format_open_orders(orders: list[dict]) -> str:
    if not orders:
        return "No open orders"
    rows = []
    for o in sorted(orders, key=lambda o: float(o.get("limitPx", 0) or 0), reverse=True):
        coin = o.get("coin", "?")
        is_buy = o.get("side") == "B"
        action = "Buy (RO)" if o.get("reduceOnly") and is_buy else ("Buy" if is_buy else "Sell")
        price = float(o.get("limitPx", 0) or 0)
        notional = price * float(o.get("sz", 0) or 0)
        rows.append([_pair(coin), action, f"${_format_price(price)}", f"${notional:,.2f}"])
    return format_grid(["Symbol", "Side", "Price", "Value"], rows)
