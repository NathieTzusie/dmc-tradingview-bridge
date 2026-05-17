# TradingView Alert 模板 — DMC TV Bridge

**Webhook URL：**
```
https://www.dmc-trader.com/webhook?token=<TV_BRIDGE_AUTH_TOKEN>
```

> Alert Frequency 统一设为 **Once Per Bar Close**

---

## BTP_30m（Bollinger Trend Pullback 30m）

### Pine Script — 添加到策略底部

```pine
// ── DMC TV Bridge Webhook Alerts ──
alertcondition(
    longSignal,
    title="BTP30 Long Entry",
    message='{"strategy_id":"btp_30m","action":"buy","symbol":"ETH/USDT:USDT","exchange":"binance","price":{{close}},"time":"{{timenow}}"}'
)

alertcondition(
    shortSignal,
    title="BTP30 Short Entry",
    message='{"strategy_id":"btp_30m","action":"sell","symbol":"ETH/USDT:USDT","exchange":"binance","price":{{close}},"time":"{{timenow}}"}'
)

exitSignal = strategy.position_size[1] != 0 and strategy.position_size == 0
alertcondition(
    exitSignal,
    title="BTP30 Exit",
    message='{"strategy_id":"btp_30m","action":"close","symbol":"ETH/USDT:USDT","exchange":"binance","price":{{close}},"time":"{{timenow}}"}'
)
```

### TV Alert 配置

| Alert | Condition | Timeframe | Message |
|-------|-----------|-----------|---------|
| BTP30 Long Entry | `longSignal` | 30m | 见上方 |
| BTP30 Short Entry | `shortSignal` | 30m | 见上方 |
| BTP30 Exit | `exitSignal` | 30m | 见上方 |

### 手动 JSON（直接在 TV Alert Message 框填写）

**开多：**
```json
{
  "strategy_id": "btp_30m",
  "action": "buy",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "price": {{close}},
  "time": "{{timenow}}",
  "comment": "{{strategy.order.comment}}"
}
```

**开空：**
```json
{
  "strategy_id": "btp_30m",
  "action": "sell",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "price": {{close}},
  "time": "{{timenow}}",
  "comment": "{{strategy.order.comment}}"
}
```

**平仓（多空通用）：**
```json
{
  "strategy_id": "btp_30m",
  "action": "close",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "price": {{close}},
  "time": "{{timenow}}",
  "comment": "{{strategy.order.comment}}"
}
```

---

## Runner_5m（Bollinger Arbitrage Williams Runner 5m）

### Pine Script — 添加到策略底部

```pine
// ── DMC TV Bridge Webhook Alerts ──
alertcondition(
    longSignal,
    title="Runner5 Long Entry",
    message='{"strategy_id":"runner_5m","action":"buy","symbol":"ETH/USDT:USDT","exchange":"binance","price":{{close}},"time":"{{timenow}}"}'
)

alertcondition(
    shortSignal,
    title="Runner5 Short Entry",
    message='{"strategy_id":"runner_5m","action":"sell","symbol":"ETH/USDT:USDT","exchange":"binance","price":{{close}},"time":"{{timenow}}"}'
)

exitSignal = strategy.position_size[1] != 0 and strategy.position_size == 0
alertcondition(
    exitSignal,
    title="Runner5 Exit",
    message='{"strategy_id":"runner_5m","action":"close","symbol":"ETH/USDT:USDT","exchange":"binance","price":{{close}},"time":"{{timenow}}"}'
)
```

### 手动 JSON

**开多：**
```json
{
  "strategy_id": "runner_5m",
  "action": "buy",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "price": {{close}},
  "time": "{{timenow}}"
}
```

**开空：**
```json
{
  "strategy_id": "runner_5m",
  "action": "sell",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "price": {{close}},
  "time": "{{timenow}}"
}
```

**平仓：**
```json
{
  "strategy_id": "runner_5m",
  "action": "close",
  "symbol": "ETH/USDT:USDT",
  "exchange": "binance",
  "price": {{close}},
  "time": "{{timenow}}"
}
```

---

## 支持的 action 值

| action | 含义 |
|--------|------|
| `buy` | 开多（有空仓时自动反手）|
| `sell` | 开空（有多仓时自动反手）|
| `close` | 平仓（自动识别方向）|
| `reduce` | 部分平仓 |
| `set_sl_tp` | 更新止损止盈 |

## 可选字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `price` | float | 当前价格（记录用）|
| `stop_loss` | float | 止损价（下保护单）|
| `take_profit` | float | 止盈价（下保护单）|
| `quantity` | float | 合约数量（不填则按策略配置自动计算）|
| `allow_reverse` | bool | 是否允许反手（默认 true）|
| `comment` | string | 备注（记录到日志）|

---

## 注意事项

1. **时间框架匹配**：BTP 用 30m，Runner 用 5m，不要混用
2. **交易所符号**：Binance 永续合约为 `BINANCE:ETHUSDT.P`
3. **不填 quantity**：Bridge 按策略配置的 `max_position_usdt` 和当前价格自动计算
4. **出场信号**：Bridge 不依赖 TV 告知方向，`close` 自动识别并平掉对应仓位
