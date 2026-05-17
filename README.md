# 🦞 Sisie Strategy Bridge

TradingView Webhook → Multi-Exchange Trade Execution Bridge

独立部署的单用户策略桥接器。接收 TradingView Alert，风控检查后在 Binance/Bybit/OKX 等交易所实盘下单。

---

## 快速开始

### 前置要求
- Python 3.10+
- 至少一个交易所的 testnet API key（[Binance testnet](https://testnet.binancefuture.com) 推荐）

### 1. 安装

```bash
git clone <repo-url>
cd sisie-strategy-bridge
python3 -m venv venv
source venv/bin/activate
pip install -e /path/to/sisie-core    # sisie-core 共享库
pip install -e .                      # Bridge 本体
```

### 2. 配置

```bash
# 编辑 configs/bridge.yaml 配置
cp deploy/bridge.env.example deploy/bridge.env
# 编辑 deploy/bridge.env，填入你的 API key 和 token
```

**最小配置**（只开 Binance testnet）：
```bash
export TV_BRIDGE_AUTH_TOKEN="your-random-token-here"
export TV_BRIDGE_BINANCE_API_KEY="your-binance-testnet-key"
export TV_BRIDGE_BINANCE_API_SECRET="your-binance-testnet-secret"
export TV_BRIDGE_BINANCE_TESTNET="true"
```

### 3. 启动

```bash
source deploy/bridge.env
python -m sisie_bridge.main --config configs/bridge.yaml
```

### 4. 验证

```bash
# 健康检查
curl http://localhost:8080/health

# Dashboard (浏览器打开)
open http://localhost:8080/dashboard

# 测试信号
curl -X POST "http://localhost:8080/webhook?token=your-token" \
  -H "Content-Type: application/json" \
  -d '{"strategy_id":"btp_30m","action":"buy","symbol":"ETH/USDT:USDT","exchange":"binance"}'
```

---

## 生产部署

### Docker
```bash
docker build -t sisie-bridge .
docker run -d --env-file deploy/bridge.env -p 8443:8080 sisie-bridge
```

### Systemd
```bash
cp deploy/bridge.service /etc/systemd/system/
sudo cp deploy/bridge.env /etc/sisie-bridge.env
sudo chmod 600 /etc/sisie-bridge.env
sudo systemctl enable --now sisie-bridge
```

### Nginx 反向代理
```bash
cp deploy/nginx.conf.example /etc/nginx/sites-enabled/bridge
sudo nginx -t && sudo systemctl reload nginx
```

---

## TradingView 集成

详见 [docs/tv_webhook_integration_spec.md](docs/tv_webhook_integration_spec.md)

Webhook URL: `https://your-server/webhook?token=<TV_BRIDGE_AUTH_TOKEN>`

支持的 Alert 消息格式：
```json
{
  "strategy_id": "btp_30m",
  "action": "buy",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance"
}
```

---

## 支持的交易所

| 交易所 | Testnet | 生产 |
|--------|---------|------|
| Binance | ✅ | ✅ |
| Bybit | ✅ | ✅ |
| OKX | ✅ | ✅ |
| Gate.io | ✅ | ✅ |
| Bitget | ✅ | ✅ |
| KuCoin | — | ✅ |
| Bitunix | — | ✅ |

---

## 架构

```
TradingView Alert → Webhook (/webhook)
  → 鉴权 (token)
  → 信号标准化 (signal_normalizer)
  → 风控 (risk/manager: Emergency Stop / 频率限制 / 仓位上限 / 杠杆上限)
  → 状态机 (main.py: 开仓/平仓/反手/部分平)
  → 下单 (exchanges/ adapter → CCXT)
  → 确认成交 → 更新本地状态 (SQLite)
  → 对账 (state/reconciler, 5min 间隔)
```

共享核心：[sisie-core](https://github.com/your-org/sisie-core) — models / exchanges / risk / config

---

## 许可证

[MIT](LICENSE)
