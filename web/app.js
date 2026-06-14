let appState = null;
let latestRun = null;
let isRunning = false;
let progressTimer = null;

const defaultSymbols = ["MU", "AAOI", "INTC", "LITE", "MRVL"];
const regimeLabels = { risk_on: "风险偏多", neutral: "中性", risk_off: "风险收缩" };
const kindLabels = { manual: "手动", premarket: "盘前", postmarket: "盘后", scheduled: "定时" };
const actionLabels = { add: "加仓", reduce: "减仓", hold: "保持", range_trade: "做T" };
const sideLabels = { buy: "买入", sell: "卖出" };
const thesisLabels = { intact: "逻辑未破", watch: "观察", broken: "逻辑破坏" };
const intradayLabels = { strong_up: "强势上行", up: "上行", mixed: "震荡", down: "下行", strong_down: "强势下行" };
const priceVolumeLabels = { strong: "量价共振强", positive: "量价偏强", mixed: "量价震荡", negative: "量价偏弱", weak: "量价转弱" };
const bucketLabels = { auto: "自动", core: "核心", satellite: "卫星", watch: "观察", trim: "清理" };
const marketProxySymbols = new Set(["SPY", "SMH", "SOXX", "VIXY"]);
const constraintLabels = {
  flexible: "灵活",
  prefer_hold: "尽量不动",
  soft_no_add: "不主动加",
  soft_no_reduce: "不主动减",
  reduce_only: "只减不加",
};

function $(id) {
  return document.getElementById(id);
}

function fmtMoney(value) {
  const number = Number(value || 0);
  return number.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function fmtPct(value) {
  if (value === null || value === undefined) return "-";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function fmtLeverage(value) {
  if (value === null || value === undefined) return "-";
  return `${Number(value).toFixed(2)}x`;
}

function fmtPrice(value) {
  if (value === null || value === undefined || value === "") return "-";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 4 });
}

function fmtScoreNumber(value, signed = true) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  const text = Number.isInteger(number) ? String(number) : number.toFixed(2).replace(/\.?0+$/, "");
  return signed && number > 0 ? `+${text}` : text;
}

function scoreRangeDefault(kindOrLabel) {
  const label = String(kindOrLabel || "");
  if (label.includes("量价") || label === "price_volume") return { min: -6, max: 6 };
  if (label.includes("市场") || label.includes("指数") || label === "market" || label === "regime") return { min: -10, max: 10 };
  if (label.includes("趋势") || label.includes("技术") || label === "technical" || label === "trend") return { min: 0, max: 8 };
  if (label.includes("分钟") || label.includes("日内") || label === "intraday") return { min: -5, max: 5 };
  if (label.includes("想法") || label.includes("约束") || label.includes("证据") || label.includes("研究") || label === "prompt") return { min: -5, max: 5 };
  if (label.includes("杠杆") || label.includes("保证金") || label === "risk") return { min: -2, max: 2, unit: "x" };
  if (label.includes("力度")) return { min: 0, max: 5 };
  return null;
}

function normalizeScoreRange(value, meta, fallback) {
  const range = meta || scoreRangeDefault(fallback);
  if (!range) return null;
  const min = Number(range.min);
  const max = Number(range.max);
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return null;
  const score = Number(value);
  if (!Number.isFinite(score)) return null;
  const rawPosition = ((score - min) / (max - min)) * 100;
  const percentile = range.percentile === undefined ? Math.max(0, Math.min(100, rawPosition)) : Number(range.percentile);
  return {
    min,
    max,
    unit: range.unit || "",
    percentile: Number.isFinite(percentile) ? percentile : Math.max(0, Math.min(100, rawPosition)),
  };
}

function formatScore(value, meta, fallback) {
  if (value === null || value === undefined || value === "") return "-";
  const range = normalizeScoreRange(value, meta, fallback);
  if (!range) return fmtScoreNumber(value, true);
  const unit = range.unit || "";
  const low = `${fmtScoreNumber(range.min, true)}${unit}`;
  const high = `${fmtScoreNumber(range.max, true)}${unit}`;
  return `${fmtScoreNumber(value, true)}${unit} / ${low}~${high} · 尺位${range.percentile.toFixed(0)}%`;
}

function formatReasonScore(value, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) return value || "";
  return formatScore(number, null, fallback);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatEtTimestamp(value, withYear = true) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  })
    .formatToParts(date)
    .reduce((acc, part) => {
      acc[part.type] = part.value;
      return acc;
    }, {});
  const dateText = withYear ? `${parts.year}-${parts.month}-${parts.day}` : `${parts.month}-${parts.day}`;
  return `${dateText} ${parts.hour}:${parts.minute} ET`;
}

function chartRowLabel(row, withYear = false) {
  if (row?.is_current) {
    const stamp = formatEtTimestamp(row.timestamp, withYear);
    return `${row.session_label || "当前"}${stamp ? ` ${stamp}` : ""}`.trim() || row.date || "当前";
  }
  return row?.date || "-";
}

function chartRowTitle(row, prefix = "收") {
  const type = row?.is_current ? "快照" : "完整日线";
  const label = chartRowLabel(row, true);
  const volume = row?.is_current && !Number(row.volume || 0) ? "未取到分时成交量" : `成交量${fmtMoney(row.volume)}`;
  return `${label} · ${type} · ${prefix}${fmtPrice(row.close)} · ${volume} · 量比${row.volume_ratio20 ?? "-"}`;
}

function parseSummaryBlocks(text) {
  const lines = String(text || "")
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
  const blocks = [];
  let pendingTitle = null;
  for (const line of lines) {
    const headingOnly = line.match(/^【([^】]+)】$/);
    if (headingOnly) {
      if (pendingTitle) blocks.push({ title: pendingTitle, body: "" });
      pendingTitle = headingOnly[1];
      continue;
    }
    const headingInline = line.match(/^【([^】]+)】\s*(.*)$/);
    if (headingInline) {
      if (pendingTitle) blocks.push({ title: pendingTitle, body: "" });
      blocks.push({ title: headingInline[1], body: headingInline[2] || "" });
      pendingTitle = null;
      continue;
    }
    const symbolLine = line.match(/^([A-Z][A-Z0-9.]{0,6})[：:]\s*(.*)$/);
    const compactSymbolLine = line.match(/^([A-Z][A-Z0-9.]{0,6})(?=现价|当前价|价格|：|:)/);
    const sectionLine = line.match(/^(市场指数分析|指数分析|持仓股票分析|持仓分析)[：:]\s*(.*)$/);
    if (pendingTitle) {
      blocks.push({ title: pendingTitle, body: line });
      pendingTitle = null;
    } else if (sectionLine) {
      blocks.push({ title: sectionLine[1], body: sectionLine[2] || "" });
    } else if (symbolLine) {
      blocks.push({ title: symbolLine[1], body: symbolLine[2] || "" });
    } else if (compactSymbolLine) {
      const symbol = compactSymbolLine[1];
      blocks.push({ title: symbol, body: line.slice(symbol.length).trim() });
    } else if (/^当前市场|^整体市场|^市场处于/.test(line)) {
      blocks.push({ title: "整体市场框架", body: line });
    } else {
      blocks.push({ title: "要点", body: line });
    }
  }
  if (pendingTitle) blocks.push({ title: pendingTitle, body: "" });
  return blocks;
}

function summaryBlockKind(title, body) {
  const text = `${title} ${body}`;
  if (marketProxySymbols.has(String(title).toUpperCase())) return "market";
  if (/整体|市场|框架|指数/.test(text) && !/^[A-Z][A-Z0-9.]{0,6}$/.test(title)) return "overview";
  if (/^[A-Z][A-Z0-9.]{0,6}$/.test(title)) return "symbol";
  return "note";
}

function summaryBadges(title, body) {
  const text = `${title} ${body}`;
  const badges = [];
  const action = text.match(/(?:建议|拟|计划|主建议是|动作|暂不动作)[^。；，,]*(加仓|减仓|买入|卖出|保持|暂不动作)/);
  if (action) {
    const word = action[1];
    const klass = /加仓|买入/.test(word) ? "buy" : /减仓|卖出/.test(word) ? "sell" : "hold";
    badges.push(`<span class="summary-badge ${klass}">${escapeHtml(word)}</span>`);
  }
  const price = text.match(/现价\s*([0-9,.]+)/);
  if (price) badges.push(`<span class="summary-badge price">现价 ${escapeHtml(price[1])}</span>`);
  const scale = text.match(/尺位\s*([0-9]{1,3})%/);
  if (scale) badges.push(`<span class="summary-badge scale">尺位 ${escapeHtml(scale[1])}%</span>`);
  const support = text.match(/支撑(?:位|约|为|在|)\s*([0-9,.]+)/);
  if (support) badges.push(`<span class="summary-badge support">支撑 ${escapeHtml(support[1])}</span>`);
  const resistance = text.match(/压力(?:位|约|为|在|)\s*([0-9,.]+)/);
  if (resistance) badges.push(`<span class="summary-badge resistance">压力 ${escapeHtml(resistance[1])}</span>`);
  return badges.join("");
}

function highlightSummaryInline(text) {
  let html = escapeHtml(text || "");
  html = html.replace(/(现价\s*[0-9,.]+)/g, '<span class="summary-token price">$1</span>');
  html = html.replace(/([买卖]\s*\d+\s*股\s*@\s*[0-9,.]+)/g, '<span class="summary-token order">$1</span>');
  html = html.replace(/(支撑(?:位|约|为|在|)?\s*[0-9,.]+)/g, '<span class="summary-token support">$1</span>');
  html = html.replace(/(压力(?:位|约|为|在|)?\s*[0-9,.]+)/g, '<span class="summary-token resistance">$1</span>');
  html = html.replace(/([+-]?\d+(?:\.\d+)?\s*\/\s*[+-]?\d+(?:\.\d+)?~[+-]?\d+(?:\.\d+)?\s*·\s*尺位\s*\d+%)/g, '<span class="summary-token score">$1</span>');
  html = html.replace(/(风险偏多|风险收缩|中性|加仓|减仓|买入|卖出|保持|暂不动作)/g, '<span class="summary-token action">$1</span>');
  return html;
}

function renderSummaryBlock(block, index) {
  const title = block.title || "要点";
  const body = block.body || "";
  const kind = body ? summaryBlockKind(title, body) : "section";
  const label = kind === "overview" ? "总览" : kind === "market" ? "指数" : kind === "symbol" ? "标的" : "备注";
  if (kind === "section") {
    return `
      <div class="summary-section-divider">
        <span>${escapeHtml(title)}</span>
      </div>
    `;
  }
  return `
    <article class="summary-block ${kind}">
      <div class="summary-block-head">
        <div>
          <span class="summary-block-kicker">${label}</span>
          <strong>${escapeHtml(title)}</strong>
        </div>
        <div class="summary-badges">${summaryBadges(title, body)}</div>
      </div>
      <p>${highlightSummaryInline(body || (index === 0 ? "暂无正文" : ""))}</p>
    </article>
  `;
}

function labelRegime(value) {
  return regimeLabels[value] || value || "-";
}

function labelKind(value) {
  return kindLabels[value] || value || "手动";
}

function labelAction(value) {
  return actionLabels[value] || value || "-";
}

function labelSide(value) {
  return sideLabels[value] || value || "-";
}

function toggleIbkrSettings() {
  $("ibkrSettings").hidden = $("provider").value !== "ibkr";
}

function currentSettingsFromForm() {
  return {
    provider: $("provider").value,
    refresh_history: $("refreshHistory").checked,
    schedule_enabled: $("scheduleEnabled").checked,
    ibkr_host: $("ibkrHost").value || "127.0.0.1",
    ibkr_port: Number($("ibkrPort").value || 4002),
    ibkr_client_id: Number($("ibkrClientId").value || 81),
    ibkr_market_data_type: Number($("ibkrMarketDataType").value || 1),
    ibkr_timeout: Number($("ibkrTimeout").value || 8),
    fetch_intraday: $("fetchIntraday").checked,
    intraday_bar_size: $("intradayBarSize").value || "5 mins",
    intraday_duration: $("intradayDuration").value || "1 D",
    intraday_use_rth: $("intradayUseRth").checked,
  };
}

function translateReason(reason) {
  if (!reason) return "";
  return String(reason)
    .split(";")
    .filter(Boolean)
    .map((part) => {
      if (part === "trend_buy") return "趋势信号偏买入";
      if (part === "trend_watch") return "趋势信号观察";
      if (part === "trend_sell") return "趋势信号卖出";
      if (part === "trend_hold") return "趋势信号保持";
      if (part.startsWith("price_volume_score:")) return `近期量价分 ${formatReasonScore(part.split(":")[1], "price_volume")}`;
      if (part === "below_sma50") return "跌破 50 日均线";
      if (part === "below_trend_stop") return "跌破趋势止损线";
      if (part === "intraday_strong_up") return "当日分钟线强势上行";
      if (part === "intraday_up") return "当日分钟线偏强";
      if (part === "intraday_mixed") return "当日分钟线震荡";
      if (part === "intraday_down") return "当日分钟线偏弱";
      if (part === "intraday_strong_down") return "当日分钟线强势下行";
      if (part === "inside_rebalance_band") return "差额在再平衡阈值内";
      if (part === "range_trade_prompt") return "本次想法：做T/高抛低吸";
      if (part === "range_trade_flat_prompt") return "目标：若两腿成交则净仓位不变";
      if (part === "range_trade_flat_preferred_prompt") return "偏好：净仓位接近不变，但模型可反驳";
      if (part === "range_trade_flat_required_prompt") return "硬约束：两腿全成交净股数尽量为 0";
      if (part === "range_trade_low_buy") return "低位买回腿";
      if (part === "range_trade_high_sell") return "高位卖出腿";
      if (part === "range_trade_buy_blocked") return "买腿受硬约束限制";
      if (part === "range_trade_sell_blocked") return "卖腿受持仓或硬约束限制";
      if (part === "buy_blocked") return "加仓条件不足";
      if (part === "prompt_no_add") return "本次想法硬限制：不加仓";
      if (part.startsWith("prompt_soft_no_add_overridden:")) return `反驳本次“不主动加仓”想法，证据分 ${formatReasonScore(part.split(":")[1], "prompt")}`;
      if (part.startsWith("prompt_soft_no_add:")) return `尊重本次“不主动加仓”想法，证据分 ${formatReasonScore(part.split(":")[1], "prompt")}`;
      if (part === "prompt_no_reduce") return "本次想法限制：不减仓";
      if (part === "bucket_auto_core") return "自动归为核心桶";
      if (part === "bucket_auto_satellite") return "自动归为卫星桶";
      if (part === "bucket_core") return "核心桶";
      if (part === "bucket_satellite") return "卫星桶";
      if (part === "bucket_watch") return "观察桶";
      if (part === "bucket_trim") return "清理桶";
      if (part === "constraint_prefer_hold") return "持仓约束：尽量不动";
      if (part === "constraint_soft_no_add") return "持仓约束：不主动加";
      if (part === "constraint_soft_no_reduce") return "持仓约束：不主动减";
      if (part === "constraint_reduce_only") return "持仓约束：只减不加";
      if (part.startsWith("constraint_soft_no_add_overridden:")) return `反驳持仓“不主动加”约束，证据分 ${formatReasonScore(part.split(":")[1], "prompt")}`;
      if (part.startsWith("constraint_soft_no_add:")) return `尊重持仓“不主动加”约束，证据分 ${formatReasonScore(part.split(":")[1], "prompt")}`;
      if (part.startsWith("constraint_soft_no_reduce_overridden:")) return `反驳持仓“不主动减”约束，证据分 ${formatReasonScore(part.split(":")[1], "prompt")}`;
      if (part.startsWith("constraint_soft_no_reduce:")) return `尊重持仓“不主动减”约束，证据分 ${formatReasonScore(part.split(":")[1], "prompt")}`;
      if (part === "no_position_to_reduce") return "无持仓可减";
      if (part === "shares_round_to_zero") return "金额不足 1 股";
      if (part === "no_data") return "缺少数据";
      if (part === "thesis_broken") return "基本面逻辑破坏";
      if (part.startsWith("thesis_watch")) return "基本面状态：观察";
      if (part.startsWith("research_bias:")) return `研究/主观覆盖修正 ${formatReasonScore(part.split(":")[1], "prompt")}`;
      if (part.startsWith("quote_stale:")) return `行情时间过旧 ${part.split(":")[1] || ""}`;
      return part;
    })
    .join("；");
}

function levelCategoryLabel(category) {
  if (category === "intraday") return "日内辅助";
  if (category === "volume_profile") return "成交密集";
  if (category === "volume_void") return "低量真空";
  if (category === "swing") return "摆动点";
  if (category === "reference") return "参考线";
  if (category === "daily_structure") return "历史结构";
  return "结构位";
}

function translateWarning(warning) {
  const text = String(warning || "");
  if (text.includes("yahoo_chart is a baseline/daily snapshot provider")) {
    return "Yahoo chart 是基线/日线快照源，不是实盘实时行情。";
  }
  if (text.includes("missing historical CSV")) {
    return text.replace("missing historical CSV", "缺少历史日线 CSV");
  }
  if (text.includes("history refresh failed")) {
    return text.replace("history refresh failed", "历史日线刷新失败");
  }
  if (text.includes("no yahoo chart daily history returned")) {
    return text.replace("no yahoo chart daily history returned", "Yahoo chart 未返回日线数据");
  }
  return text;
}

function translateError(message) {
  const text = String(message || "");
  if (text.includes("10197") || text.includes("competing live session")) {
    return "IBKR 已连接，但行情被其他 live session 占用。请关闭同账号其它占用实时行情的 TWS/Gateway/移动端行情窗口，或换一个有行情权限的用户，再重试；也可以暂时用付费数据源。";
  }
  if (text.includes("IBKR 未返回任何行情")) {
    return text.replace("IBKR 未返回任何行情。", "IBKR 未返回任何行情。");
  }
  if (text.includes("Timed out connecting to IBKR")) {
    return "连接 IBKR 超时。请确认 Gateway/TWS 已登录、API 已启用、端口填写正确。";
  }
  if (text.includes("Missing yfinance")) {
    return "缺少 yfinance 依赖，请先安装 requirements.txt。";
  }
  return text;
}

function setRunBusy(value) {
  isRunning = value;
  $("runBtn").disabled = value;
  $("runBtn").textContent = value ? "生成中..." : "生成调仓建议";
  $("runProgress").hidden = !value;
  for (const id of ["savePortfolioBtn", "parseTextBtn", "saveSettingsBtn", "reloadBtn"]) {
    const element = $(id);
    if (!element) continue;
    element.disabled = id === "parseTextBtn" ? true : value;
  }
}

function setIbkrTestBusy(value) {
  $("testIbkrBtn").disabled = value;
  $("testIbkrBtn").textContent = value ? "测试中..." : "测试 IBKR 行情";
}

function emptyPortfolio() {
  const positions = {};
  for (const symbol of defaultSymbols) {
    positions[symbol] = { shares: 0, avg_cost: "", thesis_status: "intact", conviction: 1, bucket: "auto", trade_constraint: "flexible" };
  }
  return { account_equity: "", cash: 0, margin_debit: 0, maintenance_margin: "", target_gross_hint: "", positions };
}

function currentPortfolioFromForm() {
  const rows = [...document.querySelectorAll("#positionsBody tr")];
  const positions = {};
  for (const row of rows) {
    const symbol = row.querySelector("[data-field='symbol']").value.trim().toUpperCase();
    if (!symbol) continue;
    positions[symbol] = {
      shares: Number(row.querySelector("[data-field='shares']").value || 0),
      avg_cost: row.querySelector("[data-field='avg_cost']").value === "" ? null : Number(row.querySelector("[data-field='avg_cost']").value),
      thesis_status: row.querySelector("[data-field='thesis_status']").value,
      conviction: Number(row.querySelector("[data-field='conviction']").value || 1),
      bucket: row.querySelector("[data-field='bucket']").value,
      trade_constraint: row.querySelector("[data-field='trade_constraint']").value,
    };
  }
  return {
    account_equity: Number($("accountEquity").value || 0),
    cash: Number($("cash").value || 0),
    margin_debit: Number($("marginDebit").value || 0),
    maintenance_margin: $("maintenanceMargin").value === "" ? null : Number($("maintenanceMargin").value),
    target_gross_hint: $("targetGrossHint").value === "" ? null : Number($("targetGrossHint").value),
    positions,
  };
}

function setPortfolioForm(portfolio) {
  const data = portfolio || emptyPortfolio();
  $("accountEquity").value = data.account_equity ?? "";
  $("cash").value = data.cash ?? 0;
  $("marginDebit").value = data.margin_debit ?? 0;
  $("maintenanceMargin").value = data.maintenance_margin ?? "";
  $("targetGrossHint").value = data.target_gross_hint ?? "";
  const body = $("positionsBody");
  body.innerHTML = "";
  const positions = data.positions || {};
  const symbols = Object.keys(positions).length ? Object.keys(positions).sort() : defaultSymbols;
  for (const symbol of symbols) {
    addPositionRow(symbol, positions[symbol] || {});
  }
}

function addPositionRow(symbol = "", position = {}) {
  const row = document.createElement("tr");
  row.innerHTML = `
    <td><input class="symbol-input" data-field="symbol" value="${symbol}" /></td>
    <td><input class="number-input" data-field="shares" type="number" step="1" value="${position.shares ?? 0}" /></td>
    <td><input class="number-input" data-field="avg_cost" type="number" step="0.01" value="${position.avg_cost ?? ""}" /></td>
    <td>
      <select data-field="thesis_status">
        <option value="intact">${thesisLabels.intact}</option>
        <option value="watch">${thesisLabels.watch}</option>
        <option value="broken">${thesisLabels.broken}</option>
      </select>
    </td>
    <td><input class="number-input" data-field="conviction" type="number" min="0" max="1.25" step="0.05" value="${position.conviction ?? 1}" /></td>
    <td>
      <select data-field="bucket">
        <option value="auto">${bucketLabels.auto}</option>
        <option value="core">${bucketLabels.core}</option>
        <option value="satellite">${bucketLabels.satellite}</option>
        <option value="watch">${bucketLabels.watch}</option>
        <option value="trim">${bucketLabels.trim}</option>
      </select>
    </td>
    <td>
      <select data-field="trade_constraint">
        <option value="flexible">${constraintLabels.flexible}</option>
        <option value="prefer_hold">${constraintLabels.prefer_hold}</option>
        <option value="soft_no_add">${constraintLabels.soft_no_add}</option>
        <option value="soft_no_reduce">${constraintLabels.soft_no_reduce}</option>
        <option value="reduce_only">${constraintLabels.reduce_only}</option>
      </select>
    </td>
    <td><button class="remove-btn" type="button">x</button></td>
  `;
  row.querySelector("[data-field='thesis_status']").value = position.thesis_status || "intact";
  row.querySelector("[data-field='bucket']").value = position.bucket || "auto";
  row.querySelector("[data-field='trade_constraint']").value = position.trade_constraint || "flexible";
  row.querySelector(".remove-btn").addEventListener("click", () => row.remove());
  $("positionsBody").appendChild(row);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "request failed");
  return payload;
}

function setStatus(text) {
  $("statusLine").textContent = text;
}

function scoreSummary(score) {
  const entries = Object.entries(score || {});
  if (!entries.length) return "";
  return entries
    .slice(0, 4)
    .map(([key, value]) => {
      if (value && typeof value === "object" && Object.hasOwn(value, "score")) {
        const label = value.label || key;
        return `${label}: ${formatScore(value.score, value.score_range, label)}`;
      }
      if (String(key).includes("分")) {
        return `${key}: ${formatScore(value, null, key)}`;
      }
      return `${key}: ${typeof value === "object" ? JSON.stringify(value) : value}`;
    })
    .join(" · ");
}

function scoreReference(item) {
  if (item?.reference) return item.reference;
  const label = item?.label || "";
  if (label.includes("量价")) return ">=3 量价共振强；1 到 3 偏强；-1 到 1 震荡；<=-3 明显转弱。";
  if (label.includes("市场")) return ">=4 风险偏多；-2 到 4 中性；<=-2 风险收缩。";
  if (label.includes("技术")) return ">=4 强趋势；1 到 4 可持有/观察；<=0 弱势。";
  if (label.includes("分钟")) return ">=3 强势上行；-1 到 1 震荡；<=-3 强势下行。";
  if (label.includes("想法") || label.includes("约束")) return "0 中性；<0 降低风险/加仓门槛；>0 提高进攻倾向。";
  if (label.includes("杠杆") || label.includes("保证金")) return "杠杆余量 >0 正常；填维持保证金后会显示安全垫。";
  return "";
}

function renderScorecard(scorecard) {
  const entries = Object.values(scorecard || {});
  if (!entries.length) return `<div class="muted">暂无评分</div>`;
  return `
    <div class="score-grid">
      ${entries
        .map(
          (item) => `
            <div class="score-item">
              <span>${escapeHtml(item.label || "-")}</span>
              <strong class="score-value">${escapeHtml(formatScore(item.score, item.score_range, item.label))}</strong>
              <em>${escapeHtml(item.verdict || "")}</em>
              <p>${escapeHtml(item.detail || "")}</p>
              ${scoreReference(item) ? `<small>参考：${escapeHtml(scoreReference(item))}</small>` : ""}
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderProgress(progress) {
  if (!progress) return;
  const win = $("researchWindow");
  win.hidden = false;
  const steps = progress.steps || [];
  const statusText = progress.status === "failed" ? "失败" : progress.active ? "运行中" : progress.status === "done" ? "完成" : "空闲";
  $("progressMeta").textContent = `${statusText} · ${labelKind(progress.kind)} · ${progress.run_id || "未开始"}`;
  const width = progress.status === "done" ? 100 : Math.min(96, Math.max(8, steps.length * 9));
  $("progressFill").style.width = `${width}%`;
  $("progressScorecard").innerHTML = renderScorecard(progress.scorecard);
  $("progressSteps").innerHTML = steps
    .map(
      (step) => `
        <li class="${step.status === "failed" ? "failed" : ""}">
          <div>
            <strong>${escapeHtml(step.name)}</strong>
            <span>${new Date(step.time).toLocaleTimeString()}</span>
          </div>
          <p>${escapeHtml(step.detail || "")}</p>
          ${scoreSummary(step.score) ? `<small>${escapeHtml(scoreSummary(step.score))}</small>` : ""}
        </li>
      `,
    )
    .join("");
  if (progress.error) {
    $("progressSteps").innerHTML += `<li class="failed"><div><strong>错误</strong></div><p>${escapeHtml(translateError(progress.error))}</p></li>`;
  }
}

async function pollProgress() {
  try {
    const payload = await api("/api/progress");
    renderProgress(payload.progress);
  } catch (error) {
    setStatus(`读取进度失败：${translateError(error.message)}`);
  }
}

function startProgressPolling() {
  clearInterval(progressTimer);
  pollProgress();
  progressTimer = setInterval(pollProgress, 1000);
}

function stopProgressPolling() {
  clearInterval(progressTimer);
  progressTimer = null;
  pollProgress();
}

function renderDecisionFactors(factors) {
  if (!factors || !factors.length) return "";
  return `
    <div class="decision-factors">
      ${factors
        .map(
          (factor) => `
            <div class="factor-item">
              <strong>${escapeHtml(factor.name || "-")}</strong>
              <span>${escapeHtml(factor.weight || "")}</span>
              <em>${escapeHtml(factor.status || "")}</em>
              <p>${escapeHtml(factor.detail || "")}</p>
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function findScore(scorecard, key) {
  return (scorecard || {})[key] || null;
}

function findSource(sources, type) {
  return (sources || []).find((source) => source.type === type);
}

function findStep(steps, pattern) {
  return (steps || []).find((step) => pattern.test(String(step.name || "")));
}

function processStatusBadge(status, tone = "neutral") {
  return `<span class="process-status ${escapeHtml(tone)}">${escapeHtml(status)}</span>`;
}

function processMetric(label, value, title = "") {
  return `<em title="${escapeHtml(title)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></em>`;
}

function scoreMetric(scorecard, key, fallbackLabel) {
  const item = findScore(scorecard, key);
  if (!item) return processMetric(fallbackLabel, "-", "本轮没有该项评分。");
  return processMetric(item.label || fallbackLabel, formatScore(item.score, item.score_range, item.label), item.reference || item.detail || "");
}

function sourceStatus(sources, type, activeText = "已接入", inactiveText = "本轮未用") {
  return findSource(sources, type) ? activeText : inactiveText;
}

function renderResearchFlow(process) {
  const scorecard = process.scorecard || {};
  const sources = process.sources || [];
  const steps = process.steps || [];
  const hasIntraday = !!findSource(sources, "intraday");
  const hasPrompt = !!findSource(sources, "manual_prompt");
  const hasLlm = !!findSource(sources, "llm_candidate_selector");
  const llmStep = findStep(steps, /LLM 点位复核完成|LLM 点位复核跳过|LLM 点位复核/);
  const cards = [
    {
      title: "1. 持仓与账户输入",
      status: processStatusBadge("手动表格", "ok"),
      body: "持仓、成本、状态、信心、桶、约束、账户净值、现金、融资、维持保证金和目标杠杆只来自网页/本地输入；IBKR 不读取账户。",
      metrics: [
        processMetric("持仓来源", sourceStatus(sources, "portfolio", "BBAE 手动", "未记录")),
        processMetric("自然语言持仓", "禁用", "预留入口，暂未接入 LLM；本轮不参与计算。"),
      ],
      method: "当前持仓表是唯一账户真相；成交后需要你手动改表。",
    },
    {
      title: "2. 行情与历史数据",
      status: processStatusBadge(hasIntraday ? "含分钟线" : "日线+快照", hasIntraday ? "ok" : "warn"),
      body: "实时/快照价格用于现价、市值和限价参考；历史日线用于均线、趋势、量价结构和支撑压力。日线会检查是否过旧，过旧则刷新到最近完整美股交易日。",
      metrics: [
        processMetric("行情源", sourceStatus(sources, "market_data", "已读取", "缺失")),
        processMetric("历史日线", sourceStatus(sources, "historical_daily", "已检查", "缺失")),
        processMetric("当日分钟线", hasIntraday ? "已接入" : "本轮缺失", "IBKR HMDS 若不可用，会在 warning 里显示。"),
      ],
      method: "IBKR 只调用 market data；Yahoo Chart 只作日线/兜底数据源，不视为实盘行情源。",
    },
    {
      title: "3. 量价结构主锚",
      status: processStatusBadge("主研究项", "ok"),
      body: "先找近期支撑/压力，再按成交分布和结构强度排序。买点偏向历史支撑下沿，卖点偏向历史压力/价值区上沿，日内指标只做辅助。",
      metrics: [
        scoreMetric(scorecard, "price_volume", "近期量价点位"),
        scoreMetric(scorecard, "technical", "趋势/均线"),
        scoreMetric(scorecard, "intraday", "当日分钟线"),
      ],
      method: "口径：5/10/20/60/90/126日结构、Volume Profile 的 POC/VAH/VAL/HVN/LVN、筹码占比、成交占比、共振、触碰次数、距现价、20日量比、VWAP/近30分钟。",
    },
    {
      title: "4. 仓位与杠杆预算",
      status: processStatusBadge("硬约束", "danger"),
      body: "股数不是先拍脑袋，而是先算目标总仓和可交易预算，再按核心/卫星/观察桶、信心、约束、维持保证金安全垫和压力测试切分。",
      metrics: [
        scoreMetric(scorecard, "risk", "杠杆/保证金"),
        processMetric("数量来源", "预算倒推", "目标杠杆 × 净值 -> 目标总仓；差额 -> 可买/需卖金额；再换算股数。"),
      ],
      method: "强制项：不超过最大杠杆；维持保证金安全垫不足时限制新增买单；卖单不超过持仓股数。",
    },
    {
      title: "5. 你的想法与弱覆盖因子",
      status: processStatusBadge(hasPrompt ? "本轮有输入" : "本轮未输入", hasPrompt ? "ok" : "neutral"),
      body: "本次调仓想法会进入本地 prompt 解析、点位复核 LLM 和重点总结。普通“不主动加/减”是软约束；明确“绝对禁止”才作为硬约束。",
      metrics: [
        scoreMetric(scorecard, "prompt", "本次想法/约束"),
        processMetric("外部事件/研报", sourceStatus(sources, "manual_research_overlay", "弱覆盖", "暂未自动化"), "目前只作为辅助提示，不作为主决策锚。"),
      ],
      method: "当前自动化数据源不足的宏观、研报、IPO/流动性先弱化展示；后续接可靠源后再升权重。",
    },
    {
      title: "6. LLM 复核与输出",
      status: processStatusBadge(hasLlm ? "LLM 已接入" : "本地确定性", hasLlm ? "ok" : "neutral"),
      body: "后端先生成候选支撑/压力价和预算，LLM 只允许在候选价里选或解释，不允许凭空编新价格；最终再生成中文重点总结。",
      metrics: [
        processMetric("点位复核", llmStep ? llmStep.name : "未记录", llmStep?.detail || ""),
        processMetric("输出", "建议挂单/价梯/总结", "保存到跑批记录，前端展示并可回看。"),
      ],
      method: "价格来自量价结构，数量来自杠杆预算，触发来自事件/想法，否决权来自风控和你的信念排序。",
    },
  ];

  return `
    <div class="research-flow">
      ${cards
        .map(
          (card) => `
            <article class="research-flow-card">
              <div class="research-flow-head">
                <strong>${escapeHtml(card.title)}</strong>
                ${card.status}
              </div>
              <p>${escapeHtml(card.body)}</p>
              <div class="research-flow-metrics">${card.metrics.join("")}</div>
              <small>${escapeHtml(card.method)}</small>
            </article>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderProcessSteps(steps) {
  const rows = (steps || []).map((step, index) => {
    const score = scoreSummary(step.score);
    return `
      <li class="${step.status === "failed" ? "failed" : ""}">
        <span>${String(index + 1).padStart(2, "0")}</span>
        <div>
          <strong>${escapeHtml(step.name)}</strong>
          <p>${escapeHtml(step.detail || "")}</p>
          ${score ? `<small>${escapeHtml(score)}</small>` : ""}
        </div>
      </li>
    `;
  });
  return `<ol class="process-step-list">${rows.join("") || `<li><div><p>暂无过程记录</p></div></li>`}</ol>`;
}

function renderSourceGrid(sources) {
  const rows = (sources || []).map(
    (source) => `
      <li>
        <strong>${escapeHtml(source.name)}</strong>
        <em>${escapeHtml(source.type || "")}</em>
        <span>${escapeHtml(source.usage || "")}</span>
      </li>
    `,
  );
  return `<ul class="process-source-grid">${rows.join("") || `<li><span>暂无来源记录</span></li>`}</ul>`;
}

function renderResearchProcess(process) {
  const target = $("researchProcess");
  if (!process) {
    target.innerHTML = `<div class="muted">暂无研究过程</div>`;
    return;
  }
  target.innerHTML = `
    ${renderResearchFlow(process)}
    <details class="process-detail-block" open>
      <summary>评分与阈值</summary>
      ${renderScorecard(process.scorecard)}
    </details>
    <details class="process-detail-block">
      <summary>决策因子</summary>
      ${renderDecisionFactors(process.decision_factors)}
    </details>
    <div class="process-columns">
      <div>
        <h4>运行步骤</h4>
        ${renderProcessSteps(process.steps)}
      </div>
      <div>
        <h4>数据源边界</h4>
        ${renderSourceGrid(process.sources)}
      </div>
    </div>
  `;
}

function levelPosition(price, range) {
  const min = Number(range?.min);
  const max = Number(range?.max);
  const value = Number(price);
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min || !Number.isFinite(value)) return 50;
  return Math.max(2, Math.min(98, ((value - min) / (max - min)) * 100));
}

function levelMetricText(level) {
  const chips = [];
  if (level.poc_context) chips.push(`${level.poc_context}/${level.poc_side || ""}`);
  if (level.chip_share_pct !== undefined && level.chip_share_pct !== null) chips.push(`筹码${levelWindowText(level)} ${fmtPct(level.chip_share_pct)}`);
  if (level.level_strength_score !== undefined && level.level_strength_score !== null) chips.push(`力度 ${formatScore(level.level_strength_score, level.level_strength_range, "力度")}`);
  if (level.confluence_count !== undefined && level.confluence_count !== null) chips.push(`共振 ${level.confluence_count}`);
  if (level.touch_count !== undefined && level.touch_count !== null) chips.push(`触碰126日 ${level.touch_count}`);
  if (level.profile_role) chips.push(String(level.profile_role));
  if (level.tier) chips.push(String(level.tier));
  return chips.join(" · ");
}

function levelConfidence(level) {
  const score = Number(level?.level_strength_score);
  if (!Number.isFinite(score)) {
    return { label: "待判", className: "low", title: "暂无足够指标计算力度。" };
  }
  const label = score >= 4.5 ? "高" : score >= 3.4 ? "中高" : score >= 2.2 ? "中" : "低";
  const className = score >= 4.5 ? "high" : score >= 3.4 ? "mid-high" : score >= 2.2 ? "mid" : "low";
  return {
    label,
    className,
    title: `力度 ${formatScore(score, level.level_strength_range, "力度")}。由类别权重、筹码/成交占比、共振数量、历史触碰次数和最近性合成，范围 0~5。`,
  };
}

function levelPriorityScore(level) {
  const strength = Number(level?.level_strength_score);
  const distance = Math.abs(Number(level?.distance_pct || 0));
  const chip = Number(level?.chip_share_pct ?? level?.volume_share_pct ?? 0);
  const confluence = Number(level?.confluence_count || 0);
  const role = String(level?.profile_role || "").toUpperCase();
  const tier = String(level?.tier || "");
  const category = String(level?.category || "");
  const tierScore = tier === "近端" ? 2.2 : tier === "主结构" ? 1.25 : tier === "日内辅助" ? 0.2 : tier === "深水/高抛" ? -2.2 : 0;
  const roleScore = role === "POC" || role === "HVN" ? 0.45 : role === "VAH" || role === "VAL" ? 0.3 : role === "LVN" ? -0.35 : 0;
  const categoryScore = category === "volume_profile" ? 0.65 : category === "swing" ? 0.45 : category === "daily_structure" ? 0.25 : category === "intraday" ? -0.25 : 0;
  const deepPenalty = tier === "深水/高抛" && distance > 0.2 ? 2.4 : 0;
  return (
    (Number.isFinite(strength) ? strength : 0) +
    tierScore +
    roleScore +
    categoryScore +
    Math.min(1.2, chip * 4) +
    Math.min(1.0, confluence * 0.12) -
    Math.min(2.0, distance * 7) -
    deepPenalty
  );
}

function sortedLevels(levels) {
  return [...(levels || [])].sort((a, b) => levelPriorityScore(b) - levelPriorityScore(a));
}

function levelWindowText(level) {
  const window = Number(level?.profile_window);
  if (Number.isFinite(window) && window > 0) return `${window}日`;
  const source = String(level?.source || "");
  const match = source.match(/近?(\d+)日/);
  if (match) return `${match[1]}日`;
  if (String(level?.category || "") === "intraday") return "日内";
  return "";
}

function levelHelpText(level, key) {
  const windowText = levelWindowText(level);
  if (key === "role") {
    const role = String(level?.profile_role || "").toUpperCase();
    if (role === "POC") return "POC 是该窗口成交最密集价。低于现价偏支撑侧，高于现价偏压力侧；近端/主结构/深水由距现价幅度划分。";
    if (role === "HVN") return "HVN 是高量节点，表示该价格箱成交密集，常作为支撑或压力观察区。";
    if (role === "VAH") return "VAH 是价值区上沿，表示主要成交分布的上边界，常作为压力/减仓参考。";
    if (role === "VAL") return "VAL 是价值区下沿，表示主要成交分布的下边界，常作为支撑/买入参考。";
    if (role === "LVN") return "LVN 是低量真空，代表成交稀疏区，穿越可能更快，承接置信度通常低于 POC/HVN。";
    return "该角标表示 Volume Profile 角色。";
  }
  if (key === "chip") return `筹码占比：按${windowText || "该指标"}窗口的价格箱统计成交量，并把该价附近相邻价格箱合并后占总成交量的比例。`;
  if (key === "volume") return `成交占比：该价格箱自身成交量占${windowText || "该指标"}窗口总成交量的比例。`;
  if (key === "confluence") return "共振：在现价约 0.35% 容差内，其他支撑/压力指标落在同一区域的数量；这是当前候选集合里的横向共振，不是 N 日窗口。";
  if (key === "touch") return "触碰：默认回看近 126 根日线，该点位附近被日线高低区间触达的次数；它不是指标名称里的 N 日窗口。";
  if (key === "tier") return "层级：近端为距现价约 3.5% 内，主结构约 12% 内，更远为深水/高抛。";
  return "";
}

function metricBadge(label, value, title, className = "") {
  return `<em class="metric-badge ${escapeHtml(className)}" title="${escapeHtml(title)}">${escapeHtml(label)} ${escapeHtml(value)}</em>`;
}

function levelMetricBadges(level) {
  const badges = [];
  const confidence = levelConfidence(level);
  badges.push(metricBadge("置信", confidence.label, confidence.title, `confidence-${confidence.className}`));
  if (level.chip_share_pct !== undefined && level.chip_share_pct !== null) badges.push(metricBadge(`筹码${levelWindowText(level)}`, fmtPct(level.chip_share_pct), levelHelpText(level, "chip")));
  if (level.volume_share_pct !== undefined && level.volume_share_pct !== null) badges.push(metricBadge(`成交${levelWindowText(level)}`, fmtPct(level.volume_share_pct), levelHelpText(level, "volume")));
  if (level.confluence_count !== undefined && level.confluence_count !== null) badges.push(metricBadge("共振", level.confluence_count, levelHelpText(level, "confluence")));
  if (level.touch_count !== undefined && level.touch_count !== null) badges.push(metricBadge("触碰126日", level.touch_count, levelHelpText(level, "touch")));
  if (level.tier) badges.push(metricBadge("层级", level.tier, levelHelpText(level, "tier")));
  if (level.poc_context) badges.push(metricBadge("POC", `${level.poc_context}/${level.poc_side || ""}`, levelHelpText(level, "role"), "poc-context"));
  return badges.join("");
}

function levelRoleClass(level) {
  const role = String(level?.profile_role || "").toUpperCase();
  if (!role) return "";
  return `level-role-${role.toLowerCase().replace(/[^a-z0-9_-]/g, "")}`;
}

function levelRoleBadge(level) {
  const role = String(level?.profile_role || "").toUpperCase();
  if (!role) return "";
  return `<em class="role-badge ${levelRoleClass(level)}" title="${escapeHtml(levelHelpText(level, "role"))}">${escapeHtml(role)}</em>`;
}

function renderLevelMarkers(item) {
  const range = item.chart_range || {};
  const width = 560;
  const height = 112;
  const axisStart = 34;
  const axisEnd = 526;
  const axisY = 54;
  const safeSymbol = String(item.symbol || "symbol").replace(/[^a-zA-Z0-9_-]/g, "");
  const xFor = (price) => axisStart + (levelPosition(price, range) / 100) * (axisEnd - axisStart);
  const supportTicks = sortedLevels(item.supports || [])
    .slice(0, 2)
    .map((level) => {
      const x = xFor(level.price);
      const title = `支撑 ${fmtPrice(level.price)} · ${level.source || ""} · 距现价 ${fmtPct(level.distance_pct)}${levelMetricText(level) ? ` · ${levelMetricText(level)}` : ""}`;
      return `<line class="axis-tick support ${levelRoleClass(level)}" x1="${x.toFixed(1)}" y1="34" x2="${x.toFixed(1)}" y2="76"><title>${escapeHtml(title)}</title></line>`;
    })
    .join("");
  const resistanceTicks = sortedLevels(item.resistances || [])
    .slice(0, 2)
    .map((level) => {
      const x = xFor(level.price);
      const title = `压力 ${fmtPrice(level.price)} · ${level.source || ""} · 距现价 ${fmtPct(level.distance_pct)}${levelMetricText(level) ? ` · ${levelMetricText(level)}` : ""}`;
      return `<line class="axis-tick resistance ${levelRoleClass(level)}" x1="${x.toFixed(1)}" y1="34" x2="${x.toFixed(1)}" y2="76"><title>${escapeHtml(title)}</title></line>`;
    })
    .join("");
  const referenceTicks = [
    { key: "sma20", label: "MA20", price: item.sma20, klass: "ma20" },
    { key: "sma50", label: "MA50", price: item.sma50, klass: "ma50" },
    { key: "trend_stop", label: "止损", price: item.trend_stop, klass: "stop" },
  ]
    .filter((marker) => marker.price)
    .map((marker) => {
      const x = xFor(marker.price);
      return `<g class="axis-ref ${marker.klass}">
        <line x1="${x.toFixed(1)}" y1="28" x2="${x.toFixed(1)}" y2="82"><title>${escapeHtml(`${marker.label} ${fmtPrice(marker.price)}`)}</title></line>
      </g>`;
    })
    .join("");
  const currentX = xFor(item.price);
  const lowX = xFor(item.range_low);
  const highX = xFor(item.range_high);
  const low = Math.min(lowX, highX);
  const span = Math.max(2, Math.abs(highX - lowX));
  return `
    <div class="level-axis">
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(item.symbol)} 价格区间、支撑压力、均线和止损">
        <defs>
          <linearGradient id="levelGradient-${safeSymbol}" x1="0" x2="1">
            <stop offset="0%" stop-color="#eef7f3"></stop>
            <stop offset="50%" stop-color="#ffffff"></stop>
            <stop offset="100%" stop-color="#fff3ef"></stop>
          </linearGradient>
        </defs>
        <rect class="axis-bg" x="${axisStart}" y="42" width="${axisEnd - axisStart}" height="24" rx="4" fill="url(#levelGradient-${safeSymbol})"></rect>
        <rect class="range-window" x="${low.toFixed(1)}" y="44" width="${span.toFixed(1)}" height="20" rx="3"></rect>
        <line class="axis-base" x1="${axisStart}" y1="${axisY}" x2="${axisEnd}" y2="${axisY}"></line>
        ${supportTicks}
        ${resistanceTicks}
        ${referenceTicks}
        <g class="axis-current">
          <line x1="${currentX.toFixed(1)}" y1="18" x2="${currentX.toFixed(1)}" y2="88"></line>
          <rect x="${Math.max(axisStart, Math.min(axisEnd - 86, currentX - 43)).toFixed(1)}" y="4" width="86" height="24" rx="5"></rect>
          <text x="${Math.max(axisStart + 43, Math.min(axisEnd - 43, currentX)).toFixed(1)}" y="20">现价 ${fmtPrice(item.price)}</text>
          <title>${escapeHtml(`现价 ${fmtPrice(item.price)}；60日位置 ${fmtPct(item.range_position)}`)}</title>
        </g>
        <text class="axis-min" x="${axisStart}" y="106">${fmtPrice(range.min)}</text>
        <text class="axis-max" x="${axisEnd}" y="106">${fmtPrice(range.max)}</text>
      </svg>
      <div class="level-legend">
        <span><i class="support"></i>高优先级支撑</span>
        <span><i class="resistance"></i>高优先级压力</span>
        <span><i class="ma"></i>MA20/MA50</span>
        <span><i class="stop"></i>止损线</span>
      </div>
    </div>
  `;
}

function renderLevelList(levels, emptyText, maxRows = null) {
  const allRows = sortedLevels(levels);
  const rows = Number.isFinite(maxRows) ? allRows.slice(0, maxRows) : allRows;
  if (!rows.length) return `<li class="muted">${escapeHtml(emptyText)}</li>`;
  return rows
    .map(
      (level) => {
        const metrics = levelMetricText(level);
        return `
        <li class="${levelRoleClass(level)}" title="${escapeHtml(`${level.source || ""} · 距现价 ${fmtPct(level.distance_pct)}${metrics ? ` · ${metrics}` : ""}`)}">
          <strong>${fmtPrice(level.price)}</strong>
          <span><em>${escapeHtml(levelCategoryLabel(level.category))}</em>${levelRoleBadge(level)}${escapeHtml(level.source || "")} · 距现价 ${fmtPct(level.distance_pct)}</span>
          <small class="level-metrics">${levelMetricBadges(level)}</small>
        </li>
      `;
      },
    )
    .join("");
}

function renderVolumeBars(item) {
  const rows = item.recent_volume || [];
  if (!rows.length) return `<div class="muted">暂无近期量能</div>`;
  const maxVolume = Math.max(...rows.map((row) => Number(row.volume || 0)), 1);
  return `
    <div class="volume-bars" title="最近12根日线成交量，绿色为收涨，红色为收跌。">
      ${rows
        .map((row) => {
          const height = Math.max(8, Math.round((Number(row.volume || 0) / maxVolume) * 100));
          const title = chartRowTitle(row);
          return `<span class="${row.up ? "up" : "down"}" style="height:${height}%" title="${escapeHtml(title)}"></span>`;
        })
        .join("")}
    </div>
  `;
}

function renderMiniTechChart(item) {
  const rows = item.recent_volume || [];
  if (rows.length < 2) return renderVolumeBars(item);
  const width = 380;
  const height = 136;
  const left = 18;
  const right = width - 14;
  const priceTop = 12;
  const priceBottom = 78;
  const volumeTop = 92;
  const volumeBottom = 126;
  const closes = rows.map((row) => Number(row.close || 0)).filter((value) => Number.isFinite(value) && value > 0);
  const volumes = rows.map((row) => Number(row.volume || 0));
  const minClose = Math.min(...closes);
  const maxClose = Math.max(...closes);
  const closeSpan = Math.max(0.01, maxClose - minClose);
  const maxVolume = Math.max(...volumes, 1);
  const xFor = (index) => left + (index * (right - left)) / Math.max(1, rows.length - 1);
  const yForClose = (close) => priceBottom - ((Number(close) - minClose) / closeSpan) * (priceBottom - priceTop);
  const points = rows.map((row, index) => `${xFor(index).toFixed(1)},${yForClose(row.close).toFixed(1)}`).join(" ");
  const barWidth = Math.max(6, Math.min(18, (right - left) / rows.length - 4));
  const volumeRects = rows
    .map((row, index) => {
      const volumeHeight = Math.max(2, (Number(row.volume || 0) / maxVolume) * (volumeBottom - volumeTop));
      const x = xFor(index) - barWidth / 2;
      const y = volumeBottom - volumeHeight;
      const klass = `${row.up ? "up" : "down"}${row.is_current ? " current" : ""}`;
      const title = chartRowTitle(row);
      return `<rect class="volume-bar ${klass}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${volumeHeight.toFixed(1)}"><title>${escapeHtml(title)}</title></rect>`;
    })
    .join("");
  const pointCircles = rows
    .map((row, index) => {
      const title = chartRowTitle(row);
      return `<circle class="chart-point${row.is_current ? " current" : ""}" cx="${xFor(index).toFixed(1)}" cy="${yForClose(row.close).toFixed(1)}" r="${row.is_current ? 5 : 3.5}"><title>${escapeHtml(title)}</title></circle>`;
    })
    .join("");
  return `
    <div class="mini-tech-chart" title="最近12根日线 + 当前价：上方为收盘价走势，下方为成交量柱。">
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(item.symbol)} 最近12根日线和当前价">
        <line class="chart-grid" x1="${left}" y1="${priceTop}" x2="${right}" y2="${priceTop}"></line>
        <line class="chart-grid" x1="${left}" y1="${priceBottom}" x2="${right}" y2="${priceBottom}"></line>
        <line class="chart-grid" x1="${left}" y1="${volumeBottom}" x2="${right}" y2="${volumeBottom}"></line>
        ${volumeRects}
        <polyline class="price-line" points="${points}"></polyline>
        ${pointCircles}
      </svg>
    </div>
  `;
}

function renderTechnicalAnalysis(analysis) {
  const target = $("technicalAnalysis");
  const entries = Object.values(analysis || {}).sort((a, b) => String(a.symbol).localeCompare(String(b.symbol)));
  if (!entries.length) {
    target.innerHTML = `<div class="muted">暂无量价点位</div>`;
    return;
  }
  target.innerHTML = `
    <div class="technical-grid">
      ${entries
        .map((item) => {
          const label = priceVolumeLabels[item.label] || item.label || "-";
          const components = (item.components || [])
            .map(
              (component) =>
                `<li><strong>${escapeHtml(component.name)}</strong><span>${escapeHtml(formatScore(component.score, component.score_range, component.name))} · ${escapeHtml(component.detail || "")}</span></li>`,
            )
            .join("");
          return `
            <article class="tech-card">
              <div class="tech-head">
                <div>
                  <strong>${escapeHtml(item.symbol)}</strong>
                  <span>现价 ${fmtPrice(item.price)} · ${label} ${escapeHtml(formatScore(item.score, item.score_range, "price_volume"))}</span>
                </div>
                <div class="tech-actions">
                  <small>60日位置 ${fmtPct(item.range_position)} · 20日量比 ${escapeHtml(item.volume_ratio20 ?? "-")}</small>
                  <button class="link-button open-tech-modal" type="button" data-symbol="${escapeHtml(item.symbol)}">查看大图</button>
                </div>
              </div>
              ${renderLevelMarkers(item)}
              ${renderMiniTechChart(item)}
              <div class="level-columns">
                <div>
                  <h4>支撑</h4>
                  <ul>${renderLevelList(item.supports, "暂无近端支撑", 6)}</ul>
                </div>
                <div>
                  <h4>压力</h4>
                  <ul>${renderLevelList(item.resistances, "暂无近端压力", 6)}</ul>
                </div>
              </div>
              <details class="tech-detail">
                <summary>评分拆解</summary>
                <p>${escapeHtml(item.explanation || "")}</p>
                <ul>${components || `<li class="muted">暂无拆解</li>`}</ul>
              </details>
            </article>
          `;
        })
        .join("")}
    </div>
  `;
}

function priceY(value, min, max, top, bottom) {
  if (!Number.isFinite(value) || max <= min) return (top + bottom) / 2;
  return bottom - ((value - min) / (max - min)) * (bottom - top);
}

function renderLargePriceVolumeChart(item) {
  const rows = item.recent_bars || item.recent_volume || [];
  if (rows.length < 2) return `<div class="muted">暂无足够日线数据</div>`;
  const width = 980;
  const height = 420;
  const left = 46;
  const right = 920;
  const top = 18;
  const priceBottom = 292;
  const volumeTop = 322;
  const volumeBottom = 390;
  const highs = rows.map((row) => Number(row.high ?? row.close)).filter(Number.isFinite);
  const lows = rows.map((row) => Number(row.low ?? row.close)).filter(Number.isFinite);
  const levelPrices = [...(item.supports || []), ...(item.resistances || [])].map((level) => Number(level.price)).filter(Number.isFinite);
  const minPrice = Math.min(...lows, ...levelPrices, Number(item.price || Infinity));
  const maxPrice = Math.max(...highs, ...levelPrices, Number(item.price || 0));
  const pad = Math.max((maxPrice - minPrice) * 0.08, Number(item.price || 1) * 0.01);
  const pMin = minPrice - pad;
  const pMax = maxPrice + pad;
  const maxVolume = Math.max(...rows.map((row) => Number(row.volume || 0)), 1);
  const step = (right - left) / rows.length;
  const candleWidth = Math.max(3, Math.min(10, step * 0.58));
  const yFor = (value) => priceY(Number(value), pMin, pMax, top, priceBottom);
  const candles = rows
    .map((row, index) => {
      const x = left + index * step + step / 2;
      const open = Number(row.open ?? row.close);
      const close = Number(row.close);
      const high = Number(row.high ?? Math.max(open, close));
      const low = Number(row.low ?? Math.min(open, close));
      const up = close >= open;
      const yOpen = yFor(open);
      const yClose = yFor(close);
      const bodyY = Math.min(yOpen, yClose);
      const bodyH = Math.max(1.6, Math.abs(yClose - yOpen));
      const volH = Math.max(2, (Number(row.volume || 0) / maxVolume) * (volumeBottom - volumeTop));
      const title = `${chartRowLabel(row, true)} · ${row.is_current ? "快照" : "完整日线"} · 开${fmtPrice(open)} 高${fmtPrice(high)} 低${fmtPrice(low)} 收${fmtPrice(close)} · 成交量${row.is_current && !Number(row.volume || 0) ? "未取到分时成交量" : fmtMoney(row.volume)} · 量比${row.volume_ratio20 ?? "-"}`;
      return `
        <g class="candle ${up ? "up" : "down"}${row.is_current ? " current" : ""}">
          <line x1="${x.toFixed(1)}" y1="${yFor(high).toFixed(1)}" x2="${x.toFixed(1)}" y2="${yFor(low).toFixed(1)}"></line>
          <rect x="${(x - candleWidth / 2).toFixed(1)}" y="${bodyY.toFixed(1)}" width="${candleWidth.toFixed(1)}" height="${bodyH.toFixed(1)}"></rect>
          <rect class="volume" x="${(x - candleWidth / 2).toFixed(1)}" y="${(volumeBottom - volH).toFixed(1)}" width="${candleWidth.toFixed(1)}" height="${volH.toFixed(1)}"></rect>
          <title>${escapeHtml(title)}</title>
        </g>
      `;
    })
    .join("");
  const lines = [
    ...sortedLevels(item.supports || []).slice(0, 4).map((level) => ({ ...level, type: "support", label: "支撑" })),
    ...sortedLevels(item.resistances || []).slice(0, 4).map((level) => ({ ...level, type: "resistance", label: "压力" })),
  ]
    .map((level) => {
      const y = yFor(level.price);
      const textY = Math.max(top + 10, Math.min(priceBottom - 4, y - 3));
      return `
        <g class="level-line ${level.type}">
          <line x1="${left}" y1="${y.toFixed(1)}" x2="${right}" y2="${y.toFixed(1)}"></line>
          <text x="${right + 8}" y="${textY.toFixed(1)}">${escapeHtml(level.label)} ${fmtPrice(level.price)}</text>
          <title>${escapeHtml(`${level.label} ${fmtPrice(level.price)} · ${level.source || ""} · ${fmtPct(level.distance_pct)}`)}</title>
        </g>
      `;
    })
    .join("");
  const currentY = yFor(item.price);
  return `
    <div class="large-chart">
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(item.symbol)} 60日价量结构大图">
        <line class="chart-grid" x1="${left}" y1="${top}" x2="${right}" y2="${top}"></line>
        <line class="chart-grid" x1="${left}" y1="${priceBottom}" x2="${right}" y2="${priceBottom}"></line>
        <line class="chart-grid" x1="${left}" y1="${volumeBottom}" x2="${right}" y2="${volumeBottom}"></line>
        ${lines}
        <g class="level-line current">
          <line x1="${left}" y1="${currentY.toFixed(1)}" x2="${right}" y2="${currentY.toFixed(1)}"></line>
          <text x="${right + 8}" y="${Math.max(top + 10, currentY - 4).toFixed(1)}">现价 ${fmtPrice(item.price)}</text>
        </g>
        ${candles}
        <text class="axis-label" x="${left}" y="410">${escapeHtml(chartRowLabel(rows[0]) || "")}</text>
        <text class="axis-label end" x="${right}" y="410">${escapeHtml(chartRowLabel(rows[rows.length - 1]) || "")}</text>
        <text class="axis-label" x="4" y="${top + 4}">${fmtPrice(pMax)}</text>
        <text class="axis-label" x="4" y="${priceBottom}">${fmtPrice(pMin)}</text>
      </svg>
    </div>
  `;
}

function renderTechModal(symbol) {
  const item = latestRun?.technical_analysis?.[symbol];
  if (!item) return;
  $("techModalTitle").textContent = `${symbol} 量价结构`;
  $("techModalMeta").textContent = `现价 ${fmtPrice(item.price)} · ${priceVolumeLabels[item.label] || item.label || "-"} ${formatScore(item.score, item.score_range, "price_volume")} · 60日位置 ${fmtPct(item.range_position)} · 20日量比 ${item.volume_ratio20 ?? "-"}`;
  const components = (item.components || [])
    .map(
      (component) =>
        `<li><strong>${escapeHtml(component.name)}</strong><span>${escapeHtml(formatScore(component.score, component.score_range, component.name))} · ${escapeHtml(component.detail || "")}</span></li>`,
    )
    .join("");
  $("techModalBody").innerHTML = `
    ${renderLargePriceVolumeChart(item)}
    <div class="tech-modal-grid">
      <section>
        <h3>关键支撑</h3>
        <ul class="modal-level-list">${renderLevelList(item.supports, "暂无支撑")}</ul>
      </section>
      <section>
        <h3>关键压力</h3>
        <ul class="modal-level-list">${renderLevelList(item.resistances, "暂无压力")}</ul>
      </section>
      <section>
        <h3>评分拆解</h3>
        <p>${escapeHtml(item.explanation || "")}</p>
        <ul class="modal-level-list">${components || `<li class="muted">暂无拆解</li>`}</ul>
      </section>
    </div>
  `;
  $("techModal").hidden = false;
}

function closeTechModal() {
  $("techModal").hidden = true;
}

function renderIntraday(intraday) {
  const target = $("intradaySummary");
  const symbols = Object.keys(intraday || {}).sort();
  if (!symbols.length) {
    target.innerHTML = `<div class="muted">暂无当日分钟线</div>`;
    return;
  }
  target.innerHTML = `
    <div class="table-wrap compact-table">
      <table>
        <thead>
          <tr>
            <th>代码</th>
            <th>状态</th>
            <th>开盘至今</th>
            <th>近30分钟</th>
            <th>VWAP偏离</th>
            <th>区间位置</th>
            <th>明细</th>
          </tr>
        </thead>
        <tbody>
          ${symbols
            .map((symbol) => {
              const item = intraday[symbol] || {};
              const bars = (item.recent_bars || []).slice(-6);
              const detailRows = bars
                .map(
                  (bar) => `
                    <tr>
                      <td>${escapeHtml(String(bar.timestamp || "").slice(11, 19) || bar.timestamp)}</td>
                      <td>${escapeHtml(bar.open)}</td>
                      <td>${escapeHtml(bar.high)}</td>
                      <td>${escapeHtml(bar.low)}</td>
                      <td>${escapeHtml(bar.close)}</td>
                    </tr>
                  `,
                )
                .join("");
              return `
                <tr>
                  <td>${escapeHtml(symbol)}</td>
                  <td>${escapeHtml(intradayLabels[item.label] || item.label || "-")} · ${escapeHtml(formatScore(item.score, item.score_range, "intraday"))}</td>
                  <td>${fmtPct(item.from_open_pct)}</td>
                  <td>${fmtPct(item.last_30m_pct)}</td>
                  <td>${fmtPct(item.from_vwap_pct)}</td>
                  <td>${fmtPct(item.range_position)}</td>
                  <td>
                    <details>
                      <summary>${escapeHtml(item.bar_count || 0)} 根</summary>
                      <table class="mini-bars">
                        <thead><tr><th>时间</th><th>开</th><th>高</th><th>低</th><th>收</th></tr></thead>
                        <tbody>${detailRows}</tbody>
                      </table>
                    </details>
                  </td>
                </tr>
              `;
            })
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderExecutiveSummary(summary) {
  const target = $("executiveSummary");
  const source = $("summarySource");
  if (!summary || !summary.text) {
    target.classList.add("muted");
    target.innerHTML = "暂无总结";
    source.textContent = "等待生成";
    return;
  }
  target.classList.remove("muted");
  target.innerHTML = parseSummaryBlocks(summary.text).map((block, index) => renderSummaryBlock(block, index)).join("");
  source.textContent = String(summary.source || "").startsWith("llm") ? "LLM" : "本地兜底";
  if (summary.source === "llm_with_local_fill") {
    source.textContent = "LLM+补齐";
  }
  if (summary.error) {
    source.title = `LLM 调用失败，已使用本地兜底：${summary.error}`;
  } else {
    source.removeAttribute("title");
  }
}

function fallbackExecutiveSummary(plan) {
  if (!plan) return null;
  const ordersBySymbol = {};
  for (const order of plan.orders || []) {
    const key = String(order.symbol || "");
    ordersBySymbol[key] = ordersBySymbol[key] || [];
    ordersBySymbol[key].push(order);
  }
  const rawLines = (plan.positions || [])
    .slice()
    .sort((a, b) => String(a.symbol).localeCompare(String(b.symbol)))
    .map((position) => {
      const orders = ordersBySymbol[position.symbol] || [];
      const action = labelAction(position.action);
      const orderText = orders.length
        ? `，${orders.map((order) => `${order.side === "buy" ? "买" : "卖"}${order.shares}股@${order.limit_price}`).join("；")}`
        : "";
      const technical = plan.technical_analysis?.[position.symbol];
      const support = technical?.supports?.[0]?.price ? `支撑${fmtPrice(technical.supports[0].price)}` : "";
      const resistance = technical?.resistances?.[0]?.price ? `压力${fmtPrice(technical.resistances[0].price)}` : "";
      const techText = technical ? `；量价${formatScore(technical.score, technical.score_range, "price_volume")}${support ? `，${support}` : ""}${resistance ? `，${resistance}` : ""}` : "";
      return `${position.symbol}：${action}${orderText}；权重${fmtPct(position.current_weight)}->${fmtPct(position.target_weight)}；${translateReason(position.reason) || "仓位接近目标"}${techText}。`;
    });
  const gross = Number(plan.portfolio?.current_gross_exposure || 0);
  const prefix = `整体：${labelRegime(plan.regime?.label)}(${formatScore(plan.regime?.score, plan.regime?.score_range, "regime")})，当前杠杆${gross.toFixed(2)}x。`;
  return { source: "browser_fallback", text: `${prefix}\n${rawLines.join("\n")}` };
}

async function loadState() {
  const payload = await api("/api/state");
  appState = payload.state;
  latestRun = payload.latest_run;
  $("provider").value = appState.settings.provider || "yahoo_chart";
  $("refreshHistory").checked = appState.settings.refresh_history !== false;
  $("scheduleEnabled").checked = !!appState.settings.schedule_enabled;
  $("ibkrHost").value = appState.settings.ibkr_host || "127.0.0.1";
  $("ibkrPort").value = appState.settings.ibkr_port || 4002;
  $("ibkrClientId").value = appState.settings.ibkr_client_id || 81;
  $("ibkrMarketDataType").value = String(appState.settings.ibkr_market_data_type || 1);
  $("ibkrTimeout").value = appState.settings.ibkr_timeout || 8;
  $("fetchIntraday").checked = appState.settings.fetch_intraday !== false;
  $("intradayBarSize").value = appState.settings.intraday_bar_size || "5 mins";
  $("intradayDuration").value = appState.settings.intraday_duration || "1 D";
  $("intradayUseRth").checked = !!appState.settings.intraday_use_rth;
  toggleIbkrSettings();
  setPortfolioForm(appState.portfolio);
  renderRun(latestRun);
  renderHistory(payload.runs || []);
  if (!isRunning) setStatus(`就绪 · ${new Date().toLocaleString()}`);
}

async function saveSettings() {
  const settings = currentSettingsFromForm();
  await api("/api/settings/save", { method: "POST", body: JSON.stringify({ settings }) });
  setStatus("设置已保存");
}

async function savePortfolio() {
  const portfolio = currentPortfolioFromForm();
  await api("/api/portfolio/save", { method: "POST", body: JSON.stringify({ portfolio }) });
  setStatus("持仓已保存");
  await loadState();
}

async function parseTextPortfolio() {
  setStatus("自然语言持仓解析暂未启用；当前请用表格维护持仓。");
  return;
  const text = $("portfolioText").value.trim();
  if (!text) return;
  const payload = await api("/api/portfolio/parse-text", { method: "POST", body: JSON.stringify({ text }) });
  setPortfolioForm(payload.portfolio);
  setStatus("自然语言持仓已解析");
}

async function runPlan(kind = "manual") {
  if (isRunning) return;
  const portfolio = currentPortfolioFromForm();
  if (!portfolio.account_equity) {
    setStatus("需要先填账户净值");
    return;
  }
  setRunBusy(true);
  startProgressPolling();
  setStatus("正在生成调仓建议，请等待，不需要重复点击。");
  try {
    const payload = await api("/api/run", {
      method: "POST",
      body: JSON.stringify({
        portfolio,
        prompt: $("runPrompt").value,
        provider: $("provider").value,
        refresh_history: $("refreshHistory").checked,
        settings: currentSettingsFromForm(),
        kind,
      }),
    });
    latestRun = payload.plan;
    renderRun(latestRun);
    await loadState();
    setStatus("建议已生成并保存");
  } catch (error) {
    setStatus(`生成失败：${translateError(error.message)}`);
  } finally {
    stopProgressPolling();
    setRunBusy(false);
  }
}

async function testIbkrQuotes() {
  setIbkrTestBusy(true);
  $("ibkrTestResult").textContent = "正在测试，只调用行情快照接口...";
  try {
    const payload = await api("/api/quotes/test", {
      method: "POST",
      body: JSON.stringify({ provider: "ibkr", settings: currentSettingsFromForm() }),
    });
    const sample = (payload.quotes || []).slice(0, 3).map((quote) => `${quote.symbol} ${quote.price}`).join("，");
    $("ibkrTestResult").textContent = `成功：返回 ${payload.quotes.length} 条行情${sample ? `（${sample}）` : ""}`;
    setStatus("IBKR 行情测试成功");
  } catch (error) {
    const message = translateError(error.message);
    $("ibkrTestResult").textContent = `失败：${message}`;
    setStatus(`IBKR 行情测试失败：${message}`);
  } finally {
    setIbkrTestBusy(false);
  }
}

function renderOrderLadder(order) {
  const levels = order?.llm_reference_ladder || [];
  if (!levels.length) return "";
  const totalShares = levels.reduce((sum, level) => sum + Number(level.shares || 0), 0);
  const totalNotional = levels.reduce((sum, level) => sum + Number(level.notional || 0), 0);
  return `
    <div class="order-ladder">
      <div class="order-ladder-head">
        <strong>参考价梯</strong>
        <span>拆分替代，不自动下单</span>
      </div>
      <p class="order-ladder-note">
        主建议是${labelSide(order.side)} ${escapeHtml(order.shares)}股 @ ${fmtPrice(order.limit_price)}。
        下方价梯表示可把这笔单拆成多档；不要和主建议单重复相加。
        整数股取整后合计约 ${escapeHtml(totalShares)}股 / ${fmtMoney(totalNotional)}。
      </p>
      <ul>
        ${levels
          .map(
            (level) => `
              <li>
                <em>${escapeHtml(level.label || "-")}</em>
                <div>
                  <b>${fmtPrice(level.price)}</b>
                  <span>${fmtPct(level.distance_pct)} · ${escapeHtml(level.shares ?? "-")}股 · ${fmtMoney(level.notional)}</span>
                </div>
                <small>${escapeHtml(level.rationale || "")}</small>
              </li>
            `,
          )
          .join("")}
      </ul>
    </div>
  `;
}

function renderSidePill(side) {
  return `<span class="side-pill ${side === "buy" ? "buy" : "sell"}">${labelSide(side)}</span>`;
}

function renderOrderSymbol(symbol, order) {
  return `
    <div class="order-symbol-cell">
      <strong>${escapeHtml(symbol)}</strong>
      <span>${escapeHtml((order.time_in_force || "day").toUpperCase())}</span>
    </div>
  `;
}

function renderOrderNumber(value, unit, className = "") {
  return `
    <div class="order-number ${escapeHtml(className)}">
      <strong>${escapeHtml(value)}</strong>
      <span>${escapeHtml(unit)}</span>
    </div>
  `;
}

function renderOrderBasis(order) {
  return `
    <div class="order-basis">
      <strong>主点位依据</strong>
      <p>${escapeHtml(order.limit_basis || "-")}</p>
      ${renderOrderLadder(order)}
    </div>
  `;
}

function renderOrderReason(order) {
  return `<div class="order-reason">${escapeHtml(translateReason(order.reason))}</div>`;
}

function priceForSymbol(plan, symbol) {
  const key = String(symbol || "").toUpperCase();
  const technical = plan?.technical_analysis?.[key];
  if (technical?.price !== undefined && technical?.price !== null) return Number(technical.price);
  const position = (plan?.positions || []).find((item) => String(item.symbol || "").toUpperCase() === key);
  if (position?.price !== undefined && position?.price !== null) return Number(position.price);
  return null;
}

function renderPriceCompare(currentPrice, limitPrice) {
  if (!Number.isFinite(Number(currentPrice))) return "-";
  const limit = Number(limitPrice);
  const distance = Number.isFinite(limit) && Number(currentPrice) > 0 ? limit / Number(currentPrice) - 1 : null;
  return `
    <div class="price-compare">
      <strong>${fmtPrice(currentPrice)}</strong>
      ${distance === null ? "" : `<span>距限价 ${fmtPct(distance)}</span>`}
    </div>
  `;
}

function renderTradeLeg(order, title, tone, currentPrice) {
  if (!order) {
    return `
      <div class="trade-leg blocked">
        <div class="trade-leg-head"><strong>${escapeHtml(title)}</strong><span>未生成</span></div>
        <p>受持仓、硬约束或金额限制，本轮没有生成这一腿。</p>
      </div>
    `;
  }
  const distance = Number.isFinite(Number(currentPrice)) && Number(currentPrice) > 0 ? Number(order.limit_price) / Number(currentPrice) - 1 : null;
  return `
    <div class="trade-leg ${escapeHtml(tone)}">
      <div class="trade-leg-head">
        <strong>${escapeHtml(title)}</strong>
        ${renderSidePill(order.side)}
      </div>
      <div class="trade-leg-price">
        <b>${fmtPrice(order.limit_price)}</b>
        <span>${distance === null ? "" : `距现价 ${fmtPct(distance)}`}</span>
      </div>
      <div class="trade-leg-meta">
        <span>${escapeHtml(order.shares)}股</span>
        <span>${fmtMoney(order.notional)}</span>
      </div>
      <p>${escapeHtml(order.limit_basis || "")}</p>
    </div>
  `;
}

function renderTradeGroups(groups, plan) {
  const target = $("tradeGroups");
  if (!target) return;
  const items = groups || [];
  if (!items.length) {
    target.innerHTML = "";
    target.hidden = true;
    return;
  }
  target.hidden = false;
  target.innerHTML = `
    <div class="trade-groups-head">
      <h4>做T组合计划</h4>
      <span>同一标的可同时给低吸与高抛；净股数由指标、风控和本次想法共同决定</span>
    </div>
    <div class="trade-group-grid">
      ${items
        .map((group) => {
          const currentPrice = Number(group.current_price ?? priceForSymbol(plan, group.symbol));
          const netShares = Number(group.net_shares_if_all_filled || 0);
          const netCash = Number(group.net_cash_if_all_filled || 0);
          const netClass = netShares === 0 ? "flat" : netShares > 0 ? "buy" : "sell";
          const intentLabel =
            group.intent === "flat_required"
              ? "严格净仓位"
              : group.intent === "flat_preferred" || group.intent === "flat"
                ? "偏好净仓位"
                : "弹性价差";
          return `
            <article class="trade-group-card ${netClass}">
              <div class="trade-group-title">
                <div>
                  <strong>${escapeHtml(group.symbol)}</strong>
                  <span>${escapeHtml(group.title || "做T / 高抛低吸")} · 现价 ${fmtPrice(currentPrice)}</span>
                </div>
                <em>${intentLabel}</em>
              </div>
              <div class="trade-group-summary">
                <div><span>两腿全成交净股数</span><b>${netShares > 0 ? "+" : ""}${netShares}</b></div>
                <div><span>两腿全成交净现金</span><b>${netCash >= 0 ? "+" : "-"}${fmtMoney(Math.abs(netCash))}</b></div>
                <div><span>买卖价差</span><b>${group.estimated_spread_pct === null || group.estimated_spread_pct === undefined ? "-" : fmtPct(group.estimated_spread_pct)}</b></div>
              </div>
              <div class="trade-leg-grid">
                ${renderTradeLeg(group.buy_order, "低吸买回", "buy", currentPrice)}
                ${renderTradeLeg(group.sell_order, "高抛卖出", "sell", currentPrice)}
              </div>
              <p class="trade-group-note">${escapeHtml((group.notes || []).map((item) => translateReason(item)).filter(Boolean).join("；"))}</p>
            </article>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderRun(plan) {
  const ordersBody = $("ordersBody");
  const positionsBody = $("positionsPlanBody");
  ordersBody.innerHTML = "";
  positionsBody.innerHTML = "";
  if ($("tradeGroups")) $("tradeGroups").innerHTML = "";
  $("warnings").innerHTML = "";
  $("researchProcess").innerHTML = "";
  $("technicalAnalysis").innerHTML = "";
  $("intradaySummary").innerHTML = "";

  if (!plan) {
    $("latestRunTag").textContent = "暂无记录";
    $("metricRegime").textContent = "-";
    $("metricGross").textContent = "-";
    $("metricOrders").textContent = "-";
    $("metricMargin").textContent = "-";
    renderExecutiveSummary(null);
    drawExposure(null);
    renderTechnicalAnalysis(null);
    renderResearchProcess(null);
    renderIntraday(null);
    renderTradeGroups([], null);
    return;
  }

  $("latestRunTag").textContent = `${labelKind(plan.run?.kind)} · ${plan.run?.id || ""}`;
  $("metricRegime").textContent = `${labelRegime(plan.regime?.label)} (${formatScore(plan.regime?.score, plan.regime?.score_range, "regime")})`;
  $("metricGross").textContent = fmtLeverage(plan.portfolio?.current_gross_exposure);
  $("metricOrders").textContent = String(plan.orders?.length || 0);
  const maintenance = plan.portfolio?.maintenance_margin;
  const cushion = plan.portfolio?.margin_cushion;
  $("metricMargin").textContent = maintenance === null || maintenance === undefined ? "-" : `${fmtMoney(maintenance)} / ${fmtMoney(cushion)}`;
  renderExecutiveSummary(plan.executive_summary || fallbackExecutiveSummary(plan));
  drawExposure(plan);
  renderTechnicalAnalysis(plan.technical_analysis);
  renderResearchProcess(plan.research_process);
  renderIntraday(plan.intraday);
  renderTradeGroups(plan.trade_groups, plan);

  for (const order of plan.orders || []) {
    const currentPrice = priceForSymbol(plan, order.symbol) ?? Number(order.limit_context?.reference_price);
    const row = document.createElement("tr");
    row.className = `order-row ${order.side === "buy" ? "buy" : "sell"}`;
    row.innerHTML = `
      <td>${renderSidePill(order.side)}</td>
      <td>${renderOrderSymbol(order.symbol, order)}</td>
      <td>${renderPriceCompare(currentPrice, order.limit_price)}</td>
      <td>${renderOrderNumber(order.shares, "股", "shares")}</td>
      <td>${renderOrderNumber(fmtPrice(order.limit_price), "限价", "limit")}</td>
      <td>${renderOrderNumber(fmtMoney(order.notional), "金额", "notional")}</td>
      <td>${renderOrderBasis(order)}</td>
      <td>${renderOrderReason(order)}</td>
    `;
    ordersBody.appendChild(row);
  }
  if (!(plan.orders || []).length) {
    ordersBody.innerHTML = `<tr><td colspan="8" class="muted">暂无建议挂单</td></tr>`;
  }

  for (const position of plan.positions || []) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${escapeHtml(position.symbol)}</td>
      <td>${fmtPrice(position.price)}</td>
      <td>${fmtPct(position.current_weight)}</td>
      <td>${fmtPct(position.target_weight)}</td>
      <td>${fmtMoney(position.delta_value)}</td>
      <td>${labelAction(position.action)}</td>
      <td>${escapeHtml(translateReason(position.reason))}</td>
    `;
    positionsBody.appendChild(row);
  }

  const warnings = plan.data_warnings || [];
  if (warnings.length) {
    $("warnings").innerHTML = `<strong>数据提示</strong><br>${warnings.map((warning) => `<div>${escapeHtml(translateWarning(warning))}</div>`).join("")}`;
  }
}

function drawExposure(plan) {
  const target = $("exposureGauge");
  if (!plan) {
    target.innerHTML = `<div class="muted">暂无杠杆数据</div>`;
    return;
  }
  const gross = Number(plan.portfolio?.current_gross_exposure || 0);
  const targetGross = Number(plan.regime?.target_gross_exposure || 0);
  const max = Number(plan.portfolio?.max_gross_exposure || 2);
  const gaugeMax = Math.max(max, gross, targetGross, 0.01);
  const currentPct = Math.max(0, Math.min(100, (gross / gaugeMax) * 100));
  const targetPct = Math.max(0, Math.min(100, (targetGross / gaugeMax) * 100));
  const maxPct = Math.max(0, Math.min(100, (max / gaugeMax) * 100));
  const overTarget = gross - targetGross;
  const statusDelta = gross > max ? gross - max : overTarget;
  const status = gross > max ? "超过上限" : overTarget > 0.05 ? "高于目标" : gross < targetGross - 0.05 ? "低于目标" : "接近目标";
  const tone = gross > max ? "danger" : overTarget > 0.05 ? "warn" : "ok";
  target.innerHTML = `
    <div class="gauge-head">
      <div>
        <span>当前杠杆</span>
        <strong>${gross.toFixed(2)}x</strong>
      </div>
      <div>
        <span>目标</span>
        <strong>${targetGross.toFixed(2)}x</strong>
      </div>
      <div>
        <span>上限</span>
        <strong>${max.toFixed(2)}x</strong>
      </div>
      <em class="${tone}">${status}${Math.abs(statusDelta) > 0.01 ? ` ${statusDelta >= 0 ? "+" : ""}${statusDelta.toFixed(2)}x` : ""}</em>
    </div>
    <div class="gauge-track" aria-label="当前杠杆 ${gross.toFixed(2)}x，目标 ${targetGross.toFixed(2)}x，上限 ${max.toFixed(2)}x">
      <span class="gauge-fill ${tone}" style="width:${currentPct}%"></span>
      <span class="gauge-marker target" style="left:${targetPct}%" title="目标杠杆 ${targetGross.toFixed(2)}x"></span>
      <span class="gauge-marker max" style="left:${maxPct}%" title="上限 ${max.toFixed(2)}x"></span>
    </div>
    <div class="gauge-scale">
      <span>0</span>
      <span>目标 ${targetGross.toFixed(2)}x</span>
      <span>上限 ${max.toFixed(2)}x</span>
    </div>
  `;
}

function renderHistory(runs) {
  const list = $("historyList");
  list.innerHTML = "";
  if (!runs.length) {
    list.innerHTML = `<div class="muted">暂无跑批记录</div>`;
    return;
  }
  for (const run of runs) {
    const button = document.createElement("button");
    button.className = "history-item";
    button.innerHTML = `
      <strong>${labelRegime(run.regime)} · ${run.orders_count} 条建议</strong>
      <span>${new Date(run.asof).toLocaleString()}</span>
      <span>${labelKind(run.kind)} · 当前杠杆 ${fmtLeverage(run.gross)}</span>
    `;
    button.addEventListener("click", async () => {
      latestRun = await api(`/api/runs/${run.id}`);
      renderRun(latestRun);
    });
    list.appendChild(button);
  }
}

function initResearchWindowDrag() {
  const win = $("researchWindow");
  const handle = $("researchWindowHeader");
  let dragging = false;
  let startX = 0;
  let startY = 0;
  let startLeft = 0;
  let startTop = 0;

  handle.addEventListener("pointerdown", (event) => {
    if (event.target.tagName === "BUTTON") return;
    dragging = true;
    const rect = win.getBoundingClientRect();
    startX = event.clientX;
    startY = event.clientY;
    startLeft = rect.left;
    startTop = rect.top;
    win.style.left = `${rect.left}px`;
    win.style.top = `${rect.top}px`;
    win.style.right = "auto";
    win.style.bottom = "auto";
    handle.setPointerCapture(event.pointerId);
  });

  handle.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const nextLeft = Math.max(8, Math.min(window.innerWidth - win.offsetWidth - 8, startLeft + event.clientX - startX));
    const nextTop = Math.max(8, Math.min(window.innerHeight - 80, startTop + event.clientY - startY));
    win.style.left = `${nextLeft}px`;
    win.style.top = `${nextTop}px`;
  });

  handle.addEventListener("pointerup", (event) => {
    dragging = false;
    try {
      handle.releasePointerCapture(event.pointerId);
    } catch (_error) {
      // The pointer may already be released by the browser.
    }
  });
}

function hideHelpPopover() {
  const popover = $("helpPopover");
  if (popover) popover.hidden = true;
}

function showHelpPopover(trigger) {
  const text = trigger.getAttribute("data-help") || trigger.getAttribute("title") || "";
  if (!text) return;
  const popover = $("helpPopover");
  popover.textContent = text;
  popover.hidden = false;
  const rect = trigger.getBoundingClientRect();
  const width = Math.min(360, window.innerWidth - 24);
  popover.style.maxWidth = `${width}px`;
  const nextLeft = Math.max(12, Math.min(window.innerWidth - width - 12, rect.left + rect.width / 2 - width / 2));
  const below = rect.bottom + 8;
  const top = below + 90 < window.innerHeight ? below : Math.max(12, rect.top - 98);
  popover.style.left = `${nextLeft}px`;
  popover.style.top = `${top}px`;
}

function initHelpPopovers() {
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest(".hint");
    if (trigger) {
      event.preventDefault();
      event.stopPropagation();
      if (!$("helpPopover").hidden && $("helpPopover").textContent === (trigger.getAttribute("data-help") || trigger.getAttribute("title") || "")) {
        hideHelpPopover();
      } else {
        showHelpPopover(trigger);
      }
      return;
    }
    if (!event.target.closest(".help-popover")) hideHelpPopover();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideHelpPopover();
    if ((event.key === "Enter" || event.key === " ") && event.target.classList?.contains("hint")) {
      event.preventDefault();
      showHelpPopover(event.target);
    }
  });
  window.addEventListener("resize", hideHelpPopover);
  window.addEventListener("scroll", hideHelpPopover, true);
}

$("addRowBtn").addEventListener("click", () => addPositionRow());
$("savePortfolioBtn").addEventListener("click", savePortfolio);
$("parseTextBtn").addEventListener("click", parseTextPortfolio);
$("runBtn").addEventListener("click", () => runPlan("manual"));
$("reloadBtn").addEventListener("click", loadState);
$("saveSettingsBtn").addEventListener("click", saveSettings);
$("provider").addEventListener("change", toggleIbkrSettings);
$("testIbkrBtn").addEventListener("click", testIbkrQuotes);
$("researchWindowClose").addEventListener("click", () => {
  $("researchWindow").hidden = true;
});
$("technicalAnalysis").addEventListener("click", (event) => {
  const button = event.target.closest(".open-tech-modal");
  if (!button) return;
  renderTechModal(button.dataset.symbol);
});
$("techModalClose").addEventListener("click", closeTechModal);
$("techModal").addEventListener("click", (event) => {
  if (event.target.matches("[data-close-tech-modal]")) closeTechModal();
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("techModal").hidden) closeTechModal();
});

initResearchWindowDrag();
initHelpPopovers();
loadState().catch((error) => setStatus(error.message));
