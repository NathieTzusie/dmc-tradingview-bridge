"""
信号标准化器 (Signal Normalizer)

将 TradingView webhook JSON → InternalSignal。
支持多种 TV 策略格式，通过 adapter 模式可扩展。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sisie_bridge.core.models.signal import (
    InternalSignal, SignalAction, OrderType, QuantityMode,
)
from sisie_bridge.core.config import BridgeConfig

log = logging.getLogger(__name__)


def normalize_tradingview_webhook(
    payload: Dict[str, Any],
    config: BridgeConfig,
) -> Optional[InternalSignal]:
    """将 TradingView webhook JSON 转为 InternalSignal。

    支持的 TV 变量：
      {{ticker}}              → symbol
      {{strategy.order.action}} → action (buy/sell)
      {{strategy.order.contracts}} → quantity
      {{close}}               → price
      {{timenow}}             → timestamp

    也支持用户自定义字段：
      strategy_id, exchange, stop_loss, take_profit, order_type 等。

    Args:
        payload: TV webhook 发送的 JSON body
        config: BridgeConfig（用于策略路由）

    Returns:
        InternalSignal，如果解析失败返回 None
    """
    # ── 提取核心字段 ──
    strategy_id = payload.get("strategy_id", payload.get("strategy", ""))
    if not strategy_id:
        log.warning("Webhook 缺少 strategy_id")
        return None

    # 从配置获取策略信息
    strat_cfg = config.strategies.get(strategy_id)
    exchange = payload.get("exchange", strat_cfg.exchange if strat_cfg else "")
    symbol = payload.get("symbol", payload.get("ticker", strat_cfg.symbol if strat_cfg else ""))

    if not exchange or not symbol:
        log.warning(f"Webhook 缺少 exchange/symbol: strategy={strategy_id}")
        return None

    # ── 解析动作 ──
    raw_action = (payload.get("action") or
                  payload.get("data") or
                  payload.get("side") or
                  payload.get("signal") or "").lower()

    action_map = {
        "buy": SignalAction.BUY,
        "sell": SignalAction.SELL,
        "close": SignalAction.CLOSE,
        "exit": SignalAction.CLOSE,
        "reduce": SignalAction.REDUCE,
        "set_sl_tp": SignalAction.SET_SL_TP,
    }
    action = action_map.get(raw_action)
    if not action:
        log.warning(f"未知信号动作: {raw_action} (strategy={strategy_id})")
        return None

    # ── 解析数量 ──
    quantity = payload.get("quantity", payload.get("size"))
    if quantity is not None:
        try:
            quantity = float(quantity)
        except (TypeError, ValueError):
            quantity = None

    # ── 解析订单类型 ──
    order_type_raw = payload.get("order_type", payload.get("type", "market")).lower()
    order_type = OrderType.LIMIT if order_type_raw == "limit" else OrderType.MARKET

    # ── 解析价格 ──
    price = payload.get("price", payload.get("close", payload.get("limit_price")))
    if price is not None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None

    # ── 止损止盈 ──
    stop_loss = payload.get("stop_loss", payload.get("sl"))
    if stop_loss is not None:
        try:
            stop_loss = float(stop_loss)
        except (TypeError, ValueError):
            stop_loss = None

    take_profit = payload.get("take_profit", payload.get("tp"))
    if take_profit is not None:
        try:
            take_profit = float(take_profit)
        except (TypeError, ValueError):
            take_profit = None

    # ── 时间 ──
    timestamp = payload.get("time", payload.get("date", ""))
    if not timestamp:
        timestamp = datetime.now(timezone.utc).isoformat()

    # ── 构建信号 ──
    allow_reverse = payload.get("allow_reverse", True)
    if isinstance(allow_reverse, str):
        allow_reverse = allow_reverse.lower() not in ("false", "no", "0")

    reduce_only = payload.get("reduce_only", False)
    if isinstance(reduce_only, str):
        reduce_only = reduce_only.lower() in ("true", "yes", "1")

    signal = InternalSignal(
        strategy_id=strategy_id,
        exchange=exchange,
        symbol=symbol,
        action=action,
        order_type=order_type,
        quantity=quantity,
        quantity_mode=QuantityMode(payload.get("quantity_mode", "contracts")),
        limit_price=price if order_type == OrderType.LIMIT else None,
        stop_loss=stop_loss,
        take_profit=take_profit,
        allow_reverse=allow_reverse,
        reduce_only=reduce_only,
        source="tradingview",
        source_timestamp=timestamp,
        metadata=payload,
    )

    log.info(
        f"[Normalizer] {strategy_id}: {action.value} {quantity or '?'} "
        f"{symbol} on {exchange}"
    )
    return signal
