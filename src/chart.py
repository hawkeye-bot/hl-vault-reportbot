"""Renders dark-themed charts for fill/status messages."""

import io

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

_STYLE = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=mpf.make_marketcolors(
        up="#3fb950",
        down="#f85149",
        edge="inherit",
        wick="inherit",
        volume="inherit",
    ),
    facecolor="#0d1117",
    figcolor="#0d1117",
    gridcolor="#21262d",
    gridstyle="-",
    rc={"axes.edgecolor": "#30363d", "text.color": "#c9d1d9", "axes.labelcolor": "#c9d1d9"},
)

# Candle up/down colors above are about price direction; these are about
# order side, and are deliberately the other way round per user preference.
_BUY_COLOR = "#f85149"
_SELL_COLOR = "#3fb950"


def render_candles(
    candles: list[dict],
    coin: str,
    interval: str,
    buy_fills: list[dict] | None = None,
    open_buy_prices: dict[int, float] | None = None,
    open_sell_prices: list[float] | None = None,
) -> bytes:
    """Render candles (as returned by HyperliquidClient.get_candles) to a PNG,
    with optional overlays: `buy_fills` (raw fill dicts within the candle
    window) as red upside-down triangles above each fill's candle (stacked
    upward when a candle has more than one fill), `open_buy_prices` (rung
    number -> price, from assign_order_numbers) as dashed red lines
    labeled with the rung, and `open_sell_prices` as dashed green lines
    labeled "TP" - so the chart shows where the ladder has already
    bought, where it's still resting, and where it'll take profit,
    without opening a separate app.
    """
    df = pd.DataFrame(
        [
            {
                "Date": pd.to_datetime(c["t"], unit="ms"),
                "Open": float(c["o"]),
                "High": float(c["h"]),
                "Low": float(c["l"]),
                "Close": float(c["c"]),
                "Volume": float(c["v"]),
            }
            for c in candles
        ]
    ).set_index("Date")

    addplots = []
    if buy_fills:
        # Marked with an upside-down triangle above the candle's high (not
        # at the exact fill price, and not overlapping the candle) so it
        # reads as a "bought here" flag pointing down at the candle. Two or
        # more fills landing in the same candle stack their triangles
        # upward rather than overwriting each other.
        positions = []
        for f in buy_fills:
            ts = pd.to_datetime(int(f.get("time", 0)), unit="ms")
            pos = df.index.get_indexer([ts], method="nearest")[0]
            if pos >= 0:
                positions.append(pos)

        if positions:
            # Use the full visible price range (candles plus the resting
            # order lines below, which can sit far below the candles) so
            # the stack offsets are a sensible fraction of what actually
            # renders, not just the (possibly much smaller) candle range.
            axis_prices = [df["High"].max(), df["Low"].min()]
            axis_prices += list((open_buy_prices or {}).values())
            axis_prices += list(open_sell_prices or [])
            price_range = max(axis_prices) - min(axis_prices)
            base_offset = price_range * 0.045
            stack_gap = price_range * 0.09

            fills_seen_at: dict[int, int] = {}
            stack_series: list[pd.Series] = []
            for pos in positions:
                level = fills_seen_at.get(pos, 0)
                fills_seen_at[pos] = level + 1
                while level >= len(stack_series):
                    stack_series.append(pd.Series(float("nan"), index=df.index))
                stack_series[level].iloc[pos] = df["High"].iloc[pos] + base_offset + level * stack_gap

            for series in stack_series:
                addplots.append(
                    mpf.make_addplot(
                        series, type="scatter", markersize=45, marker="v", color=_BUY_COLOR
                    )
                )

    hlines_prices: list[float] = []
    hlines_colors: list[str] = []
    for price in (open_buy_prices or {}).values():
        hlines_prices.append(price)
        hlines_colors.append(_BUY_COLOR)
    for price in open_sell_prices or []:
        hlines_prices.append(price)
        hlines_colors.append(_SELL_COLOR)
    hlines = (
        dict(hlines=hlines_prices, colors=hlines_colors, linestyle="--", linewidths=1.0)
        if hlines_prices
        else None
    )

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=_STYLE,
        volume=True,
        title=f"\n{coin}/USDC · {interval}",
        figsize=(8, 5),
        addplot=addplots or None,
        hlines=hlines,
        returnfig=True,
    )

    price_ax = axes[0]
    label_kwargs = {
        "fontsize": 8,
        "va": "center",
        "transform": price_ax.get_yaxis_transform(),
    }
    for number, price in (open_buy_prices or {}).items():
        price_ax.text(1.005, price, f"#{number}", color=_BUY_COLOR, **label_kwargs)
    for price in open_sell_prices or []:
        price_ax.text(1.005, price, "TP", color=_SELL_COLOR, **label_kwargs)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
