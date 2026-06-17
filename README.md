# Semis Position Agent

当前版本：`v0.4.0`

半导体持仓调仓研究台。系统读取网页里手动维护的 BBAE 持仓、Massive/Polygon 行情、历史日线、当日分钟线、用户本轮调仓 prompt 和本地研究覆盖项，生成中文研究过程、重点总结和人工确认用的挂单建议。

边界很明确：

- 不连接 BBAE 账户，不读取券商持仓、订单或成交。
- IBKR 仅作为备用行情源；只读 market data，不读账户，不下单。
- 所有建议都只是辅助决策；不会自动提交真实订单。
- 真实密钥、持仓、跑批记录和行情缓存不提交到 Git。

## 目录

```text
config/                  策略配置、示例 env、示例组合
data/                    本地行情缓存和研究覆盖示例
quant_trend/             后端计算、行情、LLM、Web 服务
scripts/                 命令行工具和启动脚本
tests/                   单元测试
web/                     前端页面、CSS、JS
Dockerfile               Railway/Render 部署镜像
railway.json             Railway 部署配置
```

## 快速启动

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

创建本地私密配置：

```bash
cp config/openai.env.example config/openai.env
```

常用环境变量：

```env
MASSIVE_API_KEY=REPLACE_WITH_MASSIVE_PROXY_KEY
MASSIVE_REST_URL=http://44.219.45.87:8081
MASSIVE_WS_URL=ws://44.219.45.87:8080/ws

DASHSCOPE_API_KEY=REPLACE_WITH_DASHSCOPE_KEY
OPENAI_API_KEY=REPLACE_WITH_OPENAI_KEY
DEEPSEEK_API_KEY=REPLACE_WITH_DEEPSEEK_KEY
OPENAI_MODEL=qwen3.7-max
```

启动本地网页：

```bash
python3 scripts/run_agent_app.py
```

打开：

```text
http://127.0.0.1:8765
```

云端生产地址：

```text
https://quant-trend-agent-production.up.railway.app
```

## 版本号

版本号定义在 `quant_trend/version.py`。

前端 topbar 会展示当前应用版本，例如 `v0.4.0`。后端接口也会返回版本：

```bash
curl https://quant-trend-agent-production.up.railway.app/healthz
```

版本策略：

- 小修 UI、文案、README：patch，例如 `0.4.1`
- 改计算口径、数据源、LLM 输入：minor，例如 `0.5.0`
- 改数据结构或多用户/数据库等兼容性风险较大的内容：major，例如 `1.0.0`

Railway 如果提供 commit sha，前端会在版本号后展示短构建号；如果没有，只展示显式版本。

## 数据来源

### 持仓与账户

持仓只来自网页表格或本地输入。核心字段：

- `账户净值`：用于计算仓位比例和总杠杆。
- `现金`：可为负数。
- `融资`：记录风险，不直接作为强平线。
- `维持保证金`：低于它可能触发强平；系统用 `账户净值 - 维持保证金` 估算保证金安全垫。
- `目标杠杆`：用户偏好，不是硬命令；更保守会采纳，更激进会被市场和风控截断。
- 每个持仓：`代码 / 股数 / 成本 / 状态 / 信心 / 桶 / 约束`。

### 行情

默认主行情源是 Massive/Polygon 中转 REST：

- 快照：现价、bid/ask、昨收等。
- 日线：补齐本地 CSV，默认最多约两年窗口。
- 分钟线：当日或最近一个美股交易日的分钟聚合。
- `^VIX` 映射到 Polygon 的 `I:VIX`；指数权限不可用时仅对 `^VIX` 用 Yahoo Chart 兜底。

限制：

- 不做全市场 snapshot。
- 不做 Flat Files。
- 不做批量全量。
- 不订阅期权全链 quotes。

IBKR 作为备用行情源，只允许：

- `reqMktData`
- `cancelMktData`
- `reqHistoricalData`
- `cancelHistoricalData`

禁止读取账户、持仓、订单、成交，禁止下单。

### 用户 Prompt

“本次调仓想法”会先进入 `prompt_overlay`。当前是：

```text
LLM 解析 + 规则护栏 + 规则兜底
```

规则：

- “绝对不加仓 / 禁止加仓 / 严禁加仓 / 必须不加”会变成硬约束。
- “不主动加 / 少加 / 只在深回撤加”会变成软约束或条件。
- “不主动卖 / 尽量不卖”会变成 `soft_no_reduce`，只有强技术面或风控证据才会反驳。
- “做T / 高抛低吸 / 低买高卖”会触发 `range_trade`。
- “总仓位不变”是 `flat_preferred`；“必须总仓位不变”才是 `flat_required`。
- LLM 不能删除规则识别出的硬约束，不能引入股票池外 symbol。
- LLM 失败、没 key、空输出或 JSON 解析失败时自动回规则解析。

解析后的字段会进入点位复核 LLM 和重点总结 LLM，包括：

- `no_add / soft_no_add`
- `no_reduce / soft_no_reduce`
- `trade_plan`
- `target_net_exposure`
- `buy_condition / sell_condition`
- `risk_trigger`
- `evidence / explanation`
- `macro_bias / liquidity_bias / geopolitical_bias`

## 一轮建议的计算流程

### 1. 确认股票池

股票池来自三部分并集：

- `config/agent_config.json` 里的默认持仓标的
- 当前网页持仓里的 symbol
- 市场代理：`SPY / SMH / SOXX / ^VIX`

### 2. 补齐历史日线

先检查本地 CSV 是否到最近完整美股交易日；缺失或过旧则从 Massive/Polygon 拉取。日线用于：

- 均线
- ATR
- 趋势信号
- 支撑/压力
- Volume Profile
- 自动锚定 VWAP
- 风险调整动量
- 区间波动率

### 3. 读取实时快照和分钟线

快照用于：

- 当前价格
- 仓位市值
- 目标权重差额
- 限价参考价
- 报价新鲜度检查

当日分钟线用于：

- 开盘至今涨跌
- VWAP 偏离
- 近 30 分钟走势
- 日内区间位置
- 订单流/VPIN 近似

日内数据只做辅助。支撑/压力展示和挂单点位会优先历史结构，日内点位在候选评分中会被扣分。

## 技术面计算口径

### 趋势快照

每个标的根据日线生成 `TechnicalSnapshot`：

- `SMA20 / SMA50 / SMA150`
- `ATR14`
- 近 60 日高点
- 趋势动作：`buy / watch / sell / hold`
- 趋势止损线
- 报价年龄和 stale quote 标记

报价过旧或只有前收盘兜底时，不允许触发新增买单。

### 支撑/压力候选

每个标的会生成多周期候选点位：

- 日线结构：`5 / 10 / 20 / 50 / 63 / 90 / 126 / 180 / 252` 日前高前低。
- Volume Profile：`20 / 60 / 90 / 126 / 180 / 252` 日。
- 摆动前高/前低：`20 / 60 / 90 / 126 / 180 / 252` 日。
- 自动锚定 VWAP：
  - `20 / 60 / 90 / 126 / 180 / 252` 日高低点锚。
  - 近 252 日放量上攻、放量下杀、跳空、突破、跌破、财报式冲击锚。
  - 近 180 日摆动高低点锚。
- 均线和趋势止损线。
- 当日 VWAP、当日高低点、近 30 分钟点位。
- 当日高量分钟 bar 的高低点和成交均价。

### Volume Profile

Volume Profile 使用日线 OHLCV 近似计算：

1. 取指定窗口内的日线。
2. 用窗口最高/最低划分价格桶。
3. 每根日线的成交量按其高低区间均匀分配到覆盖的价格桶。
4. 得到每个价格桶的估算成交量。

输出：

- `POC`：成交量最大的价格桶。
- `VAH`：70% value area 上沿。
- `VAL`：70% value area 下沿。
- `HVN`：高成交量节点。
- `LVN`：低成交量真空区。
- `volume_share_pct`：单价格桶成交占比。
- `chip_share_pct`：目标桶及左右相邻桶的合计占比，用来近似该区域筹码/成交堆积。

这是基于日线的成交分布近似，不是逐笔成交的真实筹码分布。

### 点位强度

每个支撑/压力会计算：

- `level_strength_score`：0 到 5。
- `confluence_count`：附近共振点位数量。
- `touch_count`：近 252 日触碰次数。
- `recency_days`：最近触碰距今天数。
- `chip_share_pct`：附近三桶成交占比。
- `volume_share_pct`：单桶成交占比。
- `tier`：
  - `近端`
  - `主结构`
  - `深水/高抛`
  - `日内辅助`

强度得分由类别、成交占比、共振、触碰次数、近期性和锚点质量共同构成。

### 风险调整动量

计算 `20 / 60 / 120` 日风险调整收益：

```text
窗口收益 / 窗口实现波动率
```

权重：

```text
20日 50% + 60日 30% + 120日 20%
```

输出 `risk_adjusted_momentum.score`，进入量价总分、前端展示、LLM 输入。

### 区间波动率

使用多种 OHLC 波动率估计：

- ATR
- Parkinson
- Garman-Klass
- Rogers-Satchell
- Yang-Zhang

形成 `range_volatility.blended_daily_pct` 和年化估计，用于：

- 判断点位间距是否需要放宽。
- 影响买卖候选区间。
- 输入 LLM 解释。

### 订单流/VPIN 近似

没有逐笔成交时，系统用分钟聚合 bar 估算订单流：

- 用分钟 bar 收盘位置和涨跌方向估算 signed volume。
- 估算买量、卖量、净失衡。
- 计算近 30 分钟失衡。
- 按成交量桶计算 VPIN 近似。
- 如果有 bid/ask，则评估价差质量。

这不是真实逐笔 VPIN，只是分钟聚合近似。

### 量价总分

每个股票的量价分范围是：

```text
-6 ~ +6
```

组成：

- 趋势/均线
- 多周期动量
- 区间波动率
- 支撑/压力位置
- 量能确认
- 订单流/VPIN
- 当日趋势

分数会用于：

- 前端“近期量价点位”展示。
- 每只股票目标权重的轻微上调/下调。
- 挂单候选排序。
- 重点总结 LLM。
- 点位复核 LLM。

## 市场环境计算

### SPY / SMH / SOXX

指数 ETF 使用和个股相同的量价框架：

- 趋势/均线
- 支撑/压力
- Volume Profile
- 动量
- 波动率
- 分钟线

它们进入 `market_technical_analysis`，再合成 `market_structure`。

### ^VIX

`^VIX` 不按股票成交量/POC 解释，而用专属波动率口径：

- 当前绝对水平。
- 近 252 日历史分位。
- 近 126 日 Z-score。
- 5 日变化。
- 相对 20 日均值偏离。
- 日内变化方向。

`^VIX` 输出进入 `volatility_analysis`，再以 `volatility_risk` 方式影响市场结构。VIX 越高、分位越高、短期升幅越大，越偏 `risk_off`。

### Regime

市场状态输出：

- `risk_on`
- `neutral`
- `risk_off`

影响：

- 目标总杠杆。
- 买入折价深度。
- 卖出溢价要求。
- 风险预算和保证金约束。

## 仓位与股数计算

### 目标总仓位

基础目标总仓位由市场状态决定：

- `risk_on_gross_exposure`
- `neutral_gross_exposure`
- `risk_off_gross_exposure`

用户填写的目标杠杆会作为参考：

- 如果用户目标更保守，通常会采纳。
- 如果用户目标更激进，会被市场状态、最大杠杆和保证金约束截断。

### 单票目标权重

单票基础权重来自 `config/agent_config.json`，再乘以多个因子：

- 趋势信号。
- 基本面状态：`intact / watch / broken`。
- 信心值。
- prompt/research bias。
- 日内趋势。
- 量价分 multiplier。
- 桶分类：`core / satellite / watch / trim / auto`。
- 持仓约束：`prefer_hold / soft_no_add / soft_no_reduce / reduce_only`。

随后归一化到目标总仓位，并应用单票上限：

- `max_symbol_weight`
- `max_noncore_symbol_weight`

### 买卖预算

系统不是先拍股数，而是先算金额：

```text
目标金额 = 目标权重 × 账户净值
差额 = 目标金额 - 当前市值
```

然后根据以下约束裁剪：

- 最大总杠杆。
- 当前总仓位。
- 维持保证金安全垫。
- 最小交易金额。
- 再平衡带宽。
- stale quote 禁买。
- 硬约束禁止买/卖。

最后：

```text
股数 = 可交易金额 // 限价
```

卖出股数不会超过当前持仓。

## 限价和价梯

### 后端确定性点位

买单和卖单会先生成候选点位，然后按结构选择主限价：

买单优先：

- 现价下方。
- 落在买入折价区间内。
- 有历史支撑、POC、VAL、HVN、AVWAP 或摆动前低。
- 成交/筹码占比高。
- 共振多。
- 日内点位会降权。

卖单优先：

- 现价上方。
- 落在卖出溢价区间内。
- 有历史压力、VAH、HVN、AVWAP 或摆动前高。
- 成交/筹码占比高。
- 可触达，不选明显脱离结构的孤立远点。

折价/溢价区间由：

- ATR
- 混合区间波动率
- 市场状态
- `market_structure.score`

共同决定。

### LLM 点位复核

后端先给出候选 `candidate_levels`，LLM 只能从候选主点位里选择，不能编造主执行价。

LLM 输入包括：

- 每张订单的候选点位，最多 8 个。
- 当前股票前 8 个支撑和前 8 个压力。
- Volume Profile、筹码占比、点位强度、AVWAP。
- 动量、波动率、订单流。
- SPY/SMH/SOXX 市场技术面。
- `^VIX` 波动率风险。
- 仓位、杠杆、保证金。
- prompt_overlay 和 decision_context。

LLM 可以：

- 改主候选点位。
- 调整建议股数，但买入不能突破预算保护，卖出不能超过持仓。
- 输出 2-3 档 `reference_ladder`。

价梯是参考分层，不会自动下单。

## 重点总结

重点总结 LLM 读取整张 plan 的压缩版：

- 市场框架：SPY、SMH、SOXX、`^VIX`。
- 每只股票的现价、动作、权重变化、挂单。
- 量价分、趋势分、日内分。
- 支撑/压力、Volume Profile、AVWAP、筹码占比。
- 订单流、波动率、动量。
- prompt 解析结果。
- LLM 点位复核结果和参考价梯。
- 数据警告和弱覆盖事件。

所有评分必须按：

```text
分数 / 下限~上限 · 尺位xx%
```

例如：

```text
量价 +2.4 / -6~+6 · 尺位70%
```

这里的“尺位”是评分尺位置，不是历史样本分位。

## 网页功能

网页支持：

- 多用户登录。
- 表格维护持仓和账户信息。
- 保存 Massive/IBKR/Yahoo/Alpaca 行情设置。
- 测试 Massive 或 IBKR 行情。
- 读取当日分钟线。
- 输入本轮调仓 prompt。
- 生成调仓建议。
- 显示研究进度。
- 展示重点总结、建议挂单、价梯、杠杆和市场结构。
- 展示近期量价点位、K 线大图、支撑/压力、指标说明。
- 保存跑批记录。
- 前端展示当前应用版本。

自然语言持仓解析入口目前保留但禁用；当前持仓以表格为准。

## 云端部署

Railway 使用：

- `Dockerfile`
- `railway.json`
- `/data` 持久化 volume

关键环境变量：

```env
APP_DATA_DIR=/data
APP_USERNAME=...
APP_PASSWORD=...
MASSIVE_API_KEY=...
DASHSCOPE_API_KEY=...
OPENAI_API_KEY=...
DEEPSEEK_API_KEY=...
APP_VERSION=0.4.0
```

如果启用多用户数据库，持仓、设置、跑批记录会写入 `/data` 下的持久化数据库或文件。Railway 重新部署不会清空 `/data` volume。

常用部署流程：

```bash
python3 -m unittest discover -s tests
node --check web/app.js
python3 scripts/check_repo_safety.py
git add .
git commit -m "..."
git push origin main
npx @railway/cli up --detach
npx @railway/cli status
```

线上健康检查：

```bash
curl https://quant-trend-agent-production.up.railway.app/healthz
```

## Git 脱敏

不要提交：

- `config/openai.env`
- `config/portfolio.json`
- `state/`
- `data/*.csv`
- `data/live_quotes.json`
- `data/research_overlay.json`
- `reports/`

提交前运行：

```bash
python3 scripts/check_repo_safety.py
```

## 测试

```bash
python3 -m unittest discover -s tests
node --check web/app.js
python3 -m py_compile quant_trend/prompt_overlay.py quant_trend/app_server.py quant_trend/agent.py quant_trend/llm_decision.py
git diff --check
```
