# TV Bridge — Changelog

## v2.1 (2026-05-17) — sisie-core 提取 (Phase 1)

- 提取共享模块到 `/mnt/c/Users/12645/sisie-core/` pip package
- 共享模块：`models/`, `exchanges/`, `risk/`, `config.py`
- live_bridge 删除重复模块，import 全部切换到 `sisie_core.*`
- RiskManager state 参数改为 duck typing（为多数据库做准备）
- sisie-core 通过 `.pth` 文件安装到 venv


---

## v2.0 (2026-05-17) — Dashboard 升级 + 对账优化 + 交易所配置面板

### Bug Fixes
- **信号计数**：Dashboard 风控面板新增「信号已处理」计数（`signal_log` 中 `result != 'claimed'` 的条数）
- **时间格式**：修复交易记录时间显示 `NaNh ago` 问题；所有时间精确到秒（年月日+时分秒）
- **对账宽限期**：新增 60s 宽限期 — 开仓 60 秒内出现 `local_only` 不计入对账差异（避免交易所持仓 API 延迟导致的误报）
- **余额精度**：账户余额显示改为两位小数
- **移除 ticker**：Dashboard 余额区块不再请求/显示 ETH 价格

### Features
- **持仓详情展开**：Dashboard 活跃持仓支持点击行展开，显示开仓时间 / 杠杆倍数 / 占用保证金，并提供「手动平仓」按钮（新增 `POST /admin/close-position`）
- **交易所状态面板重设计**：显示全部 7 个支持的交易所（Binance / Bybit / OKX / Gate.io / Bitget / KuCoin / Bitunix），灰色=未配置，绿色=已连接，橙色=连接失败
- **交易所热配置**：点击交易所可弹窗配置 API Key/Secret/Passphrase，动态重连（新增 `POST /admin/exchange-config`）
- **交易记录展开**：最近交易行可点击展开，显示手续费 / 交易所订单ID / 信号ID / 精确时间；Open 类型交易不再显示 PnL（`—`）

### Files Changed
- `webhook/server.py` — `/api/dashboard` 新增字段，`/admin/close-position`，`/admin/exchange-config`
- `webhook/dashboard.html` — 完全重写（前端状态/交易/持仓/对账全部升级）
- `state/manager.py` — 新增 `count_processed_signals()`
- `state/reconciler.py` — 对账宽限期 60s
- `main.py` — 传递 `connected_adapters` 到 `create_app()`

---

## v1.0 (2026-05-16~17) — 初始可运行版本

### 核心功能
- TradingView Webhook → 多交易所实盘执行桥接器
- 支持 Binance/Bybit/OKX/Gate/Bitget/KuCoin/Bitunix（Binance testnet 已验证）
- 风控：紧急停止 / 信号频率限制 / 策略级仓位上限 / 交易所级杠杆上限 / 每日最大亏损
- 信号幂等处理（`try_claim_signal`）
- 确认成交后更新本地仓位
- 反手先从平仓确认成交再开反向
- 平仓仅用 `reduce_only`，不影响其他策略仓位
- 仓位对账（5分钟间隔）+ Discord 告警
- Web Dashboard（FastAPI + 纯 HTML/JS）

### 架构
- 交易所适配器：CCXT 通用 CEX 适配器 + 工厂模式
- 状态管理：SQLite 持久化（`signal_log` / `exchange_positions` / `strategy_allocations` / `trade_history`）
- 鉴权：3 种方式（Header / URL query / Body token）适配 TradingView 限制
- Nginx 反向代理（443/8444）

### Known Issues (v1.0 → v2.0 已修)
- 信号计数不更新
- 时间显示 `NaNh ago`
- 对账期间短暂 `local_only` 误报
- 余额无小数精度
- 持仓无详细信息
