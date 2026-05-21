"""
exit_engine.py — Monitors open positions and executes take-profit / stop-loss sells.

Uses Helius logsSubscribe (free) on each token's bonding curve PDA.
pump.fun emits a TradeEvent on every buy/sell — we parse it to get the
current price and trigger exits.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import websockets
from solders.pubkey import Pubkey

from bot.state import bot_state, TradeLog, Position
from bot.helius_trader import get_bonding_curve_pda
from bot.pump_events import parse_trade_event

logger = logging.getLogger("exit_engine")


@dataclass
class PositionMonitor:
    """Tracks exit state for a single position."""
    mint: str
    symbol: str
    bonding_curve: str
    entry_price_usd: float
    tokens_held: int            # raw token units (6 decimals)
    sol_invested: float
    entry_time: float = field(default_factory=time.time)
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    last_price_usd: float = 0.0
    peak_price_usd: float = 0.0
    last_trade_time: float = field(default_factory=time.time)


class ExitEngine:
    """
    Subscribes to Helius logsSubscribe for each open position's bonding curve.
    Parses pump.fun TradeEvents to track price and trigger exits.
    """

    def __init__(self, trader, settings: dict, helius_ws_url: str = ""):
        self.trader = trader
        self.settings = settings
        self._ws_url = helius_ws_url
        self._monitors: dict[str, PositionMonitor] = {}
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        # subscription ID tracking for clean unsubscribe
        self._sub_ids: dict[str, int] = {}      # mint → logsSubscribe ID
        self._pending_subs: dict[int, str] = {} # req_id → mint
        self._req_counter = 100

    def update_settings(self, settings: dict):
        self.settings = settings

    def _tp_settings(self):
        return self.settings.get("take_profit", {})

    def _sl_settings(self):
        return self.settings.get("stop_loss", {})

    async def start(self):
        self._running = True
        asyncio.create_task(self._ws_loop())
        asyncio.create_task(self._time_stop_loop())
        logger.info("Exit engine started")

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    async def register_position(self, position: Position, tokens_raw: int):
        """Called by engine after a successful buy."""
        try:
            bc_pda = str(get_bonding_curve_pda(Pubkey.from_string(position.mint)))
        except Exception:
            bc_pda = ""

        monitor = PositionMonitor(
            mint=position.mint,
            symbol=position.symbol,
            bonding_curve=bc_pda,
            entry_price_usd=position.entry_price_usd,
            tokens_held=tokens_raw,
            sol_invested=position.entry_sol,
            last_price_usd=position.entry_price_usd,
            peak_price_usd=position.entry_price_usd,
        )
        self._monitors[position.mint] = monitor
        await self._subscribe_token(position.mint)
        logger.info(f"Monitoring position: {position.symbol} | entry=${position.entry_price_usd:.8f}")

    async def unregister_position(self, mint: str):
        self._monitors.pop(mint, None)
        await self._unsubscribe_token(mint)

    # ── WebSocket loop ────────────────────────────────────────────────────────

    async def _ws_loop(self):
        while self._running:
            try:
                async with websockets.connect(self._ws_url, ping_interval=20) as ws:
                    self._ws = ws
                    self._sub_ids.clear()
                    self._pending_subs.clear()
                    logger.info("Exit engine connected to Helius WS")

                    # Re-subscribe all active monitors on reconnect
                    for mint in list(self._monitors.keys()):
                        await self._do_subscribe(ws, mint)

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        # Capture subscription IDs from confirmation messages
                        if "result" in msg and isinstance(msg.get("result"), int):
                            pending_mint = self._pending_subs.pop(msg.get("id"), None)
                            if pending_mint:
                                self._sub_ids[pending_mint] = msg["result"]
                            continue

                        if msg.get("method") != "logsNotification":
                            continue

                        logs = msg["params"]["result"]["value"]["logs"]
                        trade = parse_trade_event(logs)
                        if trade and trade["mint"] in self._monitors:
                            await self._handle_price_update(trade["mint"], trade)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("Exit engine WS closed — reconnecting in 3s")
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Exit engine WS error: {e} — reconnecting in 5s")
                await asyncio.sleep(5)

    async def _do_subscribe(self, ws, mint: str):
        monitor = self._monitors.get(mint)
        if not monitor or not monitor.bonding_curve:
            return
        req_id = self._req_counter
        self._req_counter += 1
        self._pending_subs[req_id] = mint
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": req_id,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [monitor.bonding_curve]},
                {"commitment": "processed"}
            ]
        }))

    async def _subscribe_token(self, mint: str):
        if self._ws:
            try:
                await self._do_subscribe(self._ws, mint)
            except Exception as e:
                logger.warning(f"Subscribe failed for {mint[:8]}: {e}")

    async def _unsubscribe_token(self, mint: str):
        sub_id = self._sub_ids.pop(mint, None)
        if sub_id is not None and self._ws:
            try:
                req_id = self._req_counter
                self._req_counter += 1
                await self._ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": req_id,
                    "method": "logsUnsubscribe",
                    "params": [sub_id]
                }))
            except Exception:
                pass

    # ── Price update handler ──────────────────────────────────────────────────

    async def _handle_price_update(self, mint: str, trade: dict):
        monitor = self._monitors.get(mint)
        if not monitor:
            return

        mcap_sol = trade.get("marketCapSol", 0)
        sol_usd  = bot_state.sol_price_usd or 0
        if not sol_usd or not mcap_sol:
            return

        current_price = (mcap_sol * sol_usd) / 1_000_000_000
        monitor.last_price_usd = current_price
        monitor.peak_price_usd = max(monitor.peak_price_usd, current_price)
        monitor.last_trade_time = time.time()

        pos = bot_state.open_positions.get(mint)
        if pos and monitor.entry_price_usd > 0:
            pct_change = ((current_price / monitor.entry_price_usd) - 1) * 100
            pos.current_price_usd = current_price
            pos.pnl_pct = pct_change
            pos.pnl_usd = (current_price - monitor.entry_price_usd) * (monitor.tokens_held / 10**6)

        await self._evaluate_exits(mint, monitor, current_price)

    async def _evaluate_exits(self, mint: str, monitor: PositionMonitor, current_price: float):
        if monitor.entry_price_usd <= 0:
            return

        tp = self._tp_settings()
        sl = self._sl_settings()
        pct_gain = ((current_price / monitor.entry_price_usd) - 1) * 100

        # Stop loss — always checked first
        hard_stop = sl.get("hard_stop_pct", 50)
        if pct_gain <= -hard_stop:
            logger.info(f"STOP LOSS hit for {monitor.symbol}: {pct_gain:.1f}%")
            await self._execute_sell(mint, monitor, pct=100, reason=f"stop loss ({pct_gain:.1f}%)")
            return

        # TP levels checked lowest → highest so TP1 always fires before TP2/TP3.
        # Only one level fires per price tick; the next fires on the next incoming trade.
        tp1_pct  = tp.get("tp1_pct",  50)
        tp1_sell = tp.get("tp1_sell_pct", 30)
        if not monitor.tp1_hit and pct_gain >= tp1_pct:
            monitor.tp1_hit = True
            logger.info(f"TP1 hit for {monitor.symbol}: +{pct_gain:.1f}%")
            await self._execute_sell(mint, monitor, pct=tp1_sell, reason=f"TP1 +{pct_gain:.0f}%")
            return

        tp2_pct  = tp.get("tp2_pct",  100)
        tp2_sell = tp.get("tp2_sell_pct", 40)
        if not monitor.tp2_hit and pct_gain >= tp2_pct:
            monitor.tp2_hit = True
            logger.info(f"TP2 hit for {monitor.symbol}: +{pct_gain:.1f}%")
            await self._execute_sell(mint, monitor, pct=tp2_sell, reason=f"TP2 +{pct_gain:.0f}%")
            return

        tp3_pct  = tp.get("tp3_pct",  400)
        tp3_sell = tp.get("tp3_sell_pct", 20)
        if not monitor.tp3_hit and pct_gain >= tp3_pct:
            monitor.tp3_hit = True
            logger.info(f"TP3 hit for {monitor.symbol}: +{pct_gain:.1f}%")
            await self._execute_sell(mint, monitor, pct=tp3_sell, reason=f"TP3 +{pct_gain:.0f}%")
            return

    # ── Time / volume stop loop ───────────────────────────────────────────────

    async def _time_stop_loop(self):
        while self._running:
            await asyncio.sleep(10)
            now = time.time()
            sl = self._sl_settings()
            time_stop_s = sl.get("time_stop_seconds", 300)
            vol_stop_s  = sl.get("volume_stop_zero_seconds", 60)

            for mint, monitor in list(self._monitors.items()):
                age_s  = now - monitor.entry_time
                idle_s = now - monitor.last_trade_time

                if idle_s >= vol_stop_s:
                    logger.info(f"VOLUME STOP for {monitor.symbol}: dead for {idle_s:.0f}s")
                    await self._execute_sell(mint, monitor, pct=100, reason=f"volume stop (dead {idle_s:.0f}s)")
                    continue

                if age_s >= time_stop_s:
                    pct_gain = ((monitor.last_price_usd / monitor.entry_price_usd) - 1) * 100 if monitor.entry_price_usd else 0
                    if pct_gain < 20:
                        logger.info(f"TIME STOP for {monitor.symbol}: {age_s:.0f}s old, {pct_gain:.1f}%")
                        await self._execute_sell(mint, monitor, pct=100, reason=f"time stop ({age_s:.0f}s)")

    # ── Sell execution ────────────────────────────────────────────────────────

    async def _execute_sell(self, mint: str, monitor: PositionMonitor, pct: int, reason: str):
        tokens_to_sell = int(monitor.tokens_held * pct / 100)
        if tokens_to_sell <= 0:
            return

        dry_run      = self.settings.get("trading", {}).get("dry_run", True)
        sol_usd      = bot_state.sol_price_usd or 0
        current_price = monitor.last_price_usd
        usd_value    = (tokens_to_sell / 10**6) * current_price

        if dry_run:
            logger.info(f"[DRY RUN] SELL {pct}% of {monitor.symbol}: ~${usd_value:.2f} | {reason}")
            success, sig = True, "dry_run"
        else:
            slippage = self.settings.get("trading", {}).get("slippage_bps", 1000)
            success, sig = await self.trader.execute_sell(
                mint_str=mint,
                token_amount=tokens_to_sell,
                slippage_bps=slippage,
            )

        if success:
            monitor.tokens_held -= tokens_to_sell
            pnl_usd = (current_price - monitor.entry_price_usd) * (tokens_to_sell / 10**6)

            # Sync live position metrics so the dashboard reflects the reduced size
            pos = bot_state.open_positions.get(mint)
            if pos:
                pos.tokens_held      = monitor.tokens_held / 10**6
                pos.realized_pnl_usd += pnl_usd
                pos.tp1_hit          = monitor.tp1_hit
                pos.tp2_hit          = monitor.tp2_hit
                pos.tp3_hit          = monitor.tp3_hit

            bot_state.log_trade(TradeLog(
                timestamp=time.time(),
                mint=mint,
                name=monitor.symbol,
                symbol=monitor.symbol,
                action="sell",
                sol_amount=usd_value / sol_usd if sol_usd else 0,
                usd_amount=usd_value,
                reason=reason,
                pnl_usd=pnl_usd,
                dry_run=dry_run,
            ))

            if monitor.tokens_held <= 0 or pct == 100:
                bot_state.close_position(mint)
                await self.unregister_position(mint)
                logger.info(f"Position closed: {monitor.symbol} | reason: {reason}")
        else:
            logger.error(f"Sell FAILED for {monitor.symbol}: {sig}")
