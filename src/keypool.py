"""Rotate across several API keys, resting any key that gets rate-limited."""
from __future__ import annotations

import hashlib
import itertools
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class AllKeysExhausted(RuntimeError):
    pass


@dataclass
class KeyState:
    key: str
    label: str
    rest_until: float = 0.0
    calls: int = 0
    errors: int = 0
    dead: bool = False

    @property
    def available(self) -> bool:
        return not self.dead and time.time() >= self.rest_until


@dataclass
class KeyPool:
    """Round-robins keys. Rate-limited keys rest; hard-failed keys drop out.

    A pool with one key still works — it just waits instead of rotating.
    """

    name: str
    keys: list[str]
    states: list[KeyState] = field(init=False)
    _cycle: itertools.cycle = field(init=False, repr=False)

    def __post_init__(self):
        self.states = [
            KeyState(key=k, label=self._label(k, i)) for i, k in enumerate(self.keys)
        ]
        self._cycle = itertools.cycle(range(len(self.states))) if self.states else None

    @staticmethod
    def _label(key: str, idx: int) -> str:
        """Short, stable, non-reversible handle so logs never leak a key."""
        digest = hashlib.sha256(key.encode()).hexdigest()[:6]
        return f"{idx + 1}·{digest}"

    def acquire(self, wait: bool = True, timeout: float = 300.0) -> KeyState:
        if not self.states:
            raise AllKeysExhausted(f"No {self.name} keys configured.")
        deadline = time.time() + timeout
        while True:
            for _ in range(len(self.states)):
                state = self.states[next(self._cycle)]
                if state.available:
                    state.calls += 1
                    return state
            live = [s for s in self.states if not s.dead]
            if not live:
                raise AllKeysExhausted(f"Every {self.name} key failed permanently.")
            if not wait or time.time() > deadline:
                raise AllKeysExhausted(f"All {self.name} keys are resting.")
            nap = min(s.rest_until for s in live) - time.time()
            nap = max(1.0, min(nap, 30.0))
            log.info("%s: all keys resting, waiting %.0fs", self.name, nap)
            time.sleep(nap)

    def penalize(self, state: KeyState, seconds: float = 60.0) -> None:
        """Rate-limited. Rest this key and move on to the next."""
        state.rest_until = time.time() + seconds
        state.errors += 1
        log.warning("%s key %s rate-limited, resting %.0fs", self.name, state.label, seconds)

    def retire(self, state: KeyState, reason: str = "") -> None:
        """Auth failure or exhausted credits. Stop using this key this run."""
        state.dead = True
        state.errors += 1
        log.error("%s key %s retired: %s", self.name, state.label, reason)

    def snapshot(self) -> list[dict]:
        """Safe to render in the UI — labels only, never key material."""
        now = time.time()
        return [
            {
                "label": s.label,
                "calls": s.calls,
                "errors": s.errors,
                "status": "retired" if s.dead
                else "resting" if now < s.rest_until
                else "ready",
            }
            for s in self.states
        ]
