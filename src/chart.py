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
    window) as up-triangle markers at their fill price, `open_buy_prices`
    (rung number -> price, from assign_order_numbers) as dashed green lines
    labeled with the rung, and `open_sell_prices` as dashed red lines
    labeled "TP" - so the chart shows where the ladder has already bought,
    where it's still resting, and where it'll take profit, without opening
    a separate app.
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
        fill_series = pd.Series(float("nan"), index=df.index)
        for f in buy_fills:
            ts = pd.to_datetime(int(f.get("time", 0)), unit="ms")
            pos = df.index.get_indexer([ts], method="nearest")[0]
            if pos >= 0:
                fill_series.iloc[pos] = float(f.get("px", 0) or 0)
        if fill_series.notna().any():
            addplots.append(
                mpf.make_addplot(
                    fill_series, type="scatter", markersize=70, marker="^", color="#58a6ff"
                )
            )

    hlines_prices: list[float] = []
    hlines_colors: list[str] = []
    for price in (open_buy_prices or {}).values():
        hlines_prices.append(price)
        hlines_colors.append("#3fb950")
    for price in open_sell_prices or []:
        hlines_prices.append(price)
        hlines_colors.append("#f85149")
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
        price_ax.text(1.005, price, f"#{number}", color="#3fb950", **label_kwargs)
    for price in open_sell_prices or []:
        price_ax.text(1.005, price, "TP", color="#f85149", **label_kwargs)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
