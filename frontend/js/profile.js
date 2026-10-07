/* Performance profiling: per-step × per-phase timing analysis for one run. */

let runId = null;
let meta = null;
let data = null;          // last /profile analysis response
let charts = {};          // element id -> echarts instance

const FALLBACK_COLORS = ["#4f8cff", "#2ecc71", "#e74c3c", "#f39c12", "#9b59b6",
                         "#1abc9c", "#e67e22", "#3498db"];
const PHASE_COLORS = {
  move: "#4f8cff", recover: "#2ecc71", infect: "#e74c3c", update: "#9b59b6",
  occupancy: "#1abc9c", lane_change: "#f39c12", ns_update: "#e67e22",
  accelerate: "#3498db", grass: "#27ae60", animals: "#e67e22", rebuild: "#95a5a6",
  hash: "#16a085", boids: "#4f8cff", predators: "#c0392b",
  intervention: "#d35400", stats: "#f1c40f", snapshot: "#8e44ad",
  persist: "#e91e63", other: "#5b6b7c",
};

function phaseColor(key, i) {
  return PHASE_COLORS[key] || FALLBACK_COLORS[i % FALLBACK_COLORS.length];
}

function chartOf(id) {
  if (!charts[id]) charts[id] = echarts.init(el(id), "dark");
  return charts[id];
}

function fmtMs(v) {
  if (v == null) return "—";
  if (v !== 0 && Math.abs(v) < 0.01) return v.toExponential(1);
  return Number(v).toFixed(2);
}

/* ------------------------------------------------------------------ */
/* Loading                                                             */
/* ------------------------------------------------------------------ */
async function init() {
  el("refreshBtn").onclick = async () => { await fillRunSelect(el("runSelect")); };
  el("runSelect").onchange = () => loadRun();
  el("analyseBtn").onclick = () => analyse();
  el("bucketSel").onchange = () => analyse();
  el("profToggle").onchange = onToggle;
  el("probeBtn").onclick = runProbe;

  await fillRunSelect(el("runSelect"));
  const q = new URLSearchParams(window.location.search).get("run");
  if (q && el("runSelect").querySelector(`option[value="${q}"]`)) {
    el("runSelect").value = q;
  } else if (el("runSelect").options.length > 1) {
    el("runSelect").selectedIndex = 1;
  }
  await loadRun();
}

async function loadRun() {
  runId = el("runSelect").value;
  data = null;
  resetProbe();
  if (!runId) { renderEmpty("请选择一个运行。"); return; }
  meta = await get(`/api/runs/${runId}`);
  el("profToggle").checked = !!meta.profile_enabled;
  el("profState").textContent = meta.profile_enabled
    ? "已开启：每步记录分阶段耗时" : "已关闭：每步仅记录总耗时";
  data = await get(`/api/runs/${runId}/profile?buckets=${el("bucketSel").value}`);
  if (data.empty) {
    renderEmpty("该运行还没有剖析数据：先在「实时可视化」步进或批量运行若干步，再回到本页。" +
                (data.range ? "" : "（也可能该运行创建于剖析功能上线前。）"));
    return;
  }
  el("fromStep").value = data.range.from;
  el("toStep").value = data.range.to;
  el("fromStep").min = data.range.step_min;
  el("fromStep").max = data.range.step_max;
  el("toStep").min = data.range.step_min;
  el("toStep").max = data.range.step_max;
  renderAll();
}

async function analyse() {
  if (!runId) return;
  const qs = new URLSearchParams({ buckets: el("bucketSel").value });
  const f = el("fromStep").value, t = el("toStep").value;
  if (f !== "") qs.set("from", f);
  if (t !== "") qs.set("to", t);
  data = await get(`/api/runs/${runId}/profile?${qs}`);
  if (data.empty) { renderEmpty("所选步区间内没有剖析数据。"); return; }
  renderAll();
}

function renderEmpty(msg) {
  el("emptyNotice").textContent = msg;
  el("emptyNotice").style.display = "block";
  el("profileBody").style.display = "none";
}

function renderAll() {
  el("emptyNotice").style.display = "none";
  el("profileBody").style.display = "block";
  const s = data.summary;
  el("rangeHint").textContent =
    `共 ${s.steps_in_range} 步（已剖析 ${s.profiled_steps}` +
    (s.unprofiled_steps ? `，${s.unprofiled_steps} 步仅总耗时` : "") + "）";
  renderKpis();
  renderPhaseChart();
  renderShare();
  renderTables();
  renderScale();
  renderOverhead();
}

/* ------------------------------------------------------------------ */
/* KPI tiles                                                           */
/* ------------------------------------------------------------------ */
function renderKpis() {
  const s = data.summary;
  const sp = s.slowest_phase;
  const slow1 = s.slowest_steps.find((r) => r.step === s.max_total_step);
  el("kpis").innerHTML = `
    <div class="stat"><div class="k">平均每步耗时</div>
      <div class="v">${fmtMs(s.mean_total_ms)}<span class="small muted"> ms</span></div>
      <div class="d">第 ${data.range.from}–${data.range.to} 步 · p95 ${fmtMs(s.p95_total_ms)} ms</div></div>
    <div class="stat"><div class="k">最慢时间步</div>
      <div class="v">#${s.max_total_step}</div>
      <div class="d">${fmtMs(s.max_total_ms)} ms${slow1 && slow1.top_phase_label ? ` · 主导：${esc(slow1.top_phase_label)}` : ""}</div></div>
    <div class="stat"><div class="k">最慢阶段（占比最高）</div>
      <div class="v" style="font-size:20px">${sp ? esc(sp.label) : "—"}</div>
      <div class="d">${sp ? `平均 ${fmtMs(sp.mean_ms)} ms · 占 ${Math.round(sp.share * 100)}% · 峰值步 #${sp.max_step}` : "无分阶段数据"}</div></div>
    <div class="stat"><div class="k">估计测量开销</div>
      <div class="v">${fmtMs(data.overhead.estimated_overhead_pct)}<span class="small muted"> %</span></div>
      <div class="d">每步约 ${fmtMs(data.overhead.estimated_overhead_us_per_step)} µs · ${esc(data.overhead.verdict_label)}</div></div>`;
}

/* ------------------------------------------------------------------ */
/* Stacked per-phase chart over step buckets                           */
/* ------------------------------------------------------------------ */
function renderPhaseChart() {
  const keys = data.phases.map((p) => p.key);
  const labels = {};
  data.phases.forEach((p) => { labels[p.key] = p.label; });
  const xs = data.buckets.map((b) =>
    b.step_from === b.step_to ? `${b.step_from}` : `${b.step_from}–${b.step_to}`);

  const series = keys.map((k, i) => ({
    name: labels[k], type: "line", stack: "phases", areaStyle: { opacity: 0.75 },
    showSymbol: false, emphasis: { focus: "series" },
    itemStyle: { color: phaseColor(k, i) },
    lineStyle: { width: 0.5, color: phaseColor(k, i) },
    data: data.buckets.map((b) => b.profiled ? +(b.phases[k] || 0).toFixed(3) : null),
  }));
  series.push({
    name: "每步总耗时", type: "line", showSymbol: false, z: 3,
    itemStyle: { color: "#e6edf3" },
    lineStyle: { width: 1.5, type: "dashed", color: "#e6edf3" },
    data: data.buckets.map((b) => b.total_ms == null ? null : +b.total_ms.toFixed(3)),
  });

  chartOf("phaseChart").setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", valueFormatter: (v) => v == null ? "—" : `${v} ms` },
    legend: { textStyle: { color: "#8b98a5" }, top: 0, type: "scroll" },
    grid: { left: 60, right: 24, top: 40, bottom: 64 },
    xAxis: {
      type: "category", data: xs, name: "时间步",
      axisLine: { lineStyle: { color: "#26313f" } },
    },
    yAxis: {
      type: "value", name: "ms/步",
      axisLine: { lineStyle: { color: "#26313f" } },
      splitLine: { lineStyle: { color: "#1b2430" } },
    },
    dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 8 }],
    series,
  }, true);
}

/* ------------------------------------------------------------------ */
/* Phase share donut                                                   */
/* ------------------------------------------------------------------ */
function renderShare() {
  const per = data.summary.per_phase;
  if (!per.length) {
    chartOf("shareChart").clear();
    return;
  }
  chartOf("shareChart").setOption({
    backgroundColor: "transparent",
    tooltip: {
      trigger: "item",
      formatter: (p) => `${esc(p.name)}<br/>平均 ${fmtMs(p.value)} ms · 占 ${p.percent}%`,
    },
    legend: { textStyle: { color: "#8b98a5" }, bottom: 0, type: "scroll" },
    series: [{
      type: "pie", radius: ["42%", "68%"], center: ["50%", "46%"],
      label: { color: "#8b98a5", formatter: "{b}\n{d}%" },
      data: per.map((p, i) => ({
        name: p.label, value: +p.mean_ms.toFixed(4),
        itemStyle: { color: phaseColor(p.key, i) },
      })),
    }],
  }, true);
}

/* ------------------------------------------------------------------ */
/* Tables: slowest steps + per-phase detail                            */
/* ------------------------------------------------------------------ */
function renderTables() {
  const s = data.summary;
  el("slowBody").innerHTML = s.slowest_steps.map((r) => `
    <tr><td class="num">${r.step}</td><td class="num">${fmtMs(r.total_ms)}</td>
    <td>${esc(r.top_phase_label || "—")}</td>
    <td class="num">${r.top_phase_ms != null ? fmtMs(r.top_phase_ms) : "—"}</td></tr>`).join("")
    || '<tr><td colspan="4" class="muted">无数据</td></tr>';

  el("phaseBody").innerHTML = s.per_phase.map((p) => `
    <tr><td><span class="legend" style="display:inline-flex;margin:0 6px 0 0"><span class="swatch" style="background:${phaseColor(p.key, 0)}"></span></span>${esc(p.label)}</td>
    <td class="num">${fmtMs(p.mean_ms)}</td><td class="num">${Math.round(p.share * 100)}%</td>
    <td class="num">${fmtMs(p.max_ms)}</td><td class="num">#${p.max_step}</td></tr>`).join("")
    || '<tr><td colspan="5" class="muted">区间内未开启剖析，无分阶段数据</td></tr>';
}

/* ------------------------------------------------------------------ */
/* Scale trend (per-step cost vs population)                           */
/* ------------------------------------------------------------------ */
function renderScale() {
  const tr = data.scale_trend;
  const chart = chartOf("scaleChart");
  if (!tr || tr.constant || !tr.bins.length) {
    chart.clear();
    el("scaleChart").style.display = "none";
    el("scaleFit").innerHTML = tr && tr.n_min != null
      ? `本次运行个体规模基本恒定（约 ${tr.n_min}），运行内看不到规模趋势 —— 用下方「规模探针」评估不同规模下的每步耗时。`
      : "所选区间没有可用的规模数据。";
    return;
  }
  el("scaleChart").style.display = "block";
  const series = [{
    name: "步段均值", type: "scatter", symbolSize: 5,
    itemStyle: { color: "#5b6b7c", opacity: 0.45 },
    data: data.buckets.filter((b) => b.n != null && b.total_ms != null)
      .map((b) => [b.n, +b.total_ms.toFixed(3)]),
  }, {
    name: "规模分箱均值", type: "scatter",
    symbolSize: (v, p) => 8 + Math.min(18, tr.bins[p.dataIndex].samples / 4),
    itemStyle: { color: "#4f8cff" },
    data: tr.bins.map((b) => [b.n_mean, +b.mean_ms.toFixed(3)]),
  }];
  if (tr.fit) {
    const xs = [tr.n_min, tr.n_max];
    series.push({
      name: "线性拟合", type: "line", showSymbol: false,
      lineStyle: { color: "#f39c12", type: "dashed" },
      itemStyle: { color: "#f39c12" },
      data: xs.map((x) => [x, +(tr.fit.slope * x + tr.fit.intercept).toFixed(3)]),
    });
  }
  chart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "item", formatter: (p) => `规模 ${p.value[0]}<br/>${fmtMs(p.value[1])} ms/步` },
    legend: { textStyle: { color: "#8b98a5" }, top: 0 },
    grid: { left: 60, right: 24, top: 40, bottom: 44 },
    xAxis: { type: "value", name: "个体规模", axisLine: { lineStyle: { color: "#26313f" } }, splitLine: { lineStyle: { color: "#1b2430" } } },
    yAxis: { type: "value", name: "ms/步", axisLine: { lineStyle: { color: "#26313f" } }, splitLine: { lineStyle: { color: "#1b2430" } } },
    series,
  }, true);

  if (tr.fit) {
    const per1k = tr.fit.slope * 1000;
    el("scaleFit").innerHTML =
      `线性拟合：个体规模每 +1000，每步约 <b>${fmtMs(per1k)} ms</b>（R²=${tr.fit.r2}）。` +
      (tr.fit.project_ms != null
        ? ` 按此外推，规模翻倍至 ${tr.fit.project_n} 时每步约 <b>${fmtMs(tr.fit.project_ms)} ms</b>。`
        : "") +
      " 样本来自本运行内不同步，供趋势参考。";
  } else {
    el("scaleFit").textContent = "";
  }
}

/* ------------------------------------------------------------------ */
/* Overhead & credibility                                              */
/* ------------------------------------------------------------------ */
function renderOverhead() {
  const o = data.overhead;
  const s = data.summary;
  const cls = { ok: "finished", small: "running", large: "stopped" }[o.verdict] || "ready";
  const m = o.measured;
  el("overheadBody").innerHTML = `
    <div class="row between" style="margin-bottom:12px">
      <div class="row gap-6">
        <span class="badge ${cls}">${esc(o.verdict_label)}</span>
        ${m ? `<span class="badge paused">实测开/关段差 ${m.delta_pct == null ? "—" : fmtMs(m.delta_pct) + "%"}</span>` : ""}
      </div>
      <span class="muted small">时钟 ${esc(o.clock)} · 校准 ${fmtMs(o.calibration_ns_per_block)} ns/计时块</span>
    </div>
    <div class="grid grid-4">
      <div class="stat"><div class="k">每步平均计时块</div><div class="v">${fmtMs(o.avg_blocks_per_step)}</div>
        <div class="d">引擎阶段 + 管道阶段</div></div>
      <div class="stat"><div class="k">估计每步测量开销</div><div class="v">${fmtMs(o.estimated_overhead_us_per_step)}<span class="small muted"> µs</span></div>
        <div class="d">= 计时块数 × 校准开销</div></div>
      <div class="stat"><div class="k">占每步总耗时</div><div class="v">${fmtMs(o.estimated_overhead_pct)}<span class="small muted"> %</span></div>
        <div class="d">越小表示测量越不干扰运行</div></div>
      <div class="stat"><div class="k">已剖析步数</div><div class="v">${s.profiled_steps}<span class="small muted"> / ${s.steps_in_range}</span></div>
        <div class="d">${s.unprofiled_steps ? `${s.unprofiled_steps} 步仅总耗时（剖析关闭）` : "区间内全部步均有分阶段数据"}</div></div>
    </div>
    <ul class="muted small" style="margin:12px 0 0;padding-left:18px">
      ${o.notes.map((n) => `<li style="margin:4px 0">${esc(n)}</li>`).join("")}
    </ul>`;
}

/* ------------------------------------------------------------------ */
/* Profiling toggle                                                    */
/* ------------------------------------------------------------------ */
async function onToggle() {
  if (!runId) { el("profToggle").checked = false; return; }
  const enabled = el("profToggle").checked;
  try {
    await post(`/api/runs/${runId}/profile/toggle`, { enabled });
    el("profState").textContent = enabled
      ? "已开启：后续步将记录分阶段耗时" : "已关闭：后续步仅记录总耗时（用于实测对比开销）";
  } catch (e) {
    el("profState").textContent = "切换失败：" + e.message;
    el("profToggle").checked = !enabled;
  }
}

/* ------------------------------------------------------------------ */
/* Scale probe                                                         */
/* ------------------------------------------------------------------ */
function resetProbe() {
  el("probeChart").style.display = "none";
  el("probeSummary").textContent = "";
  el("probeStatus").textContent = "";
  if (charts.probeChart) { charts.probeChart.clear(); }
}

async function runProbe() {
  if (!runId) return;
  const scales = el("probeScales").value.split(",")
    .map((s) => parseFloat(s.trim())).filter((v) => isFinite(v) && v > 0);
  if (!scales.length) { el("probeStatus").textContent = "请输入有效倍率，如 0.5,1,2,4"; return; }
  const steps = parseInt(el("probeSteps").value || "30", 10);
  el("probeBtn").disabled = true;
  el("probeStatus").textContent = "探针运行中（在临时引擎上试跑，不影响本运行）…";
  try {
    const res = await post(`/api/runs/${runId}/profile/probe`, { scales, steps });
    renderProbe(res);
    el("probeStatus").textContent = "完成 ✓";
  } catch (e) {
    el("probeStatus").textContent = "失败：" + e.message;
  } finally {
    el("probeBtn").disabled = false;
  }
}

function renderProbe(res) {
  const pts = res.points;
  el("probeChart").style.display = "block";

  // Union of phase keys across probe points, labelled like the main analysis.
  const labelOf = {};
  (data.phases || []).forEach((p) => { labelOf[p.key] = p.label; });
  const keys = [];
  pts.forEach((p) => Object.keys(p.phases || {}).forEach((k) => {
    if (!keys.includes(k)) keys.push(k);
  }));

  const series = keys.map((k, i) => ({
    name: labelOf[k] || k, type: "bar", stack: "probe",
    itemStyle: { color: phaseColor(k, i) },
    data: pts.map((p) => +(p.phases[k] || 0).toFixed(3)),
  }));
  series.push({
    name: "每步总耗时", type: "line", symbolSize: 7,
    itemStyle: { color: "#e6edf3" }, lineStyle: { color: "#e6edf3", width: 2 },
    data: pts.map((p) => +p.mean_total_ms.toFixed(3)),
  });

  chartOf("probeChart").setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", valueFormatter: (v) => v == null ? "—" : `${v} ms` },
    legend: { textStyle: { color: "#8b98a5" }, top: 0, type: "scroll" },
    grid: { left: 60, right: 24, top: 40, bottom: 44 },
    xAxis: {
      type: "category",
      data: pts.map((p) => `×${p.factor}\nn=${p.n}`),
      axisLine: { lineStyle: { color: "#26313f" } },
    },
    yAxis: { type: "value", name: "ms/步", axisLine: { lineStyle: { color: "#26313f" } }, splitLine: { lineStyle: { color: "#1b2430" } } },
    series,
  }, true);

  // Summary: overall growth + fastest-growing phase -> optimisation advice.
  const first = pts[0], last = pts[pts.length - 1];
  const ratio = first.mean_total_ms > 0 ? last.mean_total_ms / first.mean_total_ms : null;
  let best = null;
  for (const k of keys) {
    const a = first.phases[k] || 0;
    const b = last.phases[k] || 0;
    if (b < 0.05 * last.mean_total_ms) continue;  // ignore negligible phases
    const r = a > 1e-9 ? b / a : Infinity;
    if (!best || r > best.r) best = { k, r };
  }
  let html = `规模 ×${first.factor}（n=${first.n}）→ ×${last.factor}（n=${last.n}）：` +
    `每步计算 ${fmtMs(first.mean_total_ms)} → <b>${fmtMs(last.mean_total_ms)} ms</b>` +
    (ratio != null ? `（${fmtMs(ratio)} 倍）。` : "。");
  if (best && isFinite(best.r)) {
    html += ` 增长最快的阶段：<b>${esc(labelOf[best.k] || best.k)}</b>（${fmtMs(best.r)} 倍）—— 规模增大前建议优先优化该阶段。`;
  }
  if (res.fit) {
    html += ` 线性拟合：每 +1000 个体约 +${fmtMs(res.fit.slope * 1000)} ms/步（R²=${res.fit.r2}）。`;
  }
  html += `<br><span class="muted">${esc(res.note)}</span>`;
  el("probeSummary").innerHTML = html;
}

/* ------------------------------------------------------------------ */
window.addEventListener("resize", () => {
  Object.values(charts).forEach((c) => c.resize());
});

init().catch((e) => console.error(e));
