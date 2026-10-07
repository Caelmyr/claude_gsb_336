"""Abstract simulation engine interface.

Every engine (cellular automaton or agent-based model) implements the same
narrow contract so the run manager, API and storage can treat them uniformly:

* ``_init()``  — build the initial individual population / lattice from config.
* ``step()``   — advance the simulation by exactly one time step.
* ``stats()``  — aggregate statistics for the current state.
* ``snapshot()`` — the full serialisable state (stats + individuals + substrate).

Individuals are always plain dicts carrying at least ``id``, ``type``, ``x``,
``y`` and a ``state`` key.  For cellular automata ``x``/``y`` are integer cell
coordinates; for agent-based models they are continuous positions in the world
``[0, width) x [0, height)``.  The ``palette()`` maps state values to display
colors so the frontend can render any domain without domain-specific logic.
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Any, Dict, List, Optional


class Engine(ABC):
    domain: str = ""
    model: str = ""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 seed: Optional[int] = None) -> None:
        # Merge the concrete class defaults with the caller-supplied config.
        self.config: Dict[str, Any] = {**self.defaults(), **(config or {})}
        self.rng: random.Random = random.Random(seed)
        self.step_count: int = 0
        self._individuals: List[Dict[str, Any]] = []
        # Profiling hooks, driven by the run manager.  Disabled profiling
        # costs a single attribute check per ``_timed`` block.
        self.profiling: bool = False
        self._phase_times: Dict[str, float] = {}
        self._phase_blocks: int = 0
        self._init()

    # ------------------------------------------------------------------ #
    # Subclass contract
    # ------------------------------------------------------------------ #
    @classmethod
    def defaults(cls) -> Dict[str, Any]:
        """Fallback defaults; the catalog supplies richer ones at call time."""
        return {}

    @abstractmethod
    def _init(self) -> None:
        """Initialise state from ``self.config``."""

    @abstractmethod
    def step(self) -> None:
        """Advance the simulation by one time step."""

    @abstractmethod
    def stats(self) -> Dict[str, Any]:
        """Return aggregate statistics for the current state."""

    # ------------------------------------------------------------------ #
    # State accessors
    # ------------------------------------------------------------------ #
    def individuals(self) -> List[Dict[str, Any]]:
        return self._individuals

    def bounds(self) -> Dict[str, float]:
        """World extent ``{"width", "height"}`` used to scale the canvas."""
        return {
            "width": float(self.config.get("width", 100)),
            "height": float(self.config.get("height", 100)),
        }

    def palette(self) -> Dict[str, Dict[str, str]]:
        """Map individual ``state`` values to ``{"label", "color"}``."""
        return {}

    def substrate(self) -> Optional[List[List[int]]]:
        """Optional 2D cell substrate (e.g. grass) drawn underneath individuals.

        ``None`` for models that need no background lattice.
        """
        return None

    def apply_intervention(self, itv: Dict[str, Any]) -> Dict[str, Any]:
        """Apply an intervention; subclasses override for the types they know.

        Returns a result dict ``{"applied": bool, "reason": str}``.
        """
        return {"applied": False, "reason": f"{self.domain}/{self.model} 不支持该干预"}

    # ------------------------------------------------------------------ #
    # Profiling
    # ------------------------------------------------------------------ #
    @contextmanager
    def _timed(self, phase: str):
        """Accumulate wall time (ms) for a named phase of ``step()``.

        Engines wrap the major computation blocks of their ``step()`` in
        these markers; the run manager collects them after each step via
        :meth:`pop_phase_times`.  When ``self.profiling`` is off the block
        runs untimed and costs only the attribute check.
        """
        if not self.profiling:
            yield
            return
        t = time.perf_counter()
        try:
            yield
        finally:
            self._phase_times[phase] = self._phase_times.get(phase, 0.0) + \
                (time.perf_counter() - t) * 1000.0
            self._phase_blocks += 1

    def pop_phase_times(self) -> tuple:
        """Return and reset accumulated ``(phase -> ms, block count)``."""
        out, self._phase_times = self._phase_times, {}
        blocks, self._phase_blocks = self._phase_blocks, 0
        return out, blocks

    def population(self) -> int:
        """Current number of simulated individuals (cheap, no materialisation)."""
        return len(self._individuals)

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Dict[str, Any]:
        """Full serialisable state at the current step (sharded to disk)."""
        return {
            "step": self.step_count,
            "bounds": self.bounds(),
            "palette": self.palette(),
            "stats": self.stats(),
            "substrate": self.substrate(),
            "individuals": self.individuals(),
        }

    # ------------------------------------------------------------------ #
    # Shared helpers for subclass implementations
    # ------------------------------------------------------------------ #
    def _roll(self) -> float:
        return self.rng.random()

    def _coin(self, p: float) -> bool:
        return self.rng.random() < p
