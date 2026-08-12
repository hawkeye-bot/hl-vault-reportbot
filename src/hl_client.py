import time

from hyperliquid.info import Info
from hyperliquid.utils import constants

# The SDK's underlying `requests` session has no timeout by default, so a
# hung connection would block the whole event loop forever (this class's
# methods are synchronous calls made directly from async code, not offloaded
# to a thread). Bound every request instead.
REQUEST_TIMEOUT_SECONDS = 15


class HyperliquidClient:
    def __init__(self, vault_address: str, user_address: str):
        self.vault_address = vault_address
        self.user_address = user_address
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=REQUEST_TIMEOUT_SECONDS)

    def get_margin_summary(self) -> dict:
        state = self.info.user_state(self.vault_address)
        return state.get("marginSummary", {})

    def get_open_positions(self) -> list[dict]:
        state = self.info.user_state(self.vault_address)
        return state.get("assetPositions", [])

    def get_fills_since(self, start_time_ms: int) -> list[dict]:
        return self.info.user_fills_by_time(self.vault_address, start_time_ms)

    def get_open_orders(self) -> list[dict]:
        return self.info.open_orders(self.vault_address)

    def get_candles(self, coin: str, interval: str, lookback_ms: int) -> list[dict]:
        end = int(time.time() * 1000)
        return self.info.candles_snapshot(coin, interval, end - lookback_ms, end)

    def get_portfolio(self) -> list:
        """Account-wide (spot + perp + vault) value/PnL/volume history per
        period (day/week/month/allTime, plus perp-only variants) - the same
        data behind Hyperliquid's own Portfolio page. Scoped to user_address,
        not the vault - this is this depositor's whole account, not just
        their stake in the one vault the rest of this bot watches.
        """
        return self.info.portfolio(self.user_address)

    def get_staking_summary(self) -> dict:
        """This user's HYPE staking summary: delegated, undelegated, and
        pending-withdrawal amounts."""
        return self.info.user_staking_summary(self.user_address)

    def get_mid_prices(self) -> dict[str, str]:
        """Mid price per coin (e.g. "HYPE"), market-wide - not scoped to
        the vault or this user."""
        return self.info.all_mids()

    def get_vault_details(self) -> dict:
        """Vault details plus this user's follower state (equity, all-time PnL, etc.)."""
        try:
            return self.info.post(
                "/info",
                {
                    "type": "vaultDetails",
                    "vaultAddress": self.vault_address,
                    "user": self.user_address,
                },
            )
        except Exception:
            return {}
