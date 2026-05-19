"""
live_bridge 全局配置

支持 YAML 文件 + 环境变量覆盖（敏感信息用 env）。
"""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml


def _env(key: str, default: str = "") -> str:
    """读取环境变量，自动去除 \r 和空白"""
    val = os.getenv(key, default)
    return val.strip().rstrip("\r") if val else default


@dataclass
class ExchangeCredentials:
    """单个交易所的 API 凭证"""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""   # OKX / KuCoin 等需要
    testnet: bool = True       # 默认 testnet


@dataclass
class ExchangeConfig:
    """单个交易所的配置"""
    enabled: bool = False
    credentials: ExchangeCredentials = field(default_factory=ExchangeCredentials)
    rate_limit_rps: float = 5.0      # 每秒最大请求数
    symbols: List[str] = field(default_factory=list)   # 允许的交易对


@dataclass
class StrategyConfig:
    """单个策略的配置"""
    strategy_id: str = ""
    exchange: str = ""                # 路由到哪个交易所
    symbol: str = ""                  # 交易对（交易所格式，如 BTC/USDT:USDT）
    max_position_usdt: float = 0      # 单策略最大仓位
    allow_reverse: bool = True        # 是否允许反手
    default_leverage: int = 1
    signal_cooldown_sec: float = 5.0  # 信号冷却（防刷）


@dataclass
class RiskConfig:
    """全局风控配置"""
    global_max_positions: int = 3
    global_max_usdt: float = 1000
    daily_max_loss_usdt: float = 100
    emergency_stop: bool = False
    max_leverage_ratio: float = 5.0
    require_confirmation: bool = False


@dataclass
class DiscordConfig:
    """Discord 推送配置"""
    signals_channel: str = ""
    fills_channel: str = ""
    alerts_channel: str = ""


@dataclass
class BridgeConfig:
    """TV Bridge 总配置"""
    webhook_port: int = 8080
    webhook_auth_token: str = ""       # TV webhook 鉴权 token
    state_db_path: str = "data/live_bridge_state.db"
    exchanges: Dict[str, ExchangeConfig] = field(default_factory=dict)
    strategies: Dict[str, StrategyConfig] = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)

    @classmethod
    def from_yaml(cls, path: str) -> "BridgeConfig":
        """从 YAML 文件加载配置，环境变量覆盖敏感字段"""
        with open(path) as f:
            raw = yaml.safe_load(f) or {}

        # 环境变量覆盖交易所凭证
        for ex_name, ex_cfg in (raw.get("exchanges") or {}).items():
            prefix = f"TV_BRIDGE_{ex_name.upper()}"
            creds = ex_cfg.get("credentials", {})
            if _env(f"{prefix}_API_KEY"):
                creds["api_key"] = _env(f"{prefix}_API_KEY")
            if _env(f"{prefix}_API_SECRET"):
                creds["api_secret"] = _env(f"{prefix}_API_SECRET")
            if _env(f"{prefix}_API_PASSPHRASE"):
                creds["api_passphrase"] = _env(f"{prefix}_API_PASSPHRASE")
            creds["testnet"] = _env(f"{prefix}_TESTNET", str(creds.get("testnet", True))).lower() == "true"

        risk = raw.get("risk", {})
        risk["emergency_stop"] = _env("TV_BRIDGE_EMERGENCY_STOP", str(risk.get("emergency_stop", False))).lower() == "true"

        # 转换嵌套 dict → dataclass
        exchanges = {}
        for k, v in (raw.get("exchanges") or {}).items():
            creds_raw = v.get("credentials", {})
            v["credentials"] = ExchangeCredentials(**creds_raw)
            exchanges[k] = ExchangeConfig(**v)

        strategies = {}
        for k, v in (raw.get("strategies") or {}).items():
            strategies[k] = StrategyConfig(**v)

        return cls(
            webhook_port=raw.get("webhook_port", 8080),
            webhook_auth_token=_env("TV_BRIDGE_AUTH_TOKEN", raw.get("webhook_auth_token", "")),
            state_db_path=raw.get("state_db_path", "data/live_bridge_state.db"),
            exchanges=exchanges,
            strategies=strategies,
            risk=RiskConfig(**risk),
            discord=DiscordConfig(**(raw.get("discord") or {})),
        )


# ─────────────────────────────────────────────────────────────────────
# 默认 Tier 1 交易所的 CCXT ID 映射
# ─────────────────────────────────────────────────────────────────────

EXCHANGE_CCXT_IDS = {
    "binance":    "binanceusdm",
    "bybit":      "bybit",
    "okx":        "okx",
    "gate":       "gate",
    "bitget":     "bitget",
    "kucoin":     "kucoinfutures",
    "mexc":       "mexc",
    "bingx":      "bingx",
    "htx":        "htx",
    "bitunix":    "bitunix",
}

TIER1_EXCHANGES = {"binance", "bybit", "okx", "gate"}


def resolve_ccxt_id(exchange: str) -> str:
    """交易所内部名 → CCXT exchange ID"""
    return EXCHANGE_CCXT_IDS.get(exchange, exchange)
