"""Per-step, per-phase wall-clock profiling for simulation runs.

The profiler answers three questions for a run:

* **Where does the time go?**  Every step is split into named phases —
  engine-internal ones (movement, infection detection, ...) measured inside
  the engines via :meth:`Engine._timed`, and pipeline ones (statistics,
  snapshot serialisation, disk persistence, interventions) measured by the
  run manager around the engine call.
* **How does it evolve?**  Rows are stored per step, so any step range can be
  analysed instead of a single run-wide average, and each phase can be
  watched independently across step buckets of the same run.
* **Can the numbers be trusted?**  Timing itself costs time.  The profiler
  calibrates the cost of one timing block on the current machine, counts how
  many blocks each step used, and reports the estimated overhead both in
  absolute terms and as a percentage of the step time.  Because the overhead
  is systematic (every phase is inflated by the same tiny amount), phase
  *shares* and step-to-step *trends* stay valid even when absolute values are
  slightly high.  Profiling can also be toggled mid-run: steps recorded while
  it is disabled carry only the total wall time, which lets the analysis
  compare profiled vs. unprofiled segments of the same run directly.

Rows are kept in memory by the run manager and flushed to ``profile.json``
(next to the run's series) on the same cadence as the series writes.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

# Canonical phase registry: key -> Chinese label.  Engines emit the compute
# phases; the run manager emits the pipeline phases; ``other`` is the derived
# remainder (total minus named phases) so shares always sum to 100%.
PHASE_LABELS: Dict[str, str] = {
    "move": "移动",
    "recover": "康复判定",
    "infect": "感染检测",
    "update": "网格同步更新",
    "occupancy": "占据表构建",
    "lane_change": "变道决策",
    "ns_update": "行驶状态更新",
    "accelerate": "加速度计算",
    "grass": "草地生长",
    "animals": "动物行动",
    "rebuild": "索引重建",
    "hash": "空间哈希构建",
    "boids": "鸟群行为",
    "predators": "捕食者行为",
    "intervention": "干预应用",
    "stats": "统计聚合",
    "snapshot": "快照序列化",
    "persist": "写盘持久化",
    "other": "其他 / 未归类",
}

# Display order: engine compute phases first, then the pipeline phases.
PHASE_ORDER: List[str] = [
    "move", "recover", "infect", "update",
    "occupancy", "lane_change", "ns_update", "accelerate",
    "grass", "animals", "rebuild",
    "hash", "boids", "predators",
    "intervention", "stats", "snapshot", "persist",
]


# --------------------------------------------------------------------------- #
# Overhead calibration
# --------------------------------------------------------------------------- #
def calibrate_block_overhead(repeat: int = 20000) -> float:
    """Estimate the cost of one timing block, in seconds.

    Compares an empty loop with a loop containing a minimal timed block (two
    ``perf_counter`` calls + one dict accumulate — exactly what
    :meth:`Engine._timed` and :meth:`StepProfiler.phase` do) and returns the
    per-iteration difference.  Run once per profiler so the estimate reflects
    the current machine rather than a hard-coded constant.
    """
    t0 = time.perf_counter()
    for _ in range(repeat):
        pass
    base = time.perf_counter() - t0

    acc: Dict[str, float] = {}
    t0 = time.perf_counter()
    for _ in range(repeat):
        t1 = time.perf_counter()
        acc["cal"] = acc.get("cal", 0.0) + (time.perf_counter() - t1)
    full = time.perf_counter() - t0
    return max(0.0, (full - base) / repeat)


# --------------------------------------------------------------------------- #
# Per-run accumulator
# --------------------------------------------------------------------------- #
class StepProfiler:
    """Accumulates per-step timing rows for one run.

    A *row* is ``{"step", "total_ms", "n"}`` plus, while profiling is enabled,
    ``"phases"`` (phase -> ms), ``"engine_ms"`` (the externally timed
    ``engine.step()``) and ``"ov_ms"`` (estimated measurement overhead).
    Disabled steps still record the total so the analysis can compare
    profiled vs. unprofiled segments of the same run.
    """

    VERSION = 1

    def __init__(self, enabled: bool = True,
                 calibration_ns: Optional[float] = None) -> None:
        self.enabled = bool(enabled)
        self.calibration_ns = (float(calibration_ns) if calibration_ns
                               else calibrate_block_overhead() * 1e9)
        self.rows: List[Dict[str, Any]] = []
        self._t0 = 0.0
        self._phases: Dict[str, float] = {}
        self._blocks = 0
        self._engine_ms = 0.0

    # -- step bracketing (run manager side) -------------------------------- #
    def begin_step(self) -> None:
        self._phases = {}
        self._blocks = 0
        self._engine_ms = 0.0
        self._t0 = time.perf_counter()

    @contextmanager
    def phase(self, name: str):
        """Time a run-manager-level phase (a no-op check when disabled)."""
        if not self.enabled:
            yield
            return
        t = time.perf_counter()
        try:
            yield
        finally:
            self._phases[name] = self._phases.get(name, 0.0) + \
                (time.perf_counter() - t) * 1000.0
            self._blocks += 1

    def set_engine(self, engine_ms: float,
                   phases: Dict[str, float], blocks: int) -> None:
        """Merge the engine-internal measurements after ``engine.step()``."""
        self._engine_ms = engine_ms
        if self.enabled:
            for k, v in phases.items():
                self._phases[k] = self._phases.get(k, 0.0) + v
            self._blocks += blocks

    def end_step(self, step: int, n: int) -> Dict[str, Any]:
        total_ms = (time.perf_counter() - self._t0) * 1000.0
        row: Dict[str, Any] = {"step": step,
                               "total_ms": round(total_ms, 4), "n": int(n)}
        if self.enabled:
            row["engine_ms"] = round(self._engine_ms, 4)
            row["phases"] = {k: round(v, 4) for k, v in self._phases.items()}
            row["ov_ms"] = round(self._blocks * self.calibration_ns / 1e6, 4)
        self.rows.append(row)
        return row

    # -- serialisation ------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.VERSION, "clock": "perf_counter",
                "enabled": self.enabled,
                "calibration_ns_per_block": round(self.calibration_ns, 1),
                "rows": list(self.rows)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StepProfiler":
        prof = cls(enabled=bool(data.get("enabled", True)),
                   calibration_ns=data.get("calibration_ns_per_block"))
        prof.rows = list(data.get("rows") or [])
        return prof


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def _row_other(row: Dict[str, Any]) -> float:
    """Unaccounted remainder of a profiled row (clamped at zero)."""
    return max(0.0, row["total_ms"] - sum(row["phases"].values()))


def _linfit(points: List[Tuple[float, float]]) -> Optional[Dict[str, float]]:
    """Least-squares fit ``y = slope*x + intercept`` with an R² goodness."""
    n = len(points)
    if n < 2:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    if sxx <= 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    slope = sxy / sxx
    intercept = my - slope * mx
    sst = sum((p[1] - my) ** 2 for p in points)
    sse = sum((p[1] - (slope * p[0] + intercept)) ** 2 for p in points)
    r2 = 1.0 - sse / sst if sst > 0 else 1.0
    return {"slope": slope, "intercept": intercept, "r2": round(r2, 4)}


def analyse(profile: Dict[str, Any], meta: Dict[str, Any],
            frm: Optional[int] = None, to: Optional[int] = None,
            n_buckets: int = 60, scale_bins: int = 12) -> Dict[str, Any]:
    """Analyse stored profile rows over a step range.

    Returns per-phase shares, the slowest steps, per-bucket phase breakdowns
    (for the stacked chart), a population-scale trend with a linear fit, and
    the measurement-overhead assessment.  Pure function of the stored rows.
    """
    rows = list(profile.get("rows") or [])
    out: Dict[str, Any] = {
        "run_id": meta.get("id"),
        "name": meta.get("name"),
        "domain": meta.get("domain"),
        "model": meta.get("model"),
        "profiling_enabled": bool(meta.get("profile_enabled", True)),
        "clock": profile.get("clock", "perf_counter"),
        "calibration_ns_per_block": profile.get("calibration_ns_per_block", 0),
    }
    if not rows:
        out["empty"] = True
        return out

    step_min, step_max = rows[0]["step"], rows[-1]["step"]
    frm = step_min if frm is None else max(int(frm), step_min)
    to = step_max if to is None else min(int(to), step_max)
    if frm > to:
        frm, to = to, frm
    sel = [r for r in rows if frm <= r["step"] <= to]
    if not sel:
        out["empty"] = True
        out["range"] = {"from": frm, "to": to,
                        "step_min": step_min, "step_max": step_max}
        return out

    profiled = [r for r in sel if r.get("phases")]
    unprofiled = [r for r in sel if not r.get("phases")]

    # -- totals ---------------------------------------------------------- #
    totals = [r["total_ms"] for r in sel]
    mean_total = sum(totals) / len(totals)
    mx_row = max(sel, key=lambda r: r["total_ms"])
    ordered = sorted(totals)
    p95 = ordered[int(0.95 * (len(ordered) - 1))]
    ns = [r["n"] for r in sel if r.get("n") is not None]

    # -- per-phase aggregates --------------------------------------------- #
    seen = set()
    for r in profiled:
        seen.update(r["phases"].keys())
    keys = [k for k in PHASE_ORDER if k in seen]
    keys += sorted(seen - set(PHASE_ORDER))

    per_phase: List[Dict[str, Any]] = []
    mean_total_profiled = (sum(r["total_ms"] for r in profiled) / len(profiled)
                           if profiled else 0.0)
    if profiled:
        for k in keys:
            vals = [r["phases"].get(k, 0.0) for r in profiled]
            mean = sum(vals) / len(vals)
            mi = max(range(len(vals)), key=lambda i: vals[i])
            per_phase.append({
                "key": k, "label": PHASE_LABELS.get(k, k),
                "mean_ms": round(mean, 4),
                "share": round(mean / mean_total_profiled, 4)
                if mean_total_profiled else 0.0,
                "max_ms": round(vals[mi], 4),
                "max_step": profiled[mi]["step"],
            })
        others = [_row_other(r) for r in profiled]
        omean = sum(others) / len(others)
        mi = max(range(len(others)), key=lambda i: others[i])
        per_phase.append({
            "key": "other", "label": PHASE_LABELS["other"],
            "mean_ms": round(omean, 4),
            "share": round(omean / mean_total_profiled, 4)
            if mean_total_profiled else 0.0,
            "max_ms": round(others[mi], 4),
            "max_step": profiled[mi]["step"],
        })
        per_phase.sort(key=lambda p: -p["mean_ms"])

    # -- slowest individual steps ------------------------------------------ #
    slowest: List[Dict[str, Any]] = []
    for r in sorted(profiled, key=lambda x: -x["total_ms"])[:5]:
        top_key, top_val = "other", _row_other(r)
        for k, v in r["phases"].items():
            if v > top_val:
                top_key, top_val = k, v
        slowest.append({"step": r["step"], "total_ms": r["total_ms"],
                        "top_phase": top_key,
                        "top_phase_label": PHASE_LABELS.get(top_key, top_key),
                        "top_phase_ms": round(top_val, 4)})
    if not slowest:  # no profiled rows — still report totals
        for r in sorted(sel, key=lambda x: -x["total_ms"])[:5]:
            slowest.append({"step": r["step"], "total_ms": r["total_ms"],
                            "top_phase": None, "top_phase_label": None,
                            "top_phase_ms": None})

    # -- step-range buckets (stacked chart data) ---------------------------- #
    n_buckets = max(1, min(int(n_buckets), len(sel)))
    size = (to - frm + 1) / n_buckets
    acc = [{"rows": 0, "total": 0.0, "n_sum": 0.0, "n_cnt": 0,
            "ph_cnt": 0, "ph": {}, "other": 0.0} for _ in range(n_buckets)]
    for r in sel:
        b = acc[min(n_buckets - 1, int((r["step"] - frm) / size))]
        b["rows"] += 1
        b["total"] += r["total_ms"]
        if r.get("n") is not None:
            b["n_sum"] += r["n"]
            b["n_cnt"] += 1
        if r.get("phases"):
            b["ph_cnt"] += 1
            for k, v in r["phases"].items():
                b["ph"][k] = b["ph"].get(k, 0.0) + v
            b["other"] += _row_other(r)
    buckets: List[Dict[str, Any]] = []
    for i, b in enumerate(acc):
        lo = frm + int(i * size)
        hi = to if i == n_buckets - 1 else frm + int((i + 1) * size) - 1
        entry: Dict[str, Any] = {
            "step_from": lo, "step_to": hi,
            "rows": b["rows"], "profiled": b["ph_cnt"],
            "total_ms": round(b["total"] / b["rows"], 4) if b["rows"] else None,
            "n": round(b["n_sum"] / b["n_cnt"], 1) if b["n_cnt"] else None,
            "phases": {},
        }
        if b["ph_cnt"]:
            entry["phases"] = {k: round(v / b["ph_cnt"], 4)
                               for k, v in b["ph"].items()}
            entry["phases"]["other"] = round(b["other"] / b["ph_cnt"], 4)
        buckets.append(entry)

    # -- population-scale trend -------------------------------------------- #
    scaled = [r for r in profiled if r.get("n") is not None]
    trend: Dict[str, Any] = {"constant": True, "bins": [], "fit": None}
    if scaled:
        n_lo = min(r["n"] for r in scaled)
        n_hi = max(r["n"] for r in scaled)
        trend["n_min"], trend["n_max"] = n_lo, n_hi
        if n_hi > n_lo:
            trend["constant"] = False
            bins_n = max(2, min(int(scale_bins), len(scaled)))
            width = (n_hi - n_lo) / bins_n
            groups: List[List[Dict[str, Any]]] = [[] for _ in range(bins_n)]
            for r in scaled:
                groups[min(bins_n - 1, int((r["n"] - n_lo) / width))].append(r)
            for i, g in enumerate(groups):
                if not g:
                    continue
                m = len(g)
                entry = {
                    "n_from": round(n_lo + i * width, 1),
                    "n_to": round(n_lo + (i + 1) * width, 1),
                    "n_mean": round(sum(x["n"] for x in g) / m, 1),
                    "mean_ms": round(sum(x["total_ms"] for x in g) / m, 4),
                    "max_ms": round(max(x["total_ms"] for x in g), 4),
                    "samples": m,
                    "phases": {k: round(sum(x["phases"].get(k, 0.0)
                                            for x in g) / m, 4) for k in keys},
                }
                entry["phases"]["other"] = round(
                    sum(_row_other(x) for x in g) / m, 4)
                trend["bins"].append(entry)
            fit = _linfit([(float(r["n"]), r["total_ms"]) for r in scaled])
            if fit:
                fit["project_n"] = 2 * n_hi
                fit["project_ms"] = round(
                    fit["slope"] * 2 * n_hi + fit["intercept"], 4)
            trend["fit"] = fit

    # -- measurement overhead ---------------------------------------------- #
    calib_ns = float(profile.get("calibration_ns_per_block") or 0.0)
    ov_rows = [r for r in profiled if r.get("ov_ms") is not None]
    est_ov_ms = (sum(r["ov_ms"] for r in ov_rows) / len(ov_rows)
                 if ov_rows else 0.0)
    blocks_avg = (est_ov_ms * 1e6 / calib_ns) if calib_ns > 0 else 0.0
    est_pct = (est_ov_ms / mean_total_profiled * 100.0
               if mean_total_profiled else 0.0)

    measured = None
    if profiled and unprofiled:
        on = sum(r["total_ms"] for r in profiled) / len(profiled)
        off = sum(r["total_ms"] for r in unprofiled) / len(unprofiled)
        measured = {
            "on_mean_ms": round(on, 4), "off_mean_ms": round(off, 4),
            "delta_ms": round(on - off, 4),
            "delta_pct": round((on - off) / off * 100.0, 2) if off else None,
            "off_steps": len(unprofiled),
        }

    if est_pct < 1.0:
        verdict, verdict_label = "ok", "测量开销可忽略，结果可信"
    elif est_pct < 5.0:
        verdict, verdict_label = "small", "测量开销很小，占比与趋势可信"
    else:
        verdict, verdict_label = "large", "测量开销偏大，请以占比与趋势为准"

    notes = [
        "计时使用 time.perf_counter() 单调高精度时钟；每个计时块 = 两次取时 + 一次字典累加。",
        f"本机校准：单个计时块约 {calib_ns:.0f} ns；区间内每步平均 "
        f"{blocks_avg:.1f} 个计时块，估计每步额外开销约 "
        f"{est_ov_ms * 1000:.2f} µs（约占每步总耗时 {est_pct:.2f}%）。",
        "测量开销是系统性、同向的（每个阶段都被同等略微放大），因此各阶段占比"
        "与步间趋势不受影响；绝对耗时略高于关闭剖析时的真实值。",
    ]
    if measured:
        notes.append(
            f"实测对比：本运行中剖析开启段平均每步 {measured['on_mean_ms']:g} ms，"
            f"关闭段 {measured['off_mean_ms']:g} ms"
            f"（{measured['off_steps']} 步），差值 "
            f"{measured['delta_ms']:g} ms。注意两段对应不同时间步、负载本身可能"
            f"不同，仅供参考。")
    else:
        notes.append("如需实测开销：在运行中关闭剖析开关若干步后再开启，本面板"
                     "将自动对比开/关步段的每步总耗时。")
    if unprofiled:
        notes.append(f"所选区间内有 {len(unprofiled)} 步未开启剖析，这些步仅"
                     f"记录总耗时，不参与分阶段统计。")
    notes.append("进程启动后的前若干步可能包含缓存预热开销，评估稳态性能时"
                 "建议用步区间排除起始段。")

    out.update({
        "empty": False,
        "range": {"from": frm, "to": to,
                  "step_min": step_min, "step_max": step_max},
        "phases": ([{"key": k, "label": PHASE_LABELS.get(k, k)} for k in keys]
                   + ([{"key": "other", "label": PHASE_LABELS["other"]}]
                      if profiled else [])),
        "summary": {
            "steps_in_range": len(sel),
            "profiled_steps": len(profiled),
            "unprofiled_steps": len(unprofiled),
            "mean_total_ms": round(mean_total, 4),
            "max_total_ms": round(mx_row["total_ms"], 4),
            "max_total_step": mx_row["step"],
            "p95_total_ms": round(p95, 4),
            "mean_engine_ms": round(
                sum(r.get("engine_ms", 0.0) for r in profiled) / len(profiled),
                4) if profiled else None,
            "mean_n": round(sum(ns) / len(ns), 1) if ns else None,
            "per_phase": per_phase,
            "slowest_phase": per_phase[0] if per_phase else None,
            "slowest_steps": slowest,
        },
        "buckets": buckets,
        "scale_trend": trend,
        "overhead": {
            "clock": "time.perf_counter()",
            "calibration_ns_per_block": round(calib_ns, 1),
            "avg_blocks_per_step": round(blocks_avg, 1),
            "estimated_overhead_us_per_step": round(est_ov_ms * 1000.0, 2),
            "estimated_overhead_pct": round(est_pct, 3),
            "measured": measured,
            "verdict": verdict,
            "verdict_label": verdict_label,
            "notes": notes,
        },
    })
    return out


# --------------------------------------------------------------------------- #
# Scale probe: extrapolate per-step cost to larger populations
# --------------------------------------------------------------------------- #
def scale_config(domain: str, model: str, config: Dict[str, Any],
                 factor: float) -> Dict[str, Any]:
    """Return a config whose *population* is scaled by ``factor``.

    The population knob differs per model: agent counts for ABMs, grid area
    for the epidemic CA, and vehicle density for the traffic CA.
    """
    cfg = dict(config)
    key = f"{domain}/{model}"
    if key in ("epidemic/abm", "traffic/abm"):
        cfg["n"] = max(1, int(int(cfg.get("n", 1)) * factor))
    elif key == "ecology/abm":
        cfg["n_boids"] = max(1, int(int(cfg.get("n_boids", 1)) * factor))
    elif key == "ecology/ca":
        cfg["n_rabbits"] = max(0, int(int(cfg.get("n_rabbits", 0)) * factor))
        cfg["n_foxes"] = max(0, int(int(cfg.get("n_foxes", 0)) * factor))
    elif key == "epidemic/ca":
        side = factor ** 0.5
        cfg["width"] = max(10, int(int(cfg.get("width", 10)) * side))
        cfg["height"] = max(10, int(int(cfg.get("height", 10)) * side))
    elif key == "traffic/ca":
        cfg["density"] = min(0.95, float(cfg.get("density", 0.2)) * factor)
    return cfg


def run_scale_probe(domain: str, model: str, config: Dict[str, Any],
                    seed: int, factors: Optional[List[float]] = None,
                    steps: int = 30) -> Dict[str, Any]:
    """Measure per-step *compute* cost at several population scales.

    Each scale runs in a throwaway engine (nothing is persisted), so the
    probe measures pure engine time — no statistics, serialisation or disk
    writes — and never touches the live run.
    """
    from .engine import make_engine  # local import: avoids a module cycle

    factors = [float(f) for f in (factors or [0.5, 1, 2, 4])]
    if not 1 <= len(factors) <= 8:
        raise ValueError("规模倍率数量需在 1–8 之间")
    if any(f <= 0 or f > 16 for f in factors):
        raise ValueError("规模倍率需在 (0, 16] 之间")
    steps = int(steps)
    if not 1 <= steps <= 200:
        raise ValueError("探针步数需在 1–200 之间")

    points: List[Dict[str, Any]] = []
    for f in sorted(set(factors)):
        cfg = scale_config(domain, model, config, f)
        eng = make_engine(domain, model, config=cfg, seed=seed)
        n = eng.population()
        if n > 100000:
            raise ValueError(f"倍率 {f:g} 对应个体规模 {n}，超出探针上限 100000")
        eng.profiling = True
        for _ in range(2):  # warm-up: stabilise caches and populations
            eng.step()
            eng.pop_phase_times()
        totals: List[float] = []
        phases_acc: Dict[str, float] = {}
        for _ in range(steps):
            t0 = time.perf_counter()
            eng.step()
            totals.append((time.perf_counter() - t0) * 1000.0)
            ph, _ = eng.pop_phase_times()
            for k, v in ph.items():
                phases_acc[k] = phases_acc.get(k, 0.0) + v
        m = len(totals)
        points.append({
            "factor": f, "n": n, "steps": m,
            "mean_total_ms": round(sum(totals) / m, 4),
            "max_total_ms": round(max(totals), 4),
            "phases": {k: round(v / m, 4) for k, v in phases_acc.items()},
        })

    fit = _linfit([(float(p["n"]), p["mean_total_ms"]) for p in points])
    return {
        "domain": domain, "model": model,
        "points": points, "fit": fit,
        "note": "探针在临时引擎上运行，仅测量引擎纯计算耗时（不含统计聚合、"
                "序列化与写盘），不影响当前运行的数据。",
    }
