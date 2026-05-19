"""
Bitunix Native REST Adapter

Bitunix 不在 CCXT 中，用原生 HTTP 实现 ExchangeAdapter 接口。
API: https://openapi.bitunix.com

Auth: double SHA256 (nonce + timestamp + api_key + query + body → SHA256 → + secret → SHA256)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Dict, List, Optional
from urllib.parse import urlencode

import aiohttp

from sisie_bridge.core.exchanges.base import ExchangeAdapter
from sisie_bridge.core.models.order import (
    OrderRequest, OrderResult, OrderSide, OrderStatus, LivePosition,
)

log = logging.getLogger(__name__)

BITUNIX_BASE = "https://openapi.bitunix.com"
BITUNIX_FUTURES = "/api/v1/futures"


class BitunixAdapter(ExchangeAdapter):
    """Bitunix 期货原生适配器"""

    def __init__(
        self,
        exchange_name: str = "bitunix",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        testnet: bool = True,
    ):
        super().__init__(exchange_name, testnet)
        self.api_key = api_key
        self.api_secret = api_secret
        self._session: Optional[aiohttp.ClientSession] = None
        self._base = BITUNIX_BASE

    def _sign(self, nonce: str, timestamp: str, query: str = "", body: str = "") -> str:
        """Bitunix 双 SHA256 签名"""
        msg = f"{nonce}{timestamp}{self.api_key}{query}{body}"
        digest = hashlib.sha256(msg.encode()).hexdigest()
        return hashlib.sha256((digest + self.api_secret).encode()).hexdigest()

    def _headers(self, query: str = "", body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex[:32]
        return {
            "API-KEY": self.api_key,
            "API-TIMESTAMP": ts,
            "API-NONCE": nonce,
            "API-SIGN": self._sign(nonce, ts, query, body),
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, params: dict = None, data: dict = None) -> dict:
        if not self._session:
            self._session = aiohttp.ClientSession()

        url = f"{self._base}{path}"
        query_str = urlencode(sorted((params or {}).items())) if params else ""
        body_str = json.dumps(data) if data else ""
        headers = self._headers(query_str, body_str)

        full_url = f"{url}?{query_str}" if query_str else url

        async with self._session.request(
            method, full_url, headers=headers, json=data,
        ) as resp:
            result = await resp.json()
            if isinstance(result, dict) and result.get("code") != 0:
                raise Exception(f"Bitunix error {result.get('code')}: {result.get('msg', '')}")
            return result

    # ═════════════════════════════════════════════════════════════
    # ExchangeAdapter implementation
    # ═════════════════════════════════════════════════════════════

    async def connect(self) -> bool:
        try:
            self._session = aiohttp.ClientSession()
            await self._request("GET", f"{BITUNIX_FUTURES}/account/info")
            log.info(f"[{self.exchange_name}] 连接成功")
            return True
        except Exception as e:
            log.error(f"[{self.exchange_name}] 连接失败: {e}")
            return False

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def health_check(self) -> bool:
        try:
            await self._request("GET", f"{BITUNIX_FUTURES}/market/time")
            return True
        except Exception:
            return False

    async def place_order(self, order: OrderRequest) -> OrderResult:
        data = {
            "symbol": order.symbol.replace("/USDT:USDT", "USDT"),
            "side": 1 if order.side == OrderSide.BUY else 2,
            "orderType": 2 if order.order_type == "market" else 1,
            "volume": str(order.quantity),
            "price": str(order.price) if order.price else "0",
        }
        try:
            raw = await self._request("POST", f"{BITUNIX_FUTURES}/trade/place_order", data=data)
            order_data = raw.get("data", {})
            return OrderResult(
                success=True,
                exchange_order_id=str(order_data.get("orderId", "")),
                status=OrderStatus.OPEN,
                raw_response=raw,
            )
        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    async def cancel_order(self, symbol: str, order_id: str) -> OrderResult:
        data = {"symbol": symbol.replace("/USDT:USDT", "USDT"), "orderId": order_id}
        try:
            raw = await self._request("POST", f"{BITUNIX_FUTURES}/trade/cancel_order", data=data)
            return OrderResult(success=True, exchange_order_id=order_id,
                             status=OrderStatus.CANCELED, raw_response=raw)
        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    async def get_order(self, symbol: str, order_id: str) -> OrderResult:
        params = {"symbol": symbol.replace("/USDT:USDT", "USDT"), "orderId": order_id}
        try:
            raw = await self._request("GET", f"{BITUNIX_FUTURES}/trade/order_detail", params=params)
            od = raw.get("data", {})
            status_map = {"0": OrderStatus.OPEN, "1": OrderStatus.PARTIALLY_FILLED,
                         "2": OrderStatus.FILLED, "3": OrderStatus.CANCELED}
            return OrderResult(
                success=True, exchange_order_id=order_id,
                status=status_map.get(str(od.get("state", 0)), OrderStatus.OPEN),
                filled_qty=float(od.get("executedQty", 0) or 0),
                avg_price=float(od.get("avgPrice", 0) or 0),
                raw_response=raw,
            )
        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    async def get_position(self, symbol: str) -> Optional[LivePosition]:
        sym = symbol.replace("/USDT:USDT", "USDT")
        try:
            raw = await self._request("GET", f"{BITUNIX_FUTURES}/account/positions",
                                      params={"symbol": sym})
            positions = raw.get("data", [])
            for p in positions:
                qty = abs(float(p.get("positionVolume", 0) or 0))
                if qty < 1e-8:
                    continue
                return LivePosition(
                    exchange=self.exchange_name, symbol=sym,
                    side=OrderSide.BUY if int(p.get("positionSide", 0)) == 1 else OrderSide.SELL,
                    quantity=qty,
                    entry_price=float(p.get("avgPrice", 0) or 0),
                    unrealized_pnl=float(p.get("unrealizedPnl", 0) or 0),
                )
            return None
        except Exception:
            return None

    async def get_all_positions(self) -> List[LivePosition]:
        try:
            raw = await self._request("GET", f"{BITUNIX_FUTURES}/account/positions")
            results = []
            for p in raw.get("data", []):
                qty = abs(float(p.get("positionVolume", 0) or 0))
                if qty < 1e-8:
                    continue
                results.append(LivePosition(
                    exchange=self.exchange_name,
                    symbol=p.get("symbol", ""),
                    side=OrderSide.BUY if int(p.get("positionSide", 0)) == 1 else OrderSide.SELL,
                    quantity=qty,
                    entry_price=float(p.get("avgPrice", 0) or 0),
                    unrealized_pnl=float(p.get("unrealizedPnl", 0) or 0),
                ))
            return results
        except Exception as e:
            log.error(f"[{self.exchange_name}] 获取持仓失败: {e}")
            return []

    async def close_position(self, symbol: str) -> OrderResult:
        pos = await self.get_position(symbol)
        if pos is None:
            return OrderResult(success=True, status=OrderStatus.FILLED)
        order = OrderRequest(
            exchange=self.exchange_name, symbol=symbol,
            side=OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY,
            order_type="market", quantity=pos.quantity, reduce_only=True,
        )
        return await self.place_order(order)

    async def get_balance(self, asset: str = "USDT") -> float:
        try:
            raw = await self._request("GET", f"{BITUNIX_FUTURES}/account/info")
            data = raw.get("data", {})
            return float(data.get("availableBalance", 0) or 0)
        except Exception:
            return 0.0

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self._request("POST", f"{BITUNIX_FUTURES}/trade/adjust_leverage",
                              data={"symbol": symbol.replace("/USDT:USDT", "USDT"),
                                    "leverage": str(leverage)})
            return True
        except Exception:
            return False

    async def get_ticker(self, symbol: str) -> dict:
        sym = symbol.replace("/USDT:USDT", "USDT")
        raw = await self._request("GET", f"{BITUNIX_FUTURES}/market/last_price",
                                  params={"symbol": sym})
        price = float(raw.get("data", {}).get("price", 0) or 0)
        return {"last": price, "bid": price, "ask": price, "timestamp": int(time.time() * 1000)}

    async def get_min_quantity(self, symbol: str) -> float:
        return 0.001
