"""
engine.py — Core sniper bot engine.
Connects to PumpPortal WebSocket (free tier) for new token creates.
Uses a persistent Helius logsSubscribe WS for observation-window trade data.

Note on PumpPortal trade subscriptions:
  PumpPortal's subscribeTokenTrade requires a funded account (>=0.02 SOL).
  The free tier only supports subscribeNewToken, so Helius logsSubscribe is
  used for per-token trade observation.
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

from bot.config import load_settings
from bot.filters import FilterEngine
from bot.sol_price import SolPriceService
from bot.state import bot_state, TradeLog, Position
from bot.helius_trader import HeliusTrader, load_keypair_from_base58, get_bonding_curve_pda
from bot.exit_engine import ExitEngine
from bot.pump_events import parse_trade_event

logger = logging.getLogger("engine")

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
TOKEN_DECIMALS = 6


class SniperEngine:
    def __init__(self):
        self.settings: dict = {}
        self.sol_price_svc: Optional[SolPriceService] = None
        self.filter_engine: Optional[FilterEngine] = None
        self.trader: Optional[HeliusTrader] = None
        self.exit_engine: Optional[ExitEngine] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._helius_ws_urls: list = []
        self._obs_url_idx: int = 0
        self._running = False
        self._pending_observations: dict[str, list] = {}
        self._last_error: Optional[str] = None
        # Persistent Helius WS for observation subscriptions
        self._helius_obs_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._obs_req_counter: int = 1
        self._obs_pending_subs: dict[int, str] = {}
        self._obs_sub_ids: dict[str, int] = {}

    def _reload_config(self):
        self.settings = load_settings()
        if self.sol_price_svc:
            self.sol_price_svc.ttl_ms = self.settings["sol_price"]["cache_ttl_minutes"] * 60 * 1000
        if self.filter_engine:
            self.filter_engine.reload(self.settings)
        if self.exit_engine:
            self.exit_engine.update_settings(self.settings)

    async def start(self):
        if self._running:
            logger.warning("Engine already running")
            return

        self._reload_config()
        self._last_error = None

        api_keys = self.settings["api_keys"]
        sol_cfg  = self.settings["sol_price"]
        trading  = self.settings["trading"]
        dry_run  = trading["dry_run"]

        rpc_url = api_keys.get("helius_rpc_url")
        if not rpc_url and api_keys.get("helius_api_key"):
            rpc_url = f"https://mainnet.helius-rpc.com/?api-key={api_keys['helius_api_key']}"

        if not rpc_url:
            self._last_error = "Helius API Key not configured in Settings"
            logger.error(self._last_error)
            return

        if not dry_run and not self.settings["wallet"].get("private_key"):
            self._last_error = "Wallet private key required for live trading"
            logger.error(self._last_error)
            return

        # Build WS URL list for key rotation across all configured Helius API keys
        if api_keys.get("helius_rpc_url"):
            self._helius_ws_urls = [rpc_url.replace("https://", "wss://")]
        else:
            _ws_urls = []
            for _key_name in ("helius_api_key", "helius_api_key_2", "helius_api_key_3"):
                _k = api_keys.get(_key_name, "").strip()
                if _k:
                    _ws_urls.append(f"wss://mainnet.helius-rpc.com/?api-key={_k}")
            self._helius_ws_urls = _ws_urls or [rpc_url.replace("https://", "wss://")]
        logger.info(f"Helius key rotation: {len(self._helius_ws_urls)} key(s) configured")

        self.sol_price_svc = SolPriceService(
            cache_ttl_minutes=sol_cfg["cache_ttl_minutes"]
        )
        await self.sol_price_svc.prefetch()

        self.filter_engine = FilterEngine(self.settings, self.sol_price_svc)

        if not dry_run:
            keypair = load_keypair_from_base58(self.settings["wallet"]["private_key"])
            if not keypair:
                self._last_error = "Failed to load wallet keypair — check private key format"
                logger.error(self._last_error)
                return
            self.trader = HeliusTrader(rpc_url=rpc_url, keypair=keypair)
            logger.info(f"Wallet loaded: {str(self.trader.pubkey)}")
        else:
            self.trader = None

        self.exit_engine = ExitEngine(self.trader, self.settings, helius_ws_urls=self._helius_ws_urls)
        await self.exit_engine.start()

        self._running = True
        bot_state.running = True
        bot_state.dry_run = dry_run
        bot_state.started_at = time.time()

        logger.info(f"Engine started | dry_run={dry_run}")
        asyncio.create_task(self._helius_obs_loop())
        await self._connect_loop()

    async def stop(self):
        self._running = False
        bot_state.running = False
        if self.exit_engine:
            await self.exit_engine.stop()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self.trader:
            await self.trader.close()
        logger.info("Engine stopped")

    async def _connect_loop(self):
        """Main PumpPortal loop — subscribes to new token creates."""
        while self._running:
            try:
                async with websockets.connect(PUMPPORTAL_WS, ping_interval=20, open_timeout=10) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    logger.info("Subscribed to PumpPortal new token stream")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if event.get("txType") == "create":
                            asyncio.create_task(self._handle_new_token(event))

            except websockets.exceptions.ConnectionClosed as e:
                self._ws = None
                logger.warning(f"PumpPortal WS closed: {e} — reconnecting in 3s")
                await asyncio.sleep(3)
            except Exception as e:
                self._ws = None
                logger.error(f"PumpPortal WS error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    # ── Persistent Helius observation WS ─────────────────────────────────────

    async def _helius_obs_loop(self):
        """
        Maintains one persistent Helius logsSubscribe WS for observation windows.
        Uses exponential backoff on 429/errors so we don't trigger rate limits.
        Re-subscribes all active mints after reconnect (same pattern as token_analyzer).
        """
        delay = 5.0
        while self._running:
            try:
                ws_url = self._helius_ws_urls[self._obs_url_idx % len(self._helius_ws_urls)]
                self._obs_url_idx += 1
                async with websockets.connect(
                    ws_url, ping_interval=20, open_timeout=15
                ) as ws:
                    self._helius_obs_ws = ws
                    self._obs_sub_ids.clear()
                    self._obs_pending_subs.clear()
                    delay = 5.0  # reset backoff on successful connect
                    # Re-subscribe any in-flight observation mints
                    for mint in list(self._pending_observations.keys()):
                        try:
                            from solders.pubkey import Pubkey as _Pubkey
                            bc = str(get_bonding_curve_pda(_Pubkey.from_string(mint)))
                            await self._obs_subscribe(mint, bc)
                        except Exception:
                            pass
                    key_slot = ((self._obs_url_idx - 1) % len(self._helius_ws_urls)) + 1
                    logger.info(f"Helius observation WS connected (key {key_slot}/{len(self._helius_ws_urls)})")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        # Subscription confirmation
                        if "result" in msg and isinstance(msg.get("result"), int):
                            pending_mint = self._obs_pending_subs.pop(msg.get("id"), None)
                            if pending_mint:
                                self._obs_sub_ids[pending_mint] = msg["result"]
                            continue

                        if msg.get("method") != "logsNotification":
                            continue

                        val = msg["params"]["result"]["value"]
                        if val.get("err"):
                            continue

                        trade = parse_trade_event(val.get("logs", []))
                        if trade and trade["mint"] in self._pending_observations:
                            self._pending_observations[trade["mint"]].append(trade)

            except asyncio.CancelledError:
                return
            except Exception as e:
                self._helius_obs_ws = None
                if not self._running:
                    return
                err_str = str(e)
                if "429" in err_str:
                    # Rate limited — back off significantly, don't hammer the API
                    delay = min(delay * 2, 120.0)
                    logger.warning(f"Helius obs WS rate limited (429) — backing off {delay:.0f}s")
                else:
                    logger.warning(f"Helius obs WS error: {e} — retrying in {delay:.0f}s")
                await asyncio.sleep(delay)

    async def _obs_subscribe(self, mint: str, bonding_curve: str):
        ws = self._helius_obs_ws
        if not ws:
            return
        req_id = self._obs_req_counter
        self._obs_req_counter += 1
        self._obs_pending_subs[req_id] = mint
        try:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "method": "logsSubscribe",
                "params": [{"mentions": [bonding_curve]}, {"commitment": "processed"}]
            }))
        except Exception as e:
            self._obs_pending_subs.pop(req_id, None)
            logger.warning(f"obs subscribe failed for {mint[:8]}: {e}")

    async def _obs_unsubscribe(self, mint: str):
        sub_id = self._obs_sub_ids.pop(mint, None)
        ws = self._helius_obs_ws
        if sub_id is not None and ws:
            try:
                req_id = self._obs_req_counter
                self._obs_req_counter += 1
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": req_id,
                    "method": "logsUnsubscribe",
                    "params": [sub_id]
                }))
            except Exception:
                pass

    async def _handle_new_token(self, event: dict):
        bot_state.tokens_seen += 1
        mint   = event.get("mint", "unknown")
        name   = event.get("name", "?")
        symbol = event.get("symbol", "?")

        obs_s     = self.settings["filters"]["observation_window_seconds"]
        obs_start = time.time()

        # Register slot before subscribing so no trades are dropped
        self._pending_observations[mint] = []
        try:
            from solders.pubkey import Pubkey as _Pubkey
            _bc_pda = str(get_bonding_curve_pda(_Pubkey.from_string(mint)))
        except Exception:
            _bc_pda = ""
        await self._obs_subscribe(mint, _bc_pda)

        price = await self.sol_price_svc.get_price()
        if price:
            bot_state.sol_price_usd = price

        passed, reason = await self.filter_engine.check_create_event(event)

        if not passed:
            self._pending_observations.pop(mint, None)
            await self._obs_unsubscribe(mint)
            bot_state.log_rejection(reason)
            bot_state.log_trade(TradeLog(
                timestamp=time.time(), mint=mint, name=name, symbol=symbol,
                action="filter_fail", sol_amount=0, usd_amount=0,
                reason=reason, dry_run=bot_state.dry_run
            ))
            logger.debug(f"REJECTED {symbol}: {reason}")
            return

        elapsed   = time.time() - obs_start
        remaining = max(0.0, obs_s - elapsed)
        logger.info(f"PASSED: {symbol} ({mint[:8]}...) observing up to {remaining:.1f}s")

        # Poll every 0.5s — buy immediately on early signal, don't wait for full window
        poll_deadline   = obs_start + obs_s
        early_triggered = False
        trigger_reason  = ""
        while time.time() < poll_deadline:
            await asyncio.sleep(0.5)
            current_trades = list(self._pending_observations.get(mint, []))
            triggered, t_reason = self.filter_engine.check_early_trigger(current_trades)
            if triggered:
                early_triggered = True
                trigger_reason  = t_reason
                elapsed_s = time.time() - obs_start
                logger.info(f"EARLY SIGNAL: {symbol} at {elapsed_s:.1f}s — {t_reason}")
                break

        trades = self._pending_observations.pop(mint, [])
        await self._obs_unsubscribe(mint)

        if not trades:
            logger.warning(f"OBS ZERO TRADES: {symbol} ({mint[:8]}...) — "
                           f"helius_obs_ws={'connected' if self._helius_obs_ws else 'DISCONNECTED'}")

        if early_triggered:
            bot_state.tokens_passed += 1
            await self._execute_buy(event, f"early signal: {trigger_reason}")
            return

        # Full window expired — fall back to end-of-window volume check
        vol_passed, vol_reason = self.filter_engine.check_trade_window(trades)

        if not vol_passed:
            bot_state.tokens_rejected += 1
            bot_state.log_rejection(vol_reason)
            bot_state.log_trade(TradeLog(
                timestamp=time.time(), mint=mint, name=name, symbol=symbol,
                action="filter_fail", sol_amount=0, usd_amount=0,
                reason=vol_reason, dry_run=bot_state.dry_run
            ))
            logger.info(f"REJECTED (volume): {symbol}: {vol_reason}")
            return

        bot_state.tokens_passed += 1
        await self._execute_buy(event, f"window close: {vol_reason}")

    async def _execute_buy(self, event: dict, reason: str):
        trading  = self.settings["trading"]
        dry_run  = trading["dry_run"]
        buy_sol  = trading["buy_amount_sol"]
        slip_bps = trading["slippage_bps"]
        mint     = event.get("mint", "")
        name     = event.get("name", "?")
        symbol   = event.get("symbol", "?")
        mcap_sol = event.get("marketCapSol", 0)
        sol_usd  = bot_state.sol_price_usd or 0
        buy_usd  = buy_sol * sol_usd

        if len(bot_state.open_positions) >= trading["max_concurrent_positions"]:
            logger.warning(f"Max positions reached — skipping {symbol}")
            return

        if bot_state.daily_loss_sol >= trading["daily_loss_limit_sol"]:
            logger.warning("Daily loss limit — circuit breaker active")
            return

        entry_price = (mcap_sol * sol_usd) / 1_000_000_000 if mcap_sol and sol_usd else 0
        tokens_raw  = int((buy_sol * sol_usd / entry_price) * 10**TOKEN_DECIMALS) if entry_price > 0 and sol_usd else 0
        tx_sig      = "dry_run"

        if not dry_run:
            success, tx_sig = await self.trader.execute_buy(
                mint_str=mint, sol_amount=buy_sol, slippage_bps=slip_bps
            )
            if not success:
                logger.error(f"BUY failed: {tx_sig}")
                return
            actual = await self.trader.get_token_balance(mint)
            if actual > 0:
                tokens_raw = actual
            logger.info(f"BUY OK: {tx_sig[:20]}...")
        else:
            logger.info(f"[DRY] BUY {symbol}: {buy_sol} SOL (${buy_usd:.2f})")

        pos = Position(
            mint=mint, name=name, symbol=symbol,
            entry_price_usd=entry_price,
            entry_sol=buy_sol,
            tokens_held=tokens_raw / 10**TOKEN_DECIMALS,
            current_price_usd=entry_price,
        )
        bot_state.open_position(pos)
        bot_state.log_trade(TradeLog(
            timestamp=time.time(), mint=mint, name=name, symbol=symbol,
            action="buy", sol_amount=buy_sol, usd_amount=buy_usd,
            reason=reason, dry_run=dry_run,
        ))

        if self.exit_engine and tokens_raw > 0:
            await self.exit_engine.register_position(pos, tokens_raw)

    def reload_settings(self):
        self._reload_config()
        logger.info("Settings hot-reloaded")

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error


engine = SniperEngine()
