"""Tracks vault state between polls and detects changes."""

from dataclasses import dataclass, field


@dataclass
class PositionSnapshot:
    coin: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    unrealized_pnl: float


@dataclass
class VaultState:
    positions: dict[str, PositionSnapshot] = field(default_factory=dict)
    seen_fill_hashes: set[str] = field(default_factory=set)
    liquidation_warned: bool = False


def parse_positions(asset_positions: list[dict]) -> dict[str, PositionSnapshot]:
    result = {}
    for ap in asset_positions:
        pos = ap.get("position", {})
        coin = pos.get("coin")
        if not coin:
            continue
        size = float(pos.get("szi", 0))
        if size == 0:
            continue
        result[coin] = PositionSnapshot(
            coin=coin,
            side="long" if size > 0 else "short",
            size=abs(size),
            entry_price=float(pos.get("entryPx", 0) or 0),
            unrealized_pnl=float(pos.get("unrealizedPnl", 0) or 0),
        )
    return result


def diff_positions(
    old: dict[str, PositionSnapshot], new: dict[str, PositionSnapshot]
) -> list[str]:
    events = []

    for coin, new_pos in new.items():
        if coin not in old:
            events.append(
                f"New {new_pos.side} position opened on {coin}: "
                f"{new_pos.size} @ ${new_pos.entry_price:,.4f}"
            )
        else:
            old_pos = old[coin]
            if abs(new_pos.size - old_pos.size) > 1e-9:
                direction = "increased" if new_pos.size > old_pos.size else "reduced"
                events.append(
                    f"{coin} {new_pos.side} position {direction}: "
                    f"{old_pos.size} → {new_pos.size} (entry ${new_pos.entry_price:,.4f})"
                )

    for coin, old_pos in old.items():
        if coin not in new:
            events.append(
                f"{coin} {old_pos.side} position closed "
                f"(was {old_pos.size} @ ${old_pos.entry_price:,.4f})"
            )

    return events


def format_fill(fill: dict) -> str:
    coin = fill.get("coin", "?")
    side = fill.get("side", "?")
    size = float(fill.get("sz", 0))
    price = float(fill.get("px", 0))
    closed_pnl = fill.get("closedPnl")
    pnl_str = ""
    if closed_pnl is not None:
        pnl = float(closed_pnl)
        sign = "+" if pnl >= 0 else ""
        pnl_str = f" | PnL: {sign}${pnl:,.2f}"
    return f"Fill: {side.upper()} {size} {coin} @ ${price:,.4f}{pnl_str}"
