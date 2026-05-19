"""
风控管理器 (Risk Manager)

所有信号在到达 Position Manager 前，必须先过风控。
风控拒绝 → 不下单 + Discord 告警。

检查项：
  1. Emergency Stop（全局开关）
  2. 信号频率限制（防刷）
  3. 单策略最大仓位
  4. 全局最大同时持仓数
  5. 每日最大亏损
"""

from __future__ import annotations
from typing import Any

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from sisie_bridge.core.models.signal import InternalSignal
# PositionStateManager via duck typing — no import

log = logging.getLogger(__name__)


@dataclass
class StrategyRiskConfig:
    """单策略风控参数（从策略配置提取）"""
    strategy_id: str
    max_position_usdt: float = 0.0
    allow_reverse: bool = True
    signal_cooldown_sec: float = 5.0


@dataclass
class RiskCheckResult:
    """风控检查结果"""
    passed: bool
    reason: str = ""
    warn_only: bool = False   # 告警但放行


@dataclass
class RiskConfig:
    """风控参数（可运行时更新）"""
    emergency_stop: bool = False
    global_max_positions: int = 3
    global_max_usdt: float = 1000.0
    daily_max_loss_usdt: float = 100.0
    signal_cooldown_sec: float = 5.0
    max_position_per_strategy_usdt: float = 500.0
    max_leverage_ratio: float = 5.0        # 名义价值 / 可用余额上限（默认 500%）
    require_confirmation: bool = False


class RiskManager:
    """风控管理器"""

    def __init__(
        self,
        config: RiskConfig,
        state: Any,  # duck typing (count_active_positions, get_all_positions, get_daily_pnl)
        strategy_configs: dict | None = None,
    ):
        self.config = config
        self.state = state
        self._strategy_configs: Dict[str, StrategyRiskConfig] = {}
        self._last_signal_time: Dict[str, float] = {}
        if strategy_configs:
            self.set_strategy_configs(strategy_configs)

    def set_strategy_configs(self, configs: dict):
        """设置策略级风控参数"""
        self._strategy_configs = {
            sid: StrategyRiskConfig(
                strategy_id=sid,
                max_position_usdt=cfg.get('max_position_usdt', 0.0),
                allow_reverse=cfg.get('allow_reverse', True),
                signal_cooldown_sec=cfg.get('signal_cooldown_sec', 5.0),
            )
            for sid, cfg in configs.items()
        }

    def update_config(self, **kwargs):
        """运行时更新风控参数（如切换紧急停止）"""
        for k, v in kwargs.items():
            if hasattr(self.config, k):
                setattr(self.config, k, v)
                log.warning(f"[Risk] {k} → {v}")

    def check(
        self, signal: InternalSignal,
        order_value: float = 0.0,
        balance: float = 0.0,
        exchange_existing_notional: float = 0.0,
        is_reverse: bool = False,
    ) -> RiskCheckResult:
        """对 signal 执行完整风控检查。

        平仓/减仓类信号绕过开仓限制（最大持仓数、单策略仓位上限）。
        Emergency Stop 只阻止开仓类信号，平仓/减仓仍然放行。

        Returns:
            RiskCheckResult — passed=True 才能继续下单。
        """
        from sisie_bridge.core.models.signal import SignalAction

        is_closing = signal.action in (SignalAction.CLOSE, SignalAction.REDUCE)
        is_opening = not is_closing

        # ── 0. Emergency Stop ──
        if self.config.emergency_stop:
            if is_opening and signal.action != SignalAction.SET_SL_TP:
                return RiskCheckResult(False, "EMERGENCY_STOP 已激活，开仓被阻止")
            # 平仓/减仓/SL_TP 修改在 Emergency Stop 下放行
            log.warning(f"[Risk] EMERGENCY_STOP 模式下放行 {signal.action.value}: {signal.strategy_id}")

        # ── 1. 信号频率限制（关闭类绕过） ──
        if not is_closing:
            cooldown = self._check_cooldown(signal.strategy_id)
            if not cooldown.passed:
                return cooldown

        # ── 2. 全局最大持仓数（仅开仓检查） ──
        if is_opening:
            max_pos = self._check_max_positions()
            if not max_pos.passed:
                return max_pos

        # ── 3. 每日最大亏损（关闭类绕过，不能阻止平仓） ──
        if is_opening:
            daily_loss = self._check_daily_loss()
            if not daily_loss.passed:
                return daily_loss

        # ── 4. 单策略最大仓位（含本次订单 notional） ──
        # 反手时旧仓位将先被平掉，current_usdt 视为 0
        if is_opening:
            strat_max = self._check_strategy_max(
                signal.strategy_id, order_value, ignore_current=is_reverse
            )
            if not strat_max.passed:
                return strat_max

        # ── 5. 交易所级名义价值上限（已有持仓 + 本次订单 ≤ 余额 × 杠杆比）──
        if is_opening and order_value > 0 and balance > 0:
            max_ok = self._check_leverage_ratio(order_value, balance, exchange_existing_notional)
            if not max_ok.passed:
                return max_ok

        # 更新最后信号时间
        self._last_signal_time[signal.strategy_id] = time.time()

        return RiskCheckResult(True)

    def _check_leverage_ratio(
        self, order_value: float, balance: float, existing_notional: float = 0.0
    ) -> RiskCheckResult:
        """(该交易所已有持仓名义价值 + 本次订单) ≤ 该交易所余额 × max_leverage_ratio"""
        if balance <= 0:
            return RiskCheckResult(True)  # 余额未知时跳过（避免误拒）
        total = existing_notional + order_value
        limit = balance * self.config.max_leverage_ratio
        if total > limit:
            return RiskCheckResult(
                False,
                f"交易所持仓过重：现有 {existing_notional:.0f} + 新单 {order_value:.0f} "
                f"= {total:.0f} USDT 超过上限 {limit:.0f} USDT"
                f"（余额 {balance:.0f} × {self.config.max_leverage_ratio:.0f}x）",
            )
        return RiskCheckResult(True)

    def _check_cooldown(self, strategy_id: str) -> RiskCheckResult:
        """信号频率限制（优先策略级 cooldown，fallback 到全局）"""
        last = self._last_signal_time.get(strategy_id, 0)
        elapsed = time.time() - last
        strat_cfg = self._strategy_configs.get(strategy_id)
        cooldown = strat_cfg.signal_cooldown_sec if strat_cfg else self.config.signal_cooldown_sec
        if elapsed < cooldown:
            return RiskCheckResult(
                False, f"信号频率过高: {elapsed:.1f}s < {cooldown}s"
            )
        return RiskCheckResult(True)

    def _check_max_positions(self) -> RiskCheckResult:
        """全局最大持仓数"""
        current = self.state.count_active_positions()
        max_pos = self.config.global_max_positions
        if current >= max_pos:
            return RiskCheckResult(
                False, f"已达全局最大持仓数: {current}/{max_pos}"
            )
        return RiskCheckResult(True)

    def _check_daily_loss(self) -> RiskCheckResult:
        """每日最大亏损"""
        daily_pnl = self.state.get_daily_pnl()
        limit = -abs(self.config.daily_max_loss_usdt)
        if daily_pnl <= limit:
            return RiskCheckResult(
                False, f"已达每日最大亏损: {daily_pnl:.2f} USDT < {limit:.2f} USDT"
            )
        return RiskCheckResult(True)

    def _check_strategy_max(
        self, strategy_id: str, order_value: float = 0.0, ignore_current: bool = False
    ) -> RiskCheckResult:
        """单策略最大仓位检查。
        ignore_current=True 时（反手场景），旧仓位被计为 0（将先被平掉）。
        """
        strat_cfg = self._strategy_configs.get(strategy_id)
        limit = strat_cfg.max_position_usdt if strat_cfg else self.config.max_position_per_strategy_usdt

        if limit <= 0:
            return RiskCheckResult(True)

        current_usdt = 0.0
        if not ignore_current:
            positions = self.state.get_all_positions()
            for pos in positions:
                if pos.strategy_id == strategy_id:
                    current_usdt = pos.quantity * pos.entry_price
                    break

        if current_usdt + order_value > limit:
            return RiskCheckResult(
                False,
                f"策略 {strategy_id} 开仓后将超最大仓位: "
                f"{current_usdt:.0f} + {order_value:.0f} > {limit:.0f} USDT",
            )
        return RiskCheckResult(True)

    def get_strategy_allow_reverse(self, strategy_id: str) -> bool:
        """获取策略的反手权限"""
        strat_cfg = self._strategy_configs.get(strategy_id)
        return strat_cfg.allow_reverse if strat_cfg else True
