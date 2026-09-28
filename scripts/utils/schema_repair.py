#!/usr/bin/env python3
"""Read, validate, and repair the structure of CineSubBench outputs."""

from __future__ import annotations

import json
import re
from typing import Any


class SchemaValidationError(ValueError):
    pass


NARRATIVE_GENRES = {
    "Action",
    "Adventure",
    "Animation",
    "Biography",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "Film-Noir",
    "History",
    "Horror",
    "Music",
    "Musical",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Sport",
    "Thriller",
    "War",
    "Western",
}

CULTURAL_AGE_LABELS = {
    "2+",
    "3+",
    "4+",
    "5+",
    "6+",
    "7+",
    "8+",
    "9+",
    "10+",
    "11+",
    "12+",
    "13+",
    "14+",
    "15+",
    "16+",
    "17+",
    "18+",
}

CULTURAL_COUNTRIES = {
    "Australia",
    "Brazil",
    "France",
    "Germany",
    "Netherlands",
    "Singapore",
    "South Korea",
    "Sweden",
    "United Kingdom",
    "United States",
}

CULTURAL_COUNTRY_ALIASES = {country.lower(): country for country in CULTURAL_COUNTRIES}
CULTURAL_COUNTRY_ALIASES.update(
    {
        "australia": "Australia",
        "brazil": "Brazil",
        "france": "France",
        "germany": "Germany",
        "netherlands": "Netherlands",
        "singapore": "Singapore",
        "south korea": "South Korea",
        "south_korea": "South Korea",
        "southkorea": "South Korea",
        "united kingdom": "United Kingdom",
        "united_kingdom": "United Kingdom",
        "unitedkingdom": "United Kingdom",
        "uk": "United Kingdom",
        "united states": "United States",
        "united_states": "United States",
        "unitedstates": "United States",
        "us": "United States",
        "usa": "United States",
    }
)


def extract_json_text(text: str) -> str:
    """Extract a JSON object from raw model text or Markdown fenced text."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


def strip_trailing_commas(text: str) -> str:
    """Remove JSON trailing commas before object/array closures."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def balance_json_closures(text: str) -> str:
    """Append missing closing braces/brackets when the prefix is otherwise sane.

    This is intentionally narrow: it only balances unterminated containers after
    checking string state, and it refuses to repair mismatched closures. It does
    not add missing fields, labels, commas, or rationale text.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                raise json.JSONDecodeError("unbalanced JSON closure", text, 0)
            opener = stack.pop()
            if (opener, char) not in {("{", "}"), ("[", "]")}:
                raise json.JSONDecodeError("mismatched JSON closure", text, 0)
    if in_string:
        raise json.JSONDecodeError("unterminated JSON string", text, len(text))
    closers = {"{": "}", "[": "]"}
    return text + "".join(closers[item] for item in reversed(stack))


def parse_jsonish(text: str) -> Any:
    candidates = []
    extracted = extract_json_text(text)
    candidates.extend([text.strip(), extracted])
    candidates.extend(strip_trailing_commas(candidate) for candidate in list(candidates))
    for candidate in list(candidates):
        try:
            candidates.append(balance_json_closures(candidate))
        except json.JSONDecodeError:
            pass

    last_error: Exception | None = None
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("empty JSON content", text, 0)


def require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaValidationError(f"{path} must be an object")
    return value


def require_string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise SchemaValidationError(f"{path} must be a string")
    if not allow_empty and not value.strip():
        raise SchemaValidationError(f"{path} must be non-empty")
    return value


def normalize_country_name(country: Any) -> str:
    country_key = str(country).strip()
    alias_key = country_key.lower().replace(" ", "").replace("_", "")
    return (
        CULTURAL_COUNTRY_ALIASES.get(country_key.lower())
        or CULTURAL_COUNTRY_ALIASES.get(alias_key)
        or country_key
    )


def normalize_narrative_schema_drift(parsed: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(parsed)
    normalized.pop("inputLanguage", None)
    normalized.pop("taskGroup", None)

    if "keyMessage" not in normalized:
        for alias in ["key_message", "message", "theme", "thematicMessage"]:
            if alias in normalized:
                normalized["keyMessage"] = normalized.pop(alias)
                break
    if "genres" not in normalized and "genre" in normalized:
        genre = normalized.pop("genre")
        normalized["genres"] = genre if isinstance(genre, list) else [genre]
    return normalized


def validate_narrative_output(parsed: dict[str, Any], sample_id: int) -> None:
    allowed_keys = {"id", "plot", "synopsis", "keyMessage", "genres"}
    extra_keys = set(parsed) - allowed_keys
    if extra_keys:
        raise SchemaValidationError(f"unexpected narrative keys: {sorted(extra_keys)}")
    if parsed.get("id") != sample_id:
        raise SchemaValidationError("id mismatch or missing")
    for key in ["plot", "synopsis", "keyMessage"]:
        require_string(parsed.get(key), key)
    genres = parsed.get("genres")
    if not isinstance(genres, list):
        raise SchemaValidationError("genres must be a list")
    if not 1 <= len(genres) <= 4:
        raise SchemaValidationError("genres must contain 1 to 4 labels")
    invalid_type = [genre for genre in genres if not isinstance(genre, str)]
    if invalid_type:
        raise SchemaValidationError(f"genre labels must be strings: {invalid_type}")


def normalize_cultural_schema_drift(parsed: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(parsed)
    root_rationale = normalized.get("rationale")

    if "ageSuitability" not in normalized and "age_suitability" in normalized:
        normalized["ageSuitability"] = normalized.pop("age_suitability")
    if "countryRatings" not in normalized and "country_ratings" in normalized:
        normalized["countryRatings"] = normalized.pop("country_ratings")

    if root_rationale is None:
        if "ageSuitabilityRationale" in normalized:
            root_rationale = {"ageSuitability": normalized.pop("ageSuitabilityRationale")}
        elif "age_suitability_rationale" in normalized:
            root_rationale = {"ageSuitability": normalized.pop("age_suitability_rationale")}
        elif "rationale_age_suitability" in normalized:
            root_rationale = {"ageSuitability": normalized.pop("rationale_age_suitability")}
        if root_rationale is not None:
            normalized["rationale"] = root_rationale

    if "ageSuitabilityPrediction" not in normalized and "ageSuitability" in normalized:
        age_value = normalized.pop("ageSuitability")
        if isinstance(age_value, dict):
            age_obj = dict(age_value)
            if "ageRating" not in age_obj and "rating" in age_obj:
                age_obj["ageRating"] = age_obj.pop("rating")
            if "rationale" not in age_obj:
                if isinstance(root_rationale, str):
                    age_obj["rationale"] = root_rationale
                elif isinstance(root_rationale, dict) and isinstance(root_rationale.get("ageSuitability"), str):
                    age_obj["rationale"] = root_rationale["ageSuitability"]
            normalized["ageSuitabilityPrediction"] = age_obj
        elif isinstance(age_value, str):
            normalized["ageSuitabilityPrediction"] = {
                "ageRating": age_value,
                "rationale": (
                    root_rationale
                    if isinstance(root_rationale, str)
                    else root_rationale.get("ageSuitability", "")
                    if isinstance(root_rationale, dict)
                    else ""
                ),
            }

    if "countryMotionPictureRatingPrediction" not in normalized and "countryRatings" in normalized:
        country_value = normalized.pop("countryRatings")
        if isinstance(country_value, list):
            normalized_items = []
            for item in country_value:
                if not isinstance(item, dict):
                    continue
                normalized_item = dict(item)
                if "countryName" in normalized_item and "country" not in normalized_item:
                    normalized_item["country"] = normalized_item.pop("countryName")
                if "motionPictureRating" in normalized_item and "rating" not in normalized_item:
                    normalized_item["rating"] = normalized_item.pop("motionPictureRating")
                if "prediction" in normalized_item and "rating" not in normalized_item:
                    normalized_item["rating"] = normalized_item.pop("prediction")
                if "country" in normalized_item:
                    normalized_item["country"] = normalize_country_name(normalized_item["country"])
                normalized_item.setdefault("rationale", "No rationale provided by model.")
                normalized_items.append(normalized_item)
            normalized["countryMotionPictureRatingPrediction"] = normalized_items
        elif isinstance(country_value, dict):
            rationale_by_country = {}
            if isinstance(root_rationale, dict):
                if isinstance(root_rationale.get("countryRatings"), dict):
                    rationale_by_country = root_rationale["countryRatings"]
                elif isinstance(root_rationale.get("countryRationales"), dict):
                    rationale_by_country = root_rationale["countryRationales"]
            normalized_items = []
            for country, rating in country_value.items():
                country_key = str(country).strip()
                normalized_country = normalize_country_name(country_key)
                if isinstance(rating, dict):
                    rating_value = rating.get("rating")
                    rationale = rating.get("rationale", "")
                else:
                    rating_value = rating
                    rationale = ""
                alias_key = country_key.lower().replace(" ", "").replace("_", "")
                if not rationale and rationale_by_country:
                    rationale = (
                        rationale_by_country.get(country_key)
                        or rationale_by_country.get(normalized_country)
                        or rationale_by_country.get(alias_key)
                        or ""
                    )
                normalized_items.append(
                    {
                        "country": normalized_country,
                        "rating": rating_value,
                        "rationale": rationale or "No rationale provided by model.",
                    }
                )
            normalized["countryMotionPictureRatingPrediction"] = normalized_items

    if isinstance(normalized.get("countryMotionPictureRatingPrediction"), list):
        for item in normalized["countryMotionPictureRatingPrediction"]:
            if not isinstance(item, dict):
                continue
            if "country" in item:
                item["country"] = normalize_country_name(item["country"])
            for rationale_alias in ["rational", "rationationale"]:
                if "rationale" not in item and rationale_alias in item:
                    item["rationale"] = item.pop(rationale_alias)
                    break

    if "ageSuitabilityPrediction" in normalized:
        age = normalized["ageSuitabilityPrediction"]
        if isinstance(age, dict):
            age.setdefault("rationale", "No rationale provided by model.")

    normalized.pop("rationale", None)
    return normalized


def validate_cultural_output(parsed: dict[str, Any], sample_id: int) -> None:
    allowed_keys = {"id", "ageSuitabilityPrediction", "countryMotionPictureRatingPrediction"}
    extra_keys = set(parsed) - allowed_keys
    if extra_keys:
        raise SchemaValidationError(f"unexpected cultural keys: {sorted(extra_keys)}")
    if parsed.get("id") != sample_id:
        raise SchemaValidationError("id mismatch or missing")

    age = require_object(parsed.get("ageSuitabilityPrediction"), "ageSuitabilityPrediction")
    age_extra_keys = set(age) - {"ageRating", "rationale"}
    if age_extra_keys:
        raise SchemaValidationError(f"unexpected ageSuitabilityPrediction keys: {sorted(age_extra_keys)}")
    age_rating = require_string(age.get("ageRating"), "ageSuitabilityPrediction.ageRating")
    if age_rating not in CULTURAL_AGE_LABELS:
        raise SchemaValidationError(f"invalid age rating: {age_rating}")
    require_string(age.get("rationale"), "ageSuitabilityPrediction.rationale")

    country_items = parsed.get("countryMotionPictureRatingPrediction")
    if not isinstance(country_items, list):
        raise SchemaValidationError("countryMotionPictureRatingPrediction must be a list")
    if len(country_items) != len(CULTURAL_COUNTRIES):
        raise SchemaValidationError("countryMotionPictureRatingPrediction must contain 10 countries")

    seen_countries = []
    for index, item in enumerate(country_items):
        item = require_object(item, f"countryMotionPictureRatingPrediction[{index}]")
        item_extra_keys = set(item) - {"country", "rating", "rationale"}
        if item_extra_keys:
            raise SchemaValidationError(
                f"unexpected countryMotionPictureRatingPrediction[{index}] keys: {sorted(item_extra_keys)}"
            )
        country = require_string(item.get("country"), f"countryMotionPictureRatingPrediction[{index}].country")
        if country not in CULTURAL_COUNTRIES:
            raise SchemaValidationError(f"invalid country: {country}")
        seen_countries.append(country)
        require_string(item.get("rating"), f"countryMotionPictureRatingPrediction[{index}].rating")
        require_string(item.get("rationale"), f"countryMotionPictureRatingPrediction[{index}].rationale")

    missing_countries = CULTURAL_COUNTRIES - set(seen_countries)
    duplicate_countries = sorted({country for country in seen_countries if seen_countries.count(country) > 1})
    if missing_countries:
        raise SchemaValidationError(f"missing countries: {sorted(missing_countries)}")
    if duplicate_countries:
        raise SchemaValidationError(f"duplicate countries: {duplicate_countries}")


def validate_task_output(task: str, parsed: Any, sample_id: int) -> dict[str, Any]:
    parsed = require_object(parsed, "root")
    parsed["id"] = sample_id
    if task == "narrative":
        parsed = normalize_narrative_schema_drift(parsed)
        parsed["id"] = sample_id
        validate_narrative_output(parsed, sample_id)
    elif task == "cultural":
        parsed = normalize_cultural_schema_drift(parsed)
        parsed["id"] = sample_id
        validate_cultural_output(parsed, sample_id)
    else:
        raise ValueError(f"Unknown task: {task}")
    return parsed
