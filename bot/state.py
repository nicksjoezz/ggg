import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

@dataclass
class Position:
    mint: str
    name: str
    symbol: str
    entry_price_usd: float
    entry_sol: float
    tokens_held: float
    current_price_usd: float
    opened_at: float = field(default_factory=time.time)
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False

@dataclass
class TradeLog:
    timestamp: float
    mint: str
    name: str
    symbol: str
    action: str
    sol_amount: float
    usd_amount: float
    reason: str
    dry_run: bool
    pnl_usd: float = 0.0

class BotState:
    def __init__(self):
        self.running = False
        self.dry_run = True
        self.started_at: Optional[float] = None
        self.tokens_seen = 0
        self.tokens_passed = 0
        self.tokens_rejected = 0
        self.sol_price_usd: Optional[float] = None
        self.open_positions: Dict[str, Position] = {}
        self.trade_log: List[TradeLog] = []
        self.rejection_reasons: Dict[str, int] = {}
        self.daily_pnl_sol = 0.0
        self.total_pnl_usd = 0.0
        self.daily_loss_sol = 0.0

    @property
    def total_buys(self) -> int:
        return len([t for t in self.trade_log if t.action == "buy"])

    @property
    def total_sells(self) -> int:
        return len([t for t in self.trade_log if t.action == "sell"])

    def log_trade(self, log: TradeLog):
        self.trade_log.insert(0, log)
        if len(self.trade_log) > 500:
            self.trade_log.pop()
        
        if log.action == "sell":
            self.total_pnl_usd += log.pnl_usd
            # Simplified daily SOL P&L tracking
            if not log.dry_run:
                # This would ideally be calculated from actual SOL delta
                pass

    def log_rejection(self, reason: str):
        self.tokens_rejected += 1
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1

    def open_position(self, pos: Position):
        self.open_positions[pos.mint] = pos

    def close_position(self, mint: str):
        self.open_positions.pop(mint, None)
    
    def positions_list(self) -> List[dict]:
        return [asdict(p) for p in self.open_positions.values()]

    def trade_log_list(self, limit: int = 50) -> List[dict]:
        return [asdict(t) for t in self.trade_log[:limit]]

    def summary(self) -> dict:
        uptime_s = time.time() - self.started_at if self.started_at else 0
        uptime_str = time.strftime("%H:%M:%S", time.gmtime(uptime_s))
        
        wins = len([t for t in self.trade_log if t.action == "sell" and t.pnl_usd > 0])
        total_sells = self.total_sells
        win_rate = (wins / total_sells * 100) if total_sells > 0 else 0
        
        pass_rate = (self.tokens_passed / self.tokens_seen * 100) if self.tokens_seen > 0 else 0
        
        return {
            "running": self.running,
            "dry_run": self.dry_run,
            "uptime": uptime_str,
            "tokens_seen": self.tokens_seen,
            "tokens_passed": self.tokens_passed,
            "tokens_rejected": self.tokens_rejected,
            "pass_rate_pct": round(pass_rate, 1),
            "open_positions": len(self.open_positions),
            "total_buys": self.total_buys,
            "total_sells": total_sells,
            "win_rate_pct": round(win_rate, 1),
            "total_pnl_usd": round(self.total_pnl_usd, 2),
            "daily_pnl_sol": round(self.daily_pnl_sol, 4),
            "daily_loss_sol": round(self.daily_loss_sol, 4),
            "rejection_reasons": self.rejection_reasons,
            "ws_connected": self.running, # Simplified
        }

bot_state = BotState()
