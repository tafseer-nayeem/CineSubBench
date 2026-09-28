"""Benchmark-wide decoding policy shared by every provider."""

from __future__ import annotations

from typing import Any


DECODING_POLICY: dict[str, dict[str, Any]] = {
    "narrative": {"temperature": 0.0, "max_output_tokens": 1200},
    "cultural": {"temperature": 0.0, "max_output_tokens": 1400},
    "language_content": {"temperature": 0.0, "max_output_tokens": 2500},
    "narrative_judge": {"temperature": 0.0, "max_output_tokens": 4096},
}


def decoding_for(task: str) -> dict[str, Any]:
    try:
        return dict(DECODING_POLICY[task])
    except KeyError as exc:
        raise ValueError(f"No decoding policy for task: {task}") from exc

