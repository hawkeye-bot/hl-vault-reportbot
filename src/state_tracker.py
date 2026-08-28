"""Tracks vault state between polls and formats fill notifications."""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class VaultState:
    seen_fill_hashes: set[str] = field(default_factory=set)
    liquidation_warned: bool = False
    sell_coverage_warned: bool = False
    sell_coverage_gap_streak: int = 0
    first_entry_price: dict[str, float] = field(default_factory=dict)
    order_numbers: dict[int, int] = field(default_factory=dict)
    single_buy_order_since: dict[str, float] = field(default_factory=dict)
    unstuck_episode_fills: dict[str, list[list[dict]]] = field(default_factory=dict)
    # Running weighted-average-cost accumulator for the user's own HYPE spot
    # holding (see update_cost_basis) - cached in-process across /account
    # calls rather than replaying this depositor's full fill history every
    # time, since that history only grows over time.
    hype_spot_position: float = 0.0
    hype_spot_cost_basis: float = 0.0
    hype_spot_synced_until_ms: int = 0


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


def format_table(rows: list[tuple[str, str]], section_breaks: set[int] | None = None) -> str:
    """Render label/value rows as a monospace table (Telegram can't bold text
    inside a <pre> block, so this trades bold labels for column alignment).

    `section_breaks` is a set of row indices before which a horizontal rule
    is inserted, to visually group related rows (e.g. staking vs. vault vs.
    earn in format_account_summary) without splitting into separate tables -
    which would each compute their own label width and end up misaligned
    with each other.
    """
    label_width = max(len(label) for label, _ in rows)
    width = max(label_width + 1 + len(value) for label, value in rows)
    section_breaks = section_breaks or set()
    lines = []
    for i, (label, value) in enumerate(rows):
        if i in section_breaks:
            lines.append("-" * width)
        lines.append(f"{label.ljust(label_width)} {value}")
    return f"<pre>{chr(10).join(lines)}</pre>"


def format_grid(
    headers: list[str], rows: list[list[str]], footer: list[str] | None = None
) -> str:
    """Render a multi-column monospace table with a header row, for listing
    several items (as opposed to format_table's one label/value per row).

    `footer`, if given, is one extra row (e.g. a totals line) appended after
    its own separator, so it reads as a summary of the rows above rather
    than one more item in the list. Column widths account for it too, so it
    doesn't get clipped or misaligned if it's wider than every data row.
    """
    all_rows = rows + ([footer] if footer else [])
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in all_rows)) if all_rows else len(headers[i])
        for i in range(len(headers))
    ]

    def fmt(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths))

    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines.extend(fmt(r) for r in rows)
    if footer:
        lines.append(fmt(["-" * w for w in widths]))
        lines.append(fmt(footer))
    return f"<pre>{chr(10).join(lines)}</pre>"


def position_after_fills(fills: list[dict]) -> float:
    """The signed position size after this list of chronological fills."""
    last = fills[-1]
    last_sz = float(last.get("sz", 0) or 0)
    start = float(last.get("startPosition", 0) or 0)
    return start + last_sz if last.get("side") == "B" else start - last_sz


def find_position_open_price(fills: list[dict], coin: str) -> float | None:
    """Find the price of the fill that most recently opened `coin`'s position
    from flat, using historical fill data (each fill records its own
    startPosition) rather than relying on having watched it happen live -
    important if the bot was offline when the position last opened.
    """
    coin_fills = sorted(
        (f for f in fills if f.get("coin") == coin), key=lambda f: f.get("time", 0)
    )
    opens = [
        f
        for f in coin_fills
        if f.get("side") == "B" and float(f.get("startPosition", 0) or 0) == 0
    ]
    return float(opens[-1]["px"]) if opens else None


def fills_since_position_opened(fills: list[dict], coin: str) -> list[dict]:
    """This coin's fills, chronological, from the point its current position
    was last opened from flat onward - same boundary as
    find_position_open_price. A coin's position can close and reopen more
    than once within a chart's lookback window, so raw `fills` alone would
    include fills from an earlier, already-closed cycle - this scopes them
    to the position that's actually still open, e.g. for the chart's "bought
    here" markers.
    """
    coin_fills = sorted(
        (f for f in fills if f.get("coin") == coin), key=lambda f: f.get("time", 0)
    )
    open_indices = [
        i
        for i, f in enumerate(coin_fills)
        if f.get("side") == "B" and float(f.get("startPosition", 0) or 0) == 0
    ]
    return coin_fills[open_indices[-1] :] if open_indices else coin_fills


def update_cost_basis(
    position: float, cost_basis: float, fills: list[dict]
) -> tuple[float, float]:
    """Apply `fills` (any order - sorted here by time) to a running
    weighted-average-cost accumulator: a buy adds to both position and cost
    basis; a sell removes proportionally from cost basis (weighted-average
    method - a sell doesn't change the average cost of what's left, it just
    scales both down together) and, if it empties the position, resets cost
    basis to 0 so the next buy starts a fresh average instead of carrying
    over floating-point dust. Designed to be called incrementally (only
    new fills since the last call) so a growing fill history doesn't have
    to be replayed from scratch every time - see VaultState's
    hype_spot_* fields and main.py's _update_hype_avg_buy_price.
    """
    for f in sorted(fills, key=lambda f: f.get("time", 0)):
        sz = float(f.get("sz", 0) or 0)
        px = float(f.get("px", 0) or 0)
        if f.get("side") == "B":
            position += sz
            cost_basis += sz * px
        else:
            if position > 0:
                cost_basis -= cost_basis * min(sz / position, 1.0)
            position -= sz
            if position <= 1e-9:
                position = 0.0
                cost_basis = 0.0
    return position, cost_basis


def historical_order_sizes(fills: list[dict], coin: str) -> list[float]:
    """Chronological sizes (summed per order id, so a partially-filled order
    counts once) of every buy order that filled for `coin` since its
    position was last opened from flat - same "since last flat" boundary as
    find_position_open_price. Used to calibrate a DCA ladder's size pattern
    for numbering currently-open orders, including rungs that already
    filled before the bot ever saw them (e.g. the very first rung, which
    can fill before the bot's first poll after a restart).
    """
    coin_fills = sorted(
        (f for f in fills if f.get("coin") == coin and f.get("side") == "B"),
        key=lambda f: f.get("time", 0),
    )
    open_indices = [
        i for i, f in enumerate(coin_fills) if float(f.get("startPosition", 0) or 0) == 0
    ]
    if not open_indices:
        return []
    since_open = coin_fills[open_indices[-1] :]

    sizes: dict[int, float] = {}
    order_seq: list[int] = []
    for f in since_open:
        oid = f.get("oid")
        if oid not in sizes:
            sizes[oid] = 0.0
            order_seq.append(oid)
        sizes[oid] += float(f.get("sz", 0) or 0)
    return [sizes[oid] for oid in order_seq]


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


def buy_order_counts(orders: list[dict]) -> dict[str, int]:
    """Count of resting (non-reduce-only) buy orders per coin."""
    counts: dict[str, int] = {}
    for o in orders:
        if o.get("side") == "B" and not o.get("reduceOnly"):
            coin = o.get("coin")
            counts[coin] = counts.get(coin, 0) + 1
    return counts


def has_single_buy_order_left(orders: list[dict]) -> bool:
    """True if any coin currently has exactly one resting (non-reduce-only)
    buy order - i.e. its DCA ladder is down to its last rung - so the poll
    loop can switch to a faster heartbeat while that's the case.
    """
    return any(count == 1 for count in buy_order_counts(orders).values())


def format_sell_coverage_gap(gap: dict) -> str:
    rows = [
        ("Symbol", _pair(gap["coin"])),
        ("Position value", f"${gap['position_value']:,.2f}"),
        ("Sell orders value", f"${gap['sell_value']:,.2f}"),
    ]
    return format_table(rows)


def is_loss_realizing_sell(fills: list[dict]) -> bool:
    """True if this fill group is a sell that realized a net loss - never a
    normal take-profit for a strategy whose closes only fire above a profit
    threshold, so it's the on-chain signature of a loss-cutting mechanism
    (e.g. passivbot's auto-unstuck) rather than an ordinary close.
    """
    if fills[0].get("side") == "B":
        return False
    return sum(float(f.get("closedPnl") or 0) for f in fills) < 0


def format_fill(
    fills: list[dict],
    vault_value: float | None,
    equity: float | None,
    entry_price: float | None = None,
    first_entry_price: float | None = None,
    stuck_hours: float | None = None,
    order_number: int | None = None,
) -> str:
    """Format one or more partial fills of the same order as a single message.

    Exposure % is relative to the whole vault; the dollar PnL is scaled down to
    this user's share of it, since the vault pools multiple followers.

    Buys report exposure added/now plus Distance (from the price that first
    opened the position, not the blended average, which shifts with every
    DCA) and a fill count, plus an "Order #" row (the rung this order held
    in format_open_orders/assign_order_numbers, if it's still known) so
    it's clear which rung just filled. Sells instead report either that the
    position was closed (plus the resulting equity)
    or, for a partial sell, the exposure still remaining - "exposure added"
    reads oddly negative for a sell - and omit Distance/Fills as noise once
    a position is being unwound.

    A sell that realizes a loss is never a normal take-profit for this
    strategy (it only closes above its threshold), so it's flagged in the
    header and, if `stuck_hours` is passed (how long the coin's buy ladder
    had been down to its last resting rung beforehand - see
    VaultState.single_buy_order_since), that context is shown too: this is
    the on-chain signature of passivbot's auto-unstuck mechanism realizing a
    loss on an over-extended position rather than an ordinary close.
    """
    first, last = fills[0], fills[-1]
    coin = first.get("coin", "?")
    is_buy = first.get("side") == "B"
    action = "Buy" if is_buy else "Sell"

    total_sz = sum(float(f.get("sz", 0)) for f in fills)
    vwap = (
        sum(float(f.get("px", 0)) * float(f.get("sz", 0)) for f in fills) / total_sz
        if total_sz
        else 0
    )

    start_pos = float(first.get("startPosition", 0))
    end_pos = position_after_fills(fills)
    closed = not is_buy and end_pos == 0

    fraction = _fraction(vault_value, equity)
    raw_pnl = sum(float(f.get("closedPnl") or 0) for f in fills)
    is_loss = is_loss_realizing_sell(fills)

    rows = []
    if fraction is not None:
        pnl = raw_pnl * fraction
        if abs(pnl) > 1e-9:
            sign = "+" if pnl >= 0 else "-"
            pct_str = f" ({pnl / equity * 100:+.2f}%)" if equity else ""
            rows.append(("PnL", f"{sign}${abs(pnl):,.2f}{pct_str}"))
    if is_loss and stuck_hours:
        rows.append(("Stuck", f"{stuck_hours:.1f}h before this"))

    if is_buy:
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
    elif closed:
        rows.append(("Position", "Closed"))
    elif vault_value:
        after_pct = abs(end_pos) * vwap / vault_value * 100
        now_str = f"{after_pct:.2f}%"
        if fraction is not None:
            after_value = abs(end_pos) * vwap * fraction
            now_str += f" (${after_value:,.2f})"
        rows.append(("Exposure remaining", now_str))

    if closed and equity is not None:
        rows.append(("Equity", f"${equity:,.2f}"))

    rows.append(("Symbol", f"{_pair(coin)} {action}"))
    if is_buy and order_number:
        rows.append(("Order #", str(order_number)))
    rows.append(("Price", f"${_format_price(vwap)}"))
    if entry_price:
        rows.append(("Entry price", f"${_format_price(entry_price)}"))

    if is_buy:
        if first_entry_price:
            rows.append(
                ("Distance", f"{(vwap - first_entry_price) / first_entry_price * 100:+.2f}%")
            )
        if len(fills) > 1:
            rows.append(("Fills", str(len(fills))))

    table = format_table(rows)
    if is_buy:
        header = "Buy order filled"
    elif is_loss:
        header = "Sell order filled (loss)"
    else:
        header = "Sell order filled"
    return f"<b>{header}</b>\n{table}"


def format_unstuck_episode(episode: list[list[dict]]) -> str:
    """Table of every fill group for a coin since its first loss-realizing
    sell (see is_loss_realizing_sell), for context on how the episode played
    out - e.g. whether it was one trim followed by a normal profitable
    close, or a longer string of reductions.
    """
    rows = []
    for group in episode:
        first = group[0]
        side = "Buy" if first.get("side") == "B" else "Sell"
        total_sz = sum(float(f.get("sz", 0)) for f in group)
        vwap = (
            sum(float(f.get("px", 0)) * float(f.get("sz", 0)) for f in group) / total_sz
            if total_sz
            else 0
        )
        pnl = sum(float(f.get("closedPnl") or 0) for f in group)
        pnl_str = f"{'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}" if pnl else ""
        when = datetime.fromtimestamp(first.get("time", 0) / 1000, tz=timezone.utc).strftime(
            "%H:%M"
        )
        rows.append([when, side, f"${_format_price(vwap)}", _format_price(total_sz), pnl_str])
    return format_grid(["Time", "Side", "Price", "Size", "PnL"], rows)


def format_account_summary(
    portfolio: list,
    staked_hype: float | None = None,
    staked_value: float | None = None,
    vault_equity: float | None = None,
    earn_value: float | None = None,
    spot_balances: list[dict] | None = None,
    spot_prices: dict[str, float] | None = None,
    spot_min_value: float = 3.0,
    hype_price: float | None = None,
    vault_all_time_pnl: float | None = None,
    btc_price: float | None = None,
    eur_rate: float | None = None,
    hype_avg_buy_price: float | None = None,
) -> str:
    """Render an account-wide summary: current account equity (plus its EUR
    equivalent just below, if `eur_rate` is available), amount staked (in
    HYPE, its equity, then HYPE's current price, this depositor's average
    HYPE spot buy price, and BTC's current price below), this depositor's
    equity and cumulative all-time profit in the
    vault the rest of the bot watches, net value in Hyperliquid's lending
    ("Earn") product, non-dust spot balances (just their $ value, one row
    per coin), then PnL per period (day/week/month/allTime - no volume,
    that's not the point here - with the all-time PnL's EUR equivalent
    shown just below it, same `eur_rate` treatment as account equity).
    `portfolio` is the raw list of (period, data) tuples from
    HyperliquidClient.get_portfolio. Only the combined (spot+perp+vault)
    periods are shown - the "perpX" variants are skipped since this account
    trades through a vault rather than directly on its
    own perp book, so they'd read as ~$0 and just be noise.
    """
    periods = {period: data for period, data in portfolio}
    period_labels = [("day", "Day"), ("week", "Week"), ("month", "Month"), ("allTime", "All time")]

    latest = periods.get("day") or periods.get("allTime") or {}
    latest_history = latest.get("accountValueHistory") or []
    account_value = float(latest_history[-1][1]) if latest_history else None

    # Built as separate groups (account, staking, vault, earn, spot) rather
    # than one flat list, so a rule can be drawn between them below - only
    # between groups that actually have rows, so e.g. no stray rule shows up
    # when there are no spot holdings to separate from Earn.
    account_rows = []
    if account_value is not None:
        account_rows.append(("Account equity", f"${account_value:,.2f}"))
        if eur_rate is not None:
            account_rows.append(("", f"€{account_value * eur_rate:,.2f}"))

    staking_rows = []
    if staked_hype is not None:
        staking_rows.append(("Staked", f"{staked_hype:,.2f} HYPE"))
    if staked_value is not None:
        staking_rows.append(("Staked equity", f"${staked_value:,.2f}"))
    if hype_price is not None:
        staking_rows.append(("HYPE price", f"${_format_price(hype_price)}"))
    if hype_avg_buy_price is not None:
        staking_rows.append(("HYPE avg buy", f"${_format_price(hype_avg_buy_price)}"))
    if btc_price is not None:
        staking_rows.append(("BTC price", f"${_format_price(btc_price)}"))

    vault_rows = []
    if vault_equity is not None:
        vault_rows.append(("Vault equity", f"${vault_equity:,.2f}"))
    if vault_all_time_pnl is not None:
        sign = "+" if vault_all_time_pnl >= 0 else "-"
        vault_rows.append(("Vault profit", f"{sign}${abs(vault_all_time_pnl):,.2f}"))

    earn_rows = []
    if earn_value is not None:
        earn_rows.append(("Earn", f"${earn_value:,.2f}"))

    # Dust and untradeable/no-price tokens are dropped so a wallet with
    # many near-zero balances doesn't clutter the summary; the rest are
    # sorted by value, highest first.
    spot_prices = spot_prices or {}
    spot_holdings = []
    for b in spot_balances or []:
        coin = b.get("coin")
        total = float(b.get("total", 0) or 0)
        price = spot_prices.get(coin)
        if not coin or total <= 0 or price is None:
            continue
        value = total * price
        if value < spot_min_value:
            continue
        spot_holdings.append((coin, value))
    spot_holdings.sort(key=lambda h: -h[1])
    spot_rows = [(coin, f"${value:,.2f}") for coin, value in spot_holdings]

    header_rows = []
    section_breaks = set()
    for section in (account_rows, staking_rows, vault_rows, earn_rows, spot_rows):
        if not section:
            continue
        if header_rows:
            section_breaks.add(len(header_rows))
        header_rows.extend(section)

    header = format_table(header_rows, section_breaks) if header_rows else ""

    grid_rows = []
    for key, label in period_labels:
        data = periods.get(key)
        pnl_history = (data or {}).get("pnlHistory") or []
        if not pnl_history:
            continue
        pnl = float(pnl_history[-1][1])
        value_history = data.get("accountValueHistory") or []
        start_value = float(value_history[0][1]) if value_history else 0
        pct_str = f" ({pnl / start_value * 100:+.2f}%)" if start_value > 1e-9 else ""
        sign = "+" if pnl >= 0 else "-"
        grid_rows.append([label, f"{sign}${abs(pnl):,.2f}{pct_str}"])
        # All-time PnL also gets its EUR equivalent right below, as an
        # unlabeled row - same treatment as Account equity's EUR row above.
        if key == "allTime" and eur_rate is not None:
            eur_sign = "+" if pnl >= 0 else "-"
            grid_rows.append(["", f"{eur_sign}€{abs(pnl) * eur_rate:,.2f}"])
    grid = format_grid(["Period", "PnL"], grid_rows) if grid_rows else ""

    return "\n\n".join(part for part in (header, grid) if part)


def format_position_status(
    asset_positions: list[dict],
    vault_value: float | None,
    equity: float | None,
    sell_price_by_coin: dict[str, float] | None = None,
    pending_entry_coin: str | None = None,
) -> str:
    """List open positions: exposure % is relative to the whole vault, dollar
    value, PnL, and funding are scaled down to this user's share of it.
    Funding is cumulative since the position was last opened from flat, same
    scope as the entry price it's shown alongside. The API's cumFunding is a
    cost accumulator (positive = paid), the opposite of PnL's sign
    convention, so it's negated here to display like PnL (positive = gained).

    With no open position at all, the flat fallback below normally leaves
    Symbol blank - but a coin can be flat and still have a single resting
    re-entry order on the exchange (passivbot places one the moment a
    position closes; see main.py's _pending_entry_coin). `pending_entry_coin`
    fills that Symbol row with it when so, since it's otherwise not named
    anywhere in the message even though format_open_orders lists its order.
    """
    fraction = _fraction(vault_value, equity)
    sell_price_by_coin = sell_price_by_coin or {}
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
        if fraction is not None:
            pnl = float(pos.get("unrealizedPnl", 0) or 0) * fraction
            sign = "+" if pnl >= 0 else "-"
            pct_str = f" ({pnl / equity * 100:+.2f}%)" if equity else ""
            rows.append(("PnL", f"{sign}${abs(pnl):,.2f}{pct_str}"))
        if equity is not None:
            rows.append(("Equity", f"${equity:,.2f}"))
        rows.append(("Exposure", f"{value_str}{exposure_pct}"))
        if fraction is not None:
            funding = -float((pos.get("cumFunding") or {}).get("sinceOpen", 0) or 0) * fraction
            funding_sign = "+" if funding >= 0 else "-"
            funding_pct_str = f" ({funding / equity * 100:+.2f}%)" if equity else ""
            rows.append(("Funding", f"{funding_sign}${abs(funding):,.2f}{funding_pct_str}"))

        current_price = notional / abs(size) if size else 0
        entry_price = float(pos.get("entryPx", 0) or 0)
        rows.append(("Symbol", f"{_pair(coin)} {side}"))
        sell_price = sell_price_by_coin.get(coin)
        if sell_price:
            rows.append(("Sell price", f"${_format_price(sell_price)}"))
        rows.append(("Current price", f"${_format_price(current_price)}"))
        rows.append(("Entry price", f"${_format_price(entry_price)}"))

        tables.append(format_table(rows))

    if tables:
        return "\n\n".join(tables)

    rows = []
    if fraction is not None:
        rows.append(("PnL", "$0.00 (0.00%)"))
    if equity is not None:
        rows.append(("Equity", f"${equity:,.2f}"))
    rows.append(("Exposure", "$0.00 (0.00%)" if vault_value else "$0.00"))
    if fraction is not None:
        rows.append(("Funding", "$0.00 (0.00%)"))
    rows.append(("Symbol", _pair(pending_entry_coin) if pending_entry_coin else ""))
    rows.append(("Current price", ""))
    rows.append(("Entry price", ""))
    return format_table(rows)


def needs_fresh_numbering(orders: list[dict], remembered: dict[int, int]) -> bool:
    """True if some resting buy order isn't in `remembered` yet, meaning
    assign_order_numbers will need historical order sizes to calibrate a
    fresh assignment - so the caller knows it's worth fetching fill history
    for (an expensive, rarely-needed call) rather than doing it every cycle.
    """
    return any(
        o.get("side") == "B" and not o.get("reduceOnly") and o.get("oid") not in remembered
        for o in orders
    )


def _multiplier(sizes: list[float]) -> float | None:
    """Median ratio between consecutive sizes - robust to a single outlier
    pair, which matters because the deepest (last) rung in a DCA ladder is
    excluded by the caller before this is used, but earlier rungs could
    still be missing/uneven."""
    ratios = [sizes[i + 1] / sizes[i] for i in range(len(sizes) - 1) if sizes[i] > 0]
    if not ratios:
        return None
    ratios.sort()
    mid = len(ratios) // 2
    return ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2


def assign_order_numbers(
    orders: list[dict],
    remembered: dict[int, int],
    historical_sizes_by_coin: dict[str, list[float]] | None = None,
) -> dict[int, int]:
    """Number each coin's resting buy orders 1 (closest to market) through N
    (deepest), fixed for as long as an order stays open - so when the
    nearest-to-market order fills, survivors keep their existing numbers
    instead of shifting down. Sell/reduce-only orders aren't numbered.

    Numbers are only assigned once, the first time a given set of oids is
    seen together, then carried forward unchanged via `remembered` (pass
    the previous call's return value, e.g. `state.order_numbers`) - a plain
    positional re-sort every cycle would renumber survivors every time the
    nearest order fills, since that's the end orders are removed from.
    Reassigned from scratch only when a new, not-yet-numbered order shows
    up alongside a coin's existing ones (e.g. the ladder got replaced).

    A fresh assignment is calibrated from `historical_sizes_by_coin`
    (chronological order sizes since the position last opened from flat -
    see historical_order_sizes), so a rung that already filled before the
    bot ever saw it (e.g. the very first rung, which can fill before the
    bot's first poll) still occupies its true number instead of the
    remaining orders starting back at 1. Order sizes in a DCA ladder like
    this one scale by a consistent multiplier per rung (~2.3x observed);
    the deepest rung is typically capped to whatever capital remains rather
    than continuing that multiplier, so rather than fitting it to the
    pattern it simply gets the number after the previous rung.
    """
    historical_sizes_by_coin = historical_sizes_by_coin or {}
    groups: dict[str, list[dict]] = {}
    for o in orders:
        if o.get("side") == "B" and not o.get("reduceOnly"):
            groups.setdefault(o.get("coin"), []).append(o)

    numbers: dict[int, int] = {}
    for coin, group in groups.items():
        oids = [o.get("oid") for o in group]
        if all(oid in remembered for oid in oids):
            for oid in oids:
                numbers[oid] = remembered[oid]
            continue

        # Closest to market first (highest price for a buy) - the order
        # rungs are expected to fill in, shallowest (next to fill) to
        # deepest (last resort).
        ranked = sorted(group, key=lambda o: float(o.get("limitPx", 0) or 0), reverse=True)
        history = historical_sizes_by_coin.get(coin, [])
        full_sequence = history + [float(o.get("sz", 0) or 0) for o in ranked]

        multiplier = _multiplier(full_sequence[:-1])
        base_size = full_sequence[0] if full_sequence else None

        levels: list[int] = []
        for i, size in enumerate(full_sequence):
            floor = (levels[-1] if levels else 0) + 1
            if i == len(full_sequence) - 1 or not base_size or not multiplier:
                level = floor
            else:
                # The very first fill (the entry that opened the position)
                # is rarely sized to fit the ladder's multiplier - its own
                # ratio-to-itself is always 1, so without a floor it can
                # round down onto the same level as the rung right after it.
                level = max(round(math.log(size / base_size, multiplier)) + 1, floor)
            levels.append(level)

        for o, level in zip(ranked, levels[len(history) :]):
            numbers[o.get("oid")] = level
    return numbers


def format_open_orders(
    orders: list[dict],
    first_entry_price_by_coin: dict[str, float] | None = None,
    order_numbers: dict[int, int] | None = None,
    vault_value: float | None = None,
    equity: float | None = None,
) -> str:
    """List resting buy orders (the DCA ladder) - sell orders are shown via
    format_position_status's "Sell price" row instead. Distance is measured
    from the fill that first opened the position (mirrors format_fill's
    convention). Numbered via order_numbers (see assign_order_numbers).

    Includes a coin with no open position right now if it has a resting
    order - passivbot places a fresh re-entry order the moment a position
    closes, so a "flat" coin can still have exactly one order sitting on
    the exchange. Distance is blank for it (no first_entry_price yet, since
    nothing's opened) and it's numbered #1 (see _update_order_numbers) since
    it's the start of a new cycle, not a continuation of the last one.

    A single table, no coin column: this vault only ever has orders for one
    coin at a time - either a full ladder for an open position, or (while
    flat) at most one pending re-entry order - so nothing here needs
    disambiguating; that coin's symbol is shown in format_position_status's
    Symbol row instead (see its `pending_entry_coin` param for the flat case).

    No "Side" column - every row here is a buy (sells are shown via
    format_position_status's "Sell price" row, not repeated here either).
    "Exposure" is relative to the **whole vault** (`vault_value`), same
    convention as everywhere else that word is used, while "Value" is
    scaled to this depositor's share (`fraction = equity / vault_value`) -
    what a resting order is actually worth to this user if it fills, not
    the whole vault's order size. A "Total" footer row sums both across
    every order, so it's clear what filling the *entire* remaining ladder
    would mean, not just one rung at a time.
    """
    buy_orders = [o for o in orders if o.get("side") == "B"]
    if not buy_orders:
        return "No open orders"
    first_entry_price_by_coin = first_entry_price_by_coin or {}
    order_numbers = order_numbers or {}
    fraction = _fraction(vault_value, equity)

    rows = []
    total_notional = 0.0
    for o in sorted(buy_orders, key=lambda o: float(o.get("limitPx", 0) or 0), reverse=True):
        price = float(o.get("limitPx", 0) or 0)
        notional = price * float(o.get("sz", 0) or 0)
        total_notional += notional
        exposure_str = f"{notional / vault_value * 100:.2f}%" if vault_value else ""
        value_str = f"${notional * fraction:,.0f}" if fraction is not None else "n/a"

        distance_ref = first_entry_price_by_coin.get(o.get("coin"))
        distance = (
            f"{(price - distance_ref) / distance_ref * 100:+.2f}%" if distance_ref else ""
        )

        number = order_numbers.get(o.get("oid"))
        rows.append(
            [
                str(number) if number else "",
                f"${_format_price(price)}",
                value_str,
                exposure_str,
                distance,
            ]
        )

    total_exposure_str = f"{total_notional / vault_value * 100:.2f}%" if vault_value else ""
    total_value_str = f"${total_notional * fraction:,.0f}" if fraction is not None else "n/a"
    footer = ["", "", total_value_str, total_exposure_str, ""]

    return format_grid(["#", "Price", "Val", "Exp", "Dist."], rows, footer)
