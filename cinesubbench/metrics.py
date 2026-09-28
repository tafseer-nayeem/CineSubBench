"""CineSubBench task metrics and composite leaderboard."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


CORE_GENRES = {
    "Action", "Adventure", "Animation", "Biography", "Comedy", "Crime", "Documentary",
    "Drama", "Family", "Fantasy", "Film-Noir", "History", "Horror", "Music", "Musical",
    "Mystery", "Romance", "Sci-Fi", "Sport", "Thriller", "War", "Western",
}
GENRE_ALIASES = {
    "Science Fiction": "Sci-Fi", "Science-Fiction": "Sci-Fi", "Science fiction": "Sci-Fi",
    "SciFi": "Sci-Fi", "Sci Fi": "Sci-Fi", "Film Noir": "Film-Noir", "Film noir": "Film-Noir",
    "Body Horror": "Horror", "Psychological Horror": "Horror", "Supernatural Horror": "Horror",
    "Monster Horror": "Horror", "Dance": "Music", "Tragedy": "Drama", "Spy": "Action",
    "Superhero": "Action", "Anthology": None,
}
COUNTRY_LABELS = {
    "Australia": ["G", "PG", "M", "MA15+", "R18+"],
    "Brazil": ["Livre", "10", "12", "14", "16", "18"],
    "France": ["Tous publics", "12", "16", "18"],
    "Germany": ["0", "6", "12", "16", "18"],
    "Netherlands": ["AL", "6", "9", "12", "14", "16", "18"],
    "Singapore": ["G", "PG", "PG13", "NC16", "M18", "R21"],
    "South Korea": ["All", "12", "15", "19"],
    "Sweden": ["Btl", "7", "11", "15"],
    "United Kingdom": ["U", "PG", "12", "15", "18"],
    "United States": ["G", "PG", "PG-13", "R", "NC-17"],
}
LC_CATEGORIES = {
    "strongProfanity": ("strongProfanityCount", "strongProfanityIndices"),
    "crudeBodilyLanguage": ("crudeBodilyLanguageCount", "crudeBodilyLanguageIndices"),
    "mildObscenity": ("mildObscenityCount", "mildObscenityIndices"),
    "religiousProfanityAndExclamation": (
        "religiousProfanityAndExclamationCount",
        "religiousProfanityAndExclamationIndices",
    ),
}


def _outputs(payload: dict[str, Any] | None, key: str = "outputs") -> list[dict[str, Any]]:
    return [] if not payload else list(payload.get(key, payload.get("judgments", [])))


def _prediction_map(payload: dict[str, Any] | None, key: str = "outputs") -> dict[int, dict[str, Any]]:
    rows = _outputs(payload, key)
    ids = [row.get("id") for row in rows]
    if any(sample_id is None for sample_id in ids):
        raise ValueError("Every prediction must contain an id")
    if len(ids) != len(set(ids)):
        raise ValueError("Prediction file contains duplicate sample IDs")
    return {int(row["id"]): row for row in rows}


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _normalize_genres(labels: Iterable[str]) -> tuple[set[str], int]:
    result, invalid = set(), 0
    for label in labels:
        mapped = GENRE_ALIASES.get(label, label)
        if mapped is None:
            continue
        if not isinstance(mapped, str) or mapped not in CORE_GENRES:
            invalid += 1
        else:
            result.add(mapped)
    return result, invalid


def genre_metrics(gold: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    predictions = _prediction_map(payload)
    totals = Counter(tp=0, fp=0, fn=0)
    per_label: dict[str, Counter] = defaultdict(Counter)
    exact = 0
    jaccards = []
    for sample in gold:
        gold_set, _ = _normalize_genres(sample.get("genres", []))
        row = predictions.get(sample["id"])
        pred_set, invalid = _normalize_genres(row.get("genres", [])) if row else (set(), 0)
        tp, fp, fn = len(gold_set & pred_set), len(pred_set - gold_set) + invalid, len(gold_set - pred_set)
        totals.update(tp=tp, fp=fp, fn=fn)
        exact += int(gold_set == pred_set and invalid == 0 and row is not None)
        union = len(gold_set | pred_set) + invalid
        jaccards.append(tp / union if union else 1.0)
        for label in gold_set | pred_set:
            per_label[label]["tp"] += int(label in gold_set and label in pred_set)
            per_label[label]["fp"] += int(label not in gold_set and label in pred_set)
            per_label[label]["fn"] += int(label in gold_set and label not in pred_set)
    precision, recall, micro_f1 = _prf(totals["tp"], totals["fp"], totals["fn"])
    label_f1 = [_prf(c["tp"], c["fp"], c["fn"])[2] for c in per_label.values()]
    n = len(gold)
    result = {
        "genre_exact_match_pct": 100 * exact / n if n else 0.0,
        "genre_micro_precision_pct": 100 * precision,
        "genre_micro_recall_pct": 100 * recall,
        "genre_micro_f1_pct": 100 * micro_f1,
        "genre_macro_f1_pct": 100 * mean(label_f1) if label_f1 else 0.0,
        "genre_mean_jaccard_pct": 100 * mean(jaccards) if jaccards else 0.0,
    }
    result["_details"] = {
        label: {
            "tp": counts["tp"],
            "fp": counts["fp"],
            "fn": counts["fn"],
            "precisionPct": 100 * _prf(counts["tp"], counts["fp"], counts["fn"])[0],
            "recallPct": 100 * _prf(counts["tp"], counts["fp"], counts["fn"])[1],
            "f1Pct": 100 * _prf(counts["tp"], counts["fp"], counts["fn"])[2],
        }
        for label, counts in sorted(per_label.items())
    }
    return result


def _age_number(label: Any) -> int | None:
    value = str(label).replace("+", "")
    return int(value) if value.isdigit() else None


def cultural_metrics(gold: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    predictions = _prediction_map(payload)
    age_exact = within_one = within_two = 0
    age_errors, country_errors = [], []
    country_exact = country_valid = 0
    per_country = {country: Counter(exact=0, valid=0, n=0) for country in COUNTRY_LABELS}
    country_errors_by_name: dict[str, list[int]] = defaultdict(list)
    country_total = len(gold) * len(COUNTRY_LABELS)
    for sample in gold:
        pred = predictions.get(sample["id"], {})
        gold_age = sample["ageSuitabilityRating"]
        pred_age = pred.get("ageSuitabilityPrediction", {}).get("ageRating")
        gold_num, pred_num = _age_number(gold_age), _age_number(pred_age)
        age_exact += int(gold_age == pred_age)
        if gold_num is not None and pred_num is not None:
            error = abs(gold_num - pred_num)
            age_errors.append(error)
            within_one += int(error <= 1)
            within_two += int(error <= 2)
        pred_ratings = {
            row.get("country"): row.get("rating")
            for row in pred.get("countryMotionPictureRatingPrediction", [])
        }
        for gold_entry in sample["motionPictureRatings"]:
            country = gold_entry["country"]
            label = pred_ratings.get(country)
            labels = COUNTRY_LABELS[country]
            per_country[country]["n"] += 1
            if label in labels:
                country_valid += 1
                country_exact += int(label == gold_entry["label"])
                per_country[country]["valid"] += 1
                per_country[country]["exact"] += int(label == gold_entry["label"])
                gold_ordinal = labels.index(gold_entry["label"])
                error = abs(labels.index(label) - gold_ordinal)
                country_errors.append(error)
                country_errors_by_name[country].append(error)
    n = len(gold)
    result = {
        "age_exact_match_pct": 100 * age_exact / n if n else 0.0,
        "age_within_one_year_pct": 100 * within_one / n if n else 0.0,
        "age_within_two_years_pct": 100 * within_two / n if n else 0.0,
        "age_mae_years": mean(age_errors) if age_errors else None,
        "country_exact_match_pct": 100 * country_exact / country_total if country_total else 0.0,
        "country_ordinal_mae": mean(country_errors) if country_errors else None,
        "country_valid_prediction_pct": 100 * country_valid / country_total if country_total else 0.0,
    }
    result["_details"] = {
        country: {
            "exactMatchPct": 100 * stats["exact"] / stats["n"] if stats["n"] else 0.0,
            "ordinalMAE": mean(country_errors_by_name[country]) if country_errors_by_name[country] else None,
            "validPredictionPct": 100 * stats["valid"] / stats["n"] if stats["n"] else 0.0,
        }
        for country, stats in per_country.items()
    }
    return result


def language_content_metrics(gold: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    predictions = _prediction_map(payload)
    totals = {category: Counter(tp=0, fp=0, fn=0, count_abs=0, n=0) for category in LC_CATEGORIES}
    agnostic = Counter(tp=0, fp=0, fn=0)
    for sample in gold:
        pred = predictions.get(sample["id"], {})
        categories = sample["languageContentAssessment"]["categories"]
        gold_all, pred_all = set(), set()
        for category, (count_key, indices_key) in LC_CATEGORIES.items():
            gold_row = categories[category]
            gold_indices = set(gold_row["evidenceSubtitleIndices"])
            nested_categories = pred.get("languageContentAssessment", pred.get("subtitleGroundedLanguageAssessment", {})).get("categories", {})
            nested_key = "religiousLanguage" if category == "religiousProfanityAndExclamation" else category
            nested = nested_categories.get(category, nested_categories.get(nested_key, {}))
            refs = nested.get("evidenceSubtitleIndices", nested.get("evidenceIndices", nested.get("evidenceRefs", [])))
            nested_indices = [ref.get("index") if isinstance(ref, dict) else ref for ref in refs]
            pred_indices = set(pred.get(indices_key, nested_indices))
            predicted_count = pred.get(count_key, nested.get("occurrenceCount", nested.get("goldCount", nested.get("count", 0))))
            gold_all |= gold_indices
            pred_all |= pred_indices
            totals[category]["tp"] += len(gold_indices & pred_indices)
            totals[category]["fp"] += len(pred_indices - gold_indices)
            totals[category]["fn"] += len(gold_indices - pred_indices)
            totals[category]["count_abs"] += abs(int(predicted_count) - int(gold_row["occurrenceCount"]))
            totals[category]["n"] += 1
        agnostic["tp"] += len(gold_all & pred_all)
        agnostic["fp"] += len(pred_all - gold_all)
        agnostic["fn"] += len(gold_all - pred_all)
    strict_f1, count_mae = [], []
    for category in LC_CATEGORIES:
        row = totals[category]
        strict_f1.append(_prf(row["tp"], row["fp"], row["fn"])[2])
        if row["n"]:
            count_mae.append(row["count_abs"] / row["n"])
    p, r, f1 = _prf(agnostic["tp"], agnostic["fp"], agnostic["fn"])
    result = {
        "lc_count_mae": mean(count_mae) if count_mae else None,
        "lc_agnostic_precision_pct": 100 * p,
        "lc_agnostic_recall_pct": 100 * r,
        "lc_agnostic_f1_pct": 100 * f1,
        "lc_strict_f1_pct": 100 * mean(strict_f1) if strict_f1 else None,
    }
    result["_details"] = {}
    for category in LC_CATEGORIES:
        row = totals[category]
        precision, recall, f1 = _prf(row["tp"], row["fp"], row["fn"])
        result["_details"][category] = {
            "countMAE": row["count_abs"] / row["n"] if row["n"] else None,
            "evidencePrecisionPct": 100 * precision,
            "evidenceRecallPct": 100 * recall,
            "evidenceF1Pct": 100 * f1,
            "tp": row["tp"],
            "fp": row["fp"],
            "fn": row["fn"],
        }
    return result


def _judge_overall(evaluation: dict[str, Any]) -> float:
    if isinstance(evaluation.get("scores"), dict):
        return float(evaluation["scores"].get("overall", evaluation["scores"].get("modelAssignedOverall")))
    return float(evaluation["overall"])


def judge_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    rows = _outputs(payload, "judgments")
    _prediction_map(payload, "judgments")
    metrics = {}
    details = {}
    values = []
    for task, key in (("plot", "plotEvaluation"), ("synopsis", "synopsisEvaluation"), ("key_message", "keyMessageEvaluation")):
        scores = [_judge_overall(row[key]) for row in rows if key in row]
        metrics[f"{task}_judge_1to5"] = mean(scores) if scores else None
        rubric = defaultdict(list)
        errors = Counter()
        for row in rows:
            evaluation = row.get(key, {})
            score_source = evaluation.get("scores", evaluation)
            for name, value in score_source.items():
                if isinstance(value, (int, float)):
                    rubric[name].append(float(value))
            for error in evaluation.get("errors", []):
                if isinstance(error, dict) and error.get("type"):
                    errors[error["type"]] += 1
            for error in evaluation.get("errorTypes", []):
                if error != "none":
                    errors[error] += 1
        details[task] = {
            "rubricAverages": {name: mean(values) for name, values in sorted(rubric.items())},
            "errorTypeCounts": dict(sorted(errors.items())),
        }
        values.extend(scores)
    task_scores = [
        metrics["plot_judge_1to5"],
        metrics["synopsis_judge_1to5"],
        metrics["key_message_judge_1to5"],
    ]
    available_scores = [score for score in task_scores if score is not None]
    metrics["narrative_overall_1to5"] = mean(available_scores) if available_scores else None
    metrics["_details"] = details
    return metrics


def constraint_metrics(gold: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, float]:
    outputs = _prediction_map(payload)
    specs = {"plot": (45, 80), "synopsis": (180, 300), "keyMessage": (8, 25)}
    result = {}
    for task, (minimum, maximum) in specs.items():
        passes = 0
        for sample in gold:
            text = outputs.get(sample["id"], {}).get(task, "")
            count = len(re.findall(r"\b[\w'-]+\b", text))
            passes += int(minimum <= count <= maximum)
        result[f"{task}_length_compliance_pct"] = 100 * passes / len(gold) if gold else 0.0
    return result


def evaluate_all(
    gold: list[dict[str, Any]],
    model: str,
    language: str,
    narrative: dict[str, Any] | None = None,
    cultural: dict[str, Any] | None = None,
    language_content: dict[str, Any] | None = None,
    judgments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    gold_ids = {int(sample["id"]) for sample in gold}
    for name, payload, key in (
        ("narrative", narrative, "outputs"),
        ("cultural", cultural, "outputs"),
        ("language content", language_content, "outputs"),
        ("narrative judgments", judgments, "judgments"),
    ):
        if payload:
            prediction_ids = set(_prediction_map(payload, key))
            unknown = sorted(prediction_ids - gold_ids)
            if unknown:
                raise ValueError(f"{name} file contains IDs absent from the gold dataset: {unknown}")
    metrics: dict[str, float] = {}
    breakdowns: dict[str, Any] = {}
    if narrative:
        metrics["narrative_completion_pct"] = 100 * len(_prediction_map(narrative)) / len(gold) if gold else 0.0
        genre = genre_metrics(gold, narrative)
        breakdowns["genreByLabel"] = genre.pop("_details")
        metrics.update(genre)
        metrics.update(constraint_metrics(gold, narrative))
    if cultural:
        metrics["cultural_completion_pct"] = 100 * len(_prediction_map(cultural)) / len(gold) if gold else 0.0
        cultural_result = cultural_metrics(gold, cultural)
        breakdowns["country"] = cultural_result.pop("_details")
        metrics.update(cultural_result)
    if language_content:
        if language != "en":
            raise ValueError("Language-content metrics are English-only")
        metrics["lc_completion_pct"] = 100 * len(_prediction_map(language_content)) / len(gold) if gold else 0.0
        language_result = language_content_metrics(gold, language_content)
        breakdowns["languageContent"] = language_result.pop("_details")
        metrics.update(language_result)
    if judgments:
        metrics["judge_completion_pct"] = 100 * len(_prediction_map(judgments, "judgments")) / len(gold) if gold else 0.0
        judge_result = judge_metrics(judgments)
        breakdowns["narrativeJudge"] = judge_result.pop("_details")
        metrics.update(judge_result)
    return {
        "benchmark": "CineSubBench",
        "model": model,
        "language": language,
        "sampleCount": len(gold),
        "metrics": metrics,
        "breakdowns": breakdowns,
    }


def composite_leaderboard(
    summaries: list[dict[str, Any]],
    languages: tuple[str, ...] = ("en", "ar", "id", "fa", "ro", "vi"),
) -> list[dict[str, Any]]:
    by_model: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    for summary in summaries:
        by_model[summary["model"]][summary["language"]] = summary["metrics"]
    rows = []
    required = ("narrative_overall_1to5", "genre_micro_f1_pct", "age_mae_years", "country_exact_match_pct")
    for model, by_language in by_model.items():
        if any(language not in by_language for language in languages) or "en" not in by_language:
            continue
        if any(any(by_language[language].get(metric) is None for metric in required) for language in languages):
            continue
        if by_language["en"].get("lc_strict_f1_pct") is None:
            continue
        completion_fields = (
            "narrative_completion_pct",
            "cultural_completion_pct",
            "judge_completion_pct",
        )
        if any(
            field in by_language[language] and by_language[language][field] < 100.0
            for language in languages
            for field in completion_fields
        ):
            continue
        if "lc_completion_pct" in by_language["en"] and by_language["en"]["lc_completion_pct"] < 100.0:
            continue
        averages = {metric: mean(float(by_language[lang][metric]) for lang in languages) for metric in required}
        normalized_narrative = 25.0 * (averages["narrative_overall_1to5"] - 1.0)
        normalized_age = 100.0 * (1.0 - min(averages["age_mae_years"], 16.0) / 16.0)
        cultural_score = (normalized_age + averages["country_exact_match_pct"]) / 2.0
        lc_strict = float(by_language["en"]["lc_strict_f1_pct"])
        composite = (normalized_narrative + averages["genre_micro_f1_pct"] + cultural_score + lc_strict) / 4.0
        rows.append({
            "model": model,
            "composite_score": composite,
            "normalized_narrative": normalized_narrative,
            "genre_micro_f1_pct": averages["genre_micro_f1_pct"],
            "normalized_age": normalized_age,
            "cultural_score": cultural_score,
            "lc_strict_f1_pct": lc_strict,
            "narrative_overall_1to5": averages["narrative_overall_1to5"],
            "age_mae_years": averages["age_mae_years"],
            "country_exact_match_pct": averages["country_exact_match_pct"],
        })
    rows.sort(key=lambda row: (-row["composite_score"], row["model"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows
