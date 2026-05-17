"""
Sisie Strategy Bridge 主入口

接线所有模块：
  1. 加载配置
  2. 初始化 state manager (SQLite)
  3. 初始化 exchange adapters (Binance/Bybit/OKX)
  4. 初始化 risk manager（含策略配置）
  5. 定义 signal_handler (仓位管理 + 下单的核心逻辑)
  6. 启动 FastAPI webhook server

核心改进（vs v0.1）：
  - 原子幂等 token（try_claim_signal）
  - 订单成交确认后才更新本地仓位
  - 反手先确认平仓成交再开反向
  - 平仓用 reduce_only，不碰其他策略的仓位
  - 所有管理端点鉴权

用法:
  python -m sisie_bridge.main --config configs/bridge.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import uvicorn

from sisie_core.config import BridgeConfig, TIER1_EXCHANGES
from sisie_core.exchanges.base import ExchangeAdapter
from sisie_core.exchanges.ccxt_adapter import CcxtCexAdapter, ExchangeAdapterFactory
from sisie_core.models.order import (
    OrderRequest, OrderSide, OrderStatus, PositionState,
)
from sisie_core.models.signal import InternalSignal, SignalAction
from sisie_core.risk.manager import RiskManager, RiskConfig, RiskCheckResult
from sisie_bridge.state.manager import PositionStateManager
from sisie_bridge.state.reconciler import PositionReconciler
from sisie_bridge.webhook.server import create_app

# ─────────────────────────────────────────────────────────────────────
# 常数
# ─────────────────────────────────────────────────────────────────────

ORDER_POLL_INTERVAL = 0.5          # 秒，订单状态轮询间隔
ORDER_POLL_TIMEOUT = 10.0          # 秒，市价单确认超时
LIMIT_ORDER_POLL_TIMEOUT = 30.0    # 秒，限价单确认超时
SL_TP_ORDER_TIMEOUT = 5.0          # 秒，止损止盈保护单确认超时

# ─────────────────────────────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────────────────────────────

def setup_logging(debug: bool = False):
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

log = logging.getLogger("sisie_bridge")


# ─────────────────────────────────────────────────────────────────────
# Signal Handler: 仓位决策 + 确认成交后更新
# ─────────────────────────────────────────────────────────────────────

class SignalHandler:
    """核心信号处理：仓位状态机 + 确认成交 + 下单执行。

    关键原则：
      - 永远先查本地仓位状态（SQLite），不依赖 TV 的记忆
      - 下单后轮询订单状态，确认成交才更新本地仓位
      - 反手：先平仓 → 确认成交 → 再开反向
      - 平仓用 reduce_only 避免影响其他策略的仓位
    """

    def __init__(
        self,
        config: BridgeConfig,
        state: PositionStateManager,
        adapters: Dict[str, ExchangeAdapter],
        risk_manager: RiskManager,
    ):
        self.config = config
        self.state = state
        self.adapters = adapters
        self.risk = risk_manager

    async def handle(self, signal: InternalSignal) -> dict:
        """处理一个信号，执行为订单。

        流程: 查本地仓位 → 状态机决策 → 下单 → 轮询确认 → 更新状态

        Returns:
            {"status": "ok"|"rejected"|"error", "signal_id": ..., "order_id": ...}
        """
        sid = signal.strategy_id
        exc = signal.exchange
        sym = signal.symbol

        # ── 获取当前本地仓位 ──
        current = self.state.get_position(sid, exc, sym)

        # ── 获取策略配置 ──
        strat_cfg = self.config.strategies.get(sid)
        leverage = strat_cfg.default_leverage if strat_cfg else 1

        # ── allow_reverse 取信号请求和策略配置的交集 ──
        # 策略配置可以限制即使信号要求也不反手
        allow_reverse = signal.allow_reverse and self.risk.get_strategy_allow_reverse(sid)

        # ── 获取 adapter ──
        adapter = self.adapters.get(exc)
        if not adapter:
            return {"status": "error", "signal_id": signal.signal_id,
                    "reason": f"交易所 {exc} 未连接"}

        # ── 名义价值风控（开仓前检查，fail closed）──
        if signal.action in (SignalAction.BUY, SignalAction.SELL):
            try:
                ticker = await adapter.get_ticker(sym)
                price = ticker.get("last", 0)
                balance = await adapter.get_balance("USDT")
                qty = signal.quantity or await self._calculate_quantity(signal, adapter)
                order_value = qty * price
                # 计算该交易所已有持仓名义价值总和
                all_positions = self.state.get_all_positions()
                exchange_existing_notional = sum(
                    p.quantity * p.entry_price
                    for p in all_positions
                    if p.exchange == exc
                )
                # 反手场景：当前策略持有反向仓位，风控应排除将被平掉的仓位
                current_pos = self.state.get_position(sid, exc, sym)
                is_reverse = (
                    current_pos is not None and (
                        (signal.action == SignalAction.BUY and current_pos.side == OrderSide.SELL) or
                        (signal.action == SignalAction.SELL and current_pos.side == OrderSide.BUY)
                    )
                )
                if is_reverse:
                    closing_notional = current_pos.quantity * current_pos.entry_price
                    exchange_existing_notional = max(0.0, exchange_existing_notional - closing_notional)
                risk_r = self.risk.check(
                    signal,
                    order_value=order_value,
                    balance=balance,
                    exchange_existing_notional=exchange_existing_notional,
                    is_reverse=is_reverse,
                )
            except Exception as e:
                risk_r = RiskCheckResult(False, f"风控前置检查失败: {e}")
            if not risk_r.passed:
                return {"status": "rejected", "signal_id": signal.signal_id,
                        "reason": risk_r.reason}

        # ═══════════════════════════════════════════════════════
        # 状态机决策
        # ═══════════════════════════════════════════════════════

        if signal.action == SignalAction.CLOSE:
            return await self._handle_close(signal, current, adapter)

        elif signal.action == SignalAction.REDUCE:
            return await self._handle_reduce(signal, current, adapter)

        elif signal.action == SignalAction.SET_SL_TP:
            return await self._handle_set_sl_tp(signal, current, adapter)

        elif signal.action == SignalAction.BUY:
            if current is None:
                return await self._open_and_confirm(signal, adapter, OrderSide.BUY, leverage)
            elif current.side == OrderSide.BUY:
                log.info(f"[Signal] {sid} 已有多仓，忽略 buy")
                return {"status": "ignored", "signal_id": signal.signal_id, "reason": "已有多仓"}
            else:
                # 有空仓 → 先平再反手
                return await self._reverse(signal, current, adapter, OrderSide.BUY, leverage, allow_reverse)

        elif signal.action == SignalAction.SELL:
            if current is None:
                return await self._open_and_confirm(signal, adapter, OrderSide.SELL, leverage)
            elif current.side == OrderSide.SELL:
                log.info(f"[Signal] {sid} 已有空仓，忽略 sell")
                return {"status": "ignored", "signal_id": signal.signal_id, "reason": "已有空仓"}
            else:
                return await self._reverse(signal, current, adapter, OrderSide.SELL, leverage, allow_reverse)

        return {"status": "error", "signal_id": signal.signal_id,
                "reason": f"未处理的动作: {signal.action}"}

    # ═════════════════════════════════════════════════════════════
    # 核心操作
    # ═════════════════════════════════════════════════════════════

    async def _handle_close(
        self, signal: InternalSignal, current: Optional[PositionState], adapter: ExchangeAdapter
    ) -> dict:
        """平仓：系统知道当前方向，不需要 TV 告诉"""
        if current is None:
            return {"status": "ignored", "signal_id": signal.signal_id, "reason": "无持仓可平"}

        close_side = OrderSide.SELL if current.side == OrderSide.BUY else OrderSide.BUY

        order = OrderRequest(
            exchange=signal.exchange, symbol=signal.symbol,
            side=close_side, order_type=signal.order_type.value,
            quantity=current.quantity,
            price=signal.limit_price,
            reduce_only=True,
            client_order_id=f"{signal.signal_id}_close",
        )
        return await self._execute_and_confirm_close(signal, current, adapter, order, "close")

    async def _handle_reduce(
        self, signal: InternalSignal, current: Optional[PositionState], adapter: ExchangeAdapter
    ) -> dict:
        """部分平仓"""
        if current is None:
            return {"status": "ignored", "signal_id": signal.signal_id, "reason": "无持仓可减"}

        reduce_qty = signal.quantity or current.quantity * 0.5
        reduce_qty = min(reduce_qty, current.quantity)

        close_side = OrderSide.SELL if current.side == OrderSide.BUY else OrderSide.BUY

        order = OrderRequest(
            exchange=signal.exchange, symbol=signal.symbol,
            side=close_side, order_type=signal.order_type.value,
            quantity=reduce_qty,
            price=signal.limit_price,
            reduce_only=True,
            client_order_id=f"{signal.signal_id}_reduce",
        )
        return await self._execute_and_confirm_partial(signal, current, adapter, order, reduce_qty)

    async def _handle_set_sl_tp(
        self, signal: InternalSignal, current: Optional[PositionState], adapter: ExchangeAdapter
    ) -> dict:
        """修改止损止盈（仅更新本地记录，交易所下保护单）"""
        if current is None:
            return {"status": "ignored", "signal_id": signal.signal_id, "reason": "无持仓可修改"}

        if signal.stop_loss is not None:
            self.state.update_strategy_position(
                current.strategy_id, current.exchange, current.symbol,
                stop_loss=signal.stop_loss,
            )
        if signal.take_profit is not None:
            self.state.update_strategy_position(
                current.strategy_id, current.exchange, current.symbol,
                take_profit=signal.take_profit,
            )

        # 下保护单到交易所（fire-and-forget，不等待成交）
        sl_tp_results = []
        if signal.stop_loss is not None:
            try:
                sl_side = OrderSide.SELL if current.side == OrderSide.BUY else OrderSide.BUY
                sl_order = OrderRequest(
                    exchange=signal.exchange, symbol=signal.symbol,
                    side=sl_side, order_type="stop_market",
                    quantity=current.quantity,
                    price=signal.stop_loss,
                    reduce_only=True,
                    client_order_id=f"{signal.signal_id}_sl",
                )
                sl_result = await adapter.place_order(sl_order)
                sl_tp_results.append(f"SL: {'ok' if sl_result.success else 'fail'}")
            except Exception as e:
                log.warning(f"[Signal] 下止损单失败: {e}")

        if signal.take_profit is not None:
            try:
                tp_side = OrderSide.SELL if current.side == OrderSide.BUY else OrderSide.BUY
                tp_order = OrderRequest(
                    exchange=signal.exchange, symbol=signal.symbol,
                    side=tp_side, order_type="limit",
                    quantity=current.quantity,
                    price=signal.take_profit,
                    reduce_only=True,
                    client_order_id=f"{signal.signal_id}_tp",
                )
                tp_result = await adapter.place_order(tp_order)
                sl_tp_results.append(f"TP: {'ok' if tp_result.success else 'fail'}")
            except Exception as e:
                log.warning(f"[Signal] 下止盈单失败: {e}")

        return {
            "status": "ok",
            "signal_id": signal.signal_id,
            "action": "set_sl_tp",
            "sl_tp_results": sl_tp_results,
        }

    async def _open_and_confirm(
        self, signal: InternalSignal, adapter: ExchangeAdapter,
        side: OrderSide, leverage: int,
    ) -> dict:
        """开仓 + 确认成交后才更新本地仓位"""
        quantity = await self._calculate_quantity(signal, adapter)

        if leverage > 1:
            try:
                await adapter.set_leverage(signal.symbol, leverage)
            except Exception as e:
                log.warning(f"[Signal] 设置杠杆失败: {e}")

        order = OrderRequest(
            exchange=signal.exchange, symbol=signal.symbol,
            side=side, order_type=signal.order_type.value,
            quantity=quantity,
            price=signal.limit_price,
            client_order_id=f"{signal.signal_id}_open",
        )

        result = await self._place_and_confirm(signal, adapter, order)

        if result.success and result.filled_qty > 0:
            self._record_open(signal, side, result)
            status = "ok"
        elif result.success and result.status == OrderStatus.OPEN:
            # 限价单已挂但未成交，记录但不更新仓位
            log.info(f"[Order] 限价单待成交: {result.exchange_order_id}")
            status = "pending"
        else:
            status = "error"

        return {
            "status": status,
            "signal_id": signal.signal_id,
            "order_id": result.exchange_order_id,
            "action": "open",
            "side": side.value,
            "filled_qty": result.filled_qty,
            "avg_price": result.avg_price,
        }

    async def _reverse(
        self, signal: InternalSignal, current: PositionState,
        adapter: ExchangeAdapter, target_side: OrderSide,
        leverage: int, allow_reverse: bool,
    ) -> dict:
        """反手：先平仓（确认成交）→ 再开反向仓。

        多策略共享仓位时：检查是否有其他策略同 symbol。
        如有，只能平自己份额，不能反手。
        """
        sid = signal.strategy_id
        exc = signal.exchange
        sym = signal.symbol

        # ── 检查是否有其他策略共享仓位 ──
        # allow_reverse=False 时：共享检查跳过（允许平仓但不反手）
        # allow_reverse=True 时：有共享策略 → 拒绝反手（避免相互干扰）
        others = self.state.get_other_strategies_on_symbol(sid, exc, sym)
        if others and allow_reverse:
            # 有其他策略在同一 symbol 开仓，不能反手
            other_names = [o.strategy_id for o in others]
            log.warning(
                f"[Signal] {sid} 不能反手: 策略 {other_names} 也持有 {sym}，"
                f"请先让其他策略平仓"
            )
            return {
                "status": "rejected",
                "signal_id": signal.signal_id,
                "reason": f"反手被阻止: 策略 {other_names} 也持有 {sym}",
            }

        log.info(f"[Signal] {sid} 反手: {current.side.value} → {target_side.value}")

        # ── Step 1: 平仓（reduce_only） ──
        close_side = OrderSide.SELL if current.side == OrderSide.BUY else OrderSide.BUY
        close_order = OrderRequest(
            exchange=exc, symbol=sym,
            side=close_side, order_type="market",
            quantity=current.quantity,
            reduce_only=True,
            client_order_id=f"{signal.signal_id}_reverse_close",
        )

        close_result = await self._place_and_confirm(signal, adapter, close_order)

        if not close_result.success or close_result.filled_qty < current.quantity * 0.99:
            log.error(f"[Signal] {sid} 平仓未完全成交: filled={close_result.filled_qty}/{current.quantity}")
            remaining = current.quantity - close_result.filled_qty
            if remaining > 1e-8:
                self.state.close_strategy_position(sid, exc, sym, close_result.filled_qty)
            else:
                self.state.close_strategy_position(sid, exc, sym, current.quantity)
            return {
                "status": "error",
                "signal_id": signal.signal_id,
                "reason": f"平仓未完全成交 ({close_result.filled_qty}/{current.quantity})",
            }

        # ── Step 2: 记录平仓 ──
        pnl = self._calc_pnl(current, close_result)
        self.state.close_strategy_position(sid, exc, sym, current.quantity)
        self.state.record_trade(
            sid, exc, sym, current.side, "close_reverse",
            current.quantity, close_result.avg_price,
            fee=close_result.fee_paid, realized_pnl=pnl,
            signal_id=signal.signal_id,
            exchange_order_id=close_result.exchange_order_id,
        )

        if not allow_reverse:
            return {
                "status": "ok", "signal_id": signal.signal_id,
                "order_id": close_result.exchange_order_id,
                "action": "close_then_flat",
            }

        # ── Step 3: 开反向 ──
        return await self._open_and_confirm(signal, adapter, target_side, leverage)

    async def _execute_and_confirm_close(
        self, signal: InternalSignal, current: PositionState,
        adapter: ExchangeAdapter, order: OrderRequest, action: str,
    ) -> dict:
        """执行平仓 + 确认成交"""
        result = await self._place_and_confirm(signal, adapter, order)

        sid, exc, sym = current.strategy_id, current.exchange, current.symbol

        if result.success and result.filled_qty > 0:
            pnl = self._calc_pnl(current, result)
            self.state.close_strategy_position(sid, exc, sym, result.filled_qty)
            self.state.record_trade(
                sid, exc, sym, current.side, action,
                result.filled_qty, result.avg_price,
                fee=result.fee_paid, realized_pnl=pnl,
                signal_id=signal.signal_id,
                exchange_order_id=result.exchange_order_id,
            )
        elif not result.success:
            log.error(f"[Signal] {sid} 平仓失败: {result.error_message}")

        return {
            "status": "ok" if result.success else "error",
            "signal_id": signal.signal_id,
            "order_id": result.exchange_order_id,
            "action": action,
            "filled_qty": result.filled_qty,
            "avg_price": result.avg_price,
        }

    async def _execute_and_confirm_partial(
        self, signal: InternalSignal, current: PositionState,
        adapter: ExchangeAdapter, order: OrderRequest, reduce_qty: float,
    ) -> dict:
        """执行部分平仓 + 确认成交"""
        result = await self._place_and_confirm(signal, adapter, order)

        sid, exc, sym = current.strategy_id, current.exchange, current.symbol

        if result.success and result.filled_qty > 0:
            actual_fill = min(result.filled_qty, reduce_qty)
            self.state.close_strategy_position(sid, exc, sym, actual_fill)
            self.state.record_trade(
                sid, exc, sym, current.side, "reduce",
                actual_fill, result.avg_price,
                fee=result.fee_paid,
                signal_id=signal.signal_id,
                exchange_order_id=result.exchange_order_id,
            )

        return {
            "status": "ok" if result.success else "error",
            "signal_id": signal.signal_id,
            "order_id": result.exchange_order_id,
            "action": "reduce",
            "filled_qty": result.filled_qty,
        }

    # ═════════════════════════════════════════════════════════════
    # 订单生命周期
    # ═════════════════════════════════════════════════════════════

    async def _place_and_confirm(
        self, signal: InternalSignal, adapter: ExchangeAdapter,
        order: OrderRequest, timeout: float | None = None,
    ) -> "OrderResult":
        """下单 + 轮询确认成交。

        市价单：轮询直到 filled 或超时。
        限价单：不轮询（可能长时间不成交），返回 OPEN 状态。
        stop_market：轮询短时间确认。

        Returns:
            OrderResult，含确切的 filled_qty 和 avg_price。
        """
        result = await adapter.place_order(order)
        if not result.success or not result.exchange_order_id:
            return result

        # 限价单：不轮询，直接返回（调用方需处理 pending 状态）
        if order.order_type == "limit":
            log.info(f"[Order] 限价单已挂: {result.exchange_order_id} @ {order.price}")
            result.status = OrderStatus.OPEN
            return result

        # 市价单 & 止损市价单：轮询确认
        if timeout is None:
            timeout = SL_TP_ORDER_TIMEOUT if order.order_type == "stop_market" else ORDER_POLL_TIMEOUT

        polled = await self._poll_until_filled(
            adapter, order.symbol, result.exchange_order_id, timeout,
        )
        if polled:
            return polled

        # 超时后最后一次查询
        try:
            return await adapter.get_order(order.symbol, result.exchange_order_id)
        except Exception:
            return result

    async def _poll_until_filled(
        self, adapter: ExchangeAdapter, symbol: str,
        order_id: str, timeout: float,
    ) -> Optional["OrderResult"]:
        """轮询订单状态直到 filled 或超时"""
        elapsed = 0.0
        while elapsed < timeout:
            await asyncio.sleep(ORDER_POLL_INTERVAL)
            elapsed += ORDER_POLL_INTERVAL

            try:
                result = await adapter.get_order(symbol, order_id)
                if result.status == OrderStatus.FILLED:
                    log.info(f"[Order] {order_id} 成交: {result.filled_qty} @ {result.avg_price}")
                    return result
                elif result.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
                    log.warning(f"[Order] {order_id} 未成交: {result.status.value}")
                    return result
            except Exception as e:
                log.debug(f"[Order] 轮询异常: {e}")

        log.warning(f"[Order] {order_id} 轮询超时 ({timeout}s)")
        return None

    # ═════════════════════════════════════════════════════════════
    # 辅助
    # ═════════════════════════════════════════════════════════════

    async def _calculate_quantity(
        self, signal: InternalSignal, adapter: ExchangeAdapter,
    ) -> float:
        """计算实际下单数量"""
        if signal.quantity is not None and signal.quantity > 0:
            return signal.quantity

        # 从策略配置计算
        strat_cfg = self.config.strategies.get(signal.strategy_id)
        max_pos = strat_cfg.max_position_usdt if strat_cfg else 500.0
        leverage = strat_cfg.default_leverage if strat_cfg else 1

        # 获取行情
        try:
            ticker = await adapter.get_ticker(signal.symbol)
            price = ticker.get("last", 0)
        except Exception:
            price = signal.limit_price or 0

        if price <= 0:
            return 0.01

        # 名义本金 → 合约数量（简化为 notional/price，不含精度）
        notional = max_pos
        quantity = notional / price

        # 获取最小数量
        min_qty = await adapter.get_min_quantity(signal.symbol)

        return max(quantity, min_qty)

    @staticmethod
    def _calc_pnl(current: PositionState, fill: "OrderResult") -> float:
        """计算已实现 PnL"""
        if current.side == OrderSide.BUY:
            return (fill.avg_price - current.entry_price) * fill.filled_qty - fill.fee_paid
        else:
            return (current.entry_price - fill.avg_price) * fill.filled_qty - fill.fee_paid

    def _record_open(self, signal: InternalSignal, side: OrderSide, fill: "OrderResult"):
        """记录开仓到状态（策略分配+交易所聚合）"""
        now = datetime.now(timezone.utc)
        self.state.open_strategy_position(PositionState(
            strategy_id=signal.strategy_id,
            exchange=signal.exchange,
            symbol=signal.symbol,
            side=side,
            quantity=fill.filled_qty,
            entry_price=fill.avg_price or signal.limit_price or 0,
            entry_time=now,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
        ))
        self.state.record_trade(
            signal.strategy_id, signal.exchange, signal.symbol,
            side, "open", fill.filled_qty, fill.avg_price,
            fee=fill.fee_paid,
            signal_id=signal.signal_id,
            exchange_order_id=fill.exchange_order_id,
        )


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

async def main_async(config_path: str, debug: bool = False):
    setup_logging(debug)

    # 1. 加载配置
    log.info(f"加载配置: {config_path}")
    config = BridgeConfig.from_yaml(config_path)

    # 鉴权检查：空 token 禁止启动
    if not config.webhook_auth_token:
        log.critical("❌ TV_BRIDGE_AUTH_TOKEN 未设置！拒绝启动。请设置环境变量。")
        sys.exit(1)

    # 2. State Manager
    state = PositionStateManager(config.state_db_path)
    log.info(f"State DB: {config.state_db_path}, 活跃持仓: {state.count_active_positions()}")

    # 3. Exchange Adapters
    adapters: Dict[str, ExchangeAdapter] = {}
    all_adapters: Dict[str, ExchangeAdapter] = {}  # 所有 enabled 交易所（含连接失败的）
    for ex_name, ex_cfg in config.exchanges.items():
        if not ex_cfg.enabled:
            continue
        creds = ex_cfg.credentials
        adapter = ExchangeAdapterFactory.create(
            exchange_name=ex_name,
            api_key=creds.api_key,
            api_secret=creds.api_secret,
            api_passphrase=creds.api_passphrase,
            testnet=creds.testnet,
        )
        all_adapters[ex_name] = adapter  # 无论成败都记录
        success = await adapter.connect()
        if success:
            adapters[ex_name] = adapter
            log.info(f"✅ {ex_name} 连接成功 (testnet={creds.testnet})")
        else:
            log.error(f"❌ {ex_name} 连接失败：{adapter.last_error}")
    providers = ", ".join(adapters.keys()) if adapters else "(无)"
    log.info(f"可用交易所: {providers}")

    # 4. Risk Manager（含策略配置）
    strategy_risk_cfgs = {
        sid: {
            "max_position_usdt": s.max_position_usdt,
            "allow_reverse": s.allow_reverse,
            "signal_cooldown_sec": s.signal_cooldown_sec,
        }
        for sid, s in config.strategies.items()
    }
    risk_mgr = RiskManager(
        RiskConfig(
            emergency_stop=config.risk.emergency_stop,
            global_max_positions=config.risk.global_max_positions,
            global_max_usdt=config.risk.global_max_usdt,
            daily_max_loss_usdt=config.risk.daily_max_loss_usdt,
            max_leverage_ratio=getattr(config.risk, 'max_leverage_ratio', 5.0),
        ),
        state,
        strategy_configs=strategy_risk_cfgs,
    )

    # 5. Signal Handler
    handler = SignalHandler(config, state, adapters, risk_mgr)

    # 6. 仓位对账（后台任务）
    reconciler = PositionReconciler(state, adapters, interval_sec=300)
    await reconciler.start()

    # 7. FastAPI
    app = create_app(config, risk_mgr, state, handler.handle, reconciler, all_adapters=all_adapters, connected_adapters=adapters)

    # 8. 启动
    log.info(f"🚀 DMC TV Bridge 启动在 0.0.0.0:{config.webhook_port}")
    log.info(f"   Emergency Stop: {'🔴 ON' if config.risk.emergency_stop else '🟢 OFF'}")
    log.info(f"   交易所: {providers}")
    log.info(f"   策略: {', '.join(config.strategies.keys()) or '(无)'}")
    log.info(f"   对账间隔: 300s")

    server = uvicorn.Server(uvicorn.Config(
        app, host="0.0.0.0", port=config.webhook_port, log_level="info" if not debug else "debug",
    ))
    await server.serve()


def main():
    parser = argparse.ArgumentParser(description="DMC TV Bridge")
    parser.add_argument("--config", default="configs/bridge.yaml", help="配置文件路径")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    args = parser.parse_args()

    try:
        asyncio.run(main_async(args.config, args.debug))
    except KeyboardInterrupt:
        log.info("收到中断信号，正在关闭...")
    except Exception as e:
        log.exception(f"致命错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
