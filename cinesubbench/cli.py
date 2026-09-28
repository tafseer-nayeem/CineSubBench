"""Command-line interface for generation, recovery, evaluation, and ranking."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import ModelConfig
from .data import DEFAULT_DATASET, LANGUAGE_NAMES, load_dataset
from .metrics import composite_leaderboard, evaluate_all
from .providers import make_provider
from .recovery import recover_response
from .tasks import PreparedRequest, prepare_judge_request, prepare_requests, task_assets


RELEASE_ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path | None) -> Any:
    return None if path is None else json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def request_dict(request: PreparedRequest) -> dict[str, Any]:
    return {
        "customId": request.custom_id,
        "sampleId": request.sample_id,
        "system": request.system,
        "user": request.user,
        "schema": request.schema,
        "chunkIndex": request.chunk_index,
        "totalChunks": request.total_chunks,
    }


def request_from_dict(row: dict[str, Any]) -> PreparedRequest:
    return PreparedRequest(
        row["customId"],
        int(row["sampleId"]),
        row["system"],
        row["user"],
        row["schema"],
        int(row.get("chunkIndex", 0)),
        int(row.get("totalChunks", 1)),
    )


def load_config(path: Path) -> ModelConfig:
    load_dotenv(RELEASE_ROOT / ".env")
    return ModelConfig.from_file(path)


def selected_samples(args) -> list[dict[str, Any]]:
    samples = load_dataset(args.dataset)
    if getattr(args, "sample_ids", None):
        wanted = set(args.sample_ids)
        samples = [sample for sample in samples if sample["id"] in wanted]
        found = {sample["id"] for sample in samples}
        missing = sorted(wanted - found)
        if missing:
            raise ValueError(f"Requested sample IDs are absent from the dataset: {missing}")
    if getattr(args, "limit", None) is not None:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        samples = samples[: args.limit]
    return samples


def build_requests(args, samples: list[dict[str, Any]]) -> tuple[str, list[PreparedRequest]]:
    if args.task != "narrative_judge":
        requests = [
            request
            for sample in samples
            for request in prepare_requests(RELEASE_ROOT, args.task, sample, args.language)
        ]
        return args.task, requests
    predictions = {row["id"]: row for row in read_json(args.predictions).get("outputs", [])}
    missing = [sample["id"] for sample in samples if sample["id"] not in predictions]
    if missing:
        raise ValueError(f"Missing narrative predictions for sample IDs: {missing}")
    return args.task, [prepare_judge_request(RELEASE_ROOT, sample, predictions[sample["id"]]) for sample in samples]


LC_FIELDS = (
    ("strongProfanityCount", "strongProfanityIndices"),
    ("crudeBodilyLanguageCount", "crudeBodilyLanguageIndices"),
    ("mildObscenityCount", "mildObscenityIndices"),
    ("religiousProfanityAndExclamationCount", "religiousProfanityAndExclamationIndices"),
)


def finalize_outputs(
    task: str,
    requests: list[PreparedRequest],
    outputs: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if task != "language_content":
        return [
            {key: value for key, value in row.items() if not key.startswith("_request")}
            for row in outputs
        ], failures
    expected: dict[int, set[int]] = {}
    for request in requests:
        expected.setdefault(request.sample_id, set()).update(range(request.total_chunks))
    chunks: dict[int, dict[int, dict[str, Any]]] = {}
    for row in outputs:
        sample_id = int(row["id"])
        chunk_index = int(row.get("_requestChunkIndex", 0))
        chunks.setdefault(sample_id, {})[chunk_index] = row
    combined = []
    incomplete = []
    for sample_id, expected_indices in expected.items():
        received = chunks.get(sample_id, {})
        missing = sorted(expected_indices - set(received))
        if missing:
            incomplete.append({
                "id": sample_id,
                "errorType": "IncompleteLanguageContentChunks",
                "errorMessage": f"Missing subtitle chunks: {missing}",
            })
            continue
        row: dict[str, Any] = {"id": sample_id}
        for count_key, indices_key in LC_FIELDS:
            row[count_key] = sum(int(chunk[count_key]) for chunk in received.values())
            row[indices_key] = sorted({
                int(index)
                for chunk in received.values()
                for index in chunk[indices_key]
            })
        combined.append(row)
    return combined, [*failures, *incomplete]


def cmd_doctor(args) -> None:
    config = load_config(args.model_config)
    samples = load_dataset(args.dataset)
    report = {
        "status": "pass",
        "modelConfig": config.public_dict(),
        "dataset": str(args.dataset),
        "sampleCount": len(samples),
        "apiKeySet": bool(os.getenv(config.api_key_env)),
    }
    if args.require_key and not report["apiKeySet"]:
        raise RuntimeError(f"{config.api_key_env} is not set")
    print(json.dumps(report, indent=2))


def cmd_prepare(args) -> None:
    config = ModelConfig.from_file(args.model_config)
    samples = selected_samples(args)
    task, requests = build_requests(args, samples)
    args.run_dir.mkdir(parents=True, exist_ok=True)
    request_file = args.run_dir / "requests.jsonl"
    with request_file.open("w", encoding="utf-8") as handle:
        for request in requests:
            handle.write(json.dumps(request_dict(request), ensure_ascii=False) + "\n")
    run_info = {
        "benchmark": "CineSubBench",
        "task": task,
        "language": args.language,
        "dataset": str(args.dataset),
        "sampleCount": len(samples),
        "modelConfig": config.public_dict(),
        "requestFile": request_file.name,
        "jobs": [],
    }
    atomic_json(args.run_dir / "run_info.json", run_info)
    print(json.dumps({"status": "prepared", "requests": len(requests), "runDir": str(args.run_dir)}, indent=2))


def load_prepared(run_dir: Path) -> tuple[dict[str, Any], list[PreparedRequest]]:
    run_info = read_json(run_dir / "run_info.json")
    requests = [
        request_from_dict(json.loads(line))
        for line in (run_dir / run_info["requestFile"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return run_info, requests


def cmd_submit(args) -> None:
    if not args.confirm:
        raise RuntimeError("Submission creates paid API requests. Re-run with --confirm after inspecting the run directory.")
    config = load_config(args.model_config)
    if config.mode != "batch":
        raise ValueError("submit requires execution.mode: batch")
    run_info, requests = load_prepared(args.run_dir)
    if run_info.get("jobs"):
        raise RuntimeError("This run already contains submitted jobs; refusing duplicate paid submission")
    provider = make_provider(config)
    jobs = []
    for index in range(0, len(requests), config.batch_size):
        chunk = requests[index:index + config.batch_size]
        chunk_dir = args.run_dir / f"batch_{index // config.batch_size + 1:04d}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        batch_id = provider.submit_batch(chunk, run_info["task"], chunk_dir)
        jobs.append({
            "batchId": batch_id,
            "customIds": [request.custom_id for request in chunk],
            "status": "submitted",
        })
    run_info["jobs"] = jobs
    atomic_json(args.run_dir / "run_info.json", run_info)
    print(json.dumps({"status": "submitted", "jobs": len(jobs), "requests": len(requests)}, indent=2))


def cmd_status(args) -> None:
    config = load_config(args.model_config)
    run_info, _ = load_prepared(args.run_dir)
    provider = make_provider(config)
    statuses = []
    for job in run_info.get("jobs", []):
        status = provider.batch_status(job["batchId"])
        statuses.append({"batchId": job["batchId"], "providerStatus": status})
    atomic_json(args.run_dir / "status.json", statuses)
    print(json.dumps(statuses, indent=2))


def cmd_collect(args) -> None:
    config = load_config(args.model_config)
    run_info, requests = load_prepared(args.run_dir)
    provider = make_provider(config)
    request_map = {request.custom_id: request for request in requests}
    if not run_info.get("jobs"):
        raise RuntimeError("No submitted jobs were found in this run directory")
    outputs, failures = [], []
    for index, job in enumerate(run_info.get("jobs", []), 1):
        subset = {custom_id: request_map[custom_id] for custom_id in job["customIds"]}
        batch_outputs, batch_failures = provider.collect_batch(
            job["batchId"], subset, run_info["task"], args.run_dir / f"batch_{index:04d}"
        )
        outputs.extend(batch_outputs)
        failures.extend(batch_failures)
    seen_custom_ids = {
        row.get("_requestCustomId") for row in outputs if row.get("_requestCustomId")
    } | {
        row.get("customId") for row in failures if row.get("customId")
    }
    for request in requests:
        if request.custom_id not in seen_custom_ids:
            failures.append({
                "id": request.sample_id,
                "customId": request.custom_id,
                "errorType": "MissingBatchResult",
                "errorMessage": "The provider output did not contain this request.",
            })
    chunk_outputs = list(outputs)
    outputs, failures = finalize_outputs(run_info["task"], requests, outputs, failures)
    payload = {
        "benchmark": "CineSubBench",
        "model": config.name,
        "provider": config.provider,
        "modelId": config.model_id,
        "task": run_info["task"],
        "inputLanguage": LANGUAGE_NAMES[run_info["language"]],
        "outputs" if run_info["task"] != "narrative_judge" else "judgments": sorted(outputs, key=lambda row: row["id"]),
        "failures": failures,
    }
    if run_info["task"] == "language_content":
        payload["partialOutputs"] = sorted(
            chunk_outputs,
            key=lambda row: (row["id"], row.get("_requestChunkIndex", 0)),
        )
        payload["chunkPlan"] = {
            str(request.sample_id): request.total_chunks for request in requests
        }
    atomic_json(args.output, payload)
    print(json.dumps({"outputs": len(outputs), "failures": len(failures), "path": str(args.output)}, indent=2))


def cmd_run(args) -> None:
    if not args.confirm:
        raise RuntimeError("Generation creates paid API requests. Re-run with --confirm.")
    config = load_config(args.model_config)
    if config.mode != "direct":
        raise ValueError("run requires execution.mode: direct; use prepare/submit/collect for batch mode")
    samples = selected_samples(args)
    task, requests = build_requests(args, samples)
    provider = make_provider(config)
    outputs, failures = [], []

    def execute(request: PreparedRequest):
        started = time.monotonic()
        try:
            output, _ = provider.generate(request, task)
            return output, None
        except Exception as exc:
            failure = {
                "id": request.sample_id,
                "customId": request.custom_id,
                "errorType": type(exc).__name__,
                "errorMessage": str(exc),
                "latencySeconds": round(time.monotonic() - started, 3),
            }
            raw = getattr(exc, "raw_text", None)
            if raw is not None:
                failure["rawResponse"] = raw
            return None, failure

    with ThreadPoolExecutor(max_workers=config.workers) as executor:
        futures = {executor.submit(execute, request): request for request in requests}
        for future in as_completed(futures):
            output, failure = future.result()
            if output is not None:
                outputs.append(output)
            else:
                failures.append(failure)
            final_outputs, final_failures = finalize_outputs(task, requests, outputs, failures)
            payload = {
                "benchmark": "CineSubBench",
                "model": config.name,
                "provider": config.provider,
                "modelId": config.model_id,
                "task": task,
                "inputLanguage": LANGUAGE_NAMES[args.language],
                "outputs" if task != "narrative_judge" else "judgments": sorted(final_outputs, key=lambda row: row["id"]),
                "failures": sorted(final_failures, key=lambda row: row["id"]),
            }
            if task == "language_content":
                payload["partialOutputs"] = sorted(
                    outputs,
                    key=lambda row: (row["id"], row.get("_requestChunkIndex", 0)),
                )
                payload["chunkPlan"] = {
                    str(request.sample_id): request.total_chunks for request in requests
                }
            atomic_json(args.output, payload)
            print(f"completed={len(outputs)} failed={len(failures)} total={len(requests)}", flush=True)


def cmd_evaluate(args) -> None:
    gold = load_dataset(args.gold)
    payload = evaluate_all(
        gold=gold,
        model=args.model,
        language=args.language,
        narrative=read_json(args.narrative),
        cultural=read_json(args.cultural),
        language_content=read_json(args.language_content),
        judgments=read_json(args.judgments),
    )
    atomic_json(args.output, payload)
    print(json.dumps(payload, indent=2, allow_nan=False))


def cmd_leaderboard(args) -> None:
    summaries = [read_json(path) for path in args.metrics]
    unknown_languages = sorted(set(args.languages) - set(LANGUAGE_NAMES))
    if unknown_languages:
        raise ValueError(f"Unsupported leaderboard language codes: {unknown_languages}")
    pairs = [(summary.get("model"), summary.get("language")) for summary in summaries]
    if len(pairs) != len(set(pairs)):
        raise ValueError("Metric inputs contain duplicate model-language summaries")
    rows = composite_leaderboard(summaries, tuple(args.languages))
    if not rows:
        raise RuntimeError("No model has complete requested-language metrics and English LC strict F1")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["rank", "model", "composite_score", "normalized_narrative", "genre_micro_f1_pct", "normalized_age", "cultural_score", "lc_strict_f1_pct", "narrative_overall_1to5", "age_mae_years", "country_exact_match_pct"]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: round(value, 4) if isinstance(value, float) else value for key, value in row.items()})
    calculation_details = args.output.with_suffix(".calculation_details.json")
    atomic_json(calculation_details, {
        "languages": args.languages,
        "formula": {
            "normalizedNarrative": "25 * (narrativeOverall1to5 - 1)",
            "normalizedAge": "100 * (1 - min(ageMAEYears, 16) / 16)",
            "cultural": "(normalizedAge + countryExactMatchPct) / 2",
            "composite": "mean(normalizedNarrative, genreMicroF1Pct, cultural, EnglishLCStrictF1Pct)",
        },
        "rows": rows,
    })
    print(json.dumps({
        "models": len(rows),
        "output": str(args.output),
        "calculationDetails": str(calculation_details),
    }, indent=2))


def cmd_recover(args) -> None:
    payload = read_json(args.input)
    schema = task_assets(RELEASE_ROOT, args.task)[1] if args.task != "narrative_judge" else read_json(
        RELEASE_ROOT / "schemas/narrative_generation/narrative_judge_output.schema.json"
    )
    outputs = {row["id"]: row for row in payload.get("outputs", payload.get("judgments", []))}
    partial_outputs = list(payload.get("partialOutputs", []))
    remaining, recovered = [], []
    for failure in payload.get("failures", []):
        if args.task == "language_content" and failure.get("errorType") == "IncompleteLanguageContentChunks":
            continue
        raw = failure.get("rawResponse")
        if raw is None and failure.get("rawResponseFile"):
            raw = Path(failure["rawResponseFile"]).read_text(encoding="utf-8")
        try:
            if raw is None:
                raise ValueError("no saved raw response")
            row = recover_response(raw if isinstance(raw, str) else json.dumps(raw), args.task, int(failure["id"]), schema)
            if args.task == "language_content" and failure.get("customId"):
                chunk_index = int(str(failure["customId"]).rsplit("-chunk-", 1)[1])
                row["_requestCustomId"] = failure["customId"]
                row["_requestChunkIndex"] = chunk_index
                row["_requestTotalChunks"] = int(payload.get("chunkPlan", {}).get(str(row["id"]), 1))
                partial_outputs = [
                    existing for existing in partial_outputs
                    if existing.get("_requestCustomId") != failure["customId"]
                ]
                partial_outputs.append(row)
            else:
                outputs[row["id"]] = row
            recovered.append(row["id"])
        except Exception as exc:
            kept = dict(failure)
            kept["recoveryError"] = f"{type(exc).__name__}: {exc}"
            remaining.append(kept)
    key = "judgments" if args.task == "narrative_judge" else "outputs"
    if args.task == "language_content":
        recovery_requests = [
            PreparedRequest(
                str(row.get("_requestCustomId", "")),
                int(row["id"]),
                "",
                "",
                {},
                int(row.get("_requestChunkIndex", 0)),
                int(row.get("_requestTotalChunks", payload.get("chunkPlan", {}).get(str(row["id"]), 1))),
            )
            for row in partial_outputs
        ]
        combined, incomplete = finalize_outputs("language_content", recovery_requests, partial_outputs, remaining)
        outputs.update({row["id"]: row for row in combined})
        remaining = incomplete
        payload["partialOutputs"] = sorted(
            partial_outputs,
            key=lambda row: (row["id"], row.get("_requestChunkIndex", 0)),
        )
    payload[key] = sorted(outputs.values(), key=lambda row: row["id"])
    payload["failures"] = remaining
    payload.setdefault("recoveryLog", []).append({
        "policy": "Structural recovery only; no semantic prediction, label, evidence index, or score was invented or changed.",
        "initialFailures": len(payload.get("failures", [])) + len(recovered),
        "recovered": len(recovered),
        "remainingFailures": len(remaining),
        "recoveredSampleIds": recovered,
    })
    atomic_json(args.output, payload)
    print(json.dumps({"recovered": len(recovered), "remaining": len(remaining), "output": str(args.output)}, indent=2))


def add_selection(parser):
    parser.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help=f"Dataset source (default: {DEFAULT_DATASET})",
    )
    parser.add_argument("--language", choices=sorted(LANGUAGE_NAMES), default="en")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--sample-ids", nargs="*", type=int)


def main() -> None:
    parser = argparse.ArgumentParser(prog="cinesubbench")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Validate configuration and dataset without an API request")
    doctor.add_argument("--model-config", type=Path, required=True)
    doctor.add_argument(
        "--dataset",
        default=DEFAULT_DATASET,
        help=f"Dataset source (default: {DEFAULT_DATASET})",
    )
    doctor.add_argument("--require-key", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    for name, function in (("prepare", cmd_prepare), ("run", cmd_run)):
        command = commands.add_parser(name)
        command.add_argument("--model-config", type=Path, required=True)
        command.add_argument("--task", choices=["narrative", "cultural", "language_content", "narrative_judge"], required=True)
        command.add_argument("--predictions", type=Path, help="Narrative outputs required for narrative_judge")
        add_selection(command)
        if name == "prepare":
            command.add_argument("--run-dir", type=Path, required=True)
        else:
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--confirm", action="store_true")
        command.set_defaults(func=function)

    submit = commands.add_parser("submit")
    submit.add_argument("--model-config", type=Path, required=True)
    submit.add_argument("--run-dir", type=Path, required=True)
    submit.add_argument("--confirm", action="store_true")
    submit.set_defaults(func=cmd_submit)
    status = commands.add_parser("status")
    status.add_argument("--model-config", type=Path, required=True)
    status.add_argument("--run-dir", type=Path, required=True)
    status.set_defaults(func=cmd_status)
    collect = commands.add_parser("collect")
    collect.add_argument("--model-config", type=Path, required=True)
    collect.add_argument("--run-dir", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    collect.set_defaults(func=cmd_collect)

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument(
        "--gold",
        default=DEFAULT_DATASET,
        help=f"Gold dataset source (default: {DEFAULT_DATASET})",
    )
    evaluate.add_argument("--model", required=True)
    evaluate.add_argument("--language", choices=sorted(LANGUAGE_NAMES), required=True)
    evaluate.add_argument("--narrative", type=Path)
    evaluate.add_argument("--cultural", type=Path)
    evaluate.add_argument("--language-content", type=Path)
    evaluate.add_argument("--judgments", type=Path)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.set_defaults(func=cmd_evaluate)

    leaderboard = commands.add_parser("leaderboard")
    leaderboard.add_argument("--metrics", type=Path, nargs="+", required=True)
    leaderboard.add_argument("--languages", nargs="+", default=["en", "ar", "id", "fa", "ro", "vi"])
    leaderboard.add_argument("--output", type=Path, required=True)
    leaderboard.set_defaults(func=cmd_leaderboard)

    recover = commands.add_parser("recover")
    recover.add_argument("--input", type=Path, required=True)
    recover.add_argument("--task", choices=["narrative", "cultural", "language_content", "narrative_judge"], required=True)
    recover.add_argument("--output", type=Path, required=True)
    recover.set_defaults(func=cmd_recover)

    args = parser.parse_args()
    if getattr(args, "task", None) == "narrative_judge" and not getattr(args, "predictions", None):
        parser.error("--predictions is required for narrative_judge")
    args.func(args)
