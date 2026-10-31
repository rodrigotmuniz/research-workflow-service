"""Task handlers: the units of work a flow step runs.

This package only depends on the standard library and boto3, so it can be deployed
as-is to AWS Lambda (boto3 ships with the Python runtime). The same functions are
called in-process by the local executor.
"""
