"""
sol_price.py — Lazy SOL/USD price fetcher with 15-min cache.
Uses CoinGecko's FREE public API — no API key required.
Endpoint: https://api.coingecko.com/api/v3/simple/price
Fetches only when cache is stale and a price is actually needed.
All concurrent callers share a single in-flight request (no duplicate calls).
"""

import asyncio
import time
import logging
from typing import Optional

import httpx

logger = logging.getLogger("sol_price")

# Free public endpoint — no API key, no auth header needed
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"


class SolPriceService:
    def __init__(self, cache_ttl_minutes: int = 15):
        self.ttl_ms = cache_ttl_minutes * 60 * 1000
        self.cached_price: Optional[float] = None
        self.last_fetched_at: Optional[float] = None   # epoch ms
        self._lock = asyncio.Lock()                    # prevents concurrent fetches

    def _is_stale(self) -> bool:
        if self.cached_price is None or self.last_fetched_at is None:
            return True
        return (time.time() * 1000 - self.last_fetched_at) > self.ttl_ms

    def _age_minutes(self) -> float:
        if self.last_fetched_at is None:
            return 0.0
        return (time.time() * 1000 - self.last_fetched_at) / 60_000

    async def get_price(self) -> Optional[float]:
        """
        Return cached SOL/USD price if fresh, else fetch from CoinGecko public API.
        Only one HTTP request fires even if many coroutines hit a stale cache at once.
        """
        # Fast path — no lock needed
        if not self._is_stale():
            return self.cached_price

        # Slow path — acquire lock so only one coroutine fetches
        async with self._lock:
            # Re-check after acquiring (another coroutine may have just fetched)
            if not self._is_stale():
                return self.cached_price
            return await self._fetch()

    async def _fetch(self) -> Optional[float]:
        logger.info("SOL price cache stale — fetching from CoinGecko (public, no key)...")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(COINGECKO_URL)
                resp.raise_for_status()
                data = resp.json()
                price = data["solana"]["usd"]

                if not isinstance(price, (int, float)) or price <= 0:
                    raise ValueError(f"Bad price value: {price}")

                self.cached_price = float(price)
                self.last_fetched_at = time.time() * 1000
                ttl_min = self.ttl_ms / 60_000
                logger.info(f"SOL price: ${price:.2f} — cached for {ttl_min:.0f} min")
                return self.cached_price

        except httpx.HTTPStatusError as e:
            logger.error(f"CoinGecko HTTP {e.response.status_code}: {e}")
        except httpx.TimeoutException:
            logger.error("CoinGecko request timed out (5s)")
        except Exception as e:
            logger.error(f"CoinGecko fetch error: {e}")

        # Return stale value rather than crashing the bot
        if self.cached_price is not None:
            logger.warning(f"Using stale SOL price: ${self.cached_price:.2f} (age: {self._age_minutes():.1f} min)")
            return self.cached_price

        return None

    async def prefetch(self) -> None:
        """Pre-warm the cache at bot startup."""
        logger.info("Pre-warming SOL price cache (CoinGecko public API)...")
        price = await self.get_price()
        if price:
            logger.info(f"SOL price ready: ${price:.2f}")
        else:
            logger.warning("SOL price pre-warm failed — will retry on first token")

    def status(self) -> dict:
        return {
            "cached_price": self.cached_price,
            "age_minutes": round(self._age_minutes(), 1),
            "is_stale": self._is_stale(),
            "ttl_minutes": self.ttl_ms / 60_000,
            "source": "CoinGecko public API (no key)",
        }
