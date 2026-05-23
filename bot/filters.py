"""
filters.py — Token filter engine.
Applies all configured filters to a new token create event.
Returns (passed: bool, reason: str).
"""

import logging
import re
from typing import Optional
from bot.sol_price import SolPriceService

logger = logging.getLogger("filters")

TOTAL_SUPPLY = 1_000_000_000  # pump.fun always mints 1B tokens
CURVE_MIN_SOL = 30.0          # approximate SOL in curve at launch (empty)
CURVE_MAX_SOL = 85.0          # graduation threshold


class FilterEngine:
    def __init__(self, settings: dict, sol_price_service: SolPriceService):
        self.sol_price = sol_price_service
        self._blacklist: set[str] = set()
        self._recent_descriptions: set[str] = set()
        self.settings: dict = {}
        self._update_settings(settings)

    def _update_settings(self, settings: dict):
        """Hot-reload settings without restarting."""
        self.settings = settings
        f = settings.get("filters", {})
        self.min_mcap_usd            = f.get("min_market_cap_usd", 750)
        self.max_mcap_usd            = f.get("max_market_cap_usd", 6000)
        self.max_curve_pct           = f.get("max_bonding_curve_progress_pct", 15)
        self.max_dev_buy_pct         = f.get("max_dev_buy_pct", 5)
        self.block_bundled           = f.get("block_bundled_launches", True)
        self.min_buy_vol_sol         = f.get("min_buy_volume_sol", 2.0)
        self.min_unique_buyers       = f.get("min_unique_buyers", 3)
        self.max_sell_buy_ratio      = f.get("max_sell_buy_ratio", 0.5)
        self.obs_window_s            = f.get("observation_window_seconds", 30)
        self.keyword_blocklist       = [kw.lower() for kw in f.get("keyword_blocklist", [])]
        self.min_desc_len            = f.get("min_description_length", 0)

    def reload(self, settings: dict):
        self._update_settings(settings)

    def add_to_blacklist(self, wallet: str):
        self._blacklist.add(wallet)
        logger.info(f"Blacklisted wallet: {wallet[:8]}...")

    def record_outcome(self, event: dict, rugged: bool):
        if rugged and event.get("traderPublicKey"):
            self.add_to_blacklist(event["traderPublicKey"])

    # ─── Tier 1: Instant checks (no RPC, no async) ─────────────────────────────

    def _check_blacklist(self, event: dict) -> Optional[str]:
        wallet = event.get("traderPublicKey", "")
        if wallet in self._blacklist:
            return f"dev wallet blacklisted ({wallet[:8]}...)"
        return None

    def _check_metadata(self, event: dict) -> Optional[str]:
        name   = event.get("name", "")
        symbol = event.get("symbol", "")
        desc = event.get("description") or event.get("desc") or event.get("vdescription") or ""
        desc = str(desc)

        if len(desc) < self.min_desc_len:
            return f"description too short ({len(desc)} chars)"

        norm_desc = desc.strip().lower()
        if norm_desc and norm_desc in self._recent_descriptions:
            return "duplicate description (recycled launch)"
        if norm_desc:
            self._recent_descriptions.add(norm_desc)
        if len(self._recent_descriptions) > 500:
            self._recent_descriptions.pop()

        if re.search(r"[^A-Z0-9$\-_.]", symbol, re.IGNORECASE):
            return f"invalid symbol chars: {symbol}"

        name_lower = name.lower()
        for kw in self.keyword_blocklist:
            if kw in name_lower:
                return f"keyword blocked: '{kw}'"

        return None

    # ─── Tier 2: On-chain / price checks (async) ───────────────────────────────

    async def check_create_event(self, event: dict) -> tuple[bool, str]:
        """
        Run all Tier 1 + Tier 2 checks on a new token create event.
        Returns (passed, reason).
        """
        # T1: blacklist
        if r := self._check_blacklist(event):
            return False, r

        # T1: metadata
        if r := self._check_metadata(event):
            return False, r

        # T2: SOL price (lazy fetch — only calls CoinGecko if cache stale)
        sol_usd = await self.sol_price.get_price()
        if not sol_usd:
            return False, "SOL price unavailable"

        # T2: market cap
        mcap_sol = event.get("marketCapSol", 0)
        mcap_usd = mcap_sol * sol_usd
        if mcap_usd < self.min_mcap_usd:
            return False, f"market cap too low (${mcap_usd:.0f} < ${self.min_mcap_usd})"
        if mcap_usd > self.max_mcap_usd:
            return False, f"market cap too high (${mcap_usd:.0f} > ${self.max_mcap_usd})"

        # T2: bonding curve progress
        v_sol = event.get("vSolInBondingCurve", 0)
        curve_pct = ((v_sol - CURVE_MIN_SOL) / (CURVE_MAX_SOL - CURVE_MIN_SOL)) * 100
        curve_pct = max(0, min(100, curve_pct))
        if curve_pct > self.max_curve_pct:
            return False, f"bonding curve {curve_pct:.1f}% > max {self.max_curve_pct}%"

        # T2: dev initial buy
        initial_buy = event.get("initialBuy", 0)
        dev_buy_pct = (initial_buy / TOTAL_SUPPLY) * 100
        if dev_buy_pct > self.max_dev_buy_pct:
            return False, f"dev bought {dev_buy_pct:.1f}% of supply"

        # T2: bundled launch detection (PumpPortal flags this directly)
        if self.block_bundled and event.get("isBundled"):
            return False, "bundled launch blocked"

        return True, "all checks passed"

    def check_trade_window(self, trades: list[dict]) -> tuple[bool, str]:
        buys  = [t for t in trades if t.get("txType") == "buy"]
        sells = [t for t in trades if t.get("txType") == "sell"]

        total_buy_sol  = sum(t.get("solAmount", 0) for t in buys)
        total_sell_sol = sum(t.get("solAmount", 0) for t in sells)
        unique_buyers  = len(set(t.get("traderPublicKey") for t in buys))
        sell_buy_ratio = total_sell_sol / total_buy_sol if total_buy_sol > 0 else 999

        if total_buy_sol < self.min_buy_vol_sol:
            return False, f"buy volume {total_buy_sol:.2f} SOL < min {self.min_buy_vol_sol}"
        if unique_buyers < self.min_unique_buyers:
            return False, f"only {unique_buyers} unique buyers (min {self.min_unique_buyers})"
        if sell_buy_ratio > self.max_sell_buy_ratio:
            return False, f"sell/buy ratio {sell_buy_ratio:.2f} > max {self.max_sell_buy_ratio}"
        if len(sells) >= len(buys):
            return False, f"more sells ({len(sells)}) than buys ({len(buys)})"

        return True, f"volume OK ({total_buy_sol:.2f} SOL, {unique_buyers} buyers)"

    def check_early_trigger(self, trades: list[dict]) -> tuple[bool, str]:
        """
        Check if current mid-window trades already meet the early-buy signal thresholds.
        Returns (triggered, reason). Called every 0.5s during the observation window.
        """
        trig = self.settings.get("early_buy_trigger", {})
        min_buys     = trig.get("min_buys", 8)
        min_vol      = trig.get("min_buy_volume_sol", 0.5)
        min_ub       = trig.get("min_unique_buyers", 3)
        max_sbr      = trig.get("max_sell_buy_ratio", 0.3)

        buys  = [t for t in trades if t.get("txType") == "buy"]
        sells = [t for t in trades if t.get("txType") == "sell"]

        buy_vol       = sum(t.get("solAmount", 0) for t in buys)
        unique_buyers = len(set(t.get("traderPublicKey") for t in buys))
        sbr           = sum(t.get("solAmount", 0) for t in sells) / buy_vol if buy_vol > 0 else 999

        if (len(buys) >= min_buys
                and buy_vol >= min_vol
                and unique_buyers >= min_ub
                and sbr <= max_sbr):
            return True, f"{len(buys)} buys, {buy_vol:.3f} SOL, {unique_buyers} buyers"
        return False, ""


