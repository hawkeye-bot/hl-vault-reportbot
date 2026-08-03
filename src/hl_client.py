from hyperliquid.info import Info
from hyperliquid.utils import constants


class HyperliquidClient:
    def __init__(self, vault_address: str, user_address: str):
        self.vault_address = vault_address
        self.user_address = user_address
        self.info = Info(constants.MAINNET_API_URL, skip_ws=True)

    def get_open_positions(self) -> list[dict]:
        state = self.info.user_state(self.vault_address)
        return state.get("assetPositions", [])

    def get_margin_summary(self) -> dict:
        state = self.info.user_state(self.vault_address)
        return state.get("marginSummary", {})

    def get_fills_since(self, start_time_ms: int) -> list[dict]:
        return self.info.user_fills_by_time(self.vault_address, start_time_ms)

    def get_vault_equity(self) -> float | None:
        """Return the user's depositor equity in the vault (USD), or None on error."""
        try:
            result = self.info.post(
                "/info",
                {
                    "type": "vaultDetails",
                    "vaultAddress": self.vault_address,
                    "user": self.user_address,
                },
            )
            follower = result.get("followerState") or {}
            equity_str = follower.get("vaultEquity")
            return float(equity_str) if equity_str is not None else None
        except Exception:
            return None

    def get_vault_all_time_pnl(self) -> float | None:
        try:
            result = self.info.post(
                "/info",
                {
                    "type": "vaultDetails",
                    "vaultAddress": self.vault_address,
                    "user": self.user_address,
                },
            )
            pnl_str = result.get("allTimePnl")
            return float(pnl_str) if pnl_str is not None else None
        except Exception:
            return None
