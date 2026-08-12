"""Renders a dark-themed candlestick chart for fill messages."""

import io

import matplotlib

matplotlib.use("Agg")

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


def render_candles(candles: list[dict], coin: str, interval: str) -> bytes:
    """Render candles (as returned by HyperliquidClient.get_candles) to a PNG."""
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

    buf = io.BytesIO()
    mpf.plot(
        df,
        type="candle",
        style=_STYLE,
        volume=True,
        title=f"\n{coin}/USDC · {interval}",
        figsize=(8, 5),
        savefig=dict(fname=buf, format="png", dpi=150, bbox_inches="tight"),
    )
    buf.seek(0)
    return buf.read()
