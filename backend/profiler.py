"""Per-step, per-stage performance profiling for simulation runs.

A :class:`StepProfiler` is attached to every run by the run manager.  For each
time step it records the wall-clock time of the whole step plus a breakdown
into named stages: engine-internal phases (movement, infection detection, …)
instrumented inside the engines via ``Engine._stage()``, and orchestration
phases (statistics aggregation, serialisation, disk writes) instrumented by
the run manager around the engine call.  Records live in memory while the run
is active and are flushed to ``profile.json`` next to the other run artefacts.

Measurement honesty
-------------------
The profiler measures its own cost in two ways and reports both so users can
judge whether the numbers are trustworthy:

* ``overhead_ms`` per record — the *measured* time spent inside the
  profiler's own bookkeeping (summing deltas into the record dict).
* ``calibration_us_per_stage`` — the cost of one full no-op stage
  enter/exit cycle, measured with a tight loop when the profiler is created.

Every record also carries ``wall_ms`` (the untruncated truth for the whole
step) so the analysis can report *unaccounted* time = wall − Σ stages, which
bounds the measurement error plus any un-instrumented code.  Profile
persistence itself happens *between* steps, never inside a step's timing
window, so flushing cannot pollute the per-step numbers.
"""

from __future__ import annotations

import contextlib
import math
import time
from typing import Any, Dict, List, Optional

# Stage keys -> Chinese labels (shipped in the API response so the frontend
# stays generic).
STAGE_LABELS: Dict[str, str] = {
    # engine-internal phases
    "move": "移动",
    "recover": "康复判定",
    "infect": "感染检测",
    "ca_update": "元胞同步更新",
    "hash": "空间哈希构建",
    "flock": "群体行为",
    "predator": "捕食",
    "grass": "草生长",
    "animals": "动物行动",
    "rebuild": "索引重建",
    "accel": "IDM 加速度",
    "ns_update": "NS 更新",
    "lane_change": "换道",
    # run-manager orchestration phases
    "intervention": "干预",
    "stats": "统计聚合",
    "serialize": "序列化",
    "io": "写盘",
}

# Bound memory: stop recording beyond this many steps (the run keeps working;
# the report is flagged ``truncated``).
MAX_RECORDS = 50_000


class _StageTimer:
    """Context manager timing one named stage inside the current step."""

    __slots__ = ("_prof", "_name", "_t0")

    def __init__(self, prof: "StepProfiler", name: str) -> None:
        self._prof = prof
        self._name = name

    def __enter__(self) -> "_StageTimer":
        self._t0 = time.perf_counter_ns()
        return self

    def __exit__(self, *exc: Any) -> bool:
        t1 = time.perf_counter_ns()
        rec = self._prof._current
        if rec is not None:
            stages = rec["stages"]
            stages[self._name] = stages.get(self._name, 0.0) + (t1 - self._t0) / 1e6
        # Measure the profiler's own bookkeeping cost so it can be reported.
        self._prof._overhead_ns += time.perf_counter_ns() - t1
        return False


class StepProfiler:
    """Collects per-step stage timings for one run."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []
        self.truncated = False
        self._current: Optional[Dict[str, Any]] = None
        self._overhead_ns = 0
        self._flushed = 0
        self._calibration_ns = self._calibrate()

    # ------------------------------------------------------------------ #
    # Instrumentation API (used by the run manager and the engines)
    # ------------------------------------------------------------------ #
    @contextlib.contextmanager
    def track(self, step: int, n: int):
        """Record one time step: wall clock + everything staged inside."""
        if self._current is not None:  # nested track — just pass through
            yield
            return
        if len(self.records) >= MAX_RECORDS:
            self.truncated = True
            yield
            return
        self._overhead_ns = 0
        self._current = {"step": int(step), "n": int(n), "stages": {}}
        t0 = time.perf_counter_ns()
        try:
            yield self._current
        finally:
            rec = self._current
            self._current = None
            rec["wall_ms"] = (time.perf_counter_ns() - t0) / 1e6
            rec["overhead_ms"] = self._overhead_ns / 1e6
            self._overhead_ns = 0
            self.records.append(rec)

    def stage(self, name: str):
        """Time a named stage of the current step (no-op outside a step)."""
        if self._current is None:
            return contextlib.nullcontext()
        return _StageTimer(self, name)

    # ------------------------------------------------------------------ #
    # Self-calibration
    # ------------------------------------------------------------------ #
    def _calibrate(self, iters: int = 3000) -> float:
        """Average nanoseconds of one full no-op stage enter/exit cycle."""
        self._current = {"step": -1, "n": 0, "stages": {}}
        t0 = time.perf_counter_ns()
        for _ in range(iters):
            with self.stage("calibration"):
                pass
        total = time.perf_counter_ns() - t0
        self._current = None
        self._overhead_ns = 0
        return total / iters

    # ------------------------------------------------------------------ #
    # Flush pacing
    # ------------------------------------------------------------------ #
    def should_flush(self) -> bool:
        """Whether enough new records accumulated to justify a disk flush.

        Flushing serialises every record, so the interval grows with the
        record count — total flush work stays ~O(n) instead of O(n²) for
        very long runs.
        """
        pending = len(self.records) - self._flushed
        return pending >= max(50, len(self.records) // 10)

    def mark_flushed(self) -> None:
        self._flushed = len(self.records)

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "calibration_ns_per_stage": self._calibration_ns,
            "truncated": self.truncated,
            "records": [
                {
                    "step": r["step"],
                    "n": r["n"],
                    "wall_ms": round(r["wall_ms"], 4),
                    "overhead_ms": round(r.get("overhead_ms", 0.0), 4),
                    "stages": {k: round(v, 4) for k, v in r["stages"].items()},
                }
                for r in self.records
            ],
        }


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def _percentile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * q
    lo = int(math.floor(k))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _linreg(xs: List[float], ys: List[float]) -> Optional[Dict[str, float]]:
    """Least-squares fit ``y = slope*x + intercept`` with R²."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return {"slope": slope, "intercept": intercept, "r2": r2}


def _dominant_stage(stages: Dict[str, float]) -> Optional[str]:
    if not stages:
        return None
    return max(stages.items(), key=lambda kv: kv[1])[0]


def _bucket_points(records: List[Dict[str, Any]], ordered: List[str],
                   max_points: int) -> tuple:
    """Downsample records to at most ``max_points`` chart points.

    Returns ``(points, bucket_size)``; ``bucket_size`` 1 means raw per-step
    data.  Aggregated points carry per-stage *means* plus the bucket's max
    wall time so spikes stay visible.
    """
    n = len(records)
    if n <= max_points:
        return ([{
            "step": r["step"], "bucket": 1, "n": r["n"],
            "wall_ms": round(r["wall_ms"], 4),
            "wall_max_ms": round(r["wall_ms"], 4),
            "stages": {k: round(r["stages"].get(k, 0.0), 4) for k in ordered},
        } for r in records], 1)
    size = math.ceil(n / max_points)
    out: List[Dict[str, Any]] = []
    for i in range(0, n, size):
        chunk = records[i:i + size]
        m = len(chunk)
        out.append({
            "step": chunk[0]["step"],
            "bucket": size,
            "n": round(sum(r["n"] for r in chunk) / m),
            "wall_ms": round(sum(r["wall_ms"] for r in chunk) / m, 4),
            "wall_max_ms": round(max(r["wall_ms"] for r in chunk), 4),
            "stages": {k: round(sum(r["stages"].get(k, 0.0)
                                    for r in chunk) / m, 4) for k in ordered},
        })
    return out, size


def _summarize(sel: List[Dict[str, Any]], ordered: List[str]) -> Dict[str, Any]:
    """Window statistics over the selected step records."""
    count = len(sel)
    walls = sorted(r["wall_ms"] for r in sel)
    wall_total = sum(walls)
    overhead_total = sum(r.get("overhead_ms", 0.0) for r in sel)

    stage_stats: Dict[str, Dict[str, Any]] = {}
    accounted = 0.0
    for k in ordered:
        tot = 0.0
        mx = 0.0
        mx_step = sel[0]["step"]
        for r in sel:
            v = r["stages"].get(k, 0.0)
            tot += v
            if v > mx:
                mx, mx_step = v, r["step"]
        accounted += tot
        stage_stats[k] = {
            "total_ms": round(tot, 4),
            "mean_ms": round(tot / count, 4),
            "share": round(tot / wall_total, 4) if wall_total > 0 else 0.0,
            "max_ms": round(mx, 4),
            "max_step": mx_step,
        }

    p50 = _percentile(walls, 0.5)
    p95 = _percentile(walls, 0.95)
    slowest_stage = max(
        ((k, s["total_ms"]) for k, s in stage_stats.items()),
        key=lambda kv: kv[1], default=(None, 0.0))[0]

    by_wall = sorted(sel, key=lambda r: r["wall_ms"], reverse=True)
    slowest_steps = [{
        "step": r["step"],
        "n": r["n"],
        "wall_ms": round(r["wall_ms"], 4),
        "dominant_stage": _dominant_stage(r["stages"]),
        "stages": {k: round(v, 4) for k, v in r["stages"].items()},
    } for r in by_wall[:10]]

    threshold = 3 * p50
    spikes = [{
        "step": r["step"],
        "wall_ms": round(r["wall_ms"], 4),
        "ratio": round(r["wall_ms"] / p50, 1) if p50 > 0 else 0.0,
        "dominant_stage": _dominant_stage(r["stages"]),
    } for r in sel if p50 > 0 and r["wall_ms"] > threshold][:20]

    max_rec = by_wall[0]
    unaccounted = wall_total - accounted
    return {
        "steps": count,
        "wall_total_ms": round(wall_total, 4),
        "wall_mean_ms": round(wall_total / count, 4),
        "wall_p50_ms": round(p50, 4),
        "wall_p95_ms": round(p95, 4),
        "wall_max_ms": round(max_rec["wall_ms"], 4),
        "wall_max_step": max_rec["step"],
        "stage_stats": stage_stats,
        "slowest_stage": slowest_stage,
        "slowest_steps": slowest_steps,
        "spike_threshold_ms": round(threshold, 4),
        "spike_count": len(spikes),
        "spikes": spikes,
        "overhead_total_ms": round(overhead_total, 4),
        "overhead_pct": round(overhead_total / wall_total * 100, 4)
        if wall_total > 0 else 0.0,
        "unaccounted_total_ms": round(unaccounted, 4),
        "unaccounted_pct": round(unaccounted / wall_total * 100, 4)
        if wall_total > 0 else 0.0,
    }


def _scaling(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Trend of per-step wall time vs population size (whole run)."""
    ns = sorted({r["n"] for r in records})
    base: Dict[str, Any] = {
        "n_min": ns[0] if ns else 0,
        "n_max": ns[-1] if ns else 0,
    }
    # Need real variation in n for a meaningful fit.
    if len(ns) < 8 or ns[-1] < max(1, ns[0]) * 1.2:
        return {**base, "available": False}
    fit = _linreg([float(r["n"]) for r in records],
                  [r["wall_ms"] for r in records])
    if fit is None:
        return {**base, "available": False}
    return {
        **base,
        "available": True,
        "slope": fit["slope"],
        "intercept": fit["intercept"],
        "r2": round(fit["r2"], 4),
        "slope_ms_per_1000": round(fit["slope"] * 1000, 4),
    }


def build_report(data: Dict[str, Any], frm: Optional[int] = None,
                 to: Optional[int] = None,
                 max_points: int = 1200) -> Dict[str, Any]:
    """Build the API payload for the profiling panel.

    ``points`` always cover the whole run (downsampled if needed) so the chart
    keeps full context; ``summary`` is computed over the requested step window
    so users are not stuck with one global average.
    """
    records = list((data or {}).get("records") or [])
    if not records:
        return {
            "empty": True,
            "reason": "该运行还没有剖析数据：请先运行若干时间步"
                      "（在此功能上线前创建的运行需要重新运行才会记录）。",
        }

    first, last = records[0]["step"], records[-1]["step"]
    f = first if frm is None else max(int(frm), first)
    t = last if to is None else min(int(to), last)
    if f > t:
        f, t = first, last
    sel = [r for r in records if f <= r["step"] <= t]
    if not sel:
        sel = records
        f, t = first, last

    totals: Dict[str, float] = {}
    for r in records:
        for k, v in r["stages"].items():
            totals[k] = totals.get(k, 0.0) + v
    ordered = sorted(totals, key=lambda k: (-totals[k], k))

    points, bucket = _bucket_points(records, ordered, max_points)
    summary = _summarize(sel, ordered)
    summary["scaling"] = _scaling(records)

    return {
        "empty": False,
        "recorded_steps": len(records),
        "first_step": first,
        "last_step": last,
        "range": {"from": f, "to": t},
        "bucket": bucket,
        "stages": ordered,
        "stage_labels": STAGE_LABELS,
        "points": points,
        "summary": summary,
        "totals": {
            "wall_ms": round(sum(r["wall_ms"] for r in records), 4),
            "overhead_ms": round(sum(r.get("overhead_ms", 0.0)
                                       for r in records), 4),
        },
        "calibration_us_per_stage": round(
            (data.get("calibration_ns_per_stage") or 0.0) / 1000.0, 3),
        "truncated": bool(data.get("truncated")),
    }


# --------------------------------------------------------------------------- #
# Scale probe: measure step cost at several population sizes
# --------------------------------------------------------------------------- #
# Which config keys control the population for each engine, and how they scale.
_SCALE_KEYS = {
    ("traffic", "ca"): (("length",), "linear"),       # n = lanes*length*density
    ("traffic", "abm"): (("n",), "linear"),
    ("ecology", "ca"): (("n_rabbits", "n_foxes"), "linear"),
    ("ecology", "abm"): (("n_boids",), "linear"),
    ("epidemic", "ca"): (("width", "height"), "sqrt"),  # cells = width*height
    ("epidemic", "abm"): (("n",), "linear"),
}

_MAX_PROBE_POPULATION = 200_000


def scale_probe(meta: Dict[str, Any], factors: Optional[List[float]] = None,
                steps: int = 30) -> Dict[str, Any]:
    """Benchmark ``engine.step()`` at several population scales.

    Spins up throwaway engines with the run's config (and seed) but scaled
    population knobs, warms up, then times raw steps — no stats, no
    serialisation, no disk I/O — so the result isolates how the compute core
    scales.  This is what lets a user extrapolate before scaling up.
    """
    from .engine import make_engine  # deferred: engines must not import this

    domain, model = meta["domain"], meta["model"]
    key = (domain, model)
    if key not in _SCALE_KEYS:
        raise ValueError(f"{domain}/{model} 不支持规模探针")
    keys, mode = _SCALE_KEYS[key]
    base_config = dict(meta.get("config") or {})
    seed = meta.get("seed", 0)
    steps = max(5, min(int(steps), 100))

    try:
        fs = sorted({round(float(f), 3) for f in (factors or [0.5, 1, 2, 4])})
    except (TypeError, ValueError):
        raise ValueError("规模因子格式不正确")
    fs = [f for f in fs if 0.25 <= f <= 8][:6]
    if not fs:
        raise ValueError("没有有效的规模因子（允许 0.25–8，最多 6 个）")

    base = make_engine(domain, model, config=base_config, seed=seed)
    base_n = max(1, base.population())
    del base

    points: List[Dict[str, Any]] = []
    skipped: List[float] = []
    for f in fs:
        if base_n * f > _MAX_PROBE_POPULATION:
            skipped.append(f)
            continue
        cfg = dict(base_config)
        if mode == "sqrt":
            s = math.sqrt(f)
            for k in keys:
                cfg[k] = max(4, int(round(float(cfg.get(k, 1)) * s)))
        else:
            for k in keys:
                cfg[k] = max(1, int(round(float(cfg.get(k, 1)) * f)))
        eng = make_engine(domain, model, config=cfg, seed=seed)
        for _ in range(max(2, steps // 6)):  # warm-up
            eng.step()
        samples: List[float] = []
        pops: List[int] = []
        for _ in range(steps):
            # Population can drift while timing (breeding, deaths), so the
            # x-axis uses the mean population over the timed window.
            pops.append(eng.population())
            t0 = time.perf_counter_ns()
            eng.step()
            samples.append((time.perf_counter_ns() - t0) / 1e6)
        samples.sort()
        points.append({
            "factor": f,
            "n": round(sum(pops) / len(pops)),
            "mean_ms": round(sum(samples) / len(samples), 4),
            "p95_ms": round(_percentile(samples, 0.95), 4),
            "min_ms": round(samples[0], 4),
        })
    if not points:
        raise ValueError("所有规模因子都超出安全上限，无法探测")

    fit = _linreg([float(p["n"]) for p in points],
                  [p["mean_ms"] for p in points])
    return {
        "points": points,
        "fit": fit,
        "steps_per_scale": steps,
        "current_n": base_n,
        "skipped_factors": skipped,
        "note": "探针以相同配置与种子新建临时引擎，仅测量 engine.step() 核心计算"
                "（不含统计、序列化与写盘），每档规模先热身后计时。",
    }
