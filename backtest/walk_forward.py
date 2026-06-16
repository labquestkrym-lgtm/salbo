"""Walk-forward window generation (R24).

Splits a sequence of ``n`` periods into rolling (train, validation, out-of-sample)
windows so parameters are never tuned and judged on the same data. Index-based;
the caller maps indices to its own bar/date series.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train: range
    validation: range
    out_of_sample: range


def walk_forward_windows(
    n: int, *, train: int, validation: int, out_of_sample: int, step: int | None = None
) -> list[WalkForwardWindow]:
    """Rolling windows over ``range(n)``.

    Each window is [train | validation | out_of_sample] contiguous slices; the
    whole block then advances by ``step`` (default = out_of_sample, i.e.
    non-overlapping OOS).
    """
    if min(train, validation, out_of_sample) <= 0:
        raise ValueError("train/validation/out_of_sample must be positive")
    block = train + validation + out_of_sample
    advance = step if step is not None else out_of_sample
    if advance <= 0:
        raise ValueError("step must be positive")

    def _gen() -> Iterator[WalkForwardWindow]:
        start = 0
        while start + block <= n:
            t0 = start
            v0 = t0 + train
            o0 = v0 + validation
            yield WalkForwardWindow(
                train=range(t0, v0),
                validation=range(v0, o0),
                out_of_sample=range(o0, o0 + out_of_sample),
            )
            start += advance

    return list(_gen())
