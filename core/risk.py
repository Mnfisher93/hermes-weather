"""Risk management module — enforces the 7 mandatory controls."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeRecord:
    """One tracked open position."""
    market_id: str
    city: str
    side: str
    signal_type: str
    entry_price: float
    size_usd: float
    timestamp: float = field(default_factory=time.time)
    current_price: Optional[float] = None


class RiskManager:
    """
    Enforces risk limits before every trade.

    Controls:
      1. Hard stop-loss: 20% drawdown halts all new orders
      2. Max single position: min(kelly_size, MAX_BET, 5% bankroll)
      3. Max total exposure: MAX_EXPOSURE across all open positions
      4. Slippage filter: skip markets where spread > max_slippage
      5. Liquidity filter: skip markets with volume < min_volume
      6. Kill switch: halt() cancels all open orders
      7. Paper-trade mode: handled by executor.py sim_mode flag
    """

    def __init__(self, config: dict):
        self.max_drawdown_pct: float = config.get("max_drawdown_pct", 0.20)
        self.max_bet_usd: float = config.get("max_bet_usd", 50.0)
        self.max_exposure_usd: float = config.get("max_exposure_usd", 200.0)
        self.max_slippage: float = config.get("max_slippage", 0.03)
        self.min_volume: float = config.get("min_market_volume", 5000.0)
        self.bankroll: float = config.get("max_exposure_usd", 200.0)
        self.starting_bankroll: float = self.bankroll

        self.current_exposure: float = 0.0
        self.open_positions: list[TradeRecord] = []
        self.halted: bool = False
        self._log: list[str] = []

    # ── Pre-trade checks ────────────────────────────────────────

    def check_trade(self, signal: dict) -> tuple[bool, str]:
        """
        Run all risk checks on a proposed trade signal.
        Returns (allowed, reason_if_blocked).
        """
        if self.halted:
            return False, "HALTED — kill switch active"

        # 1. Drawdown check
        drawdown = 1 - (self.bankroll / self.starting_bankroll)
        if drawdown >= self.max_drawdown_pct:
            self.halted = True
            return False, f"HALTED — drawdown {drawdown:.1%} exceeds {self.max_drawdown_pct:.0%}"

        # 2. Single position cap (already enforced in kelly, but double-check)
        size = signal.get("position_usd", 0)
        cap = min(self.max_bet_usd, self.bankroll * 0.05)
        if size > cap + 0.01:  # 1¢ tolerance for rounding
            return False, f"Position ${size:.2f} exceeds cap ${cap:.2f}"

        # 3. Total exposure
        if self.current_exposure + size > self.max_exposure_usd:
            remaining = self.max_exposure_usd - self.current_exposure
            return False, (
                f"Exposure would reach ${self.current_exposure + size:.2f} "
                f"(max ${self.max_exposure_usd:.2f}, remaining ${remaining:.2f})"
            )

        # 4. Book-health check: yes_price + no_price should sum near 1.0.
        # A large deviation means the book is stale, crossed, or imbalanced
        # and any fill will incur significant slippage.
        yes_p = signal.get("yes_price", 0) or 0
        no_p  = signal.get("no_price", 0) or 0
        if yes_p > 0 and no_p > 0:
            book_skew = abs(yes_p + no_p - 1.0)
            if book_skew > self.max_slippage:
                return False, (
                    f"Book skew {book_skew:.3f} exceeds max {self.max_slippage:.3f}"
                )

        # 5. Liquidity check
        volume = signal.get("volume", 0)
        if volume < self.min_volume:
            return False, f"Volume ${volume:,.0f} below min ${self.min_volume:,.0f}"

        # 6. Duplicate position — don't double up on the same market
        cid = signal.get("condition_id")
        if cid and any(p.market_id == cid for p in self.open_positions):
            return False, f"Already long {signal.get('city')} on {cid[:10]}…"

        return True, "OK"

    # ── Position tracking ───────────────────────────────────────

    def record_trade(self, signal: dict) -> TradeRecord:
        """Track a new position after executor confirms."""
        rec = TradeRecord(
            market_id=signal.get("condition_id", "unknown"),
            city=signal.get("city", ""),
            side=signal.get("side", ""),
            signal_type=signal.get("type", ""),
            entry_price=signal.get("yes_price", 0),
            size_usd=signal.get("position_usd", 0),
        )
        self.open_positions.append(rec)
        self.current_exposure += rec.size_usd
        self.bankroll -= rec.size_usd
        self._log.append(
            f"[RISK] Opened {rec.side} ${rec.size_usd:.2f} on {rec.city} | "
            f"exposure=${self.current_exposure:.2f} bankroll=${self.bankroll:.2f}"
        )
        return rec

    def close_position(self, market_id: str, pnl: float):
        """Close a position and update bankroll."""
        for i, pos in enumerate(self.open_positions):
            if pos.market_id == market_id:
                self.current_exposure -= pos.size_usd
                self.bankroll += pos.size_usd + pnl
                self._log.append(
                    f"[RISK] Closed {pos.city} PnL=${pnl:+.2f} | "
                    f"exposure=${self.current_exposure:.2f} bankroll=${self.bankroll:.2f}"
                )
                self.open_positions.pop(i)
                return
        self._log.append(f"[RISK] WARN: market_id={market_id} not found in open positions")

    # ── Kill switch ─────────────────────────────────────────────

    def halt(self, client=None) -> str:
        """Emergency stop — cancel all open orders if client provided."""
        self.halted = True
        msg = "[RISK] ⛔ KILL SWITCH ACTIVATED — halting all trading"
        self._log.append(msg)
        if client:
            try:
                client.cancel_all()
                self._log.append("[RISK] All open orders cancelled")
            except Exception as e:
                self._log.append(f"[RISK] Failed to cancel orders: {e}")
        return msg

    def resume(self):
        """Resume after manual review."""
        self.halted = False
        self._log.append("[RISK] Trading resumed")

    # ── Status ──────────────────────────────────────────────────

    def status(self) -> dict:
        drawdown = 1 - (self.bankroll / self.starting_bankroll) if self.starting_bankroll else 0
        return {
            "halted": self.halted,
            "bankroll": round(self.bankroll, 2),
            "starting_bankroll": round(self.starting_bankroll, 2),
            "drawdown_pct": round(drawdown, 4),
            "current_exposure": round(self.current_exposure, 2),
            "max_exposure": self.max_exposure_usd,
            "open_positions": len(self.open_positions),
        }

    def drain_log(self) -> list[str]:
        """Return and clear pending log messages."""
        msgs = self._log[:]
        self._log.clear()
        return msgs
