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

from backend import models, profiler, report, storage  # noqa: E402
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


def profiling_records() -> None:
    scene = models.Scene(domain="epidemic", model="abm",
                         config={"n": 150, "width": 200, "height": 200,
                                 "initial_infected": 3})
    meta = manager.create_run(scene, seed=1, snapshot_interval=2)
    rid = meta["id"]
    try:
        manager.step(rid, 5)
        data = storage.load_profile(rid)
        assert data and len(data["records"]) == 5, "expected 5 profile records"
        rec = data["records"][0]
        assert rec["step"] == 1 and rec["n"] == 150, rec
        assert rec["wall_ms"] > 0
        stages = rec["stages"]
        for key in ("move", "infect", "stats", "io"):
            assert key in stages, f"missing stage {key}: {stages}"
        # stages are a breakdown of the wall time (allow timer slack)
        assert sum(stages.values()) <= rec["wall_ms"] * 1.5
        assert rec["overhead_ms"] >= 0
        assert data["calibration_ns_per_stage"] > 0

        rep = profiler.build_report(data, None, None)
        assert not rep["empty"]
        s = rep["summary"]
        assert s["steps"] == 5
        assert s["slowest_stage"] in stages
        share_sum = sum(v["share"] for v in s["stage_stats"].values())
        assert 0.3 < share_sum <= 1.1, share_sum
        assert len(s["slowest_steps"]) == 5
        assert s["slowest_steps"][0]["wall_ms"] >= s["slowest_steps"][-1]["wall_ms"]

        # windowed view: only steps 2..3 are summarised
        rep2 = profiler.build_report(data, 2, 3)
        assert rep2["range"] == {"from": 2, "to": 3}
        assert rep2["summary"]["steps"] == 2
        assert len(rep2["points"]) == 5, "points still cover the whole run"

        # batch runs keep profiling too
        manager.run_batch(rid, 5)
        data2 = storage.load_profile(rid)
        assert len(data2["records"]) == 10

        # reset clears the profile
        manager.reset(rid)
        assert storage.load_profile(rid)["records"] == []
    finally:
        manager.delete_run(rid)


def scale_probe_works() -> None:
    scene = models.Scene(domain="traffic", model="abm", config={"n": 30})
    meta = manager.create_run(scene, seed=1)
    rid = meta["id"]
    try:
        out = profiler.scale_probe(storage.load_run_meta(rid), [1, 2], 5)
        assert len(out["points"]) == 2
        assert out["points"][0]["n"] == 30 and out["points"][1]["n"] == 60
        assert all(p["mean_ms"] > 0 for p in out["points"])
        assert out["fit"] is not None
        # invalid factors are rejected
        try:
            profiler.scale_probe(storage.load_run_meta(rid), [100], 5)
            raise AssertionError("expected ValueError for factor 100")
        except ValueError:
            pass
    finally:
        manager.delete_run(rid)


def main() -> None:
    check("six engines step and snapshot", engines_step)
    check("interventions apply", interventions_apply)
    check("atomic sharded storage", storage_atomic_roundtrip)
    check("run lifecycle + report", run_lifecycle)
    check("profiling records per step/stage", profiling_records)
    check("scale probe benchmarks sizes", scale_probe_works)
    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
