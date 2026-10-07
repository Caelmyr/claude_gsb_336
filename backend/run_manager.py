"""Run lifecycle management: real-time stepping, batch runs, interventions.

A :class:`RunManager` owns the in-memory engines for the current server
process.  Every mutation funnels through :mod:`backend.storage` so the
atomic-write / time-step-sharding guarantees hold whether a step comes from the
UI or a background batch.  Engines are kept in memory while a run exists in
this process so a run can be stepped interactively; batch runs that belong to a
comparison experiment can drop their engine on completion to bound memory.

Concurrency: each run has its own re-entrant lock, so long batch runs on one
scene never block interactive stepping on another.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from . import models, profiler, storage, util
from .engine import make_engine
from .engine.base import Engine


class RunManager:
    def __init__(self) -> None:
        self._engines: Dict[str, Engine] = {}
        self._profilers: Dict[str, profiler.StepProfiler] = {}
        self._locks: Dict[str, threading.RLock] = {}
        self._abort: set = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _lock_for(self, run_id: str) -> threading.RLock:
        with self._lock:
            return self._locks.setdefault(run_id, threading.RLock())

    def _engine(self, run_id: str) -> Optional[Engine]:
        with self._lock:
            return self._engines.get(run_id)

    def _require(self, run_id: str) -> tuple:
        eng = self._engine(run_id)
        meta = storage.load_run_meta(run_id)
        if meta is None:
            raise KeyError(f"run not found: {run_id}")
        return eng, meta

    def _profiler_for(self, run_id: str,
                      meta: Dict[str, Any]) -> profiler.StepProfiler:
        """Return the run's profiler, recreating it from disk if needed."""
        with self._lock:
            prof = self._profilers.get(run_id)
        if prof is None:
            saved = storage.load_profile(run_id)
            prof = (profiler.StepProfiler.from_dict(saved) if saved
                    else profiler.StepProfiler(
                        enabled=bool(meta.get("profile_enabled", True))))
            prof.enabled = bool(meta.get("profile_enabled", prof.enabled))
            with self._lock:
                self._profilers[run_id] = prof
        return prof

    # ------------------------------------------------------------------ #
    # Create
    # ------------------------------------------------------------------ #
    def create_run(self, scene: models.Scene, name: Optional[str] = None,
                   seed: Optional[int] = None,
                   snapshot_interval: int = 1,
                   profile: bool = True) -> Dict[str, Any]:
        config = models.resolve_config(scene)
        if seed is None:
            seed = int(config.get("seed", 0))
        engine = make_engine(scene.domain, scene.model, config=config, seed=seed)
        run_id = util.new_id("run")
        now = util.now_iso()
        meta: Dict[str, Any] = {
            "id": run_id,
            "name": name or f"{scene.name} · 运行",
            "scene_id": scene.id,
            "scene_name": scene.name,
            "domain": scene.domain,
            "model": scene.model,
            "config": config,
            "interventions": [{**i, "applied": False}
                              for i in scene.interventions],
            "snapshot_interval": max(1, int(snapshot_interval)),
            "profile_enabled": bool(profile),
            "status": "ready",
            "current_step": 0,
            "total_steps": 0,
            "seed": seed,
            "created_at": now,
            "updated_at": now,
        }
        prof = profiler.StepProfiler(enabled=meta["profile_enabled"])
        storage.create_run_dir(run_id)
        storage.save_step(run_id, 0, engine.snapshot())
        storage.save_series(run_id, [{"step": 0, **engine.stats()}])
        storage.save_events(run_id, [])
        storage.save_profile(run_id, prof.to_dict())
        storage.save_run_meta(meta)
        with self._lock:
            self._engines[run_id] = engine
            self._profilers[run_id] = prof
        return meta

    # ------------------------------------------------------------------ #
    # Stepping
    # ------------------------------------------------------------------ #
    def _apply_due(self, run_id: str, engine: Engine,
                   meta: Dict[str, Any], step: int) -> None:
        """Apply any scheduled intervention whose ``at_step`` has been reached."""
        events = storage.load_events(run_id)
        changed = False
        for itv in meta["interventions"]:
            if itv.get("applied"):
                continue
            if int(itv.get("at_step", 0)) <= step:
                result = engine.apply_intervention(itv)
                itv["applied"] = True
                events.append({"step": step, "type": itv["type"],
                               "params": itv.get("params", {}),
                               "scheduled": True, "result": result})
                changed = True
        if changed:
            storage.save_events(run_id, events)
            storage.save_run_meta(meta)

    def _engine_step_timed(self, engine: Engine,
                           prof: profiler.StepProfiler) -> None:
        """Run one ``engine.step()`` and merge its internal phase timings."""
        engine.profiling = prof.enabled
        t0 = time.perf_counter()
        engine.step()
        engine_ms = (time.perf_counter() - t0) * 1000.0
        phases, blocks = engine.pop_phase_times()
        prof.set_engine(engine_ms, phases, blocks)

    def step(self, run_id: str, n: int = 1) -> Dict[str, Any]:
        """Advance ``n`` steps and return the current snapshot + stats."""
        with self._lock_for(run_id):
            engine, meta = self._require(run_id)
            if engine is None:
                raise RuntimeError("该运行未载入内存（服务器重启后不可续跑），请重开新运行")
            if meta["status"] in ("finished", "stopped"):
                meta["status"] = "ready"
            prof = self._profiler_for(run_id, meta)
            for _ in range(int(n)):
                prof.begin_step()
                with prof.phase("intervention"):
                    self._apply_due(run_id, engine, meta, engine.step_count)
                self._engine_step_timed(engine, prof)
                meta["current_step"] = engine.step_count
                meta["updated_at"] = util.now_iso()
                with prof.phase("stats"):
                    st = engine.stats()
                with prof.phase("persist"):
                    storage.append_series(
                        run_id, {"step": engine.step_count, **st})
                if engine.step_count % meta["snapshot_interval"] == 0:
                    with prof.phase("snapshot"):
                        snap = engine.snapshot()
                    with prof.phase("persist"):
                        storage.save_step(run_id, engine.step_count, snap)
                prof.end_step(engine.step_count, engine.population())
            storage.save_run_meta(meta)
            storage.save_profile(run_id, prof.to_dict())
            return {"step": engine.step_count, "stats": engine.stats(),
                    "snapshot": engine.snapshot()}

    def run_batch(self, run_id: str, steps: int,
                  snapshot_interval: Optional[int] = None,
                  keep_engine: bool = True) -> Dict[str, Any]:
        """Run ``steps`` steps to completion, returning final stats.

        Series rows are accumulated in memory and flushed periodically (and at
        the end) so the per-step write cost stays O(1) amortised even for very
        long runs.  Profile rows flush on the same cadence.
        """
        with self._lock_for(run_id):
            engine, meta = self._require(run_id)
            if engine is None:
                raise RuntimeError("该运行未载入内存，请重开新运行")
            if snapshot_interval is not None:
                meta["snapshot_interval"] = max(1, int(snapshot_interval))
            meta["status"] = "running"
            storage.save_run_meta(meta)

            series = storage.load_series(run_id)
            prof = self._profiler_for(run_id, meta)
            self._abort.discard(run_id)
            for _ in range(int(steps)):
                if run_id in self._abort:
                    break
                prof.begin_step()
                with prof.phase("intervention"):
                    self._apply_due(run_id, engine, meta, engine.step_count)
                self._engine_step_timed(engine, prof)
                meta["current_step"] = engine.step_count
                with prof.phase("stats"):
                    st = engine.stats()
                series.append({"step": engine.step_count, **st})
                if engine.step_count % meta["snapshot_interval"] == 0:
                    with prof.phase("snapshot"):
                        snap = engine.snapshot()
                    with prof.phase("persist"):
                        storage.save_step(run_id, engine.step_count, snap)
                if engine.step_count % 50 == 0:
                    with prof.phase("persist"):
                        storage.save_series(run_id, series)
                        storage.save_profile(run_id, prof.to_dict())
                        meta["updated_at"] = util.now_iso()
                        storage.save_run_meta(meta)
                prof.end_step(engine.step_count, engine.population())

            meta["status"] = "stopped" if run_id in self._abort else "finished"
            meta["updated_at"] = util.now_iso()
            storage.save_series(run_id, series)
            storage.save_profile(run_id, prof.to_dict())
            storage.save_run_meta(meta)
            self._abort.discard(run_id)

            final = engine.snapshot()
            if not keep_engine:
                with self._lock:
                    self._engines.pop(run_id, None)
            return {"step": engine.step_count, "stats": engine.stats(),
                    "snapshot": final}

    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    def pause(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            meta["status"] = "paused"
            meta["updated_at"] = util.now_iso()
            storage.save_run_meta(meta)
            return meta

    def resume(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            meta["status"] = "ready"
            meta["updated_at"] = util.now_iso()
            storage.save_run_meta(meta)
            return meta

    def stop(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            with self._lock:
                self._abort.add(run_id)
            meta["status"] = "stopped"
            meta["updated_at"] = util.now_iso()
            storage.save_run_meta(meta)
            return meta

    def reset(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            seed = meta.get("seed", 0)
            engine = make_engine(meta["domain"], meta["model"],
                                 config=meta["config"], seed=seed)
            prof = profiler.StepProfiler(
                enabled=bool(meta.get("profile_enabled", True)))
            with self._lock:
                self._engines[run_id] = engine
                self._profilers[run_id] = prof
            for itv in meta["interventions"]:
                itv["applied"] = False
            meta["current_step"] = 0
            meta["status"] = "ready"
            meta["updated_at"] = util.now_iso()
            storage.save_step(run_id, 0, engine.snapshot())
            storage.save_series(run_id, [{"step": 0, **engine.stats()}])
            storage.save_events(run_id, [])
            storage.save_profile(run_id, prof.to_dict())
            storage.save_run_meta(meta)
            return meta

    def delete_run(self, run_id: str) -> bool:
        with self._lock_for(run_id):
            with self._lock:
                self._engines.pop(run_id, None)
                self._profilers.pop(run_id, None)
                self._locks.pop(run_id, None)
                self._abort.discard(run_id)
            return storage.delete_run(run_id)

    # ------------------------------------------------------------------ #
    # Interventions
    # ------------------------------------------------------------------ #
    def apply_intervention(self, run_id: str,
                           itv: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock_for(run_id):
            engine, meta = self._require(run_id)
            if engine is None:
                raise RuntimeError("该运行未载入内存，请重开新运行")
            result = engine.apply_intervention(itv)
            events = storage.load_events(run_id)
            events.append({"step": engine.step_count, "type": itv["type"],
                           "params": itv.get("params", {}),
                           "scheduled": False, "result": result})
            storage.save_events(run_id, events)
            meta["updated_at"] = util.now_iso()
            storage.save_run_meta(meta)
            return result

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def status(self, run_id: str) -> Dict[str, Any]:
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            out = dict(meta)
            engine = self._engine(run_id)
            if engine is not None:
                out["stats"] = engine.stats()
            return out

    def get_snapshot(self, run_id: str, step: Optional[int] = None) -> Dict[str, Any]:
        with self._lock_for(run_id):
            engine, meta = self._require(run_id)
            if step is None:
                step = meta["current_step"]
            if engine is not None and int(step) == engine.step_count:
                return engine.snapshot()
            snap = storage.load_step(run_id, int(step))
            if snap is None:
                raise KeyError(f"snapshot not found: step {step}")
            return snap

    def get_series(self, run_id: str) -> List[Dict[str, Any]]:
        with self._lock_for(run_id):
            self._require(run_id)
            return storage.load_series(run_id)

    def get_events(self, run_id: str) -> List[Dict[str, Any]]:
        with self._lock_for(run_id):
            self._require(run_id)
            return storage.load_events(run_id)

    def get_individuals(self, run_id: str, step: Optional[int] = None) -> List[Dict[str, Any]]:
        return self.get_snapshot(run_id, step).get("individuals", [])

    # ------------------------------------------------------------------ #
    # Profiling
    # ------------------------------------------------------------------ #
    def get_profile(self, run_id: str, frm: Optional[int] = None,
                    to: Optional[int] = None, buckets: int = 60,
                    scale_bins: int = 12) -> Dict[str, Any]:
        """Analyse the run's per-step timing rows over a step range.

        Reads the live in-memory profiler when the run is loaded (its rows
        may be newer than the last flush), otherwise the persisted file.
        Does not take the run lock so analysis never blocks a running batch.
        """
        meta = storage.load_run_meta(run_id)
        if meta is None:
            raise KeyError(f"run not found: {run_id}")
        with self._lock:
            prof = self._profilers.get(run_id)
        profile = prof.to_dict() if prof is not None \
            else (storage.load_profile(run_id) or {})
        return profiler.analyse(profile, meta, frm, to, buckets, scale_bins)

    def set_profiling(self, run_id: str, enabled: bool) -> Dict[str, Any]:
        """Toggle per-phase profiling; later steps record accordingly."""
        with self._lock_for(run_id):
            _, meta = self._require(run_id)
            meta["profile_enabled"] = bool(enabled)
            meta["updated_at"] = util.now_iso()
            storage.save_run_meta(meta)
            prof = self._profiler_for(run_id, meta)
            prof.enabled = bool(enabled)
            storage.save_profile(run_id, prof.to_dict())
            return {"run_id": run_id, "profile_enabled": prof.enabled,
                    "calibration_ns_per_block": round(prof.calibration_ns, 1)}

    def run_scale_probe(self, run_id: str,
                        factors: Optional[List[float]] = None,
                        steps: int = 30) -> Dict[str, Any]:
        """Measure per-step compute cost at several population scales."""
        meta = storage.load_run_meta(run_id)
        if meta is None:
            raise KeyError(f"run not found: {run_id}")
        return profiler.run_scale_probe(
            meta["domain"], meta["model"], meta["config"],
            meta.get("seed", 0), factors, steps)


# Global singleton used by the Flask app.
manager = RunManager()
