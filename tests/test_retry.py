import pytest

from rws.flows import RetryPolicy
from rws.tasks.errors import TaskError, TransientError
from rws.tasks.retry import backoff_delays, call_with_retry


def test_backoff_is_exponential_without_jitter() -> None:
    policy = RetryPolicy(max_attempts=4, interval_seconds=1, backoff_rate=2.0)
    assert backoff_delays(policy, jitter=False) == [1, 2, 4]


def test_jitter_stays_within_bounds() -> None:
    policy = RetryPolicy(max_attempts=4, interval_seconds=2, backoff_rate=3.0)
    for delay, cap in zip(backoff_delays(policy), [2, 6, 18], strict=True):
        assert 0 <= delay <= cap


def test_retries_transient_errors_then_succeeds() -> None:
    calls: list[int] = []
    sleeps: list[float] = []
    retries: list[int] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("try again")
        return "ok"

    result = call_with_retry(
        flaky,
        RetryPolicy(max_attempts=3),
        sleep=sleeps.append,
        on_retry=lambda attempt, _: retries.append(attempt),
    )
    assert result == "ok"
    assert len(calls) == 3
    assert len(sleeps) == 2
    assert retries == [1, 2]


def test_gives_up_after_max_attempts() -> None:
    def always_fails() -> None:
        raise TransientError("still down")

    with pytest.raises(TransientError):
        call_with_retry(always_fails, RetryPolicy(max_attempts=2), sleep=lambda _: None)


def test_permanent_errors_are_not_retried() -> None:
    calls = []

    def bad_input() -> None:
        calls.append(1)
        raise TaskError("bad input")

    with pytest.raises(TaskError):
        call_with_retry(bad_input, RetryPolicy(max_attempts=5), sleep=lambda _: None)
    assert len(calls) == 1
