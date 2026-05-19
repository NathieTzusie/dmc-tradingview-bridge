"""
FastAPI Webhook Server

接收 TradingView 的 HTTP POST,验证 → 标准化 → 风控 → 仓位管理 → 下单。

v0.2 改进:
  - 原子幂等 (try_claim_signal)
  - 全端点鉴权
  - 仓位对账端点
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pathlib import Path

from sisie_bridge.core.config import BridgeConfig
from sisie_bridge.signal_normalizer import normalize_tradingview_webhook
from sisie_bridge.core.risk.manager import RiskManager
from sisie_bridge.state.manager import PositionStateManager
from sisie_bridge.core.models.signal import InternalSignal

log = logging.getLogger(__name__)


def create_app(
    config: BridgeConfig,
    risk_manager: RiskManager,
    state_manager: PositionStateManager,
    signal_handler,              # callable: (InternalSignal) -> dict
    reconciler=None,             # Optional[PositionReconciler]
    all_adapters: dict | None = None,     # 所有 enabled 交易所（含连接失败的）
    connected_adapters: dict | None = None,  # 实际执行交易所（仅已连接的）
) -> FastAPI:
    """创建 FastAPI 应用。"""
    app = FastAPI(
        title="DMC TV Bridge",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
    )

    def _require_auth(request: Request):
        """对所有非 webhook 端点鉴权"""
        if config.webhook_auth_token:
            auth = request.headers.get("X-Auth-Token", "")
            if auth != config.webhook_auth_token:
                raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/health")
    async def health():
        """健康检查（公开）"""
        return {
            "status": "ok",
            "emergency_stop": config.risk.emergency_stop,
            "active_positions": state_manager.count_active_positions(),
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        """Web 仪表盘"""
        dash_path = Path(__file__).parent / "dashboard.html"
        return dash_path.read_text(encoding="utf-8")

    @app.get("/api/dashboard")
    async def api_dashboard(request: Request):
        """仪表盘数据 API"""
        _require_auth(request)
        
        # 交易所状态（包含连接失败的交易所）
        exchanges = {}
        adapter_source = all_adapters or (reconciler.adapters if reconciler else {})
        for ex_name, adapter in (adapter_source or {}).items():
            if not adapter.connected:
                exchanges[ex_name] = {
                    "connected": False, "balance": 0,
                    "error_message": adapter.last_error[:120] if adapter.last_error else "连接失败",
                }
                continue
            try:
                bal = await adapter.get_balance("USDT")
                exchanges[ex_name] = {
                    "connected": True, "balance": bal, "error_message": "",
                }
            except Exception as e:
                exchanges[ex_name] = {
                    "connected": False, "balance": 0, "error_message": str(e)[:120],
                }
        
        # 持仓
        positions = state_manager.get_all_positions()
        
        # 最近交易
        trades = []
        try:
            rows = state_manager._conn.execute(
                "SELECT * FROM trade_history ORDER BY id DESC LIMIT 20"
            ).fetchall()
            cols = [d[1] for d in state_manager._conn.execute("PRAGMA table_info(trade_history)").fetchall()]
            for row in rows:
                trades.append(dict(zip(cols, row)))
        except Exception:
            pass
        
        # 对账
        reconciliation = {}
        if reconciler:
            last = reconciler.last_results()
            for ex_name, r in last.items():
                reconciliation[ex_name] = {
                    "is_clean": r.is_clean,
                    "matched": r.matched,
                    "local_only": len(r.local_only),
                    "exchange_only": len(r.exchange_only),
                    "mismatched": len(r.mismatched),
                }
        
        # 构建持仓详情（含开仓时间、杆杆、保证金）
        positions_detail = []
        for p in positions:
            strat_cfg = config.strategies.get(p.strategy_id)
            leverage = strat_cfg.default_leverage if strat_cfg else 1
            margin = round(p.quantity * p.entry_price / max(leverage, 1), 2)
            positions_detail.append({
                "strategy_id": p.strategy_id,
                "exchange": p.exchange,
                "symbol": p.symbol,
                "side": p.side.value,
                "quantity": p.quantity,
                "entry_price": p.entry_price,
                "entry_time": p.entry_time.isoformat() if p.entry_time else "",
                "leverage": leverage,
                "margin": margin,
                "realized_pnl": p.realized_pnl,
            })

        return {
            "emergency_stop": config.risk.emergency_stop,
            "active_positions": len(positions),
            "signals_total": state_manager.count_processed_signals(),
            "daily_pnl": state_manager.get_daily_pnl(),
            "exchanges": exchanges,
            "positions": positions_detail,
            "recent_trades": trades,
            "reconciliation": reconciliation,
        }
    
    @app.post("/webhook")
    async def webhook(request: Request):
        """TradingView Webhook 入口。

        鉴权（任选一种即可）：
          1. Header: X-Auth-Token: <token>
          2. URL param: /webhook?token=<token>  (← TradingView 推荐方式)
          3. Body JSON: {"token": "<token>", ...}
        Body: JSON (TradingView alert message)
        """
        if config.webhook_auth_token:
            # 1. Header
            auth = request.headers.get("X-Auth-Token", "")
            # 2. URL query param
            if not auth:
                auth = request.query_params.get("token", "")
            # 3. Body 中的 token（需要先读 body，下面统一处理）
            if not auth:
                try:
                    _peek = await request.body()
                    import json as _json
                    _body_data = _json.loads(_peek)
                    auth = _body_data.get("token", "")
                except Exception:
                    pass
            if auth != config.webhook_auth_token:
                raise HTTPException(status_code=401, detail="Unauthorized")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        log.info(f"[Webhook] 收到信号: {payload.get('strategy_id', '?')}")

        signal = normalize_tradingview_webhook(payload, config)
        if signal is None:
            raise HTTPException(status_code=400, detail="无法解析信号格式")

        # 原子幂等
        if not state_manager.try_claim_signal(signal.signal_id):
            log.info(f"[Webhook] 重复信号: {signal.signal_id}")
            return JSONResponse({
                "status": "duplicate",
                "signal_id": signal.signal_id,
                "message": "信号已处理过",
            })

        # 处理信号（风控在 SignalHandler 内统一执行，含名义价值检查）
        result = await signal_handler(signal)
        state_manager.mark_processed(signal.signal_id, result.get("status", "unknown"))

        return JSONResponse(result)

    @app.get("/state")
    async def state(request: Request):
        """查询当前状态(需鉴权)"""
        _require_auth(request)
        positions = state_manager.get_all_positions()
        result = {
            "active_positions": [
                {
                    "strategy_id": p.strategy_id,
                    "exchange": p.exchange,
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "realized_pnl": p.realized_pnl,
                }
                for p in positions
            ],
            "total_positions": len(positions),
            "emergency_stop": config.risk.emergency_stop,
            "daily_pnl": state_manager.get_daily_pnl(),
        }

        # 对账状态
        if reconciler:
            last = reconciler.last_results()
            result["reconciliation"] = {
                ex: {
                    "is_clean": r.is_clean,
                    "matched": r.matched,
                    "local_only": len(r.local_only),
                    "exchange_only": len(r.exchange_only),
                    "mismatched": len(r.mismatched),
                    "timestamp": r.timestamp.isoformat(),
                }
                for ex, r in last.items()
            }

        return result

    @app.post("/admin/emergency-stop")
    async def emergency_stop(request: Request):
        """一键紧急停止(需鉴权)"""
        _require_auth(request)
        config.risk.emergency_stop = True
        risk_manager.update_config(emergency_stop=True)
        log.warning("⚠️ EMERGENCY STOP 已激活!开仓被阻止,平仓仍然放行")
        return {"status": "emergency_stop_activated"}

    @app.post("/admin/resume")
    async def resume(request: Request):
        """恢复交易(需鉴权)"""
        _require_auth(request)
        config.risk.emergency_stop = False
        risk_manager.update_config(emergency_stop=False)
        log.warning("✅ EMERGENCY STOP 已解除")
        return {"status": "resumed"}

    @app.post("/admin/reconcile")
    async def reconcile(request: Request):
        """手动触发对账(需鉴权)"""
        _require_auth(request)
        if not reconciler:
            return {"status": "error", "reason": "对账器未启用"}

        results = await reconciler.reconcile_all()
        return {
            "status": "ok",
            "results": {
                ex: {
                    "is_clean": r.is_clean,
                    "matched": r.matched,
                    "local_only": len(r.local_only),
                    "exchange_only": len(r.exchange_only),
                    "mismatched": len(r.mismatched),
                    "has_discrepancy": r.has_discrepancy,
                }
                for ex, r in results.items()
            },
        }

    @app.post("/admin/exchange-config")
    async def exchange_config(request: Request):
        """热更新交易所配置 + 重新连接"""
        _require_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        ex_name = body.get("exchange", "")
        if not ex_name:
            raise HTTPException(status_code=400, detail="缺少 exchange 字段")

        # 更新配置对象
        from sisie_bridge.core.config import ExchangeConfig, ExchangeCredentials
        ex_cfg = config.exchanges.get(ex_name) or ExchangeConfig()
        ex_cfg.enabled = True
        ex_cfg.credentials = ExchangeCredentials(
            api_key=body.get("api_key", ""),
            api_secret=body.get("api_secret", ""),
            api_passphrase=body.get("api_passphrase", ""),
            testnet=body.get("testnet", True),
        )
        config.exchanges[ex_name] = ex_cfg

        # 创建新 adapter 并尝试连接
        from sisie_bridge.core.exchanges.ccxt_adapter import ExchangeAdapterFactory
        new_adapter = ExchangeAdapterFactory.create(
            exchange_name=ex_name,
            api_key=ex_cfg.credentials.api_key,
            api_secret=ex_cfg.credentials.api_secret,
            api_passphrase=ex_cfg.credentials.api_passphrase,
            testnet=ex_cfg.credentials.testnet,
        )
        connected = await new_adapter.connect()
        if all_adapters is not None:
            all_adapters[ex_name] = new_adapter

        return {
            "status": "ok",
            "exchange": ex_name,
            "connected": connected,
            "error": new_adapter.last_error if not connected else "",
        }

    @app.post("/admin/close-position")
    async def close_position_manual(request: Request):
        """手动平仓（Dashboard 按钒触发）"""
        _require_auth(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        strategy_id = body.get("strategy_id")
        exchange = body.get("exchange")
        symbol = body.get("symbol")
        if not all([strategy_id, exchange, symbol]):
            raise HTTPException(status_code=400, detail="缺少 strategy_id/exchange/symbol")

        from sisie_bridge.core.models.signal import InternalSignal, SignalAction, OrderType, QuantityMode
        import uuid
        sig = InternalSignal(
            strategy_id=strategy_id,
            exchange=exchange,
            symbol=symbol,
            action=SignalAction.CLOSE,
            order_type=OrderType.MARKET,
            source="manual",
            source_timestamp="",
            signal_id=f"manual_{uuid.uuid4().hex[:8]}",
        )
        # 平仓信号不过幼影检查，直接执行
        result = await signal_handler(sig)
        return result

    return app
