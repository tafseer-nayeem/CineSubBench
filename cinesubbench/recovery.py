"""Structural recovery for saved model responses.

Recovery is structural only: JSON fences, trailing commas, missing container
closures, and known key/container aliases may be repaired. Predicted labels,
text, evidence indices, and scores are never created or changed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import validate


RELEASE_ROOT = Path(__file__).resolve().parents[1]
if str(RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(RELEASE_ROOT))

from scripts.utils import schema_repair  # noqa: E402


def recover_response(
    raw_text: str,
    task: str,
    sample_id: int,
    schema_wrapper: dict[str, Any],
) -> dict[str, Any]:
    parsed = schema_repair.parse_jsonish(raw_text)
    if task in {"narrative", "cultural"}:
        parsed = schema_repair.validate_task_output(task, parsed, sample_id)
    else:
        if not isinstance(parsed, dict):
            raise ValueError("Recovered response must be a JSON object")
        parsed["id"] = sample_id
    schema = schema_wrapper.get("schema", schema_wrapper)
    validate(instance=parsed, schema=schema)
    return parsed


def raw_text_from_payload(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("rawResponse", "raw_response", "response", "content", "text"):
            if isinstance(value.get(key), str):
                return value[key]
    return json.dumps(value, ensure_ascii=False)
