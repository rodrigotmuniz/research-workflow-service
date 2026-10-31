"""Task error types. The class *name* matters: Step Functions matches retry rules on it."""


class TaskError(Exception):
    """A permanent failure: retrying will not help (bad input, missing bucket...)."""


class DatasetValidationError(TaskError):
    """The dataset does not meet the step's validation rules."""


class TransientError(Exception):
    """A temporary failure (throttling, network, 5xx): safe to retry with backoff."""
