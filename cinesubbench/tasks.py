"""Task prompt and schema preparation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .data import LANGUAGE_NAMES, subtitle_text, subtitle_track


@dataclass(frozen=True)
class PreparedRequest:
    custom_id: str
    sample_id: int
    system: str
    user: str
    schema: dict[str, Any]
    chunk_index: int = 0
    total_chunks: int = 1


LANGUAGE_CONTENT_CHUNK_SIZE = 500


TASK_FILES = {
    "narrative": (
        "prompts/narrative_generation/narrative_generation_prompt.md",
        "schemas/narrative_generation/narrative_generation_output.schema.json",
    ),
    "cultural": (
        "prompts/cultural_assessment/cultural_prediction_prompt.md",
        "schemas/cultural_assessment/cultural_prediction_output.schema.json",
    ),
    "language_content": (
        "prompts/cultural_assessment/language_content_assessment_prompt.md",
        "schemas/cultural_assessment/language_content_assessment_output.schema.json",
    ),
}


def task_assets(release_root: Path, task: str) -> tuple[str, dict[str, Any]]:
    if task not in TASK_FILES:
        raise ValueError(f"Unknown task: {task}")
    prompt_rel, schema_rel = TASK_FILES[task]
    return (
        (release_root / prompt_rel).read_text(encoding="utf-8"),
        json.loads((release_root / schema_rel).read_text(encoding="utf-8")),
    )


def prepare_request(
    release_root: Path,
    task: str,
    sample: dict[str, Any],
    language_code: str,
    entries: list[dict[str, Any]] | None = None,
    chunk_index: int = 0,
    total_chunks: int = 1,
) -> PreparedRequest:
    if task == "language_content" and language_code != "en":
        raise ValueError("Language-content assessment is defined for English subtitles only")
    system, schema = task_assets(release_root, task)
    track = subtitle_track(sample, language_code)
    label = LANGUAGE_NAMES[language_code]
    verb = {
        "narrative": "Generate the CineSubBench narrative outputs in English",
        "cultural": "Predict the CineSubBench cultural labels in English",
        "language_content": "Extract the CineSubBench language-content evidence",
    }[task]
    selected_entries = track.get("entries", []) if entries is None else entries
    payload = {
        "id": sample["id"],
        "inputLanguage": label,
        "subtitles": subtitle_text(selected_entries),
    }
    if task == "language_content":
        payload.update({
            "chunkIndex": chunk_index,
            "totalChunks": total_chunks,
            "chunkStartIndex": selected_entries[0].get("index") if selected_entries else None,
            "chunkEndIndex": selected_entries[-1].get("index") if selected_entries else None,
        })
        verb += " for this non-overlapping subtitle chunk only"
    return PreparedRequest(
        custom_id=(
            f"{task}-{language_code}-{sample['id']}-chunk-{chunk_index}"
            if task == "language_content"
            else f"{task}-{language_code}-{sample['id']}"
        ),
        sample_id=int(sample["id"]),
        system=system,
        user=f"These are subtitles in {label}. {verb}:\n" + json.dumps(payload, ensure_ascii=False),
        schema=schema,
        chunk_index=chunk_index,
        total_chunks=total_chunks,
    )


def prepare_requests(
    release_root: Path,
    task: str,
    sample: dict[str, Any],
    language_code: str,
) -> list[PreparedRequest]:
    if task != "language_content":
        return [prepare_request(release_root, task, sample, language_code)]
    entries = subtitle_track(sample, language_code).get("entries", [])
    chunks = [
        entries[start : start + LANGUAGE_CONTENT_CHUNK_SIZE]
        for start in range(0, len(entries), LANGUAGE_CONTENT_CHUNK_SIZE)
    ] or [[]]
    return [
        prepare_request(
            release_root,
            task,
            sample,
            language_code,
            entries=chunk,
            chunk_index=index,
            total_chunks=len(chunks),
        )
        for index, chunk in enumerate(chunks)
    ]


def prepare_judge_request(
    release_root: Path,
    sample: dict[str, Any],
    prediction: dict[str, Any],
) -> PreparedRequest:
    prompt_path = release_root / "prompts/narrative_generation/narrative_judge_prompt.md"
    schema_path = release_root / "schemas/narrative_generation/narrative_judge_output.schema.json"
    payload = {
        "id": sample["id"],
        "references": {
            "plot": sample["plot"],
            "synopsis": sample["synopsis"],
            "keyMessage": sample["keyMessage"],
        },
        "candidate": {
            "plot": prediction["plot"],
            "synopsis": prediction["synopsis"],
            "keyMessage": prediction["keyMessage"],
        },
    }
    return PreparedRequest(
        custom_id=f"narrative-judge-{sample['id']}",
        sample_id=int(sample["id"]),
        system=prompt_path.read_text(encoding="utf-8"),
        user=json.dumps(payload, ensure_ascii=False),
        schema=json.loads(schema_path.read_text(encoding="utf-8")),
    )
