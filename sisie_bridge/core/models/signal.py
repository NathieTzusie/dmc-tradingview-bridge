"""
统一内部信号格式 (Internal Signal Schema)

所有 TV webhook 或 LiveMonitor 信号在进入系统前，
都先转换成这个结构，确保交易所无关。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, model_validator


class SignalAction(str, Enum):
    """信号动作（策略意图，不是下单方向）"""
    BUY = "buy"            # 开多 / 空仓时反手开多
    SELL = "sell"          # 开空 / 多仓时反手开空
    CLOSE = "close"        # 平当前持仓（系统知道方向）
    REDUCE = "reduce"      # 部分平仓
    SET_SL_TP = "set_sl_tp"  # 只修改止损止盈


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class QuantityMode(str, Enum):
    CONTRACTS = "contracts"          # 绝对数量
    PERCENT_EQUITY = "percent_equity"  # 占总权益百分比
    PERCENT_POSITION = "percent_position"  # 占当前仓位百分比（部分平仓用）


class InternalSignal(BaseModel):
    """统一内部信号。

    所有进入系统的信号（无论来源）统一转成此格式。
    TradingView webhook → SignalNormalizer → InternalSignal
    """
    # ── 身份 ──
    strategy_id: str = Field(..., description="策略唯一标识")
    signal_id: str = Field(default="", description="信号唯一 ID（幂等用），自动生成")

    # ── 路由 ──
    exchange: str = Field(..., description="目标交易所（如 binance/bybit/okx）")
    symbol: str = Field(..., description="交易对（交易所标准格式）")

    # ── 动作 ──
    action: SignalAction
    order_type: OrderType = OrderType.MARKET

    # ── 数量 ──
    quantity: Optional[float] = Field(default=None, description="下单数量")
    quantity_mode: QuantityMode = QuantityMode.CONTRACTS

    # ── 价格 ──
    limit_price: Optional[float] = Field(default=None, description="限价单价格")
    stop_loss: Optional[float] = Field(default=None, description="止损价")
    take_profit: Optional[float] = Field(default=None, description="止盈价")

    # ── 行为控制 ──
    allow_reverse: bool = Field(default=True, description="是否允许平反向仓后反手")
    reduce_only: bool = Field(default=False, description="只减仓不反手")

    # ── 来源 ──
    source: str = Field(default="tradingview", description="信号来源：tradingview / livemonitor / manual")
    source_timestamp: str = Field(default="", description="原始信号时间戳")

    # ── 元数据 ──
    metadata: Dict[str, Any] = Field(default_factory=dict, description="保留原始 payload")

    @model_validator(mode="after")
    def ensure_signal_id(self) -> "InternalSignal":
        """如果未提供 signal_id，基于策略+时间+动作生成唯一 ID"""
        if self.signal_id:
            return self
        raw = f"{self.strategy_id}|{self.source_timestamp}|{self.action.value}|{self.exchange}|{self.symbol}"
        self.signal_id = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return self

    @property
    def side_for_exchange(self) -> str:
        """转化为交易所方向的 buyer/seller（不包含仓位管理逻辑）"""
        if self.action == SignalAction.BUY:
            return "buy"
        elif self.action == SignalAction.SELL:
            return "sell"
        return self.action.value
