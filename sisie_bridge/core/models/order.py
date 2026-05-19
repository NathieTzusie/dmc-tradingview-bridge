"""
实盘订单与持仓数据模型

与回测的 OpenOrder/CloseOrder/Fill/Position 完全独立。
实盘有额外的状态字段：exchange_order_id, status, partial_fill 等。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field


class OrderStatus(str, Enum):
    """订单生命周期状态"""
    PENDING = "pending"          # 待发送
    OPEN = "open"               # 已提交，等待成交
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    FAILED = "failed"            # 网络错误等


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class OrderRequest:
    """发给交易所的下单请求（内部格式，adapter 层转换）"""
    exchange: str
    symbol: str
    side: OrderSide
    order_type: str               # market / limit
    quantity: float
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reduce_only: bool = False
    leverage: int = 1
    client_order_id: str = ""     # 防重用


@dataclass
class OrderResult:
    """交易所订单返回结果"""
    success: bool
    exchange_order_id: str = ""
    client_order_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fee_paid: float = 0.0
    raw_response: dict = field(default_factory=dict)
    error_message: str = ""


@dataclass
class LivePosition:
    """实盘持仓状态（交易所视角）"""
    exchange: str
    symbol: str
    side: OrderSide
    quantity: float
    entry_price: float
    mark_price: float = 0.0
    leverage: int = 1
    unrealized_pnl: float = 0.0
    liquidation_price: float = 0.0
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_empty(self) -> bool:
        return abs(self.quantity) < 1e-8


@dataclass
class PositionState:
    """本地追踪的仓位状态（含策略维度信息）"""
    strategy_id: str
    exchange: str
    symbol: str
    side: OrderSide
    quantity: float
    entry_price: float
    entry_time: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    tp1_hit: bool = False
    realized_pnl: float = 0.0
    active_orders: list[str] = field(default_factory=list)  # exchange_order_ids

    def is_flat(self) -> bool:
        return abs(self.quantity) < 1e-8
