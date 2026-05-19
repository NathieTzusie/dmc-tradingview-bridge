"""
仓位对账器 (Position Reconciler)

定期查询交易所真实持仓，与本地 SQLite 持仓做对比。
发现不一致 → Discord 告警（默认不自愈，需要人工确认）。

用法:
    reconciler = Reconciler(state, adapters)
    await reconciler.reconcile_all()
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sisie_bridge.core.exchanges.base import ExchangeAdapter
from sisie_bridge.state.manager import PositionStateManager
from sisie_bridge.core.models.order import LivePosition, OrderSide, OrderStatus

log = logging.getLogger(__name__)


@dataclass
class ReconciliationResult:
    """对账结果"""
    exchange: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    local_positions: int = 0
    exchange_positions: int = 0
    matched: int = 0
    local_only: List[dict] = field(default_factory=list)     # 本地有、交易所无
    exchange_only: List[dict] = field(default_factory=list)   # 交易所有、本地无
    mismatched: List[dict] = field(default_factory=list)      # 两边都有但不一致

    @property
    def is_clean(self) -> bool:
        return len(self.local_only) == 0 and len(self.exchange_only) == 0 and len(self.mismatched) == 0

    @property
    def has_discrepancy(self) -> bool:
        return not self.is_clean


DEFAULT_RECONCILE_INTERVAL = 300  # 5 分钟


class PositionReconciler:
    """定时对账引擎"""

    def __init__(
        self,
        state: PositionStateManager,
        adapters: Dict[str, ExchangeAdapter],
        interval_sec: float = DEFAULT_RECONCILE_INTERVAL,
    ):
        self.state = state
        self.adapters = adapters
        self.interval = interval_sec
        self._last_results: Dict[str, ReconciliationResult] = {}
        self._alert_callback = None   # async callable (ReconciliationResult) -> None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def set_alert_callback(self, cb):
        """设置告警回调（如 Discord 推送）"""
        self._alert_callback = cb

    async def start(self):
        """启动后台对账循环"""
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info(f"[Reconciler] 启动，间隔 {self.interval}s")

    async def stop(self):
        """停止对账"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while self._running:
            try:
                await self.reconcile_all()
            except Exception as e:
                log.exception(f"[Reconciler] 对账异常: {e}")
            await asyncio.sleep(self.interval)

    async def reconcile_all(self) -> Dict[str, ReconciliationResult]:
        """对所有已连接交易所执行对账"""
        results = {}
        for ex_name, adapter in self.adapters.items():
            try:
                result = await self._reconcile_exchange(ex_name, adapter)
                results[ex_name] = result
                self._last_results[ex_name] = result

                if result.has_discrepancy:
                    log.warning(f"[Reconciler] {ex_name} 发现差异: "
                                f"local_only={len(result.local_only)} "
                                f"exchange_only={len(result.exchange_only)} "
                                f"mismatched={len(result.mismatched)}")
                    if self._alert_callback:
                        await self._alert_callback(result)
                else:
                    log.debug(f"[Reconciler] {ex_name} 对账一致: "
                              f"{result.matched} 笔持仓匹配")
            except Exception as e:
                log.error(f"[Reconciler] {ex_name} 对账失败: {e}")

        return results

    async def _reconcile_exchange(
        self, ex_name: str, adapter: ExchangeAdapter,
    ) -> ReconciliationResult:
        """对单个交易所执行对账"""
        # 获取交易所真实持仓
        try:
            exchange_positions = await adapter.get_all_positions()
        except Exception as e:
            log.error(f"[Reconciler] {ex_name} 获取持仓失败: {e}")
            return ReconciliationResult(exchange=ex_name)

        # 获取本地持仓（只取该交易所的）
        local_positions = [
            p for p in self.state.get_all_positions()
            if p.exchange == ex_name
        ]

        # 构建索引
        ex_index: Dict[str, LivePosition] = {
            p.symbol: p for p in exchange_positions
        }
        local_index: Dict[str, object] = {
            p.symbol: p for p in local_positions
        }

        result = ReconciliationResult(
            exchange=ex_name,
            local_positions=len(local_positions),
            exchange_positions=len(exchange_positions),
        )

        # 匹配
        now_utc = datetime.now(timezone.utc)
        GRACE_SECONDS = 60  # 刚开仓在交易所延迟期内不告警
        for sym, local_pos in local_index.items():
            if sym not in ex_index:
                # 如果开仓在 60s 内，跳过告警（交易所可能还未同步持仓）
                try:
                    age = (now_utc - local_pos.entry_time.replace(tzinfo=timezone.utc)
                           if local_pos.entry_time.tzinfo is None
                           else now_utc - local_pos.entry_time).total_seconds()
                except Exception:
                    age = 9999
                if age < GRACE_SECONDS:
                    log.debug(f"[Reconciler] {sym} local_only 但开仓 {age:.0f}s 内，宿为正常延迟")
                    result.matched += 1  # 不计入差异
                    continue
                result.local_only.append({
                    "symbol": sym,
                    "side": local_pos.side.value,
                    "quantity": local_pos.quantity,
                    "entry_price": local_pos.entry_price,
                    "strategy_id": local_pos.strategy_id,
                })
            else:
                ex_pos = ex_index[sym]
                # 检查方向/数量是否一致
                local_side = local_pos.side.value
                ex_side = ex_pos.side.value
                qty_diff = abs(local_pos.quantity - ex_pos.quantity)
                if local_side != ex_side or qty_diff > abs(local_pos.quantity * 0.01):
                    result.mismatched.append({
                        "symbol": sym,
                        "strategy_id": local_pos.strategy_id,
                        "local": {"side": local_side, "qty": local_pos.quantity},
                        "exchange": {"side": ex_side, "qty": ex_pos.quantity},
                    })
                else:
                    result.matched += 1

        # 交易所独有持仓
        for sym, ex_pos in ex_index.items():
            if sym not in local_index:
                result.exchange_only.append({
                    "symbol": sym,
                    "side": ex_pos.side.value,
                    "quantity": ex_pos.quantity,
                    "entry_price": ex_pos.entry_price,
                    "unrealized_pnl": ex_pos.unrealized_pnl,
                })

        return result

    def last_results(self) -> Dict[str, ReconciliationResult]:
        """获取最近一次对账结果"""
        return dict(self._last_results)
