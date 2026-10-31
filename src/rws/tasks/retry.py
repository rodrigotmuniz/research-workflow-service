"""Retry with exponential backoff and jitter, mirroring an ASL ``Retry`` block."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Protocol

from rws.tasks.errors import TransientError

log = logging.getLogger(__name__)


class RetrySettings(Protocol):
    @property
    def max_attempts(self) -> int: ...
    @property
    def interval_seconds(self) -> int: ...
    @property
    def backoff_rate(self) -> float: ...


def backoff_delays(policy: RetrySettings, jitter: bool = True) -> list[float]:
    """Delays before each retry: interval, interval*rate, interval*rate^2, ...

    "Full jitter" picks a random delay in ``[0, delay]`` so many failing clients
    do not retry in lockstep against an already struggling dependency.
    """
    delays = []
    for retry in range(policy.max_attempts - 1):
        delay = policy.interval_seconds * policy.backoff_rate**retry
        delays.append(random.uniform(0, delay) if jitter else delay)  # noqa: S311
    return delays


def call_with_retry[T](
    fn: Callable[[], T],
    policy: RetrySettings,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    """Call ``fn``; on :class:`TransientError` wait and try again, up to the policy limit.

    Any other exception is permanent and propagates immediately.
    """
    delays = backoff_delays(policy)
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except TransientError as exc:
            if attempt == policy.max_attempts:
                raise
            if on_retry:
                on_retry(attempt, exc)
            delay = delays[attempt - 1]
            log.warning("transient failure, retrying", extra={"attempt": attempt, "delay": delay})
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
