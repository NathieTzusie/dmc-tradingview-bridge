"""
ExchangeAdapter 抽象基类

所有交易所 adapter 必须实现此接口。
每个方法都是 async，因为实盘涉及网络 I/O。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from sisie_bridge.core.models.order import OrderRequest, OrderResult, LivePosition


class ExchangeAdapter(ABC):
    """交易所适配器抽象基类"""

    def __init__(self, exchange_name: str, testnet: bool = True):
        self.exchange_name = exchange_name
        self.testnet = testnet
        self.connected: bool = False      # 连接状态（connect() 后更新）
        self.last_error: str = ""         # 最近一次连接/操作错误信息

    # ═════════════════════════════════════════════════════════════
    # 连接
    # ═════════════════════════════════════════════════════════════

    @abstractmethod
    async def connect(self) -> bool:
        """初始化连接、验证 API 密钥。

        Returns:
            True 如果认证成功。
        """
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """关闭连接、清理资源。"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """轻量健康检查（查询服务器时间或账户状态）。

        Returns:
            True 如果服务正常。
        """
        ...

    # ═════════════════════════════════════════════════════════════
    # 下单
    # ═════════════════════════════════════════════════════════════

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """下单（市价/限价）。

        Args:
            order: 标准化订单请求。

        Returns:
            OrderResult 含 exchange_order_id 和状态。
        """
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        """撤单。

        Args:
            symbol: 交易对。
            order_id: 交易所订单 ID。

        Returns:
            OrderResult 含撤单结果。
        """
        ...

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> OrderResult:
        """查询订单状态。

        Args:
            symbol: 交易对。
            order_id: 交易所订单 ID。

        Returns:
            OrderResult 含当前状态和成交信息。
        """
        ...

    # ═════════════════════════════════════════════════════════════
    # 仓位
    # ═════════════════════════════════════════════════════════════

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[LivePosition]:
        """查询当前持仓。

        Args:
            symbol: 交易对。

        Returns:
            LivePosition，无持仓返回 None。
        """
        ...

    @abstractmethod
    async def get_all_positions(self) -> List[LivePosition]:
        """查询所有持仓。"""
        ...

    @abstractmethod
    async def close_position(self, symbol: str) -> OrderResult:
        """市价全平指定交易对。

        Args:
            symbol: 交易对。

        Returns:
            OrderResult。
        """
        ...

    # ═════════════════════════════════════════════════════════════
    # 账户
    # ═════════════════════════════════════════════════════════════

    @abstractmethod
    async def get_balance(self, asset: str = "USDT") -> float:
        """查询可用余额。

        Args:
            asset: 资产符号。

        Returns:
            可用余额。
        """
        ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """设置杠杆倍数。

        Args:
            symbol: 交易对。
            leverage: 杠杆倍数。

        Returns:
            True 如果设置成功。
        """
        ...

    # ═════════════════════════════════════════════════════════════
    # 市场数据
    # ═════════════════════════════════════════════════════════════

    @abstractmethod
    async def get_ticker(self, symbol: str) -> dict:
        """获取最新行情。

        Returns:
            dict with keys: last, bid, ask, timestamp
        """
        ...

    @abstractmethod
    async def get_min_quantity(self, symbol: str) -> float:
        """获取最小下单数量。

        Returns:
            最小数量。
        """
        ...
