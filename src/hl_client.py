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

    def get_spot_balances(self) -> list[dict]:
        """This user's spot wallet balances (token, total, hold, entryNtl)."""
        return self.info.spot_user_state(self.user_address).get("balances", [])

    def _token_names_and_prices(self) -> tuple[dict[int, str], dict[str, float]]:
        """Token index -> name, and token name -> USD mid price (from each
        token's USDC-quoted spot pair - all_mids doesn't cover spot tokens
        directly, only perps and the raw "@<index>" pair names). Tokens
        with no direct USDC pair are omitted from the price dict. Shared by
        get_spot_prices and get_earn_value, which both need to resolve raw
        token indices to something valuable.
        """
        meta, ctxs = self.info.spot_meta_and_asset_ctxs()
        # Token list position doesn't always match its declared "index"
        # (it's sparse - e.g. 485 tokens but indices up to 858), so this
        # must be looked up by index, not by position.
        token_name_by_index = {t["index"]: t["name"] for t in meta["tokens"]}
        ctx_by_pair_name = {c["coin"]: c for c in ctxs}
        prices = {"USDC": 1.0}
        for pair in meta["universe"]:
            base_idx, quote_idx = pair["tokens"]
            if quote_idx != 0:  # only USDC-quoted pairs give a direct USD price
                continue
            mid_px = ctx_by_pair_name.get(pair["name"], {}).get("midPx")
            token_name = token_name_by_index.get(base_idx)
            if mid_px is None or token_name is None:
                continue
            prices[token_name] = float(mid_px)
        return token_name_by_index, prices

    def get_spot_prices(self) -> dict[str, float]:
        """USD mid price per spot token name (e.g. "UBTC")."""
        _, prices = self._token_names_and_prices()
        return prices

    def get_spot_pair_name(self, token_name: str) -> str | None:
        """The internal pair name (e.g. "@107") for `token_name`'s
        USDC-quoted spot market - candleSnapshot and similar endpoints need
        this, not the token's own symbol, and it's often a raw "@<index>"
        rather than something human-readable. Returns None if the token has
        no USDC-quoted spot market.
        """
        meta, _ = self.info.spot_meta_and_asset_ctxs()
        token_idx = next(
            (t["index"] for t in meta["tokens"] if t["name"] == token_name), None
        )
        if token_idx is None:
            return None
        for pair in meta["universe"]:
            base_idx, quote_idx = pair["tokens"]
            if base_idx == token_idx and quote_idx == 0:
                return pair["name"]
        return None

    def get_earn_value(self) -> float:
        """Net USD value of this user's Hyperliquid lending ("Earn")
        positions - total supplied minus borrowed, across every token,
        valued at each token's current price. Not wrapped by the SDK, so
        this goes straight to the "borrowLendUserState" info type.
        """
        state = self.info.post(
            "/info", {"type": "borrowLendUserState", "user": self.user_address}
        )
        token_name_by_index, prices = self._token_names_and_prices()
        total = 0.0
        for idx, entry in state.get("tokenToState", []):
            price = prices.get(token_name_by_index.get(idx))
            if price is None:
                continue
            supply_value = float((entry.get("supply") or {}).get("value", 0) or 0)
            borrow_value = float((entry.get("borrow") or {}).get("value", 0) or 0)
            total += (supply_value - borrow_value) * price
        return total

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
