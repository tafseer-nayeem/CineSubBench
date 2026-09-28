import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cinesubbench.decoding import DECODING_POLICY
from cinesubbench.cli import cmd_recover, finalize_outputs
from cinesubbench.metrics import (
    _normalize_genres,
    composite_leaderboard,
    cultural_metrics,
    genre_metrics,
    language_content_metrics,
)
from cinesubbench.providers import AnthropicProvider, GeminiProvider, OpenAIProvider
from cinesubbench.recovery import recover_response
from cinesubbench.tasks import PreparedRequest, prepare_requests


ROOT = Path(__file__).resolve().parents[1]


def test_sample_has_public_schema_only():
    samples = json.loads((ROOT / "dataset/CineSubBench.sample20.json").read_text(encoding="utf-8"))
    assert len(samples) == 20
    assert len({sample["id"] for sample in samples}) == 20
    forbidden = {"plotSynopsisRaw", "storylineRaw", "labelPolicy", "kidsInMindLanguageAssessment"}
    for sample in samples:
        assert not (set(sample) & forbidden)
        assert "languageContentAssessment" in sample
        assert "motionPictureRatings" in sample


def test_recovery_is_structural():
    schema = json.loads((ROOT / "schemas/narrative_generation/narrative_generation_output.schema.json").read_text())
    raw = '```json\n{"id": 9, "plot": "p", "synopsis": "s", "keyMessage": "k", "genres": ["Drama"],}\n```'
    recovered = recover_response(raw, "narrative", 9, schema)
    assert recovered == {"id": 9, "plot": "p", "synopsis": "s", "keyMessage": "k", "genres": ["Drama"]}


def test_decoding_policy_is_provider_independent():
    assert DECODING_POLICY["narrative"] == {"temperature": 0.0, "max_output_tokens": 1200}
    assert DECODING_POLICY["cultural"] == {"temperature": 0.0, "max_output_tokens": 1400}
    assert DECODING_POLICY["language_content"] == {"temperature": 0.0, "max_output_tokens": 2500}


def test_composite_formula_and_sorting():
    rows = []
    for model, narrative in (("A", 4.0), ("B", 3.0)):
        for language in ("en", "ar", "id", "fa", "ro", "vi"):
            metrics = {
                "narrative_overall_1to5": narrative,
                "genre_micro_f1_pct": 80.0,
                "age_mae_years": 1.0,
                "country_exact_match_pct": 70.0,
            }
            if language == "en":
                metrics["lc_strict_f1_pct"] = 60.0
                metrics["lc_completion_pct"] = 100.0
            metrics["narrative_completion_pct"] = 50.0 if model == "B" else 100.0
            metrics["cultural_completion_pct"] = 100.0
            metrics["judge_completion_pct"] = 100.0
            rows.append({"model": model, "language": language, "metrics": metrics})
    leaderboard = composite_leaderboard(rows)
    assert [row["model"] for row in leaderboard] == ["A"]
    assert leaderboard[0]["rank"] == 1


def test_language_content_is_chunked_and_combined_only_when_complete():
    sample = json.loads((ROOT / "dataset/CineSubBench.sample20.json").read_text(encoding="utf-8"))[0]
    requests = prepare_requests(ROOT, "language_content", sample, "en")
    assert len(requests) > 1
    assert {request.total_chunks for request in requests} == {len(requests)}

    def chunk_output(request):
        return {
            "id": sample["id"],
            "strongProfanityCount": 1,
            "strongProfanityIndices": [request.chunk_index],
            "crudeBodilyLanguageCount": 0,
            "crudeBodilyLanguageIndices": [],
            "mildObscenityCount": 0,
            "mildObscenityIndices": [],
            "religiousProfanityAndExclamationCount": 0,
            "religiousProfanityAndExclamationIndices": [],
            "_requestChunkIndex": request.chunk_index,
        }

    partial, failures = finalize_outputs(
        "language_content", requests, [chunk_output(requests[0])], []
    )
    assert partial == []
    assert failures[0]["errorType"] == "IncompleteLanguageContentChunks"

    combined, failures = finalize_outputs(
        "language_content", requests, [chunk_output(request) for request in requests], []
    )
    assert failures == []
    assert combined[0]["strongProfanityCount"] == len(requests)
    assert combined[0]["strongProfanityIndices"] == list(range(len(requests)))


def test_missing_language_content_output_is_scored_as_missing_evidence():
    sample = json.loads((ROOT / "dataset/CineSubBench.sample20.json").read_text(encoding="utf-8"))[0]
    result = language_content_metrics([sample], {"outputs": []})
    assert result["lc_agnostic_recall_pct"] == 0.0
    assert result["lc_count_mae"] is not None


def test_perfect_structured_predictions_receive_perfect_scores():
    gold = json.loads((ROOT / "dataset/CineSubBench.sample20.json").read_text(encoding="utf-8"))
    narrative = {"outputs": [
        {"id": sample["id"], "genres": sorted(_normalize_genres(sample["genres"])[0])}
        for sample in gold
    ]}
    cultural = {"outputs": [{
        "id": sample["id"],
        "ageSuitabilityPrediction": {"ageRating": sample["ageSuitabilityRating"]},
        "countryMotionPictureRatingPrediction": [
            {"country": entry["country"], "rating": entry["label"]}
            for entry in sample["motionPictureRatings"]
        ],
    } for sample in gold]}
    genre = genre_metrics(gold, narrative)
    culture = cultural_metrics(gold, cultural)
    assert genre["genre_exact_match_pct"] == 100.0
    assert genre["genre_micro_f1_pct"] == 100.0
    assert culture["age_exact_match_pct"] == 100.0
    assert culture["age_mae_years"] == 0
    assert culture["country_exact_match_pct"] == 100.0
    assert culture["country_ordinal_mae"] == 0


def test_chunk_recovery_rebuilds_complete_film_output():
    partial = {
        "id": 7,
        "strongProfanityCount": 1,
        "strongProfanityIndices": [3],
        "crudeBodilyLanguageCount": 0,
        "crudeBodilyLanguageIndices": [],
        "mildObscenityCount": 0,
        "mildObscenityIndices": [],
        "religiousProfanityAndExclamationCount": 0,
        "religiousProfanityAndExclamationIndices": [],
        "_requestCustomId": "language_content-en-7-chunk-0",
        "_requestChunkIndex": 0,
        "_requestTotalChunks": 2,
    }
    recovered_chunk = {
        "id": 7,
        "strongProfanityCount": 1,
        "strongProfanityIndices": [503],
        "crudeBodilyLanguageCount": 0,
        "crudeBodilyLanguageIndices": [],
        "mildObscenityCount": 0,
        "mildObscenityIndices": [],
        "religiousProfanityAndExclamationCount": 0,
        "religiousProfanityAndExclamationIndices": [],
    }
    with TemporaryDirectory() as directory:
        source = Path(directory) / "source.json"
        destination = Path(directory) / "recovered.json"
        source.write_text(json.dumps({
            "outputs": [],
            "partialOutputs": [partial],
            "chunkPlan": {"7": 2},
            "failures": [{
                "id": 7,
                "customId": "language_content-en-7-chunk-1",
                "rawResponse": json.dumps(recovered_chunk),
            }, {
                "id": 7,
                "errorType": "IncompleteLanguageContentChunks",
                "errorMessage": "Missing subtitle chunks: [1]",
            }],
        }), encoding="utf-8")
        cmd_recover(SimpleNamespace(input=source, output=destination, task="language_content"))
        payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["failures"] == []
    assert payload["outputs"][0]["strongProfanityCount"] == 2
    assert payload["outputs"][0]["strongProfanityIndices"] == [3, 503]


def test_gemini_batch_results_fall_back_to_request_order():
    schema = json.loads(
        (ROOT / "schemas/narrative_generation/narrative_generation_output.schema.json").read_text()
    )
    requests = {
        f"narrative-en-{sample_id}": PreparedRequest(
            f"narrative-en-{sample_id}", sample_id, "system", "user", schema
        )
        for sample_id in (4, 10)
    }
    lines = []
    for sample_id in (4, 10):
        answer = json.dumps({
            "id": sample_id,
            "plot": "Plot",
            "synopsis": "Synopsis",
            "keyMessage": "Message",
            "genres": ["Drama"],
        })
        lines.append(json.dumps({
            "response": {"candidates": [{"content": {"parts": [{"text": answer}]}}]}
        }))

    class Files:
        @staticmethod
        def download(file):
            return "\n".join(lines).encode()

    class Batches:
        @staticmethod
        def get(name):
            return SimpleNamespace(state="JOB_STATE_SUCCEEDED", dest=SimpleNamespace(file_name="files/result"))

    provider = object.__new__(GeminiProvider)
    provider.client = SimpleNamespace(files=Files(), batches=Batches())
    outputs, failures = provider.collect_batch("batch/1", requests, "narrative", Path("."))
    assert failures == []
    assert [row["id"] for row in outputs] == [4, 10]
    assert [row["_requestCustomId"] for row in outputs] == list(requests)


def test_openai_and_anthropic_batch_results_are_collected():
    schema = json.loads(
        (ROOT / "schemas/narrative_generation/narrative_generation_output.schema.json").read_text()
    )
    request = PreparedRequest("narrative-en-4", 4, "system", "user", schema)
    answer = json.dumps({
        "id": 4,
        "plot": "Plot",
        "synopsis": "Synopsis",
        "keyMessage": "Message",
        "genres": ["Drama"],
    })

    openai_line = json.dumps({
        "custom_id": request.custom_id,
        "response": {"body": {"choices": [{"message": {"content": answer}}]}},
    })
    openai_provider = object.__new__(OpenAIProvider)
    openai_provider.client = SimpleNamespace(
        batches=SimpleNamespace(retrieve=lambda batch_id: SimpleNamespace(
            status="completed", output_file_id="file/result"
        )),
        files=SimpleNamespace(content=lambda file_id: SimpleNamespace(text=openai_line)),
    )
    outputs, failures = openai_provider.collect_batch(
        "batch_1", {request.custom_id: request}, "narrative", Path(".")
    )
    assert failures == [] and outputs[0]["id"] == 4

    result = SimpleNamespace(
        custom_id=request.custom_id,
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(content=[SimpleNamespace(type="text", text=answer)]),
        ),
    )
    anthropic_provider = object.__new__(AnthropicProvider)
    anthropic_provider.client = SimpleNamespace(
        messages=SimpleNamespace(
            batches=SimpleNamespace(results=lambda batch_id: [result])
        )
    )
    outputs, failures = anthropic_provider.collect_batch(
        "msgbatch_1", {request.custom_id: request}, "narrative", Path(".")
    )
    assert failures == [] and outputs[0]["id"] == 4
