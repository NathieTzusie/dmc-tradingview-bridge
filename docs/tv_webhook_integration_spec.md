# TradingView Webhook 集成规范

> TV Bridge 支持的完整 Webhook 消息格式、字段说明、策略 Alert 撰写规范

---

## 一、Webhook 入口

| 字段 | 值 |
|------|-----|
| URL | `https://www.dmc-trader.com/webhook?token=<TV_BRIDGE_AUTH_TOKEN>` |
| Method | POST |
| Content-Type | `application/json` |

**鉴权**（任选一种）：
1. URL query `?token=xxx`（推荐，TradingView 不支持自定义 Header）
2. Header `X-Auth-Token: xxx`（curl/编程使用）
3. Body JSON `"token": "xxx"`（备选）

---

## 二、消息格式规范

### 2.1 必填字段

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `strategy_id` | string | 策略唯一标识，必须匹配 `live_bridge.yaml` 中的策略 ID | `"btp_30m"` |
| `action` | string | 信号动作，见下方合法值 | `"buy"` |
| `symbol` | string | 交易对（交易所标准格式） | `"ETH/USDT:USDT"` |
| `exchange` | string | 目标交易所 | `"binance"` |

#### `action` 合法值

| 值 | 含义 | 触发行为 |
|----|------|----------|
| `buy` | 开多（空仓时反手开多） | 无仓→直接开多；有空仓→平空+开多 |
| `sell` | 开空（多仓时反手开空） | 无仓→直接开空；有多仓→平多+开空 |
| `close` | 平仓（系统自动识别方向） | 按策略当前仓位方向平仓，不影响其他策略 |
| `exit` | 同 `close`（别名） | 同上 |
| `reduce` | 部分平仓 | 按 `quantity` 减少仓位（不填则平一半） |
| `set_sl_tp` | 更新止损止盈 | 仅更新止损止盈价，不下开/平仓单 |

### 2.2 可选字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `quantity` | float | 自动计算 | 合约数量（不填则按策略配置 `max_position_usdt` / 当前价 自动算） |
| `order_type` | string | `"market"` | `"market"` 或 `"limit"` |
| `price` | float | — | 限价单价格（`order_type=limit` 时必填） |
| `stop_loss` | float | — | 止损价（下保护单用） |
| `take_profit` | float | — | 止盈价（下保护单用） |
| `allow_reverse` | bool | `true` | 是否允许平反向仓后反手 |
| `reduce_only` | bool | `false` | 是否只减仓不反手 |
| `time` | string | — | 原始信号时间戳（记录用） |

### 2.3 别名兼容

为兼容不同 TV 策略的变量命名，以下别名同样有效：

| 标准字段 | 兼容别名 |
|----------|----------|
| `action` | `data`, `side`, `signal` |
| `symbol` | `ticker` |
| `quantity` | `size` |
| `price` | `close`, `limit_price` |
| `order_type` | `type` |
| `stop_loss` | `sl` |
| `take_profit` | `tp` |

---

## 三、完整 JSON 示例

### 开多（默认参数）
```json
{
  "strategy_id": "btp_30m",
  "action": "buy",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance"
}
```

### 开空 + 止损止盈
```json
{
  "strategy_id": "btp_30m",
  "action": "sell",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "stop_loss": 2100,
  "take_profit": 2300
}
```

### 平仓
```json
{
  "strategy_id": "btp_30m",
  "action": "close",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance"
}
```

### 部分平仓（平一半）
```json
{
  "strategy_id": "runner_5m",
  "action": "reduce",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance"
}
```

### 限价开多
```json
{
  "strategy_id": "btp_30m",
  "action": "buy",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "order_type": "limit",
  "price": 2150
}
```

---

## 四、TradingView Alert 配置规范

### 4.1 Alert 设置参数

| 设置 | 值 |
|------|-----|
| Condition | 策略入场/出场信号变量 |
| Webhook URL | `https://www.dmc-trader.com/webhook?token=xxx` |
| Frequency | **Once Per Bar Close**（K 线收盘后触发一次） |
| Message | 见下方模板 |

### 4.2 TV 变量映射

Pine Script 中可用的 `{{...}}` 模板变量：

| TV 变量 | 字段 | 示例 |
|---------|------|------|
| `{{close}}` | `price` | 当前 K 线收盘价 |
| `{{timenow}}` | `time` | 触发时间（UTC） |
| `{{ticker}}` | `symbol` | 不推荐，建议硬编码 |
| `{{strategy.order.action}}` | `action` | TV 做的动作（可映射到 JSON `action`） |
| `{{strategy.order.comment}}` | — | 策略注释（`strategy.entry(comment=...)`） |
| `{{strategy.position_size}}` | — | 当前持仓大小 |

### 4.3 推荐策略 Alert 模板

#### 方法一：手动 JSON（推荐，全控制）

每个 Alert 的 Message 写死完整 JSON：

**开多 Alert：**
```json
{
  "strategy_id": "btp_30m",
  "action": "buy",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "price": {{close}},
  "time": "{{timenow}}"
}
```

**开空 Alert：**
```json
{
  "strategy_id": "btp_30m",
  "action": "sell",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "price": {{close}},
  "time": "{{timenow}}"
}
```

**平仓 Alert（多空通用）：**
```json
{
  "strategy_id": "btp_30m",
  "action": "close",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "price": {{close}},
  "time": "{{timenow}}"
}
```

#### 方法二：Pine Script `alertcondition`（自动化）

在策略文件底部添加：

```pine
// ── DMC TV Bridge Alerts ──
if longSignal
    alert(
        '{"strategy_id":"btp_30m","action":"buy","symbol":"ETH/USDT:USDT","exchange":"binance","price":' +
        str.tostring(close) + ',"time":"' + str.tostring(time) + '"}',
        alert.freq_once_per_bar_close
    )

if shortSignal
    alert(
        '{"strategy_id":"btp_30m","action":"sell","symbol":"ETH/USDT:USDT","exchange":"binance","price":' +
        str.tostring(close) + ',"time":"' + str.tostring(time) + '"}',
        alert.freq_once_per_bar_close
    )

exitSignal = strategy.position_size[1] != 0 and strategy.position_size == 0
if exitSignal
    alert(
        '{"strategy_id":"btp_30m","action":"close","symbol":"ETH/USDT:USDT","exchange":"binance","price":' +
        str.tostring(close) + ',"time":"' + str.tostring(time) + '"}',
        alert.freq_once_per_bar_close
    )
```

---

## 五、Bridge 内部处理流程

```
TradingView Alert → Webhook POST
    │
    ▼
┌─ 鉴权 ───────────────────┐  token 不匹配 → 401 Unauthorized
│  URL ?token= / Header / Body token
└──────┬───────────────────┘
       ▼
┌─ 幂等检查 ───────────────┐  重复 signal_id → 返回 "duplicate"
│  try_claim_signal(signal_id)
└──────┬───────────────────┘
       ▼
┌─ 信号标准化 ─────────────┐
│  normalize_tradingview_webhook()
│  ↓
│  InternalSignal（统一格式）
└──────┬───────────────────┘
       ▼
┌─ 风控 ───────────────────┐
│  1. Emergency Stop       │  任一不通过 → "rejected"
│  2. 信号频率限制          │
│  3. 全局最大持仓数        │
│  4. 每日最大亏损          │
│  5. 策略最大仓位          │
│  6. 交易所级名义价值上限  │
└──────┬───────────────────┘
       ▼
┌─ 状态机决策 ─────────────┐
│  buy/sell/close/reduce   │
└──────┬───────────────────┘
       ▼
┌─ 下单 ───────────────────┐
│  adapter.place_order()   │
└──────┬───────────────────┘
       ▼
┌─ 确认成交 ───────────────┐
│  轮询订单状态 → 确认 filled
└──────┬───────────────────┘
       ▼
┌─ 更新本地状态 ───────────┐
│  SQLite: strategy_allocations
│  trade_history
└──────────────────────────┘
```

---

## 六、常见问题

### Q: `action: buy` 时已有空仓会发生什么？
A: Bridge 自动先平空、确认成交后再开多（反手流程）。风控在反手时不会把旧仓位计入新开仓上限。

### Q: 不填 `quantity` 会怎样？
A: Bridge 根据策略配置的 `max_position_usdt` ÷ 当前价格自动计算仓位，四舍五入到最小精度。

### Q: 为什么平仓不需要填方向？
A: Bridge 自己知道当前仓位方向。`close` 信号会查 SQLite → 自动用正确的 `reduce_only` 方向平仓。

### Q: `close` 和 `exit` 有什么区别？
A: 没有区别，`exit` 是 `close` 的别名，完全等价。

### Q: 限价单多久会取消？
A: 当前版本无自动取消。限价单挂单后保持 `OPEN` 状态，超时需要手动撤单。

### Q: 我的策略用 BINANCE:ETHUSDT.P 可以吗？
A: 交易所符号需要写入 JSON 的 `symbol` 字段（`ETH/USDT:USDT`）。TradingView 图表上的 `BINANCE:ETHUSDT.P` 只是图表设置，不影响 Webhook。

---

## 七、策略 Checklist

在 TV 中为一个策略添加 Alert 前，确认：

- [ ] `strategy_id` 已在 `live_bridge.yaml` 的 `strategies` 中注册
- [ ] `exchange` 对应的交易所已在 Bridge 中配置并连接
- [ ] `symbol` 格式为 `BASE/QUOTE:SETTLE`（如 `ETH/USDT:USDT`）
- [ ] Alert Frequency = **Once Per Bar Close**
- [ ] Webhook URL 带 `?token=xxx` 鉴权参数
- [ ] 入场和出场 Alert 都已创建（注意 TV 一个 Alert 只能触发一个条件）
- [ ] 反手权限：`allow_reverse: true`（默认）即 TV 连续发 buy→sell 时会自动反手
- [ ] 如果是限价单：必须填 `order_type: "limit"` 和 `price`
