/* Performance profiling panel: per-step / per-stage timing analysis.
 *
 * The stacked chart always shows the whole run; the step window (set via the
 * inputs or by zooming the chart) scopes the KPIs, stage shares, slowest-step
 * table and spike detection, so users can inspect a suspicious segment
 * instead of one global average.
 */

let runId = null;
let report = null;          // latest report; `points` always cover the full run
let stageChart = null;
let shareChart = null;
let scaleChart = null;
let probeChart = null;

const PALETTE = ["#4f8cff", "#2ecc71", "#f39c12", "#e74c3c", "#9b59b6",
                 "#1abc9c", "#e67e22", "#95a5a6", "#6ea8ff", "#d35400",
                 "#16a085", "#c0392b", "#7f8c8d", "#f1c40f"];

/* Optimisation hints keyed by the slowest stage. */
const STAGE_HINTS = {
  move: "「移动」是引擎核心循环：优化需从算法入手（向量化 / 降低单步计算量），或控制个体规模。",
  infect: "「感染检测」随接触密度上升：可尝试调整感染半径 / 空间哈希粒度，或降低个体密度。",
  recover: "「康复判定」为 O(n) 遍历：通常占比很小，若突出说明个体规模已很大。",
  ca_update: "「元胞同步更新」随格子数增长：缩小网格或优化邻域统计是主要方向。",
  hash: "「空间哈希构建」为 O(n)：若占比高，说明个体规模大，可考虑复用哈希而非每步重建。",
  flock: "「群体行为」随邻居数增长：减小感知半径或个体规模可显著降耗。",
  predator: "「捕食」为 捕食者×鸟群 双重循环：捕食者增多时成本线性上升。",
  grass: "「草生长」随网格面积增长：与个体数无关，缩小网格可降耗。",
  animals: "「动物行动」随动物数量增长：种群爆发时该阶段会显著变慢，属预期现象。",
  rebuild: "「索引重建」为 O(格子数+个体数)：若占比高可考虑增量维护占用表。",
  accel: "「IDM 加速度」为 O(n)：车辆数是主要驱动，规模翻倍耗时近似翻倍。",
  ns_update: "「NS 更新」随车道格数与车辆数增长：加长车道比加密车辆更省。",
  lane_change: "「换道」含占用表重建：多车道高密度时占比上升，可考虑按需重建。",
  intervention: "「干预」只在干预生效的步出现尖峰，属一次性成本，无需优化。",
  stats: "「统计聚合」为 O(n) 全量统计：个体规模大时占比上升，属每步固定成本。",
  serialize: "「序列化」随个体数增长：增大 snapshot_interval 可摊薄快照成本。",
  io: "「写盘」是瓶颈：增大 snapshot_interval、减少快照频率；交互步进时系列文件每步重写，长运行建议用批量运行。",
};

function stageLabel(k) { return (report && report.stage_labels[k]) || k; }

function fmtMs(v) {
  if (v == null || isNaN(v)) return "—";
  if (v < 0.01) return v.toFixed(4);
  if (v < 1) return v.toFixed(3);
  if (v < 100) return v.toFixed(2);
  return v.toFixed(1);
}

function pct(v, digits = 1) { return (v * 100).toFixed(digits) + "%"; }

/* ------------------------------------------------------------------ */
/* Data loading                                                        */
/* ------------------------------------------------------------------ */
async function fetchReport(params) {
  const q = params ? `?${params}` : "";
  return get(`/api/runs/${runId}/profile${q}`);
}

async function load() {
  runId = el("runSelect").value;
  if (!runId) return;
  let rep;
  try {
    rep = await fetchReport("");
  } catch (e) {
    showNotice(el("emptyNotice"), "加载失败：" + e.message, "error");
    el("content").style.display = "none";
    return;
  }
  report = rep;
  if (rep.empty) {
    showNotice(el("emptyNotice"), rep.reason, "warn");
    el("content").style.display = "none";
    return;
  }
  el("emptyNotice").style.display = "none";
  el("content").style.display = "block";
  el("fromStep").value = rep.first_step;
  el("toStep").value = rep.last_step;
  renderStageChart();
  renderWindowed();
}

/* Re-fetch the summary for the step window in the inputs. */
async function refreshWindow() {
  if (!runId || !report || report.empty) return;
  const f = parseInt(el("fromStep").value, 10);
  const t = parseInt(el("toStep").value, 10);
  let params = "";
  if (!isNaN(f) && !isNaN(t)) params = `from=${f}&to=${t}`;
  const rep = await fetchReport(params);
  if (rep.empty) return;
  // Keep the chart (and its zoom) as-is; only swap the windowed analytics.
  report.summary = rep.summary;
  report.range = rep.range;
  report.totals = rep.totals;
  report.recorded_steps = rep.recorded_steps;
  report.truncated = rep.truncated;
  renderWindowed();
}

/* ------------------------------------------------------------------ */
/* Rendering: windowed analytics                                       */
/* ------------------------------------------------------------------ */
function renderWindowed() {
  renderKpis();
  renderShareChart();
  renderStageTable();
  renderSlowTable();
  renderScaleChart();
  renderOverhead();
}

function renderKpis() {
  const s = report.summary;
  const slowest = s.slowest_stage;
  const slowShare = slowest ? s.stage_stats[slowest].share : 0;
  const tiles = [
    ["区间步数", s.steps, `全程已记录 ${report.recorded_steps} 步`],
    ["区间总耗时", fmtMs(s.wall_total_ms) + " ms",
     `占全程 ${pct(s.wall_total_ms / (report.totals.wall_ms || 1))}`],
    ["平均每步", fmtMs(s.wall_mean_ms) + " ms", `P95 ${fmtMs(s.wall_p95_ms)} ms`],
    ["最慢单步", fmtMs(s.wall_max_ms) + " ms", `第 ${s.wall_max_step} 步`],
    ["最慢阶段", slowest ? stageLabel(slowest) : "—",
     slowest ? `占区间耗时 ${pct(slowShare)}` : ""],
    ["测量开销", s.overhead_pct.toFixed(3) + "%",
     `每步约 ${(s.overhead_total_ms / s.steps * 1000).toFixed(2)} µs`],
  ];
  el("kpis").innerHTML = tiles.map(([k, v, d]) => `
    <div class="stat"><div class="k">${esc(k)}</div>
    <div class="v" style="font-size:20px">${esc(String(v))}</div>
    <div class="d">${esc(d)}</div></div>`).join("");
}

function renderStageChart() {
  const pts = report.points;
  if (!stageChart) {
    stageChart = echarts.init(el("stageChart"), "dark");
    stageChart.on("datazoom", debounce(onChartZoom, 350));
  }
  const option = {
    backgroundColor: "transparent",
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => v == null ? "—" : fmtMs(v) + " ms",
    },
    legend: { textStyle: { color: "#8b98a5" }, top: 0, type: "scroll" },
    grid: { left: 64, right: 24, top: 36, bottom: 62 },
    xAxis: {
      type: "category",
      data: pts.map((p) => p.step),
      name: report.bucket > 1 ? `时间步（每点为 ${report.bucket} 步均值）` : "时间步",
      axisLine: { lineStyle: { color: "#26313f" } },
    },
    yAxis: {
      type: "value", name: "耗时 (ms)",
      axisLine: { lineStyle: { color: "#26313f" } },
      splitLine: { lineStyle: { color: "#1b2430" } },
    },
    dataZoom: [
      { type: "inside" },
      { type: "slider", height: 18, bottom: 8 },
    ],
    series: [
      ...report.stages.map((s, i) => ({
        name: stageLabel(s),
        type: "line",
        stack: "stage",
        areaStyle: { opacity: 0.7 },
        symbol: "none",
        lineStyle: { width: 0.6 },
        color: PALETTE[i % PALETTE.length],
        emphasis: { focus: "series" },
        data: pts.map((p) => p.stages[s] || 0),
      })),
      {
        name: "单步总计",
        type: "line",
        symbol: "none",
        z: 5,
        lineStyle: { width: 1.4, color: "#e6edf3", type: "dashed" },
        itemStyle: { color: "#e6edf3" },
        data: pts.map((p) => p.wall_ms),
      },
      // When points are aggregated, surface the slowest single step inside
      // each bucket so spikes stay visible.
      ...(report.bucket > 1 ? [{
        name: "桶内最慢单步",
        type: "line",
        symbol: "none",
        z: 4,
        lineStyle: { width: 1, color: "#e74c3c", type: "dotted" },
        itemStyle: { color: "#e74c3c" },
        data: pts.map((p) => p.wall_max_ms),
      }] : []),
    ],
  };
  stageChart.setOption(option, true);
  el("bucketNote").textContent = report.bucket > 1
    ? `运行较长，图上每个点聚合了 ${report.bucket} 步（阶段取均值，白线为单步总计均值）；最慢步 Top 10 仍基于原始逐步数据。`
    : "堆叠面积为各阶段耗时，白色虚线为单步总耗时；滚轮 / 框选 / 拖动滑块可选择步区间。";
}

/* Chart zoom -> sync the range inputs -> refresh windowed analytics. */
function onChartZoom() {
  if (!report || report.empty) return;
  const opt = stageChart.getOption();
  const dz = opt.dataZoom && opt.dataZoom[0];
  if (!dz) return;
  const pts = report.points;
  const last = pts.length - 1;
  let si = dz.startValue, ei = dz.endValue;
  if (si == null || ei == null) {
    si = Math.floor(((dz.start != null ? dz.start : 0) / 100) * last);
    ei = Math.ceil(((dz.end != null ? dz.end : 100) / 100) * last);
  }
  si = Math.max(0, Math.min(si, last));
  ei = Math.max(si, Math.min(ei, last));
  el("fromStep").value = pts[si].step;
  el("toStep").value = pts[ei].step + (pts[ei].bucket || 1) - 1;
  refreshWindow();
}

/* Programmatically zoom the stacked chart to a step window. */
function zoomChartTo(f, t) {
  if (!stageChart || !report || report.empty) return;
  const pts = report.points;
  let si = 0, ei = pts.length - 1;
  for (let i = 0; i < pts.length; i++) {
    if (pts[i].step <= f) si = i; else break;
  }
  for (let i = pts.length - 1; i >= 0; i--) {
    if (pts[i].step + (pts[i].bucket || 1) - 1 >= t) ei = i; else break;
  }
  stageChart.dispatchAction({ type: "dataZoom", startValue: si, endValue: ei });
}

function renderShareChart() {
  const s = report.summary;
  const rows = report.stages
    .map((k) => ({ key: k, ...s.stage_stats[k] }))
    .filter((r) => r.total_ms > 0)
    .sort((a, b) => a.total_ms - b.total_ms);   // ascending: largest on top
  if (!shareChart) shareChart = echarts.init(el("shareChart"), "dark");
  shareChart.setOption({
    backgroundColor: "transparent",
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (items) => {
        const r = rows[items[0].dataIndex];
        return `${stageLabel(r.key)}<br/>总耗时 ${fmtMs(r.total_ms)} ms<br/>` +
               `占比 ${pct(r.share)}<br/>平均每步 ${fmtMs(r.mean_ms)} ms`;
      },
    },
    grid: { left: 90, right: 60, top: 10, bottom: 24 },
    xAxis: {
      type: "value", name: "ms",
      axisLine: { lineStyle: { color: "#26313f" } },
      splitLine: { lineStyle: { color: "#1b2430" } },
    },
    yAxis: {
      type: "category",
      data: rows.map((r) => stageLabel(r.key)),
      axisLine: { lineStyle: { color: "#26313f" } },
    },
    series: [{
      type: "bar",
      data: rows.map((r, i) => ({
        value: r.total_ms,
        itemStyle: { color: PALETTE[report.stages.indexOf(r.key) % PALETTE.length] },
      })),
      label: {
        show: true, position: "right", color: "#8b98a5",
        formatter: (p) => pct(rows[p.dataIndex].share),
      },
      barMaxWidth: 18,
    }],
  }, true);
}

function renderStageTable() {
  const s = report.summary;
  const rows = report.stages
    .map((k) => ({ key: k, ...s.stage_stats[k] }))
    .filter((r) => r.total_ms > 0)
    .sort((a, b) => b.total_ms - a.total_ms);
  el("stageTable").innerHTML = `
    <thead><tr>
      <th>阶段</th><th class="num">总耗时 (ms)</th><th class="num">占比</th>
      <th class="num">平均每步 (ms)</th><th class="num">单步最大 (ms)</th><th class="num">出现于</th>
    </tr></thead>
    <tbody>${rows.map((r) => `
      <tr>
        <td><span class="legend" style="display:inline-flex"><span class="swatch" style="background:${PALETTE[report.stages.indexOf(r.key) % PALETTE.length]}"></span></span> ${esc(stageLabel(r.key))}</td>
        <td class="num">${fmtMs(r.total_ms)}</td>
        <td class="num">${pct(r.share)}</td>
        <td class="num">${fmtMs(r.mean_ms)}</td>
        <td class="num">${fmtMs(r.max_ms)}</td>
        <td class="num">第 ${r.max_step} 步</td>
      </tr>`).join("")}
    </tbody>`;
}

function renderSlowTable() {
  const s = report.summary;
  el("spikeNote").textContent = s.spike_count > 0
    ? `区间中位数 ${fmtMs(s.wall_p50_ms)} ms；有 ${s.spike_count} 步超过 3× 中位数（阈值 ${fmtMs(s.spike_threshold_ms)} ms），下表以红色标出。`
    : `区间中位数 ${fmtMs(s.wall_p50_ms)} ms；未发现超过 3× 中位数的异常突增步。`;
  el("slowTable").innerHTML = `
    <thead><tr>
      <th class="num">步</th><th class="num">个体数</th><th class="num">总耗时 (ms)</th>
      <th class="num">× 中位数</th><th>主导阶段</th><th>各阶段明细 (ms)</th>
    </tr></thead>
    <tbody>${s.slowest_steps.map((r) => {
      const ratio = s.wall_p50_ms > 0 ? (r.wall_ms / s.wall_p50_ms).toFixed(1) : "—";
      const detail = Object.entries(r.stages)
        .sort((a, b) => b[1] - a[1]).slice(0, 3)
        .map(([k, v]) => `${stageLabel(k)} ${fmtMs(v)}`).join(" · ");
      const spike = r.wall_ms > s.spike_threshold_ms;
      return `<tr data-step="${r.step}" class="slow-row" style="cursor:pointer">
        <td class="num">${r.step}</td>
        <td class="num">${r.n}</td>
        <td class="num" style="${spike ? "color:var(--red);font-weight:700" : ""}">${fmtMs(r.wall_ms)}</td>
        <td class="num">${ratio}</td>
        <td>${esc(stageLabel(r.dominant_stage))}</td>
        <td class="muted small">${esc(detail)}</td>
      </tr>`;
    }).join("")}
    </tbody>`;
  el("slowTable").querySelectorAll(".slow-row").forEach((tr) => {
    tr.onclick = () => {
      const step = parseInt(tr.dataset.step, 10);
      const half = Math.max(15, Math.round(30 / (report.bucket || 1)));
      zoomChartTo(step - half * (report.bucket || 1), step + half * (report.bucket || 1));
    };
  });
}

/* ------------------------------------------------------------------ */
/* Rendering: scaling trend + extrapolation                            */
/* ------------------------------------------------------------------ */
function renderScaleChart() {
  const sc = report.summary.scaling;
  if (!scaleChart) scaleChart = echarts.init(el("scaleChart"), "dark");
  const series = [{
    name: "每步耗时",
    type: "scatter",
    symbolSize: 6,
    itemStyle: { color: "#4f8cff", opacity: 0.7 },
    data: report.points.map((p) => [p.n, p.wall_ms]),
  }];
  if (sc.available) {
    series.push({
      name: "线性拟合",
      type: "line",
      showSymbol: false,
      lineStyle: { color: "#f39c12", width: 2 },
      itemStyle: { color: "#f39c12" },
      data: [
        [sc.n_min, sc.intercept + sc.slope * sc.n_min],
        [sc.n_max, sc.intercept + sc.slope * sc.n_max],
      ],
    });
  }
  scaleChart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "item", formatter: (p) => `规模 ${p.value[0]} · ${fmtMs(p.value[1])} ms/步` },
    legend: { textStyle: { color: "#8b98a5" }, top: 0 },
    grid: { left: 60, right: 24, top: 30, bottom: 40 },
    xAxis: {
      type: "value", name: "个体数",
      axisLine: { lineStyle: { color: "#26313f" } },
      splitLine: { lineStyle: { color: "#1b2430" } },
    },
    yAxis: {
      type: "value", name: "ms/步",
      axisLine: { lineStyle: { color: "#26313f" } },
      splitLine: { lineStyle: { color: "#1b2430" } },
    },
    series,
  }, true);

  if (sc.available) {
    el("scaleNote").textContent =
      `拟合（基于全程 ${report.recorded_steps} 步）：规模每增加 1000 个体，每步约 +${fmtMs(sc.slope_ms_per_1000)} ms，` +
      `R²=${sc.r2.toFixed(3)}（越接近 1 趋势越可信）。`;
    el("estInput").disabled = false;
  } else {
    el("scaleNote").textContent =
      `本次运行个体数基本恒定（${sc.n_min}–${sc.n_max}），无法拟合规模趋势；` +
      `可用右侧「规模探针」实测多档规模，或在对比实验中跑不同规模。`;
    el("estInput").disabled = true;
    el("estOut").textContent = "";
  }
  updateEstimate();
}

function updateEstimate() {
  const sc = report && report.summary && report.summary.scaling;
  if (!sc || !sc.available) return;
  const n = parseFloat(el("estInput").value);
  if (!n || n <= 0) { el("estOut").textContent = ""; return; }
  const ms = Math.max(0, sc.intercept + sc.slope * n);
  const extra = (n < sc.n_min || n > sc.n_max) ? "（外推，仅供参考）" : "";
  // 1000 步 × ms/步 ÷ 1000 ms/s —— 数值上等于 ms
  el("estOut").textContent =
    `≈ ${fmtMs(ms)} ms/步，1000 步约 ${ms.toFixed(1)} s ${extra}`;
}

/* ------------------------------------------------------------------ */
/* Rendering: scale probe                                              */
/* ------------------------------------------------------------------ */
async function runProbe() {
  if (!runId) return;
  const factors = el("probeFactors").value.split(",")
    .map((s) => parseFloat(s.trim())).filter((v) => !isNaN(v));
  const steps = parseInt(el("probeSteps").value, 10) || 30;
  el("probeBtn").disabled = true;
  el("probeNote").textContent = "探针运行中……";
  try {
    const res = await post(`/api/runs/${runId}/profile/scale-probe`, { factors, steps });
    renderProbe(res);
  } catch (e) {
    el("probeNote").textContent = "探针失败：" + e.message;
  } finally {
    el("probeBtn").disabled = false;
  }
}

function renderProbe(res) {
  if (!probeChart) probeChart = echarts.init(el("probeChart"), "dark");
  const pts = res.points;
  probeChart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", valueFormatter: (v) => fmtMs(v) + " ms" },
    legend: { textStyle: { color: "#8b98a5" }, top: 0 },
    grid: { left: 60, right: 24, top: 30, bottom: 40 },
    xAxis: {
      type: "value", name: "个体数",
      axisLine: { lineStyle: { color: "#26313f" } },
      splitLine: { lineStyle: { color: "#1b2430" } },
    },
    yAxis: {
      type: "value", name: "ms/步",
      axisLine: { lineStyle: { color: "#26313f" } },
      splitLine: { lineStyle: { color: "#1b2430" } },
    },
    series: [
      {
        name: "平均每步", type: "line", smooth: true,
        itemStyle: { color: "#4f8cff" },
        data: pts.map((p) => [p.n, p.mean_ms]),
        markLine: {
          symbol: "none",
          lineStyle: { color: "#8b98a5", type: "dashed" },
          label: { formatter: "当前规模", color: "#8b98a5" },
          data: [{ xAxis: res.current_n }],
        },
      },
      {
        name: "P95", type: "line", smooth: true,
        lineStyle: { type: "dashed", color: "#f39c12" },
        itemStyle: { color: "#f39c12" },
        data: pts.map((p) => [p.n, p.p95_ms]),
      },
    ],
  }, true);
  let note = `实测 ${pts.length} 档规模 × 每档 ${res.steps_per_scale} 步`;
  if (res.fit) {
    note += `；拟合：规模每 +1000 个体 ≈ +${fmtMs(res.fit.slope * 1000)} ms/步（R²=${res.fit.r2.toFixed(2)}）`;
  }
  if (res.skipped_factors && res.skipped_factors.length) {
    note += `；档位 ${res.skipped_factors.join(", ")} 超出安全上限已跳过`;
  }
  el("probeNote").textContent = note + "。" + res.note;
}

/* ------------------------------------------------------------------ */
/* Rendering: measurement overhead & credibility                       */
/* ------------------------------------------------------------------ */
function renderOverhead() {
  const s = report.summary;
  const perStepUs = (s.overhead_total_ms / s.steps * 1000).toFixed(2);
  const credible = s.overhead_pct < 1 && s.unaccounted_pct < 20;
  const hint = STAGE_HINTS[s.slowest_stage] ||
    "瓶颈阶段占比较低且分散：可结合上方堆叠图观察其随时间的变化。";
  el("overheadBody").innerHTML = `
    <table>
      <tbody>
        <tr><th style="width:220px">计时器</th>
          <td><span class="mono">time.perf_counter_ns</span>（单调时钟，纳秒级；统计的是墙钟时间，包含 I/O 等待）</td></tr>
        <tr><th>测量开销（实测）</th>
          <td>区间内剖析器自身簿记共 ${fmtMs(s.overhead_total_ms)} ms，占墙钟时间
            <b>${s.overhead_pct.toFixed(3)}%</b>（每步约 ${perStepUs} µs）；
            单次阶段计时成本（创建时校准）≈ ${report.calibration_us_per_stage} µs。</td></tr>
        <tr><th>未归因时间</th>
          <td>${fmtMs(s.unaccounted_total_ms)} ms（${s.unaccounted_pct.toFixed(2)}%）＝ 墙钟 − 各阶段之和，
            含计时器自身进入成本与未插桩代码；占比越小说明阶段划分覆盖越完整。</td></tr>
        <tr><th>数据完整性</th>
          <td>${report.truncated
            ? "记录已达 5 万步上限，仅保留前 5 万步（运行本身不受影响）"
            : `完整：已记录 ${report.recorded_steps} 步，每步一条记录`}；
            剖析数据写盘发生在<b>步与步之间</b>，不计入单步耗时，但会使批量运行总时长略微增加。</td></tr>
        <tr><th>结论</th>
          <td>${credible
            ? "测量开销与未归因占比都很低，本次剖析数据<b>可信</b>，测量本身对运行影响可忽略。"
            : "测量开销或未归因占比较高：步耗时本身极小时计时误差占比会放大，建议以趋势与占比而非绝对值做判断。"}</td></tr>
        <tr><th>优化建议</th><td>${esc(hint)}</td></tr>
      </tbody>
    </table>`;
}

/* ------------------------------------------------------------------ */
/* Init                                                                */
/* ------------------------------------------------------------------ */
async function init() {
  el("loadBtn").onclick = load;
  el("runSelect").onchange = load;
  el("applyRange").onclick = () => {
    const f = parseInt(el("fromStep").value, 10);
    const t = parseInt(el("toStep").value, 10);
    if (!isNaN(f) && !isNaN(t)) zoomChartTo(f, t);   // triggers refreshWindow via zoom event
    refreshWindow();
  };
  el("fullRange").onclick = () => {
    if (!report || report.empty) return;
    el("fromStep").value = report.first_step;
    el("toStep").value = report.last_step;
    zoomChartTo(report.first_step, report.last_step);
    refreshWindow();
  };
  el("probeBtn").onclick = runProbe;
  el("estInput").oninput = debounce(updateEstimate, 200);

  await fillRunSelect(el("runSelect"));
  const q = new URLSearchParams(window.location.search).get("run");
  if (q && el("runSelect").querySelector(`option[value="${q}"]`)) {
    el("runSelect").value = q;
  } else if (el("runSelect").options.length > 1) {
    el("runSelect").selectedIndex = 1;
  }
  if (el("runSelect").value) await load();
}

window.addEventListener("resize", () => {
  [stageChart, shareChart, scaleChart, probeChart].forEach((c) => c && c.resize());
});

init().catch((e) => showNotice(el("emptyNotice"), "初始化失败：" + e.message, "error"));
