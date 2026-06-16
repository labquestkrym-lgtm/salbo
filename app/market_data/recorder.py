"""Record quotes to disk and replay them (R12).

Quotes are stored as JSON Lines (one ``Quote`` per line) so a recorded session
can be replayed deterministically for backtests and debugging. Replay yields the
same :class:`Quote` objects an adapter stream would.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import TracebackType

from app.models import Quote


class MarketDataRecorder:
    """Append-only JSONL recorder. Usable as a context manager."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")

    def record(self, quote: Quote) -> None:
        self._fh.write(quote.model_dump_json() + "\n")

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> MarketDataRecorder:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _iter_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                yield stripped


async def replay_quotes(path: str | Path) -> AsyncIterator[Quote]:
    """Async generator replaying a recorded JSONL quote file.

    File reads happen in a synchronous helper (local recorded data, not a hot
    network path), keeping the async surface clean for the ingest pipeline.
    """
    for line in _iter_lines(Path(path)):
        yield Quote.model_validate_json(line)
