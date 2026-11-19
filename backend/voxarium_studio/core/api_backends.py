"""Personal OpenAI-compatible connections; never part of project config (§10.6)."""
from __future__ import annotations

from urllib.parse import urlsplit
from uuid import UUID
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator


class ApiBackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: str
    name: str = Field(min_length=1, max_length=120)
    base_url: str = "https://api.openai.com/v1"
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    model: str = Field(min_length=1, max_length=200)
    models: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]] = Field(default_factory=list, max_length=200)
    api_mode: Literal["auto", "chat_completions", "responses"] = "auto"
    use_limits: bool = False
    tpm: int = Field(default=1_000_000, ge=1, strict=True)
    context_window: int | None = Field(default=None, ge=1024, le=100_000_000, strict=True)
    icon: str | None = Field(default=None, max_length=80)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if str(UUID(value)) != value:
            raise ValueError("Invalid API backend ID")
        return value

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Use an HTTP(S) API base URL without credentials, query or fragment")
        return value

    @field_validator("name", "model")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank")
        return value.strip()

    @field_validator("models")
    @classmethod
    def unique_models(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @property
    def model_ids(self) -> list[str]:
        return list(dict.fromkeys([self.model, *self.models]))

    @property
    def effective_api_mode(self) -> str:
        # §10.6: OpenAI's current reasoning models need Responses for tools.
        if self.api_mode == "auto":
            return "responses" if urlsplit(self.base_url).hostname == "api.openai.com" else "chat_completions"
        return self.api_mode

    @property
    def backend_id(self) -> str:
        return f"openai_api:{self.id}"

    def public(self) -> dict:
        return {**self.model_dump(mode="json", exclude={"api_key"}),
                "has_api_key": bool(self.api_key.get_secret_value())}


def connections() -> list[ApiBackendConfig]:
    from .application_config import load_application_config
    return load_application_config().api_backends
