"""Smoke tests for the simulation engines, storage and run lifecycle.

Run directly::

    python3 tests/smoke.py

Each check is independent and prints PASS / FAIL; the script exits non-zero on
the first failure so it can be wired into CI or a pre-commit hook.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import models, report, storage  # noqa: E402
from backend.engine import make_engine  # noqa: E402
from backend.run_manager import manager  # noqa: E402

_ENGINES = ["traffic/ca", "traffic/abm", "ecology/ca", "ecology/abm",
            "epidemic/ca", "epidemic/abm"]


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"PASS  {name}")
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)


def engines_step() -> None:
    for key in _ENGINES:
        d, m = key.split("/")
        eng = make_engine(d, m, seed=42)
        for _ in range(5):
            eng.step()
        assert eng.step_count == 5, key
        assert len(eng.individuals()) > 0, f"{key} has no individuals"
        stats = eng.stats()
        assert stats, f"{key} produced empty stats"
        snap = eng.snapshot()
        assert snap["step"] == 5
        assert snap["stats"] == stats


def interventions_apply() -> None:
    eng = make_engine("epidemic", "abm", seed=1)
    before = eng.stats()["susceptible"]
    res = eng.apply_intervention({"type": "vaccinate", "params": {"fraction": 1.0}})
    assert res["applied"], res
    assert eng.stats()["susceptible"] == 0
    assert before > 0


def storage_atomic_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        # Redirect the module's DATA_DIR for this isolated check.
        old = storage.DATA_DIR
        storage.DATA_DIR = td
        try:
            storage.ensure_dirs()
            storage.save_scene({"id": "x", "name": "t", "updated_at": "z"})
            assert storage.load_scene("x")["name"] == "t"
            storage.save_step("r1", 0, {"step": 0, "v": 1})
            storage.save_step("r1", 7, {"step": 7, "v": 2})
            assert storage.load_step("r1", 7)["v"] == 2
            assert storage.list_steps("r1") == [0, 7]
        finally:
            storage.DATA_DIR = old


def run_lifecycle() -> None:
    scene = models.Scene(domain="epidemic", model="abm",
                         config={"n": 200, "width": 300, "height": 300,
                                 "initial_infected": 5})
    meta = manager.create_run(scene, seed=1, snapshot_interval=2)
    rid = meta["id"]
    try:
        r = manager.step(rid, 6)
        assert r["step"] == 6
        series = manager.get_series(rid)
        assert series[0]["step"] == 0 and series[-1]["step"] == 6
        assert len(manager.get_individuals(rid, 6)) == 200
        # snapshot_interval=2 -> full snapshots persisted at 0,2,4,6
        steps = storage.list_steps(rid)
        assert steps == [0, 2, 4, 6], steps
        rpt = report.generate_report(rid)
        assert rpt["steps"] == 7
    finally:
        manager.delete_run(rid)


def profiling_lifecycle() -> None:
    scene = models.Scene(domain="epidemic", model="abm",
                         config={"n": 300, "width": 300, "height": 300,
                                 "initial_infected": 5})
    meta = manager.create_run(scene, seed=7, snapshot_interval=2)
    rid = meta["id"]
    try:
        manager.step(rid, 3)                       # interactive path, profiled
        manager.set_profiling(rid, False)
        manager.step(rid, 2)                       # totals only
        manager.set_profiling(rid, True)
        manager.run_batch(rid, 10, keep_engine=True)   # batch path, profiled

        prof = storage.load_profile(rid)
        rows = prof["rows"]
        assert len(rows) == 15, len(rows)
        assert all("phases" in r for r in rows[:3])
        assert all("phases" not in r for r in rows[3:5])
        assert all("phases" in r for r in rows[5:])
        phases = rows[-1]["phases"]
        for key in ("move", "infect", "stats"):
            assert key in phases, phases
        assert all(r["total_ms"] >= 0 for r in rows)
        assert all(r["n"] == 300 for r in rows)

        res = manager.get_profile(rid)
        assert not res["empty"]
        s = res["summary"]
        assert s["steps_in_range"] == 15 and s["profiled_steps"] == 13
        assert s["unprofiled_steps"] == 2
        assert s["per_phase"] and s["slowest_phase"]
        assert len(s["slowest_steps"]) == 5
        assert res["buckets"] and res["overhead"]["estimated_overhead_pct"] >= 0
        assert res["overhead"]["measured"] is not None  # has on/off segments
        shares = sum(p["share"] for p in s["per_phase"])
        assert 0.9 <= shares <= 1.1, shares

        # Step-range selection narrows the summary to profiled steps only.
        res2 = manager.get_profile(rid, frm=6, to=15)
        assert res2["summary"]["steps_in_range"] == 10
        assert res2["summary"]["profiled_steps"] == 10

        # Scale probe grows the population and reports per-phase means.
        probe = manager.run_scale_probe(rid, factors=[1, 2], steps=5)
        assert len(probe["points"]) == 2
        assert probe["points"][1]["n"] > probe["points"][0]["n"]
        assert probe["points"][0]["mean_total_ms"] > 0
        assert probe["points"][0]["phases"]

        # Reset clears the profiling rows like it clears series/events.
        manager.reset(rid)
        assert storage.load_profile(rid)["rows"] == []
    finally:
        manager.delete_run(rid)


def main() -> None:
    check("six engines step and snapshot", engines_step)
    check("interventions apply", interventions_apply)
    check("atomic sharded storage", storage_atomic_roundtrip)
    check("run lifecycle + report", run_lifecycle)
    check("profiling lifecycle + analysis + probe", profiling_lifecycle)
    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
