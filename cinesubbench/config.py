"""Configuration loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .decoding import DECODING_POLICY


@dataclass(frozen=True)
class ModelConfig:
    name: str
    provider: str
    model_id: str
    api_key_env: str
    mode: str = "direct"
    timeout_seconds: float = 300.0
    max_retries: int = 3
    workers: int = 1
    batch_size: int = 100

    @classmethod
    def from_file(cls, path: Path) -> "ModelConfig":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Model configuration must be a YAML object: {path}")
        credentials = raw.get("credentials", {})
        execution = raw.get("execution", {})
        provider = str(raw.get("provider", "")).lower()
        if provider not in {"openai", "gemini", "anthropic"}:
            raise ValueError("provider must be one of: openai, gemini, anthropic")
        mode = str(execution.get("mode", "direct")).lower()
        if mode not in {"direct", "batch"}:
            raise ValueError("execution.mode must be 'direct' or 'batch'")
        config = cls(
            name=str(raw.get("name", "")).strip(),
            provider=provider,
            model_id=str(raw.get("model_id", "")).strip(),
            api_key_env=str(credentials.get("env_var", "")).strip(),
            mode=mode,
            timeout_seconds=float(execution.get("timeout_seconds", 300.0)),
            max_retries=int(execution.get("max_retries", 3)),
            workers=int(execution.get("workers", 1)),
            batch_size=int(execution.get("batch_size", 100)),
        )
        if not config.name or not config.model_id or not config.api_key_env:
            raise ValueError("name, model_id, and credentials.env_var are required")
        if config.workers < 1 or config.batch_size < 1:
            raise ValueError("execution.workers and execution.batch_size must be positive")
        if config.timeout_seconds <= 0 or config.max_retries < 0:
            raise ValueError("execution.timeout_seconds must be positive and max_retries cannot be negative")
        return config

    def api_key(self) -> str:
        value = os.getenv(self.api_key_env)
        if not value:
            raise RuntimeError(
                f"Missing API key. Set the {self.api_key_env} environment variable; "
                "do not place credentials in the YAML configuration."
            )
        return value

    def public_dict(self) -> dict[str, Any]:
        """Return provenance safe to save without credentials."""
        return {
            "name": self.name,
            "provider": self.provider,
            "modelId": self.model_id,
            "apiKeyEnvironmentVariable": self.api_key_env,
            "executionMode": self.mode,
            "decodingPolicy": DECODING_POLICY,
        }
