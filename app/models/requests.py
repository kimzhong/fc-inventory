"""Pydantic request models for the /api/* routes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class CollectRequest(BaseModel):
    """Body for `POST /api/collect`."""

    model_config = ConfigDict(str_strip_whitespace=True)

    host: str = Field(
        ...,
        min_length=1,
        description="FusionCompute VRM host or IP. The 'https://' prefix is auto-stripped.",
        examples=["10.0.0.10"],
    )
    port: int = Field(
        default=7443,
        ge=1,
        le=65535,
        description="Primary port; the auto-detect login also tries 7443 and 8443.",
    )
    username: str = Field(
        ...,
        min_length=1,
        description="FusionCompute username.",
        examples=["readonly"],
    )
    password: SecretStr = Field(
        ...,
        description=(
            "FusionCompute password. Wrapped in SecretStr so it never appears in "
            "logs, repr(), or Pydantic's model_dump(); the route calls "
            "`.get_secret_value()` only when constructing the FCClient."
        ),
    )
