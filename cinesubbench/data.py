"""Dataset loading and subtitle selection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


LANGUAGE_NAMES = {
    "en": "English (en)",
    "ar": "Arabic (ar)",
    "id": "Indonesian (id)",
    "fa": "Persian (fa)",
    "ro": "Romanian (ro)",
    "vi": "Vietnamese (vi)",
}

DEFAULT_DATASET = "hf://tafseer-nayeem/CineSubBench?split=test"

REQUIRED_FIELDS = {
    "id", "title", "plot", "synopsis", "keyMessage", "genres",
    "ageSuitabilityRating", "motionPictureRatings", "subtitles",
}


def _validate_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ValueError(f"Dataset item {index} is not a JSON object")
        missing = sorted(REQUIRED_FIELDS - set(sample))
        if missing:
            raise ValueError(f"Dataset item {index} is missing required fields: {missing}")
        if not isinstance(sample["id"], int):
            raise ValueError(f"Dataset item {index} has a non-integer id")
        available_languages = {
            str(track.get("language", ""))
            for track in sample["subtitles"]
            if isinstance(track, dict)
        }
        missing_languages = sorted(set(LANGUAGE_NAMES.values()) - available_languages)
        if missing_languages:
            raise ValueError(
                f"Dataset item {index} is missing subtitle tracks: {missing_languages}"
            )
    ids = [sample["id"] for sample in samples]
    if len(ids) != len(set(ids)):
        raise ValueError("Dataset contains duplicate sample IDs")
    return samples


def load_dataset(path: Path | str) -> list[dict[str, Any]]:
    source = str(path)
    if source.startswith("hf://"):
        try:
            from datasets import load_dataset as hf_load_dataset
        except ImportError as exc:
            raise RuntimeError("Hugging Face loading requires the 'datasets' package") from exc
        location = source.removeprefix("hf://")
        repo_id, _, query = location.partition("?")
        options = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        split = options.get("split", "test")
        config = options.get("config")
        dataset = hf_load_dataset(repo_id, config, split=split) if config else hf_load_dataset(repo_id, split=split)
        samples = [dict(row) for row in dataset]
        return _validate_samples(samples)
    path = Path(source)
    payload = json.loads(path.read_text(encoding="utf-8"))
    samples = payload if isinstance(payload, list) else payload.get("samples")
    if not isinstance(samples, list):
        raise ValueError("Dataset must be a JSON array or an object with a samples array")
    return _validate_samples(samples)


def subtitle_track(sample: dict[str, Any], language_code: str) -> dict[str, Any]:
    expected = LANGUAGE_NAMES.get(language_code)
    if expected is None:
        raise ValueError(f"Unsupported language code: {language_code}")
    for track in sample.get("subtitles", []):
        label = str(track.get("language", ""))
        if label == expected or label.endswith(f"({language_code})"):
            return track
    raise KeyError(f"Sample {sample.get('id')} has no {expected} subtitle track")


def subtitle_text(entries: list[dict[str, Any]]) -> str:
    lines = []
    for entry in entries:
        content = " ".join(str(entry.get("content", "")).split())
        if content:
            lines.append(f"[{entry.get('index')}] {entry.get('timeframe')} {content}")
    return "\n".join(lines)
