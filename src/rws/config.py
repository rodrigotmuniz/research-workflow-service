"""Application settings, loaded from environment variables prefixed with ``RWS_``."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration in one typed object.

    Values come from the environment (``RWS_DATABASE_URL=...``) so the same image runs
    locally, in CI and on ECS without code changes.
    """

    model_config = SettingsConfigDict(env_prefix="RWS_", extra="ignore")

    database_url: str = "sqlite:///./rws.db"
    log_level: str = "INFO"
    log_json: bool = True
    # Deployment marker. "production" refuses unsafe settings such as dev auth.
    env: Literal["development", "production"] = "development"

    # --- Auth -------------------------------------------------------------------
    # "jwks" (default): tokens come from an external OpenID Connect provider.
    # "dev": the API mints and trusts its own tokens. Local only; must be set explicitly.
    auth_mode: Literal["dev", "jwks"] = "jwks"
    auth_issuer: str = "http://localhost:8000/dev"
    auth_audience: str = "research-workflow-service"
    auth_jwks_url: str | None = None

    # --- Execution ----------------------------------------------------------------
    executor: Literal["local", "stepfunctions"] = "local"
    aws_region: str = "us-east-1"
    sfn_role_arn: str | None = None
    state_machine_prefix: str = "rws-flow"
    # Lambda ARNs for each task action (only needed with the Step Functions executor).
    task_arn_validate_dataset: str | None = None
    task_arn_compute_checksums: str | None = None
    task_arn_copy_objects: str | None = None
    task_arn_notify: str | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.auth_mode == "dev" and self.env == "production":
            raise ValueError("RWS_AUTH_MODE=dev is not allowed when RWS_ENV=production")
        if self.auth_mode == "jwks" and not self.auth_jwks_url:
            raise ValueError("RWS_AUTH_JWKS_URL is required when RWS_AUTH_MODE=jwks")
        if self.executor == "stepfunctions" and not self.sfn_role_arn:
            raise ValueError("RWS_SFN_ROLE_ARN is required when RWS_EXECUTOR=stepfunctions")
        return self

    def task_arns(self) -> dict[str, str]:
        """Map each flow action to the Lambda that implements it."""
        arns = {
            "validate_dataset": self.task_arn_validate_dataset,
            "compute_checksums": self.task_arn_compute_checksums,
            "copy_objects": self.task_arn_copy_objects,
            "notify": self.task_arn_notify,
        }
        return {action: arn for action, arn in arns.items() if arn}


@lru_cache
def get_settings() -> Settings:
    return Settings()
