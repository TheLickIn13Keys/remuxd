"""Lightweight timing helpers for pipeline profiling (all output at DEBUG level).

Run with ``--log-level DEBUG`` to see, per request, how long each stage of the
``id -> video`` pipeline takes and a one-line summary of where the time went.
"""
import time


class Timeline:
    """Accumulates named laps and logs each one plus a final summary. Create one
    per request; call ``lap(label)`` after each stage and ``done()`` at the end."""

    def __init__(self, log, name: str):
        self._log = log
        self.name = name
        self._t0 = time.perf_counter()
        self._last = self._t0
        self._laps = []

    def lap(self, label: str) -> float:
        now = time.perf_counter()
        ms = (now - self._last) * 1000
        self._last = now
        self._laps.append((label, ms))
        self._log.debug("[%s] %-14s +%6.0f ms", self.name, label, ms)
        return ms

    def done(self, note: str = "") -> float:
        total = (time.perf_counter() - self._t0) * 1000
        summary = "  ".join(f"{l}={ms:.0f}" for l, ms in self._laps)
        self._log.debug("[%s] TOTAL %.0f ms%s | %s", self.name, total,
                        f" [{note}]" if note else "", summary)
        return total
