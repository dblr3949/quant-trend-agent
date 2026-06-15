# Trend Quant Simulator

一个面向美股和 A 股趋势股的轻量模拟盘项目。核心回测和信号生成只使用 Python 标准库，不会连接券商，也不会真实下单。

## 功能

- 从 CSV 读取日线行情
- 趋势策略回测：均线多头排列、动量过滤、ATR 风险控制
- 生成每日观察信号
- 维护本地模拟盘持仓
- 美股 / A 股股票池分开配置

## 目录

```text
config/watchlists.json     股票池
data/                      放历史行情 CSV
reports/                   输出回测和信号结果
state/paper_portfolio.json 模拟盘状态
quant_trend/               核心代码
scripts/                   命令行工具
tests/                     标准库单元测试
```

## 行情 CSV 格式

每个股票一个文件，放到 `data/` 目录下，文件名使用代码：

- 美股：`data/MU.csv`
- A 股：`data/600519.SH.csv`

必须包含这些列：

```csv
date,open,high,low,close,volume
2025-01-02,100,103,99,102,1234567
```

## 回测

```bash
python3 scripts/run_backtest.py --symbol MU --cash 100000
```

批量跑股票池：

```bash
python3 scripts/run_backtest.py --watchlist config/watchlists.json --cash 100000
```

## 生成今日趋势信号

```bash
python3 scripts/generate_signals.py --watchlist config/watchlists.json
```

输出文件：

```text
reports/signals.csv
```

## 模拟盘

查看模拟盘：

```bash
python3 scripts/paper_trade.py show
```

按最新信号生成计划，不会真实交易：

```bash
python3 scripts/paper_trade.py plan --signals reports/signals.csv
```

手动记录模拟买入：

```bash
python3 scripts/paper_trade.py buy --symbol MU --price 120 --shares 10
```

手动记录模拟卖出：

```bash
python3 scripts/paper_trade.py sell --symbol MU --price 130 --shares 5
```

## 策略规则

默认趋势信号：

- `close > SMA20 > SMA50 > SMA150`
- 近 60 日新高距离不超过 8%
- 20 日成交量均值高于 60 日成交量均值
- ATR 止损线用于控制风险

回测买卖：

- 买入：出现 `buy` 信号且无持仓
- 卖出：跌破 SMA50、触发 ATR 止损、或趋势转弱
- 单票风险按账户资金的 1% 估算仓位

## 下载行情

核心项目不依赖第三方包。若要自动下载：

- 美股可安装 `yfinance`
- A 股可安装 `akshare`
- IBKR 实时行情可安装 `ibapi`，通过 TWS / IB Gateway 只读 Level I 快照
- Massive/Polygon 中转行情可设置 `MASSIVE_API_KEY`，作为当前默认美股快照、日线和分钟线来源
- 更可靠的美股实时/日线数据可使用 Alpaca Market Data，设置 `ALPACA_API_KEY`、`ALPACA_API_SECRET`，并优先使用 `ALPACA_DATA_FEED=sip`

```bash
python3 -m pip install -r requirements.txt
```

然后使用：

```bash
python3 scripts/download_data.py --market us --symbol MU --start 2024-01-01
python3 scripts/download_data.py --market cn --symbol 600519 --start 2024-01-01
```

安装依赖和联网下载都可能需要你授权网络访问。

用 Alpaca 下载美股日线：

```bash
ALPACA_DATA_FEED=sip python3 scripts/download_data.py --market us --provider alpaca --symbol MU --start 2024-01-01
```

## 半导体持仓 Agent

这个 agent 是决策辅助工具，不连接券商，也不会真实下单。它读取你的 BBAE 组合、实时/近实时行情、日线技术面、宏观/研报/事件覆盖项，输出人工确认用的挂单建议。

### 1. 准备日线

至少下载这些标的的日线。`VIXY` 是便于用股票行情源获取的 VIX 代理；如果你的数据源支持指数，也可以把配置里的 `VIXY` 改成 `^VIX`。

```bash
for s in MU AAOI INTC LITE MRVL SPY SMH SOXX VIXY; do
  python3 scripts/download_data.py --market us --symbol "$s" --start 2024-01-01
done
```

### 2. 准备组合

参考 `config/portfolio.example.json` 创建 `config/portfolio.json`，填入 BBAE 的账户净值、现金/融资和当前股数。`thesis_status` 支持：

- `intact`：基本面逻辑未破
- `watch`：逻辑需要观察，自动降低目标权重
- `broken`：逻辑破坏，目标权重归零

也可以用脚本生成 `config/portfolio.json`，避免手改 JSON。

从本地 CSV：

```bash
python3 scripts/import_portfolio.py \
  --csv portfolio.csv \
  --account-equity 100000 \
  --cash -60000 \
  --margin-debit 60000
```

CSV 列名支持英文或中文：

```csv
symbol,shares,avg_cost,thesis_status,conviction
MU,300,115,intact,1
AAOI,500,20,intact,0.85
INTC,400,31,watch,0.55
LITE,180,65,intact,0.9
MRVL,220,78,intact,0.9
```

从在线 CSV，例如 Google Sheet 发布后的 CSV 链接：

```bash
python3 scripts/import_portfolio.py \
  --url "https://docs.google.com/spreadsheets/d/e/.../pub?output=csv" \
  --account-equity 100000 \
  --cash -60000 \
  --margin-debit 60000
```

从自然语言：

```bash
python3 scripts/import_portfolio.py --text "
账户净值 10万 现金 -6万 融资 6万
MU 300股 成本115 intact 信心1
AAOI 500股 成本20 intact 信心0.85
INTC 400股 成本31 watch 信心0.55
LITE 180股 成本65 intact 信心0.9
MRVL 220股 成本78 intact 信心0.9
"
```

自然语言输入必须包含账户净值。因为这个 agent 要管理 2x 杠杆，缺净值就无法判断仓位比例。

### 3. 拉实时报价

当前默认使用 Massive/Polygon 中转 REST 作为美股快照、日线和分钟线来源。程序按单标的查询，不做全市场 snapshot、批量全量、Flat Files 或期权全链 quotes；持仓仍只来自你手动输入的 BBAE 表格，不读取任何券商账户。

先在本地私密配置里填 Massive key：

```bash
cp config/openai.env.example config/openai.env
```

```env
MASSIVE_API_KEY=REPLACE_WITH_MASSIVE_PROXY_KEY
MASSIVE_REST_URL=http://44.219.45.87:8081
MASSIVE_WS_URL=ws://44.219.45.87:8080/ws
```

默认拉 Massive 行情：

```bash
python3 scripts/fetch_quotes.py --symbols MU,AAOI,INTC,LITE,MRVL,SPY,SMH,SOXX,VIXY
```

IBKR 仍可作为备用。启动 TWS 或 IB Gateway 后，在 API 设置中启用 Socket 客户端，并确认端口：

```bash
python3 scripts/fetch_quotes.py --provider ibkr \
  --ibkr-host 127.0.0.1 \
  --ibkr-port 7497 \
  --ibkr-client-id 81 \
  --symbols MU,AAOI,INTC,LITE,MRVL,SPY,SMH,SOXX,VIXY
```

如果这个 IBKR 账号没有订阅实时美股行情，可以先用延迟行情测试链路：

```bash
python3 scripts/fetch_quotes.py --provider ibkr \
  --ibkr-market-data-type 3 \
  --symbols MU,AAOI,INTC,LITE,MRVL,SPY,SMH,SOXX,VIXY
```

IBKR 脚本不会调用账户/仓位/订单接口；你的真实仓位只来自 `config/portfolio.json` 或网页表格。

Alpaca 仍可作为备用：

```bash
ALPACA_API_KEY=... ALPACA_API_SECRET=... ALPACA_DATA_FEED=sip \
  python3 scripts/fetch_quotes.py --provider alpaca --symbols MU,AAOI,INTC,LITE,MRVL,SPY,SMH,SOXX,VIXY
```

没有数据账号时可先演示：

```bash
python3 scripts/fetch_quotes.py --provider yfinance --symbols MU,AAOI,INTC,LITE,MRVL,SPY,SMH,SOXX,VIXY
```

### 4. 更新事件/研报覆盖

参考 `data/research_overlay.example.json` 创建 `data/research_overlay.json`。宏观、流动性、战争、IPO/大额融资等事件可以写成：

```json
{
  "liquidity_bias": -1,
  "events": [
    {
      "name": "large_ai_ipo_liquidity_drain",
      "direction": "risk_off",
      "severity": 1,
      "expires": "2026-06-12"
    }
  ]
}
```

### 5. 生成挂单建议

```bash
python3 scripts/position_agent.py \
  --portfolio config/portfolio.json \
  --quotes data/live_quotes.json \
  --research data/research_overlay.json
```

输出：

- `reports/agent_plan.json`：完整上下文、市场状态、权重、原因、数据警告
- `reports/agent_orders.csv`：挂单方向、股数、限价、金额、理由

### 推荐日常闭环

每天触发后按这个顺序：

1. 用 BBAE 当前页面或在线表格更新持仓，运行 `scripts/import_portfolio.py`
2. 拉行情：`scripts/fetch_quotes.py`
3. 录入当天调仓 prompt，例如“CPI 前不主动加仓，MU 回踩才加，INTC 只减不加”
4. 运行 `scripts/position_agent.py`
5. 人工确认 `reports/agent_orders.csv`，在 BBAE 手动挂单
6. 成交后再次更新持仓并重跑，防止仓位滞后

## 本地网页工作台

启动本地服务：

```bash
python3 scripts/run_agent_app.py
```

然后打开：

```text
http://127.0.0.1:8765
```

网页支持：

- 表格输入/修改持仓
- 自然语言解析持仓
- 手动输入维持保证金，用 `账户净值 - 维持保证金` 估算保证金安全垫，用于限制新增买单预算
- 手动输入目标杠杆参考；更保守会直接采纳，更激进会被风险环境截断
- 每个持仓可选择仓位桶和交易约束，例如核心、卫星、清理、不主动加、不主动减
- 输入本次调仓想法 prompt
- 一键生成调仓建议
- 生成时显示可拖动的研究进度窗口，包含步骤、评分和错误
- 展示当日分钟线摘要：开盘至今、VWAP 偏离、近 30 分钟、区间位置
- 保存每次跑批记录到 `reports/agent_runs/`
- 查看上一轮到本轮的持仓股数变化
- 服务运行期间可开启盘前/盘后自动跑批
- Massive/Polygon 默认行情设置和连接测试
- IBKR 只读行情设置和连接测试，作为备用

默认 Massive/Polygon 是主行情源。`Yahoo chart` 只适合 baseline 和流程调试，不是实时交易行情；`file` 可读取你自己写入的 `data/live_quotes.json`。

### OpenAI / Codex 配置

网页里的 `重点总结` 可以调用 OpenAI API 生成更像研究员口径的中文摘要。ChatGPT/Codex Pro 订阅不能直接作为本地程序的 API 凭证；本地服务需要单独的 `OPENAI_API_KEY`。没有 key 时，程序会自动使用本地兜底总结。

创建本地私密配置：

```bash
cp config/openai.env.example config/openai.env
```

然后只在 `config/openai.env` 里填真实 key：

```env
OPENAI_API_KEY=REPLACE_WITH_OPENAI_API_KEY
OPENAI_MODEL=gpt-5.5
OPENAI_DECISION_REASONING_EFFORT=medium
OPENAI_SUMMARY_REASONING_EFFORT=medium
OPENAI_DECISION_VERBOSITY=low
OPENAI_SUMMARY_VERBOSITY=medium
MASSIVE_API_KEY=REPLACE_WITH_MASSIVE_PROXY_KEY
MASSIVE_REST_URL=http://44.219.45.87:8081
MASSIVE_WS_URL=ws://44.219.45.87:8080/ws
```

默认使用 `gpt-5.5`。点位复核和重点总结默认都用 `medium` 推理档位；当前输入量下，这比 `high/xhigh` 更稳定地产出 JSON 价梯，也更可控。如果后续要单独拆模型，可以额外设置 `OPENAI_DECISION_MODEL` 或 `OPENAI_SUMMARY_MODEL`。

`config/openai.env` 已加入 `.gitignore`，不会被提交。修改后重启网页服务：

```bash
python3 scripts/run_agent_app.py
```

服务启动时只会打印已加载的 key 名，不会打印密钥值。网页结果里如果 `重点总结` 标签显示 `LLM`，说明已经走 OpenAI API；如果显示 `本地兜底`，说明未配置 key 或 API 调用失败。

### IBKR 行情

网页里选择 `IBKR 只读行情` 后会出现 Gateway/TWS 参数：

- Gateway paper 常见端口：`4002`
- Gateway live 常见端口：`4001`
- TWS paper 常见端口：`7497`
- TWS live 常见端口：`7496`
- 行情类型：`1` 实时，`3` 延迟

网页的 `测试 IBKR 行情` 只调用行情快照接口；生成建议时还会调用 IBKR 历史行情接口读取当日分钟线。安全边界是：

- 允许：`reqMktData` / `cancelMktData`
- 允许：`reqHistoricalData` / `cancelHistoricalData`
- 禁止：账户、持仓、订单、成交、下单接口

如果看到 `No market data during competing live session`，说明 IBKR 已连接但行情被同账号的其它 live session 占用，需要关闭其它占用行情的 TWS/Gateway/移动端行情窗口，或换一个有行情权限的用户。

### Prompt 约束

- “不主动加仓 / 只减不加 / 少加仓”会被解析成软约束：证据不足时尊重，证据很强时可以反驳并在原因里写出证据分
- “绝对不加仓 / 禁止加仓 / 严禁加仓 / hard no add”会被解析成硬约束：不会生成买入建议
- 持仓表里的交易约束也是软约束：Agent 可以反驳，但会在原因里写出证据分
- 维持保证金安全垫低于安全线时，新增买单预算会被压到 0；高于安全线时，会按安全垫倒推可用买单额度

### Git 同步与脱敏

仓库只提交程序、示例配置和测试。下面这些本地文件不会提交：

- `config/openai.env`：真实 OpenAI API key
- `config/portfolio.json`：真实 BBAE 持仓和账户输入
- `state/`：网页保存状态
- `data/*.csv`、`data/live_quotes.json`：本地行情缓存
- `reports/`：每次跑批、截图和调仓建议记录

提交前建议运行：

```bash
python3 scripts/check_repo_safety.py
```

它会检查已跟踪文件里是否误加入私密路径或疑似密钥。

关键风控：

- 超过 `max_quote_age_minutes` 的报价不会触发加仓
- 总风险暴露不超过 `max_gross_exposure`
- `risk_off` 时自动降总仓位目标
- `watch` / `broken` 基本面状态会降低或清零目标权重
- 单票权重受 `max_symbol_weight` / `max_noncore_symbol_weight` 限制
