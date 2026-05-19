"""
仓位状态管理器 v2 — 按策略分仓聚合

核心改进：
  - 交易所级仓位 (exchange_positions): 追踪真实净仓
  - 策略分配 (strategy_allocations): 每个策略占仓位的比例
  - 多策略同 symbol 时共用交易所仓位，各自追踪份额
  - 对账按交易所净仓做，不再按策略混

状态机 (per strategy):
  FLAT → LONG/SHORT → FLAT

操作语义：
  buy/sell by strategy_A:
    → 查交易所仓位
    → 同方向: 增加策略A份额, 增加交易所总仓
    → 反方向: 只有A有仓时反手, A与其他策略共享时不反手
  close by strategy_A:
    → 减少A的份额, 减少交易所总仓
    → 如果A份额归零, A变FLAT
    → 如果交易所总仓归零, 交易所仓位关闭
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sisie_bridge.core.models.order import OrderSide, PositionState

log = logging.getLogger(__name__)


class PositionStateManager:
    """仓位状态管理器 v2 — 交易所仓位 + 策略分配"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS signal_log (
        signal_id TEXT PRIMARY KEY,
        processed_at TEXT NOT NULL,
        result TEXT NOT NULL
    );

    -- 交易所级净仓位 (exchange, symbol) → side, total_quantity
    CREATE TABLE IF NOT EXISTS exchange_positions (
        exchange TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT,
        total_quantity REAL DEFAULT 0,
        entry_price REAL DEFAULT 0,
        entry_time TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (exchange, symbol)
    );

    -- 策略级分配：每个策略占交易所仓位的份额
    CREATE TABLE IF NOT EXISTS strategy_allocations (
        strategy_id TEXT NOT NULL,
        exchange TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity REAL DEFAULT 0,
        entry_price REAL DEFAULT 0,
        entry_time TEXT,
        stop_loss REAL,
        take_profit REAL,
        tp1_hit INTEGER DEFAULT 0,
        realized_pnl REAL DEFAULT 0,
        active_orders TEXT DEFAULT '[]',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (strategy_id, exchange, symbol)
    );

    CREATE TABLE IF NOT EXISTS trade_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL,
        exchange TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        action TEXT NOT NULL,
        quantity REAL NOT NULL,
        price REAL NOT NULL,
        fee REAL DEFAULT 0,
        realized_pnl REAL DEFAULT 0,
        signal_id TEXT,
        exchange_order_id TEXT,
        created_at TEXT NOT NULL
    );
    """

    def __init__(self, db_path: str = "data/live_bridge_state.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    # ═════════════════════════════════════════════════════════════
    # 信号幂等
    # ═════════════════════════════════════════════════════════════

    def try_claim_signal(self, signal_id: str) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO signal_log (signal_id, processed_at, result) VALUES (?, ?, ?)",
            (signal_id, datetime.now(timezone.utc).isoformat(), "claimed"),
        )
        claimed = cursor.rowcount > 0
        self._conn.commit()
        return claimed

    def is_duplicate(self, signal_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM signal_log WHERE signal_id = ?", (signal_id,)
        ).fetchone()
        return row is not None

    def mark_processed(self, signal_id: str, result: str):
        self._conn.execute(
            "UPDATE signal_log SET result=? WHERE signal_id=?",
            (result, signal_id),
        )
        self._conn.commit()

    # ═════════════════════════════════════════════════════════════
    # 交易所仓位 CRUD
    # ═════════════════════════════════════════════════════════════

    def get_exchange_position(self, exchange: str, symbol: str) -> Optional[dict]:
        """获取交易所净仓位"""
        row = self._conn.execute(
            "SELECT * FROM exchange_positions WHERE exchange=? AND symbol=? AND total_quantity > 0",
            (exchange, symbol),
        ).fetchone()
        if not row:
            return None
        cols = [d[1] for d in self._conn.execute("PRAGMA table_info(exchange_positions)").fetchall()]
        return dict(zip(cols, row))

    def _upsert_exchange_position(
        self, exchange: str, symbol: str, side: str,
        total_quantity: float, entry_price: float, entry_time: str,
    ):
        self._conn.execute(
            """INSERT OR REPLACE INTO exchange_positions
               (exchange, symbol, side, total_quantity, entry_price, entry_time, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (exchange, symbol, side, total_quantity, entry_price, entry_time,
             datetime.now(timezone.utc).isoformat()),
        )
        # commit 由调用方控制（不在这里 commit，保证外层事务原子性）

    def _clear_exchange_position(self, exchange: str, symbol: str):
        self._conn.execute(
            "UPDATE exchange_positions SET total_quantity=0, updated_at=? WHERE exchange=? AND symbol=?",
            (datetime.now(timezone.utc).isoformat(), exchange, symbol),
        )

    # ═════════════════════════════════════════════════════════════
    # 策略分配 CRUD
    # ═════════════════════════════════════════════════════════════

    def get_strategy_position(
        self, strategy_id: str, exchange: str, symbol: str,
    ) -> Optional[PositionState]:
        """获取策略的持仓分配"""
        row = self._conn.execute(
            "SELECT * FROM strategy_allocations WHERE strategy_id=? AND exchange=? AND symbol=? AND quantity > 0",
            (strategy_id, exchange, symbol),
        ).fetchone()
        if not row:
            return None
        cols = [d[1] for d in self._conn.execute("PRAGMA table_info(strategy_allocations)").fetchall()]
        d = dict(zip(cols, row))
        return PositionState(
            strategy_id=d["strategy_id"],
            exchange=d["exchange"],
            symbol=d["symbol"],
            side=OrderSide(d["side"]),
            quantity=d["quantity"],
            entry_price=d["entry_price"],
            entry_time=datetime.fromisoformat(d["entry_time"]) if d["entry_time"] else datetime.now(timezone.utc),
            stop_loss=d["stop_loss"],
            take_profit=d["take_profit"],
            tp1_hit=bool(d["tp1_hit"]),
            realized_pnl=d["realized_pnl"],
            active_orders=json.loads(d["active_orders"] or "[]"),
        )

    def get_all_strategy_positions(self) -> List[PositionState]:
        """获取所有策略的活跃持仓"""
        rows = self._conn.execute(
            "SELECT * FROM strategy_allocations WHERE quantity > 0"
        ).fetchall()
        cols = [d[1] for d in self._conn.execute("PRAGMA table_info(strategy_allocations)").fetchall()]
        results = []
        for row in rows:
            d = dict(zip(cols, row))
            results.append(PositionState(
                strategy_id=d["strategy_id"],
                exchange=d["exchange"],
                symbol=d["symbol"],
                side=OrderSide(d["side"]),
                quantity=d["quantity"],
                entry_price=d["entry_price"],
                entry_time=datetime.fromisoformat(d["entry_time"]) if d["entry_time"] else datetime.now(timezone.utc),
                stop_loss=d["stop_loss"],
                take_profit=d["take_profit"],
                tp1_hit=bool(d["tp1_hit"]),
                realized_pnl=d["realized_pnl"],
                active_orders=json.loads(d["active_orders"] or "[]"),
            ))
        return results

    def open_strategy_position(self, pos: PositionState):
        """记录策略开仓 + 更新交易所仓位（事务保护）"""
        now = datetime.now(timezone.utc).isoformat()

        try:
            self._conn.execute("BEGIN IMMEDIATE")

            # 策略分配
            self._conn.execute(
                """INSERT OR REPLACE INTO strategy_allocations
                   (strategy_id, exchange, symbol, side, quantity, entry_price,
                    entry_time, stop_loss, take_profit, tp1_hit, realized_pnl,
                    active_orders, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pos.strategy_id, pos.exchange, pos.symbol,
                 pos.side.value, pos.quantity, pos.entry_price,
                 pos.entry_time.isoformat(),
                 pos.stop_loss, pos.take_profit,
                 int(pos.tp1_hit), pos.realized_pnl,
                 json.dumps(pos.active_orders), now),
            )

            # 交易所仓位聚合
            ex_pos = self.get_exchange_position(pos.exchange, pos.symbol)
            if ex_pos is None:
                self._upsert_exchange_position(
                    pos.exchange, pos.symbol, pos.side.value,
                    pos.quantity, pos.entry_price, pos.entry_time.isoformat(),
                )
            elif ex_pos["side"] == pos.side.value:
                new_qty = ex_pos["total_quantity"] + pos.quantity
                old_notional = ex_pos["total_quantity"] * ex_pos["entry_price"]
                new_notional = pos.quantity * pos.entry_price
                new_price = (old_notional + new_notional) / new_qty if new_qty > 0 else 0
                self._upsert_exchange_position(
                    pos.exchange, pos.symbol, pos.side.value,
                    new_qty, new_price, pos.entry_time.isoformat(),
                )

            self._conn.commit()
            log.info(f"[State] 策略开仓: {pos.strategy_id} {pos.side.value} {pos.quantity} "
                     f"{pos.symbol} @ {pos.entry_price}")
        except Exception:
            self._conn.rollback()
            raise

    def close_strategy_position(
        self, strategy_id: str, exchange: str, symbol: str, close_qty: float,
    ):
        """减少策略仓位 + 更新交易所仓位（事务保护）。"""
        alloc = self.get_strategy_position(strategy_id, exchange, symbol)
        if not alloc:
            return

        remaining = alloc.quantity - close_qty
        now = datetime.now(timezone.utc).isoformat()

        try:
            self._conn.execute("BEGIN IMMEDIATE")

            if remaining <= 1e-8:
                self._conn.execute(
                    "DELETE FROM strategy_allocations WHERE strategy_id=? AND exchange=? AND symbol=?",
                    (strategy_id, exchange, symbol),
                )
            else:
                self._conn.execute(
                    "UPDATE strategy_allocations SET quantity=?, updated_at=? WHERE strategy_id=? AND exchange=? AND symbol=?",
                    (remaining, now, strategy_id, exchange, symbol),
                )

            # 更新交易所仓位
            ex_pos = self.get_exchange_position(exchange, symbol)
            if ex_pos:
                ex_remaining = ex_pos["total_quantity"] - close_qty
                if ex_remaining <= 1e-8:
                    self._clear_exchange_position(exchange, symbol)
                else:
                    self._upsert_exchange_position(
                        exchange, symbol, ex_pos["side"],
                        ex_remaining, ex_pos["entry_price"], ex_pos["entry_time"],
                    )

            self._conn.commit()
            log.info(f"[State] 策略平仓: {strategy_id} {close_qty} {symbol}, 剩余: {remaining:.4f}")
        except Exception:
            self._conn.rollback()
            raise

    def get_other_strategies_on_symbol(
        self, strategy_id: str, exchange: str, symbol: str,
    ) -> List[PositionState]:
        """获取同一 symbol 上其他策略的持仓"""
        rows = self._conn.execute(
            "SELECT * FROM strategy_allocations WHERE exchange=? AND symbol=? AND strategy_id!=? AND quantity>0",
            (exchange, symbol, strategy_id),
        ).fetchall()
        cols = [d[1] for d in self._conn.execute("PRAGMA table_info(strategy_allocations)").fetchall()]
        results = []
        for row in rows:
            d = dict(zip(cols, row))
            results.append(PositionState(
                strategy_id=d["strategy_id"],
                exchange=d["exchange"],
                symbol=d["symbol"],
                side=OrderSide(d["side"]),
                quantity=d["quantity"],
                entry_price=d["entry_price"],
                entry_time=datetime.fromisoformat(d["entry_time"]) if d["entry_time"] else datetime.now(timezone.utc),
            ))
        return results

    def update_strategy_position(
        self, strategy_id: str, exchange: str, symbol: str, **kwargs,
    ):
        """更新策略仓位字段（止损止盈等）"""
        allowed = {"stop_loss", "take_profit", "tp1_hit", "realized_pnl", "active_orders"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return
        if "tp1_hit" in updates:
            updates["tp1_hit"] = int(updates["tp1_hit"])
        if "active_orders" in updates and isinstance(updates["active_orders"], list):
            updates["active_orders"] = json.dumps(updates["active_orders"])

        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [
            datetime.now(timezone.utc).isoformat(),
            strategy_id, exchange, symbol,
        ]
        self._conn.execute(
            f"UPDATE strategy_allocations SET {set_clause}, updated_at=? "
            f"WHERE strategy_id=? AND exchange=? AND symbol=?",
            values,
        )
        self._conn.commit()

    # ═════════════════════════════════════════════════════════════
    # 兼容旧接口
    # ═════════════════════════════════════════════════════════════

    def get_position(
        self, strategy_id: str, exchange: str, symbol: str,
    ) -> Optional[PositionState]:
        """兼容旧接口 → 返回策略仓位"""
        return self.get_strategy_position(strategy_id, exchange, symbol)

    def get_all_positions(self) -> List[PositionState]:
        """兼容旧接口 → 返回所有策略仓位"""
        return self.get_all_strategy_positions()

    def open_position(self, pos: PositionState):
        """兼容旧接口"""
        self.open_strategy_position(pos)

    def close_position(self, strategy_id: str, exchange: str, symbol: str):
        """兼容旧接口 → 全平策略仓位"""
        alloc = self.get_strategy_position(strategy_id, exchange, symbol)
        if alloc:
            self.close_strategy_position(strategy_id, exchange, symbol, alloc.quantity)

    def update_position(self, strategy_id: str, exchange: str, symbol: str, **kwargs):
        """兼容旧接口"""
        self.update_strategy_position(strategy_id, exchange, symbol, **kwargs)

    def count_active_positions(self) -> int:
        """活跃策略仓位总数"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM strategy_allocations WHERE quantity > 0"
        ).fetchone()
        return row[0] if row else 0

    def count_processed_signals(self) -> int:
        """已处理信号数（排除 claimed 状态）"""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM signal_log WHERE result != 'claimed'"
        ).fetchone()
        return row[0] if row else 0

    def get_daily_pnl(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_history WHERE created_at >= ?",
            (today,),
        ).fetchone()
        return row[0] if row else 0.0

    # ═════════════════════════════════════════════════════════════
    # 交易记录
    # ═════════════════════════════════════════════════════════════

    def record_trade(
        self, strategy_id: str, exchange: str, symbol: str,
        side: OrderSide, action: str, quantity: float, price: float,
        fee: float = 0.0, realized_pnl: float = 0.0,
        signal_id: str = "", exchange_order_id: str = "",
    ):
        self._conn.execute(
            """INSERT INTO trade_history
               (strategy_id, exchange, symbol, side, action, quantity, price,
                fee, realized_pnl, signal_id, exchange_order_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy_id, exchange, symbol, side.value, action,
             quantity, price, fee, realized_pnl,
             signal_id, exchange_order_id,
             datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
