"""
progress.py

Minimal stdlib-only progress indicator. Used across the pipeline scripts.

Design notes:
  - Output goes to stderr so redirecting stdout to a file still shows
    progress in the terminal.
  - In a TTY, we use carriage return + ANSI clear-line to update in place.
  - In a non-TTY (logs, CI), we print a one-line update every 10% and at
    completion, so the log stays readable.
  - No external dependency. The project intentionally stays stdlib-only
    outside of the API clients (anthropic, deepl).

Typical use:

    with Progress(len(rows), label="enriching") as prog:
        for row in rows:
            prog.update(detail=row.key)
            do_work(row)
"""

from __future__ import annotations

import sys
import time
from typing import TextIO


_BAR_WIDTH = 24
_CLEAR_LINE = "\r\x1b[2K"


def _fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return "<1s"
    if seconds < 60:
        return f"{int(seconds)}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


class Progress:
    """Lightweight progress indicator.

    Parameters
    ----------
    total : int
        Total number of units of work. If zero, renders "done" immediately
        on close.
    label : str
        Short description of what's being done ("enriching", "syncing", ...).
    stream : TextIO
        Where to write. Defaults to stderr.
    detail_width : int
        Max characters of per-step detail string to show.
    """

    def __init__(
        self,
        total: int,
        label: str = "",
        *,
        stream: TextIO | None = None,
        detail_width: int = 48,
    ):
        self.total = max(0, total)
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self.detail_width = detail_width
        self.current = 0
        self.tty = self.stream.isatty()
        self.start = time.monotonic()
        self._last_non_tty_pct = -1
        self._closed = False

    # ------------------------------------------------------------------ API

    def update(self, n: int = 1, *, detail: str = "") -> None:
        """Advance progress by `n` units and re-render."""
        self.current += n
        self._render(detail)

    def set_detail(self, detail: str) -> None:
        """Re-render with a new detail string but without advancing."""
        self._render(detail)

    def close(self, *, final_message: str | None = None) -> None:
        """Finish the progress display. In a TTY the progress line is
        cleared; a one-line summary is then printed. Idempotent — calling
        close() a second time (e.g. because the caller closes manually and
        then exits a `with` block) is a no-op."""
        if self._closed:
            return
        self._closed = True
        elapsed = time.monotonic() - self.start
        if self.tty:
            self.stream.write(_CLEAR_LINE)
        if final_message is None:
            final_message = (
                f"{self.label}: {self.current}/{self.total} "
                f"in {_fmt_duration(elapsed)}"
            ) if self.label else (
                f"done: {self.current}/{self.total} in {_fmt_duration(elapsed)}"
            )
        self.stream.write(final_message + "\n")
        self.stream.flush()

    # -------------------------------------------------------------- context

    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -------------------------------------------------------------- render

    def _render(self, detail: str) -> None:
        if self.total <= 0:
            return

        pct = min(100, int(self.current / self.total * 100))
        elapsed = time.monotonic() - self.start

        if self.current > 0 and self.current < self.total:
            rate = self.current / elapsed if elapsed > 0 else 0
            remaining = (self.total - self.current) / rate if rate > 0 else 0
            eta = _fmt_duration(remaining)
        elif self.current >= self.total:
            eta = "0s"
        else:
            eta = "?"

        if self.tty:
            filled = int(_BAR_WIDTH * self.current / self.total)
            bar = "█" * filled + "·" * (_BAR_WIDTH - filled)
            line = (
                f"{self.label}  [{bar}]  "
                f"{self.current}/{self.total}  {pct:>3d}%  "
                f"elapsed {_fmt_duration(elapsed)}  eta {eta}"
            )
            if detail:
                clipped = detail if len(detail) <= self.detail_width else (
                    detail[: self.detail_width - 1] + "…"
                )
                line += f"  {clipped}"
            self.stream.write(f"{_CLEAR_LINE}{line}")
            self.stream.flush()
        else:
            # Non-TTY: emit a single line every 10 % so logs stay clean.
            tier = pct // 10
            if tier != self._last_non_tty_pct:
                self._last_non_tty_pct = tier
                self.stream.write(
                    f"{self.label}: {self.current}/{self.total} "
                    f"({pct}%, eta {eta})\n"
                )
                self.stream.flush()
