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
_CURRENT_PRICE_COLOR = "#f0883e"
_ENTRY_PRICE_COLOR = "#58a6ff"


def _fill_markers(
    fills: list[dict],
    df: pd.DataFrame,
    marker: str,
    color: str,
    base_ref: pd.Series,
    offset_sign: int,
) -> list:
    """Build one scatter addplot per stack level for a set of fills, deduped
    by oid (so an order that filled across several partials still gets just
    one marker) and matched to their nearest candle. Offsets are sized
    against the candle range so they land at a sensible height regardless of
    how far any order lines sit, and multiple fills landing on the same
    candle stack outward from it (`offset_sign` +1 = upward from High, -1 =
    downward from Low) rather than overwriting each other.
    """
    seen_oids = set()
    deduped_fills = []
    for f in fills:
        oid = f.get("oid")
        if oid in seen_oids:
            continue
        seen_oids.add(oid)
        deduped_fills.append(f)

    positions = []
    for f in deduped_fills:
        ts = pd.to_datetime(int(f.get("time", 0)), unit="ms")
        pos = df.index.get_indexer([ts], method="nearest")[0]
        if pos >= 0:
            positions.append(pos)

    if not positions:
        return []

    price_range = df["High"].max() - df["Low"].min()
    base_offset = price_range * 0.045
    stack_gap = price_range * 0.09

    fills_seen_at: dict[int, int] = {}
    stack_series: list[pd.Series] = []
    for pos in positions:
        level = fills_seen_at.get(pos, 0)
        fills_seen_at[pos] = level + 1
        while level >= len(stack_series):
            stack_series.append(pd.Series(float("nan"), index=df.index))
        stack_series[level].iloc[pos] = base_ref.iloc[pos] + offset_sign * (
            base_offset + level * stack_gap
        )

    return [
        mpf.make_addplot(series, type="scatter", markersize=45, marker=marker, color=color)
        for series in stack_series
    ]


def render_candles(
    candles: list[dict],
    coin: str,
    interval: str,
    buy_fills: list[dict] | None = None,
    open_buy_prices: dict[int, float] | None = None,
    open_sell_prices: list[float] | None = None,
    entry_price: float | None = None,
    sell_fills: list[dict] | None = None,
) -> bytes:
    """Render candles (as returned by HyperliquidClient.get_candles) to a PNG,
    with optional overlays: `buy_fills` (raw fill dicts within the candle
    window) as red upside-down triangles above each fill's candle, and
    `sell_fills` as green right-side-up triangles below each fill's candle -
    a mirror image of the buy markers (opposite side of the candle, opposite
    point direction, opposite color) so a trade's entries and exit(s) both
    show on the same chart. Each is one marker per order that filled
    (deduped by oid, so an order that filled across several partials still
    gets just one marker), stacked outward when a candle has more than one
    fill of that side. `open_buy_prices` (rung number -> price, from
    assign_order_numbers) draws dashed red lines labeled with the rung, and
    `open_sell_prices` dashed green lines labeled "TP" - so the chart shows
    where the ladder has already bought, where it's still resting, and
    where it'll take profit, without opening a separate app. The last
    candle's close is always drawn as a dotted orange "Now" line, and
    `entry_price` (the position's blended average entry) as a dotted blue
    "Entry" line - both dotted rather than dashed to stay visually distinct
    from the order lines.
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
        addplots += _fill_markers(buy_fills, df, "v", _BUY_COLOR, df["High"], 1)
    if sell_fills:
        addplots += _fill_markers(sell_fills, df, "^", _SELL_COLOR, df["Low"], -1)

    hlines_prices: list[float] = []
    hlines_colors: list[str] = []
    hlines_styles: list[str] = []
    for price in (open_buy_prices or {}).values():
        hlines_prices.append(price)
        hlines_colors.append(_BUY_COLOR)
        hlines_styles.append("--")
    for price in open_sell_prices or []:
        hlines_prices.append(price)
        hlines_colors.append(_SELL_COLOR)
        hlines_styles.append("--")
    current_price = float(candles[-1]["c"]) if candles else None
    if current_price is not None:
        hlines_prices.append(current_price)
        hlines_colors.append(_CURRENT_PRICE_COLOR)
        hlines_styles.append(":")
    if entry_price is not None:
        hlines_prices.append(entry_price)
        hlines_colors.append(_ENTRY_PRICE_COLOR)
        hlines_styles.append(":")
    hlines = (
        dict(hlines=hlines_prices, colors=hlines_colors, linestyle=hlines_styles, linewidths=1.0)
        if hlines_prices
        else None
    )

    # mplfinance's kwarg validators reject `addplot=None`/`hlines=None`
    # outright (only accepting a real list/dict, or the kwarg being absent
    # entirely) - both can legitimately be empty for a chart with no
    # order/fill context to overlay (e.g. a plain spot-price chart), so
    # those kwargs are only included when there's actually something to plot.
    plot_kwargs = dict(
        type="candle",
        style=_STYLE,
        volume=True,
        panel_ratios=(6, 1),
        title=f"\n{coin}/USDC · {interval}",
        figsize=(8, 5),
        returnfig=True,
    )
    if addplots:
        plot_kwargs["addplot"] = addplots
    if hlines:
        plot_kwargs["hlines"] = hlines

    fig, axes = mpf.plot(df, **plot_kwargs)

    price_ax, volume_ax = axes[0], axes[2]
    price_ax.set_ylabel("")
    volume_ax.set_ylabel("")
    volume_ax.set_yticklabels([])

    # mplfinance plots candles at integer x-positions 0..len(df)-1 and adds
    # its own ~7% margin on either side by default; tighten every panel to
    # just half a candle-width of margin (enough that the outermost
    # candles aren't clipped) instead of that empty left/right gap.
    for ax in axes:
        ax.set_xlim(-0.5, len(df) - 0.5)

    # Order lines can sit far from the candle action (a deep DCA rung, or a
    # take-profit well above it), which would otherwise stretch the axis
    # and squash the candles into an unreadable band. Only order lines
    # within "nearby_span" of the candle range count as close enough to
    # show; the axis is then fit tightly to whatever that actually
    # includes (candles plus any qualifying lines), not padded out to a
    # fixed margin regardless of whether anything is there to show.
    candle_low, candle_high = df["Low"].min(), df["High"].max()
    nearby_span = (candle_high - candle_low) * 0.5
    nearby_prices = [
        p
        for p in [
            *(open_buy_prices or {}).values(),
            *(open_sell_prices or []),
            *([current_price] if current_price is not None else []),
            *([entry_price] if entry_price is not None else []),
        ]
        if candle_low - nearby_span <= p <= candle_high + nearby_span
    ]
    content_low = min([candle_low, *nearby_prices])
    content_high = max([candle_high, *nearby_prices])
    breathing_room = (content_high - content_low) * 0.03
    visible_low, visible_high = content_low - breathing_room, content_high + breathing_room
    price_ax.set_ylim(visible_low, visible_high)
    for ax in axes:
        ax.tick_params(axis="x", labelbottom=False, labeltop=False)

    label_kwargs = {
        "fontsize": 8,
        "va": "center",
        "transform": price_ax.get_yaxis_transform(),
    }

    def label(price: float, text: str, color: str) -> None:
        # text() isn't clipped to the axes by default, so a label for a
        # line that's now outside the capped range would otherwise float
        # off in blank space rather than just not being drawn.
        if visible_low <= price <= visible_high:
            price_ax.text(1.005, price, text, color=color, **label_kwargs)

    for number, price in (open_buy_prices or {}).items():
        label(price, f"#{number}", _BUY_COLOR)
    for price in open_sell_prices or []:
        label(price, "TP", _SELL_COLOR)
    if current_price is not None:
        label(current_price, "Now", _CURRENT_PRICE_COLOR)
    if entry_price is not None:
        label(entry_price, "Entry", _ENTRY_PRICE_COLOR)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
