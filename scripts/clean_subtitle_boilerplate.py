#!/usr/bin/env python3
"""Remove subtitle-credit and contact boilerplate from CineSubBench JSON.

The input is processed one film at a time. Subtitle indices are never
renumbered, which preserves links from evidence annotations to subtitle lines.
The source file is never overwritten unless --in-place is explicitly supplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, TextIO


EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
URL_RE = re.compile(
    r"(?i)(?:https?://|www\.|(?:t|telegram)\.me/|fb\.com/|"
    r"\b[a-z0-9][a-z0-9.-]*\.(?:com|net|org|info|biz|ir|co|cc|ro|id|tv)(?:[/\s<]|$))"
)
SOCIAL_RE = re.compile(
    r"(?i)\b(?:contact|email|e-mail|facebook|telegram|instagram|twitter|website|web)\s*[:：]"
)
CREDIT_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:subtitles?|subtitled|translation|translated|translator|transcribed|"
    r"sync(?:ed|hronized)?|resync(?:ed)?|encoded|ripped)\s+(?:by|from)\b|"
    r"\b(?:subtitle|translation|translator|sync|resync|rip|encode)\s*[:：]|"
    r"\b(?:alih\s+bahasa(?:\s+oleh)?|penerjemah\s*[:：]|penyelaras\s+(?:akhir|oleh)|"
    r"bi[eê]n\s+d[iị]ch\s*[:：]|subtitrare\s+prin|traducere\s+[şs]i\s+adaptare|"
    r"traducido\s+por|tradu(?:ção|zido)\s+por|"
    r"legendas?\s+por|[cç]eviri(?:\s*[:：]|\s+by))\b|"
    r"(?:مترجم(?:ین|ان)?\s*[:：]|ز[يی]رنویس\s+توسط|تيم\s+ترجمه|تیم\s+ترجمه|"
    r"ترجمه\s+(?:و\s+ز[يی]رنویس|از|به\s+فارسي|توسط)|"
    r"اين\s+ز[يی]رنویس\s+تماما|تـــرجمه\s*[:：]|تـرجـمـة\s*[:：])"
    r")"
)
PROVIDER_RE = re.compile(
    r"(?ix)(?:opensubtitles?|subdl|subscene|subs?indo|subtitrari\s*online|"
    r"download(?:ed)?\s+(?:the\s+)?subtitles?|subtitle\s+(?:team|group|crew)|"
    r"(?:sub(?:title)?s?|this\s+sub)\s+(?:is\s+)?(?:provided|presented)\s+by)"
)
TAG_RE = re.compile(r"<[^>]*>|\{\\[^}]*\}")
DECORATION_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def iter_json_array(handle: TextIO, chunk_size: int = 1024 * 1024) -> Iterator[Any]:
    """Incrementally decode a top-level JSON array."""
    decoder = json.JSONDecoder()
    buffer = ""
    pos = 0
    eof = False
    started = False

    while True:
        if pos >= len(buffer) and not eof:
            buffer = handle.read(chunk_size)
            pos = 0
            eof = not buffer

        while True:
            while pos < len(buffer) and (buffer[pos].isspace() or (started and buffer[pos] == ",")):
                pos += 1
            if pos < len(buffer):
                break
            if eof:
                raise ValueError("Unexpected end of JSON input")
            buffer = handle.read(chunk_size)
            pos = 0
            eof = not buffer

        if not started:
            if buffer[pos] != "[":
                raise ValueError("Expected a top-level JSON array")
            started = True
            pos += 1
            continue

        if buffer[pos] == "]":
            return

        while True:
            try:
                value, end = decoder.raw_decode(buffer, pos)
                yield value
                buffer = buffer[end:]
                pos = 0
                break
            except json.JSONDecodeError:
                if eof:
                    raise
                buffer = buffer[pos:] + handle.read(chunk_size)
                pos = 0
                eof = len(buffer) == 0


def visible_text(text: str) -> str:
    return " ".join(TAG_RE.sub(" ", text).replace("\\N", " ").split())


def reasons_for(text: str) -> set[str]:
    searchable = visible_text(text)
    reasons: set[str] = set()
    if EMAIL_RE.search(searchable):
        reasons.add("email")
    if URL_RE.search(searchable):
        reasons.add("url_or_domain")
    if SOCIAL_RE.search(searchable):
        reasons.add("contact_label")
    if CREDIT_RE.search(searchable):
        reasons.add("subtitle_credit")
    if PROVIDER_RE.search(searchable):
        reasons.add("subtitle_provider_promo")
    return reasons


def clean_content(content: str) -> tuple[str | None, set[str]]:
    """Return cleaned content, or None when the complete cue is boilerplate."""
    block_reasons = reasons_for(content)
    if not block_reasons:
        return content, set()

    # An address can be narratively meaningful on screen. When no credit,
    # provider, URL, or contact marker accompanies it, redact only the address.
    if block_reasons == {"email"}:
        return EMAIL_RE.sub("[email redacted]", content), block_reasons

    compact = visible_text(content)
    high_confidence = {
        "email",
        "url_or_domain",
        "contact_label",
        "subtitle_credit",
        "subtitle_provider_promo",
    }
    if len(compact) <= 500 and block_reasons & high_confidence:
        return None, block_reasons

    kept: list[str] = []
    removed_reasons: set[str] = set()
    for line in content.splitlines():
        line_reasons = reasons_for(line)
        if line_reasons:
            removed_reasons.update(line_reasons)
            continue
        if removed_reasons and DECORATION_RE.fullmatch(visible_text(line)):
            removed_reasons.add("decoration")
            continue
        kept.append(line)

    cleaned = "\n".join(kept).strip()
    if not visible_text(cleaned):
        return None, block_reasons | removed_reasons
    return cleaned, removed_reasons


def clean_film(film: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    report["filmsProcessed"] += 1
    film_id = film.get("id")
    for track in film.get("subtitles", []):
        report["tracksProcessed"] += 1
        language = track.get("language")
        cleaned_entries = []
        for entry in track.get("entries", []):
            report["entriesInspected"] += 1
            original = entry.get("content")
            if not isinstance(original, str):
                cleaned_entries.append(entry)
                continue
            cleaned, reasons = clean_content(original)
            if not reasons:
                cleaned_entries.append(entry)
                continue

            location = {
                "filmId": film_id,
                "language": language,
                "subtitleIndex": entry.get("index"),
                "timeframe": entry.get("timeframe"),
                "action": "removed" if cleaned is None else "modified",
                "reasons": sorted(reasons),
                "contentSha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            }
            report["changes"].append(location)
            report["reasonCounts"].update(reasons)
            if cleaned is None:
                report["entriesRemoved"] += 1
                continue
            report["entriesModified"] += 1
            updated = dict(entry)
            updated["content"] = cleaned
            cleaned_entries.append(updated)
        track["entries"] = cleaned_entries
    return film


def write_cleaned(source: Path, destination: Path, report: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8") as src, destination.open("w", encoding="utf-8") as dst:
        dst.write("[\n")
        first = True
        for film in iter_json_array(src):
            if not isinstance(film, dict):
                raise ValueError("Every top-level array item must be a film object")
            film = clean_film(film, report)
            if not first:
                dst.write(",\n")
            rendered = json.dumps(film, ensure_ascii=False, indent=2)
            dst.write("  " + rendered.replace("\n", "\n  "))
            first = False
        dst.write("\n]\n")


def residual_audit(path: Path) -> dict[str, int]:
    counts: Counter[str] = Counter()
    with path.open("r", encoding="utf-8") as handle:
        for film in iter_json_array(handle):
            for track in film.get("subtitles", []):
                for entry in track.get("entries", []):
                    content = entry.get("content")
                    if isinstance(content, str):
                        counts.update(reasons_for(content))
    return dict(sorted(counts.items()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Source CineSubBench JSON file")
    parser.add_argument("output", type=Path, nargs="?", help="Cleaned JSON destination")
    parser.add_argument("--report", type=Path, help="Audit-report destination")
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Replace the input atomically after successful cleaning and validation",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.input.resolve()
    if not source.is_file():
        raise SystemExit(f"Input file does not exist: {source}")
    if args.in_place and args.output is not None:
        raise SystemExit("Do not provide output together with --in-place")
    if not args.in_place and args.output is None:
        raise SystemExit("Provide an output path, or use --in-place")

    if args.in_place:
        fd, temporary_name = tempfile.mkstemp(prefix=source.stem + ".cleaning-", suffix=".json", dir=source.parent)
        os.close(fd)
        destination = Path(temporary_name)
        final_path = source
    else:
        destination = args.output.resolve()
        final_path = destination
        if destination == source:
            raise SystemExit("Refusing to overwrite the input without --in-place")

    report: dict[str, Any] = {
        "input": source.name,
        "output": final_path.name,
        "inputSha256": sha256(source),
        "filmsProcessed": 0,
        "tracksProcessed": 0,
        "entriesInspected": 0,
        "entriesRemoved": 0,
        "entriesModified": 0,
        "reasonCounts": Counter(),
        "changes": [],
    }

    try:
        write_cleaned(source, destination, report)
        residuals = residual_audit(destination)
        report["residualFlagCounts"] = residuals
        if residuals:
            raise RuntimeError(f"Residual subtitle boilerplate patterns remain: {residuals}")
        report["outputSha256"] = sha256(destination)
        report["reasonCounts"] = dict(sorted(report["reasonCounts"].items()))
        report["status"] = "pass"
        if args.in_place:
            shutil.copymode(source, destination)
            os.replace(destination, source)
    except Exception:
        if args.in_place and destination.exists():
            destination.unlink()
        raise

    report_path = args.report or final_path.with_suffix(".cleaning-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "changes"}, indent=2))
    print(f"Detailed audit: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
