#!/usr/bin/env python3
"""
DMC TradingView Bridge — 配置校驗腳本

在啟動前檢查配置是否正確，給出明確的錯誤提示。

用法：
  python -m sisie_bridge.check_config
"""

import os
import sys
import re

CHECKS_PASSED = 0
CHECKS_WARN = 0
CHECKS_FAILED = 0


def ok(msg: str):
    global CHECKS_PASSED
    CHECKS_PASSED += 1
    print(f"  ✅ {msg}")


def warn(msg: str):
    global CHECKS_WARN
    CHECKS_WARN += 1
    print(f"  ⚠️  {msg}")


def fail(msg: str):
    global CHECKS_FAILED
    CHECKS_FAILED += 1
    print(f"  ❌ {msg}")


def main():
    global CHECKS_PASSED, CHECKS_WARN, CHECKS_FAILED

    print("\n🔍 DMC TradingView Bridge — 配置检查\n")

    # ── Auth Token ──
    print("[鉴权]")
    token = os.environ.get("TV_BRIDGE_AUTH_TOKEN", "")
    if not token:
        fail("TV_BRIDGE_AUTH_TOKEN 未设置！")
    elif token == "change-me-to-a-random-token":
        fail("TV_BRIDGE_AUTH_TOKEN 是示例值，请修改为随机字符串！")
    elif len(token) < 16:
        warn("TV_BRIDGE_AUTH_TOKEN 长度建议至少 16 位")
    else:
        ok(f"TV_BRIDGE_AUTH_TOKEN 已设置 ({len(token)} 位)")

    # ── Exchanges ──
    print("\n[交易所]")
    exchanges_found = 0
    supported = ["binance", "bybit", "okx", "gate", "bitget", "kucoin", "bitunix"]

    for ex in supported:
        key = os.environ.get(f"TV_BRIDGE_{ex.upper()}_API_KEY", "")
        secret = os.environ.get(f"TV_BRIDGE_{ex.upper()}_API_SECRET", "")
        if key and secret:
            testnet = os.environ.get(f"TV_BRIDGE_{ex.upper()}_TESTNET", "true")
            exchanges_found += 1
            mode = "TESTNET" if testnet.lower() == "true" else "PRODUCTION"
            ok(f"{ex.upper()}: API Key 已配置 ({mode})")
        elif key or secret:
            fail(f"{ex.upper()}: 配置不完整（缺少 Key 或 Secret）")
        else:
            pass  # 未配置不報警

    if exchanges_found == 0:
        fail("未检测到任何交易所 API Key！至少需要配置一个交易所")

    # ── OKX Passphrase ──
    if os.environ.get("TV_BRIDGE_OKX_API_KEY", ""):
        pp = os.environ.get("TV_BRIDGE_OKX_API_PASSPHRASE", "")
        if not pp:
            fail("OKX 需要 API Passphrase，请设置 TV_BRIDGE_OKX_API_PASSPHRASE")
        else:
            ok("OKX Passphrase 已配置")

    # ── Emergency Stop ──
    print("\n[安全]")
    estop = os.environ.get("TV_BRIDGE_EMERGENCY_STOP", "false")
    if estop.lower() == "true":
        warn("EMERGENCY STOP 已开启！所有交易将被阻止")
    else:
        ok("Emergency Stop 未开启")

    # ── Python version ──
    print("\n[环境]")
    v = sys.version_info
    if v.major >= 3 and v.minor >= 10:
        ok(f"Python {v.major}.{v.minor}.{v.micro} (≥3.10)")
    else:
        fail(f"Python {v.major}.{v.minor}.{v.micro}，需要 ≥3.10")

    # ── Summary ──
    print(f"\n{'='*40}")
    print(f"  通过: {CHECKS_PASSED}  |  警告: {CHECKS_WARN}  |  失败: {CHECKS_FAILED}")
    print(f"{'='*40}\n")

    return 0 if CHECKS_FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
