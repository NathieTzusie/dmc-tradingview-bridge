# 🦞 DMC TradingView Bridge

TradingView Webhook → Multi-Exchange Trade Execution Bridge

**单用户版**— 独立部署，接收 TradingView Alert，风控检查后在 Binance/Bybit/OKX 等交易所实盘下单。

> 无外部依赖，开箱即用。`pip install` + `.env` + 一键启动。

> ⚠️ **安全提醒：** `.env` 文件包含你的交易所 API Key 和 Token，**永遠不要提交到 Git**。
> 默認的 `.gitignore` 已包含 `.env`，但請 double check 不要用 `git add --force` 意外提交。

---

## 🚀 快速开始（30 秒）

### 前置要求
- Python 3.10+
- 一个交易所的 API key（[Binance testnet](https://testnet.binancefuture.com) 推荐新手）

### Option 1: Docker（推荐）

```bash
git clone https://github.com/NathieTzusie/dmc-tradingview-bridge
cd dmc-tradingview-bridge

cp .env.example .env
# 编辑 .env，填入你的 API key 和 Token

docker compose up -d
open http://localhost:8080/dashboard
```

### Option 2: 本地运行

```bash
git clone https://github.com/NathieTzusie/dmc-tradingview-bridge
cd dmc-tradingview-bridge

python3 -m venv venv
source venv/bin/activate
pip install .

cp .env.example .env
# 编辑 .env，填入你的 API key 和 Token

python -m sisie_bridge.main --config configs/bridge.yaml
```

### Option 3: 一键部署（服务器）

```bash
curl -sL https://raw.githubusercontent.com/NathieTzusie/dmc-tradingview-bridge/main/deploy/install.sh | sudo bash
```

安装完成后编辑 `.env`，然后重启服务。

---

## ✅ 验证

```bash
# 健康检查
curl http://localhost:8080/health

# 打开 Dashboard
open http://localhost:8080/dashboard

# 测试信号
curl -X POST "http://localhost:8080/webhook?token=<你的 Token>" \
  -H "Content-Type: application/json" \
  -d '{"strategy_id":"btp_30m","action":"buy","symbol":"ETH/USDT:USDT","exchange":"binance"}'
```

---

## 📋 配置

所有配置通过环境变量（`.env` 文件）进行：

| 变量 | 说明 | 必填 |
|------|------|:----:|
| `TV_BRIDGE_AUTH_TOKEN` | Webhook 鉴权 Token | ✅ |
| `TV_BRIDGE_BINANCE_API_KEY` | Binance API Key | 可选 |
| `TV_BRIDGE_BINANCE_API_SECRET` | Binance API Secret | 可选 |
| `TV_BRIDGE_BINANCE_TESTNET` | Binance Testnet | 可选 |
| `TV_BRIDGE_BYBIT_API_KEY` | Bybit API Key | 可选 |
| `TV_BRIDGE_OKX_API_KEY` | OKX API Key | 可选 |
| `TV_BRIDGE_EMERGENCY_STOP` | 紧急停止开关 | 可选 |

无需编辑 YAML 配置文件即可运行。高级配置参见 `configs/bridge.yaml`。

---

## 📊 Dashboard

Dashboard 在 `http://localhost:8080/dashboard`，功能：

- 📍 交易所实时状态（连接/余额）
- 💰 当前持仓（多交易所汇总）
- 📜 交易历史
- 🛡️ Emergency Stop
- 🔧 交易所 API Key 自助管理

> 手机自动适配，支持移动端访问。

---

## 🔗 TradingView 集成

Webhook URL：
```
https://你的域名/webhook?token=<TV_BRIDGE_AUTH_TOKEN>
```

支持的 Alert 格式：

```json
{
  "strategy_id": "btp_30m",
  "action": "buy",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance"
}
```

支持的 action：`buy` / `sell` / `close` / `reverse` / `reduce`

详见 [docs/tv_webhook_integration_spec.md](docs/tv_webhook_integration_spec.md)

---

## 🏗️ 架构

```
TradingView Alert → Webhook (/webhook)
  → 鉴权 (token)
  → 信号标准化 (signal_normalizer)
  → 风控 (risk/manager: Emergency Stop / 频率限制 / 仓位上限)
  → 状态机 (开仓/平仓/反手/部分平)
  → 下单 (CCXT → 交易所)
  → 确认成交 → 更新本地状态 (SQLite)
  → 对账 (state/reconciler, 5min 间隔)
```

---

## 🏪 支持的交易所

| 交易所 | Testnet | 生产 |
|--------|:-------:|:----:|
| Binance | ✅ | ✅ |
| Bybit | ✅ | ✅ |
| OKX | ✅ | ✅ |
| Gate.io | ✅ | ✅ |
| Bitget | ✅ | ✅ |
| KuCoin | — | ✅ |
| Bitunix | — | ✅ |

---

## 📦 部署清单

部署到生产环境：

1. 配置 SSL（推荐 Nginx + Let's Encrypt）：
   ```bash
   sudo apt install nginx certbot
   sudo certbot --nginx -d 你的域名
   ```
   参考 `deploy/nginx.conf.example`

2. 推荐使用 systemd：
   ```bash
   sudo cp deploy/bridge.service /etc/systemd/system/dmc-bridge.service
   # 编辑 dmc-bridge.service 修正路径
   sudo systemctl enable --now dmc-bridge
   ```

3. 可选：配置 Discord 通知（在 `configs/bridge.yaml` 中设置）

---

## ⚖️ 许可证

MIT License — 自由使用、修改、分发。

---

## 🔗 相关项目

- - [DMC-Sisie-Quantive](https://github.com/NathieTzusie/DMC-Sisie-Quantive) — 量化策略回测框架
- [Sisie-Quantive](https://github.com/NathieTzusie/Sisie-Quantive) — 多用户版策略桥接平台（Sisie 私有）
