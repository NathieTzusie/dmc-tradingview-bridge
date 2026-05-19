"""
CCXT 通用 CEX 适配器

基于 CCXT 统一接口适配所有 HMAC-SHA256 认证的 CEX。
支持 Binance (USDT-M Futures)、Bybit (V5 Unified)、OKX (V5 Unified)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional

import ccxt
import ccxt.async_support as ccxt_async

from sisie_bridge.core.exchanges.base import ExchangeAdapter
from sisie_bridge.core.exchanges.bitunix_adapter import BitunixAdapter
from sisie_bridge.core.models.order import (
    OrderRequest, OrderResult, OrderSide, OrderStatus, LivePosition,
)
from sisie_bridge.core.config import resolve_ccxt_id

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────
# 交易所特有参数映射
# ─────────────────────────────────────────────────────────────────────

# 每个 CCXT 交易所的下单默认参数
_EXCHANGE_PARAMS: Dict[str, dict] = {
    "binanceusdm": {
        "defaultType": "future",
        "marginMode": "isolated",
    },
    "bybit": {
        "defaultType": "swap",
        "category": "linear",
    },
    "okx": {
        "defaultType": "swap",
        "tdMode": "isolated",
    },
    "bitget": {
        "defaultType": "swap",
        "marginCoin": "USDT",
    },
    "kucoinfutures": {
        "defaultType": "future",
    },
    "gate": {
        "defaultType": "swap",
        "settle": "usdt",
    },
    "mexc": {
        "defaultType": "swap",
    },
    "bingx": {
        "defaultType": "swap",
    },
    "htx": {
        "defaultType": "swap",
    },
}


class CcxtCexAdapter(ExchangeAdapter):
    """CCXT 驱动的通用 CEX 适配器。

    支持 Tier 1: Binance USDT-M, Bybit Linear, OKX Linear
    以及 Tier 2/3 中的 Bitget, KuCoin, Gate, MEXC, BingX, HTX。

    架构：每个 CCXT exchange 实例 = 一个交易所连接。
    不会创建全局 CCXT 实例池，避免限流和凭证混淆。
    """

    def __init__(
        self,
        exchange_name: str,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        testnet: bool = True,
    ):
        super().__init__(exchange_name, testnet)
        self.ccxt_id = resolve_ccxt_id(exchange_name)
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self._exchange: Optional[ccxt_async.Exchange] = None
        self._default_params = _EXCHANGE_PARAMS.get(self.ccxt_id, {}).copy()

    # ═════════════════════════════════════════════════════════════
    # 连接
    # ═════════════════════════════════════════════════════════════

    async def connect(self) -> bool:
        """创建 CCXT async exchange 实例并验证密钥。

        Returns:
            True 如果认证成功。
        """
        try:
            exchange_class = getattr(ccxt_async, self.ccxt_id)
        except AttributeError:
            log.error(f"CCXT 不支持交易所: {self.ccxt_id}")
            return False

        config: dict = {
            "apiKey": self.api_key,
            "secret": self.api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": self._default_params.get("defaultType", "swap"),
                "fetchCurrencies": False,  # testnet 避免不必要的 API 调用
            },
        }

        if self.api_passphrase:
            config["password"] = self.api_passphrase

        # 测试网
        if self.testnet:
            if self.ccxt_id == "bybit":
                config["sandbox"] = True

        self._exchange = exchange_class(config)

        # OKX demo: 用 CCXT 内置 sandbox mode
        if self.testnet and self.ccxt_id == "okx":
            self._exchange.set_sandbox_mode(True)
            self._exchange.options['fetchCurrencies'] = False
            log.info(f"[{self.exchange_name}] demo trading mode")
            try:
                await self._exchange.load_markets()
            except Exception:
                pass

        # 测试网 URL 配置
        if self.testnet:
            if self.ccxt_id == "bybit":
                pass  # sandbox 已在构造参数中设置
            elif self.ccxt_id == "okx":
                pass  # simulated 已在上面设置
            elif self.ccxt_id == "binanceusdm":
                # Binance 旧 testnet futures sandbox (CCXT 4.5+ 封锁 set_sandbox_mode)
                # 但 testnet.binancefuture.com API 仍可用 — 绕过 CCXT 直接设 URL
                testnet_urls = {
                    'fapiPublic': 'https://testnet.binancefuture.com/fapi/v1',
                    'fapiPrivate': 'https://testnet.binancefuture.com/fapi/v1',
                    'fapiPublicV2': 'https://testnet.binancefuture.com/fapi/v2',
                    'fapiPrivateV2': 'https://testnet.binancefuture.com/fapi/v2',
                    'fapiPublicV3': 'https://testnet.binancefuture.com/fapi/v3',
                    'fapiPrivateV3': 'https://testnet.binancefuture.com/fapi/v3',
                    'fapiData': 'https://testnet.binancefuture.com/futures/data',
                }
                self._exchange.urls['api'].update(testnet_urls)
                log.info(f"[{self.exchange_name}] testnet.binancefuture.com (futures testnet)")
            elif self.ccxt_id == "gate":
                # Gate testnet: swap all URLs to testnet
                test_urls = self._exchange.urls.get('test', {})
                if test_urls:
                    self._exchange.urls['api'] = test_urls
                log.info(f"[{self.exchange_name}] 使用 testnet URLs")

        try:
            # 验证连通性（用公开接口）
            await self._exchange.fetch_time()
            self.connected = True
            self.last_error = ""
            log.info(f"[{self.exchange_name}] 连接成功 (testnet={self.testnet})")
            return True
        except Exception as e:
            self.connected = False
            self.last_error = str(e)
            log.error(f"[{self.exchange_name}] 连接失败: {e}")
            await self._exchange.close()
            self._exchange = None
            return False

    async def disconnect(self) -> None:
        if self._exchange:
            await self._exchange.close()
            self._exchange = None
            log.info(f"[{self.exchange_name}] 已断开")

    async def health_check(self) -> bool:
        if not self._exchange:
            self.connected = False
            return False
        try:
            await self._exchange.fetch_time()
            self.connected = True
            self.last_error = ""
            return True
        except Exception as e:
            self.connected = False
            self.last_error = str(e)
            return False

    def _check_connected(self):
        if not self._exchange:
            raise RuntimeError(f"[{self.exchange_name}] 未连接，请先调用 connect()")

    # ═════════════════════════════════════════════════════════════
    # 下单
    # ═════════════════════════════════════════════════════════════

    async def place_order(self, order: OrderRequest) -> OrderResult:
        self._check_connected()

        params = self._default_params.copy()

        # 精度处理（防止 CCXT 因精度不匹配拒单）
        if self._exchange is not None:
            try:
                if not getattr(self._exchange, 'markets', None):
                    await self._exchange.load_markets()
                if order.quantity:
                    order.quantity = float(
                        self._exchange.amount_to_precision(order.symbol, order.quantity)
                    )
                if order.price:
                    order.price = float(
                        self._exchange.price_to_precision(order.symbol, order.price)
                    )
            except Exception as e:
                log.debug(f"[{self.exchange_name}] 精度处理跳过: {e}")

        if order.reduce_only:
            params["reduceOnly"] = True

        # OKX 合约需要 posSide（双向持仓模式下关键）
        if self.ccxt_id == "okx":
            if order.reduce_only:
                # 平仓：posSide 跟随要平的仓位方向
                params["posSide"] = "long" if order.side == OrderSide.SELL else "short"
            else:
                params["posSide"] = "long" if order.side == OrderSide.BUY else "short"

        try:
            raw = await self._exchange.create_order(  # type: ignore[union-attr]
                symbol=order.symbol,
                type=order.order_type,
                side=order.side.value,
                amount=order.quantity,
                price=order.price,
                params=params,
            )

            log.info(
                f"[{self.exchange_name}] 下单成功: {order.side.value} {order.quantity} "
                f"{order.symbol} @ {order.order_type} → {raw.get('id', '?')}"
            )
            return OrderResult(
                success=True,
                exchange_order_id=str(raw.get("id", "")),
                client_order_id=order.client_order_id,
                status=OrderStatus.OPEN,
                filled_qty=float(raw.get("filled") or 0),
                avg_price=float(raw.get("average") or raw.get("price") or 0),
                raw_response=raw,
            )

        except ccxt.InsufficientFunds as e:
            log.warning(f"[{self.exchange_name}] 余额不足: {e}")
            return OrderResult(
                success=False, status=OrderStatus.REJECTED,
                client_order_id=order.client_order_id,
                error_message=f"余额不足: {e}",
            )
        except ccxt.InvalidOrder as e:
            log.warning(f"[{self.exchange_name}] 无效订单: {e}")
            return OrderResult(
                success=False, status=OrderStatus.REJECTED,
                client_order_id=order.client_order_id,
                error_message=f"无效订单: {e}",
            )
        except Exception as e:
            log.error(f"[{self.exchange_name}] 下单异常: {e}")
            return OrderResult(
                success=False, status=OrderStatus.FAILED,
                client_order_id=order.client_order_id,
                error_message=str(e),
            )

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        self._check_connected()
        try:
            raw = await self._exchange.cancel_order(order_id, symbol)  # type: ignore[union-attr]
            return OrderResult(
                success=True,
                exchange_order_id=order_id,
                status=OrderStatus.CANCELED,
                raw_response=raw,
            )
        except Exception as e:
            log.error(f"[{self.exchange_name}] 撤单失败 {order_id}: {e}")
            return OrderResult(
                success=False, status=OrderStatus.FAILED,
                exchange_order_id=order_id,
                error_message=str(e),
            )

    async def get_order(self, symbol: str, order_id: str) -> OrderResult:
        self._check_connected()
        try:
            raw = await self._exchange.fetch_order(order_id, symbol)  # type: ignore[union-attr]
            status_map = {
                "open": OrderStatus.OPEN,
                "closed": OrderStatus.FILLED,
                "canceled": OrderStatus.CANCELED,
                "expired": OrderStatus.EXPIRED,
                "rejected": OrderStatus.REJECTED,
            }
            ccxt_status = raw.get("status", "")
            return OrderResult(
                success=True,
                exchange_order_id=order_id,
                status=status_map.get(ccxt_status, OrderStatus.OPEN),
                filled_qty=float(raw.get("filled", 0) or 0),
                avg_price=float(raw.get("average") or raw.get("price") or 0),
                fee_paid=float((raw.get("fee") or {}).get("cost", 0) or 0),
                raw_response=raw,
            )
        except ccxt.OrderNotFound:
            return OrderResult(
                success=False, exchange_order_id=order_id,
                status=OrderStatus.CANCELED,
                error_message="订单未找到",
            )
        except Exception as e:
            log.error(f"[{self.exchange_name}] 查询订单失败 {order_id}: {e}")
            return OrderResult(
                success=False, exchange_order_id=order_id,
                status=OrderStatus.FAILED, error_message=str(e),
            )

    # ═════════════════════════════════════════════════════════════
    # 仓位
    # ═════════════════════════════════════════════════════════════

    async def get_position(self, symbol: str) -> Optional[LivePosition]:
        self._check_connected()
        try:
            positions = await self._exchange.fetch_positions([symbol])  # type: ignore[union-attr]
        except Exception:
            # fallback: 部分交易所用 fetch_position
            try:
                positions = [await self._exchange.fetch_position(symbol)]  # type: ignore[union-attr]
            except Exception:
                return None

        if not positions:
            return None

        pos = positions[0]
        qty = abs(float(pos.get("contracts", 0)))
        if qty < 1e-8:
            return None

        side_str = pos.get("side", "").lower()
        return LivePosition(
            exchange=self.exchange_name,
            symbol=symbol,
            side=OrderSide.BUY if side_str == "long" else OrderSide.SELL,
            quantity=qty,
            entry_price=float(pos.get("entryPrice", 0)),
            mark_price=float(pos.get("markPrice", 0)),
            leverage=int(pos.get("leverage", 1)),
            unrealized_pnl=float(pos.get("unrealizedPnl", 0)),
            liquidation_price=float(pos.get("liquidationPrice", 0)),
        )

    async def get_all_positions(self) -> List[LivePosition]:
        self._check_connected()
        try:
            raw = await self._exchange.fetch_positions()  # type: ignore[union-attr]
        except Exception as e:
            log.error(f"[{self.exchange_name}] 获取所有持仓失败: {e}")
            return []

        results = []
        for pos in raw:
            qty = abs(float(pos.get("contracts") or 0))
            if qty < 1e-8:
                continue
            side_str = (pos.get("side") or "").lower()
            results.append(LivePosition(
                exchange=self.exchange_name,
                symbol=pos.get("symbol", ""),
                side=OrderSide.BUY if side_str == "long" else OrderSide.SELL,
                quantity=qty,
                entry_price=float(pos.get("entryPrice") or 0),
                mark_price=float(pos.get("markPrice") or 0),
                leverage=int(pos.get("leverage") or 1),
                unrealized_pnl=float(pos.get("unrealizedPnl") or 0),
                liquidation_price=float(pos.get("liquidationPrice") or 0),
            ))
        return results

    async def close_position(self, symbol: str) -> OrderResult:
        """市价全平指定交易对"""
        pos = await self.get_position(symbol)
        if pos is None:
            return OrderResult(
                success=True, status=OrderStatus.FILLED,
                error_message="无持仓可平",
            )

        close_order = OrderRequest(
            exchange=self.exchange_name,
            symbol=symbol,
            side=OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY,
            order_type="market",
            quantity=pos.quantity,
            reduce_only=True,
            client_order_id=f"close_{symbol}_{pos.side.value}",
        )
        return await self.place_order(close_order)

    # ═════════════════════════════════════════════════════════════
    # 账户
    # ═════════════════════════════════════════════════════════════

    async def get_balance(self, asset: str = "USDT") -> float:
        self._check_connected()
        try:
            if self.ccxt_id == "binanceusdm":
                # Binance: fapi/v2/account
                account = await self._exchange.fapiPrivateV2GetAccount()  # type: ignore[union-attr]
                for b in account.get("assets", []):
                    if b.get("asset") == asset:
                        return float(b.get("walletBalance", 0))
                return 0.0
            else:
                # Bybit/OKX/其他: 用 CCXT fetch_balance
                balance = await self._exchange.fetch_balance()  # type: ignore[union-attr]
                free = balance.get(asset, {}).get("free", 0)
                return float(free)
        except Exception as e:
            log.error(f"[{self.exchange_name}] 获取余额失败: {e}")
            return 0.0

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        self._check_connected()
        try:
            await self._exchange.set_leverage(leverage, symbol)  # type: ignore[union-attr]
            log.info(f"[{self.exchange_name}] {symbol} 杠杆设为 {leverage}x")
            return True
        except Exception as e:
            log.warning(f"[{self.exchange_name}] 设置杠杆失败: {e}")
            return False

    # ═════════════════════════════════════════════════════════════
    # 市场数据
    # ═════════════════════════════════════════════════════════════

    async def get_ticker(self, symbol: str) -> dict:
        self._check_connected()
        ticker = await self._exchange.fetch_ticker(symbol)  # type: ignore[union-attr]
        return {
            "last": ticker.get("last", 0),
            "bid": ticker.get("bid", 0),
            "ask": ticker.get("ask", 0),
            "timestamp": ticker.get("timestamp", 0),
        }

    async def get_min_quantity(self, symbol: str) -> float:
        self._check_connected()
        try:
            market = await self._exchange.load_markets()  # type: ignore[union-attr]
            info = market.get(symbol, {})
            limits = info.get("limits", {}).get("amount", {})
            return float(limits.get("min", 0.001))
        except Exception:
            return 0.001


class ExchangeAdapterFactory:
    """交易所适配器工厂。

    根据 exchange_name 创建对应的 adapter 实例。
    目前所有 HMAC-SHA256 CEX 使用 CcxtCexAdapter。
    Hyperliquid 等特殊交易所使用独立 adapter。
    """

    _adapters: Dict[str, type] = {}

    @classmethod
    def register(cls, exchange_name: str, adapter_class: type):
        """注册自定义 adapter"""
        cls._adapters[exchange_name] = adapter_class

    @classmethod
    def create(
        cls,
        exchange_name: str,
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        testnet: bool = True,
    ) -> ExchangeAdapter:
        """创建交易所适配器实例。

        Args:
            exchange_name: 交易所名称 (binance/bybit/okx/...)
            api_key: API 密钥
            api_secret: API 密钥
            api_passphrase: 密码短语（OKX/KuCoin 需要）
            testnet: 是否使用测试网

        Returns:
            ExchangeAdapter 实例
        """
        adapter_class = cls._adapters.get(exchange_name, CcxtCexAdapter)
        if exchange_name == "bitunix":
            adapter_class = BitunixAdapter
        return adapter_class(
            exchange_name=exchange_name,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
            testnet=testnet,
        )
