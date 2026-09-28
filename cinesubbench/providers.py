"""Provider adapters for OpenAI, Google Gemini, and Anthropic."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable

from .config import ModelConfig
from .decoding import decoding_for
from .recovery import raw_text_from_payload, recover_response
from .tasks import PreparedRequest


class ResponseParseError(ValueError):
    def __init__(self, message: str, raw_text: str):
        super().__init__(message)
        self.raw_text = raw_text


def _recover(raw: str, task: str, request: PreparedRequest) -> dict[str, Any]:
    try:
        row = recover_response(raw, task, request.sample_id, request.schema)
        row["_requestCustomId"] = request.custom_id
        row["_requestChunkIndex"] = request.chunk_index
        row["_requestTotalChunks"] = request.total_chunks
        return row
    except Exception as exc:
        raise ResponseParseError(f"{type(exc).__name__}: {exc}", raw) from exc


class Provider(ABC):
    def __init__(self, config: ModelConfig):
        self.config = config

    @abstractmethod
    def generate(self, request: PreparedRequest, task: str) -> tuple[dict[str, Any], str]:
        """Return normalized JSON and the raw provider text."""

    @abstractmethod
    def submit_batch(self, requests: list[PreparedRequest], task: str, work_dir: Path) -> str:
        """Submit one provider batch and return its identifier."""

    @abstractmethod
    def batch_status(self, batch_id: str) -> dict[str, Any]:
        """Return a JSON-serializable provider status."""

    @abstractmethod
    def collect_batch(
        self,
        batch_id: str,
        requests_by_id: dict[str, PreparedRequest],
        task: str,
        work_dir: Path,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return normalized outputs and failures."""


def _schema(wrapper: dict[str, Any]) -> dict[str, Any]:
    return wrapper.get("schema", wrapper)


class OpenAIProvider(Provider):
    def __init__(self, config: ModelConfig):
        super().__init__(config)
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install the 'openai' package from requirements.txt") from exc
        self.client = OpenAI(
            api_key=config.api_key(),
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )

    def _body(self, request: PreparedRequest, task: str) -> dict[str, Any]:
        decoding = decoding_for(task)
        return {
            "model": self.config.model_id,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": decoding["temperature"],
            "max_completion_tokens": decoding["max_output_tokens"],
            "response_format": {"type": "json_schema", "json_schema": request.schema},
        }

    def generate(self, request: PreparedRequest, task: str) -> tuple[dict[str, Any], str]:
        response = self.client.chat.completions.create(**self._body(request, task))
        raw = response.choices[0].message.content or ""
        return _recover(raw, task, request), raw

    def submit_batch(self, requests: list[PreparedRequest], task: str, work_dir: Path) -> str:
        path = work_dir / "openai_requests.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for request in requests:
                row = {
                    "custom_id": request.custom_id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": self._body(request, task),
                }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        with path.open("rb") as handle:
            uploaded = self.client.files.create(file=handle, purpose="batch")
        batch = self.client.batches.create(
            input_file_id=uploaded.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
            metadata={"benchmark": "CineSubBench", "task": task},
        )
        return batch.id

    def batch_status(self, batch_id: str) -> dict[str, Any]:
        batch = self.client.batches.retrieve(batch_id)
        return batch.model_dump(mode="json")

    def collect_batch(self, batch_id, requests_by_id, task, work_dir):
        batch = self.client.batches.retrieve(batch_id)
        if batch.status != "completed":
            raise RuntimeError(f"OpenAI batch is not complete: {batch.status}")
        text = self.client.files.content(batch.output_file_id).text
        outputs, failures = [], []
        for line in text.splitlines():
            row = json.loads(line)
            custom_id = row.get("custom_id")
            request = requests_by_id.get(custom_id)
            raw = None
            try:
                raw = row["response"]["body"]["choices"][0]["message"]["content"]
                outputs.append(_recover(raw, task, request))
            except Exception as exc:
                failures.append({
                    "customId": custom_id,
                    "id": request.sample_id if request else None,
                    "errorType": type(exc).__name__,
                    "errorMessage": str(exc),
                    "rawResponse": raw if raw is not None else row,
                })
        return outputs, failures


class AnthropicProvider(Provider):
    def __init__(self, config: ModelConfig):
        super().__init__(config)
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("Install the 'anthropic' package from requirements.txt") from exc
        self.anthropic = anthropic
        self.client = anthropic.Anthropic(
            api_key=config.api_key(),
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )

    def _params(self, request: PreparedRequest, task: str) -> dict[str, Any]:
        decoding = decoding_for(task)
        schema_instruction = (
            "\n\nReturn only JSON satisfying this JSON Schema:\n"
            + json.dumps(_schema(request.schema), ensure_ascii=False)
        )
        return {
            "model": self.config.model_id,
            "max_tokens": decoding["max_output_tokens"],
            "temperature": decoding["temperature"],
            "system": request.system,
            "messages": [{"role": "user", "content": request.user + schema_instruction}],
        }

    def generate(self, request: PreparedRequest, task: str) -> tuple[dict[str, Any], str]:
        response = self.client.messages.create(**self._params(request, task))
        raw = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        return _recover(raw, task, request), raw

    def submit_batch(self, requests: list[PreparedRequest], task: str, work_dir: Path) -> str:
        batch = self.client.messages.batches.create(
            requests=[{"custom_id": request.custom_id, "params": self._params(request, task)} for request in requests]
        )
        return batch.id

    def batch_status(self, batch_id: str) -> dict[str, Any]:
        return self.client.messages.batches.retrieve(batch_id).model_dump(mode="json")

    def collect_batch(self, batch_id, requests_by_id, task, work_dir):
        outputs, failures = [], []
        for result in self.client.messages.batches.results(batch_id):
            custom_id = result.custom_id
            request = requests_by_id.get(custom_id)
            raw = None
            try:
                if result.result.type != "succeeded":
                    raise RuntimeError(f"Anthropic result status: {result.result.type}")
                message = result.result.message
                raw = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
                outputs.append(_recover(raw, task, request))
            except Exception as exc:
                failures.append({
                    "customId": custom_id,
                    "id": request.sample_id if request else None,
                    "errorType": type(exc).__name__,
                    "errorMessage": str(exc),
                    "rawResponse": raw if raw is not None else result.model_dump(mode="json"),
                })
        return outputs, failures


class GeminiProvider(Provider):
    def __init__(self, config: ModelConfig):
        super().__init__(config)
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("Install the 'google-genai' package from requirements.txt") from exc
        self.types = types
        self.client = genai.Client(api_key=config.api_key())

    def _generation_config(self, request: PreparedRequest, task: str):
        decoding = decoding_for(task)
        return self.types.GenerateContentConfig(
            system_instruction=request.system,
            temperature=decoding["temperature"],
            max_output_tokens=decoding["max_output_tokens"],
            response_mime_type="application/json",
            response_json_schema=_schema(request.schema),
        )

    def generate(self, request: PreparedRequest, task: str) -> tuple[dict[str, Any], str]:
        response = self.client.models.generate_content(
            model=self.config.model_id,
            contents=request.user,
            config=self._generation_config(request, task),
        )
        raw = response.text or ""
        return _recover(raw, task, request), raw

    def submit_batch(self, requests: list[PreparedRequest], task: str, work_dir: Path) -> str:
        decoding = decoding_for(task)
        path = work_dir / "gemini_requests.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for request in requests:
                row = {
                    "key": request.custom_id,
                    "request": {
                        "contents": [{"role": "user", "parts": [{"text": request.user}]}],
                        "system_instruction": {"parts": [{"text": request.system}]},
                        "generation_config": {
                            "temperature": decoding["temperature"],
                            "max_output_tokens": decoding["max_output_tokens"],
                            "response_mime_type": "application/json",
                            "response_json_schema": _schema(request.schema),
                        },
                    },
                }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        uploaded = self.client.files.upload(file=str(path), config={"mime_type": "jsonl"})
        batch = self.client.batches.create(
            model=self.config.model_id,
            src=uploaded.name,
            config={"display_name": f"cinesubbench-{task}"},
        )
        return batch.name

    def batch_status(self, batch_id: str) -> dict[str, Any]:
        batch = self.client.batches.get(name=batch_id)
        return batch.model_dump(mode="json") if hasattr(batch, "model_dump") else dict(batch)

    def collect_batch(self, batch_id, requests_by_id, task, work_dir):
        batch = self.client.batches.get(name=batch_id)
        state = str(getattr(batch, "state", ""))
        if "SUCCEEDED" not in state.upper():
            raise RuntimeError(f"Gemini batch is not complete: {state}")
        destination = getattr(batch, "dest", None)
        file_name = getattr(destination, "file_name", None)
        if not file_name:
            raise RuntimeError("Gemini batch did not expose a file destination")
        downloaded = self.client.files.download(file=file_name)
        text = downloaded.decode("utf-8") if isinstance(downloaded, bytes) else str(downloaded)
        outputs, failures = [], []
        ordered_requests = list(requests_by_id.items())
        lines = [line for line in text.splitlines() if line.strip()]
        for line_index, line in enumerate(lines):
            row = json.loads(line)
            custom_id = row.get("key")
            if custom_id is None and line_index < len(ordered_requests):
                custom_id = ordered_requests[line_index][0]
            request = requests_by_id.get(custom_id)
            raw = None
            try:
                response = row.get("response", row)
                raw = response["candidates"][0]["content"]["parts"][0]["text"]
                outputs.append(_recover(raw, task, request))
            except Exception as exc:
                failures.append({
                    "customId": custom_id,
                    "id": request.sample_id if request else None,
                    "errorType": type(exc).__name__,
                    "errorMessage": str(exc),
                    "rawResponse": raw if raw is not None else row,
                })
        return outputs, failures


def make_provider(config: ModelConfig) -> Provider:
    return {
        "openai": OpenAIProvider,
        "gemini": GeminiProvider,
        "anthropic": AnthropicProvider,
    }[config.provider](config)
