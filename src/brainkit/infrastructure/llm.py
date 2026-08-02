from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from importlib.resources import files
from typing import Any, Protocol, Sequence

from brainkit.domain.model import PolicyError, PrivacyMode, ValidationError, VaultConfig


SUPPORTED_PROVIDERS = {"anthropic", "openai", "openrouter", "ollama"}

#: Request options sent to Ollama unless `providers.ollama.options` overrides
#: them. Ollama defaults `num_ctx` to 4096 regardless of what the model
#: advertises, which truncates judgment prompts long before the model's real
#: window; 16384 covers a bounded evidence bundle plus job scaffolding and
#: repair feedback without forcing a large KV-cache allocation. `temperature`
#: stays at 0 because judgment output is schema-bound.
DEFAULT_OLLAMA_OPTIONS: dict[str, Any] = {"temperature": 0, "num_ctx": 16384}


class ProviderDriver(Protocol):
    def complete(
        self, prompt: str, *, model: str, output_schema: dict[str, Any] | None
    ) -> str: ...


class JobSpecs:
    def prompt(self, job: str, variables: dict[str, Any]) -> str:
        resource = files("brainkit").joinpath("jobs", f"{job}.md")
        if not resource.is_file():
            raise ValidationError("Unknown judgment job", details={"job": job})
        template = resource.read_text(encoding="utf-8")
        placeholders = set(re.findall(r"\{\{([a-zA-Z0-9_]+)\}\}", template))
        missing = sorted(placeholders - variables.keys())
        if missing:
            raise ValidationError(
                "Job variables are incomplete",
                details={"job": job, "missing": missing},
            )
        for key, value in variables.items():
            template = template.replace("{{" + key + "}}", str(value))
        return template

    def schema(self, job: str) -> dict[str, Any] | None:
        resource = files("brainkit").joinpath(
            "jobs", "_output-schemas", f"{job}.json"
        )
        if not resource.is_file():
            return None
        return json.loads(resource.read_text(encoding="utf-8"))


class PolicyJudgmentRouter:
    """Selects a provider per branch privacy policy and job mapping."""

    def __init__(self, config: VaultConfig, jobs: JobSpecs):
        self.config = config
        self.jobs = jobs

    def run(
        self,
        *,
        job: str,
        branches: Sequence[str],
        variables: dict[str, Any],
        output_schema: dict[str, Any] | None = None,
    ) -> str:
        if not branches:
            branches = ["_inbox"]
        policies = {
            branch: (
                self.config.inbox_policy
                if branch == "_inbox"
                else self.config.branches.get(branch)
            )
            for branch in set(branches)
        }
        missing = sorted(branch for branch, policy in policies.items() if policy is None)
        if missing:
            raise PolicyError(
                "No privacy policy exists for one or more branches",
                details={"branches": missing},
            )
        never_ingest = sorted(
            branch
            for branch, policy in policies.items()
            if policy and policy.privacy == PrivacyMode.NEVER_INGEST
        )
        if never_ingest:
            raise PolicyError(
                "Branch policy forbids judgment ingestion",
                details={"branches": never_ingest},
            )
        effective_privacy = (
            PrivacyMode.LOCAL_ONLY
            if any(
                policy and policy.privacy == PrivacyMode.LOCAL_ONLY
                for policy in policies.values()
            )
            else PrivacyMode.CLOUD
        )
        mapping = self.config.job_models.get(job)
        if not mapping:
            raise ValidationError(
                "No model is configured for this job", details={"job": job}
            )
        route_mapping = mapping.get(effective_privacy.value, mapping)
        if not isinstance(route_mapping, dict):
            raise ValidationError(
                "Job privacy route is invalid",
                details={"job": job, "privacy": effective_privacy.value},
            )
        provider_name = route_mapping.get("provider")
        model = route_mapping.get("model")
        if provider_name not in SUPPORTED_PROVIDERS or not model:
            raise ValidationError(
                "Job model mapping is invalid", details={"job": job}
            )
        if (
            effective_privacy == PrivacyMode.LOCAL_ONLY
            and provider_name != "ollama"
        ):
            raise PolicyError(
                "Local-only content can only be routed to Ollama",
                details={"branches": sorted(policies), "provider": provider_name},
            )
        provider_config = self.config.providers.get(provider_name)
        if not isinstance(provider_config, dict):
            raise ValidationError(
                "Selected provider is not configured",
                details={"provider": provider_name},
            )
        prompt = self.jobs.prompt(
            job,
            {
                "wiki_language": self.config.wiki_language,
                **variables,
            },
        )
        driver = _create_driver(provider_name, provider_config)
        return driver.complete(
            prompt,
            model=model,
            output_schema=output_schema or self.jobs.schema(job),
        )


class OpenAICompatibleDriver:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 120,
    ):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.timeout = timeout

    def complete(
        self, prompt: str, *, model: str, output_schema: dict[str, Any] | None
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if output_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "brainkit_output",
                    "strict": True,
                    "schema": output_schema,
                },
            }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        response = _post_json(self.url, payload, headers, self.timeout)
        try:
            return str(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ValidationError("Provider returned an unexpected response") from exc


class AnthropicDriver:
    def __init__(self, *, base_url: str, api_key: str, timeout: float = 120):
        self.url = base_url.rstrip("/") + "/messages"
        self.api_key = api_key
        self.timeout = timeout

    def complete(
        self, prompt: str, *, model: str, output_schema: dict[str, Any] | None
    ) -> str:
        if output_schema:
            prompt += (
                "\n\nReturn only JSON matching this schema:\n"
                + json.dumps(output_schema, ensure_ascii=False)
            )
        response = _post_json(
            self.url,
            {
                "model": model,
                "max_tokens": 8192,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            self.timeout,
        )
        try:
            return "".join(
                block["text"]
                for block in response["content"]
                if block.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:
            raise ValidationError("Provider returned an unexpected response") from exc


class OllamaDriver:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = 120,
        options: dict[str, Any] | None = None,
    ):
        self.url = base_url.rstrip("/") + "/api/chat"
        self.timeout = timeout
        self.options = {**DEFAULT_OLLAMA_OPTIONS, **(options or {})}

    def complete(
        self, prompt: str, *, model: str, output_schema: dict[str, Any] | None
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "options": dict(self.options),
        }
        if output_schema:
            payload["format"] = output_schema
        response = _post_json(
            self.url, payload, {"Content-Type": "application/json"}, self.timeout
        )
        try:
            return str(response["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise ValidationError("Provider returned an unexpected response") from exc


def _create_driver(name: str, config: dict[str, Any]) -> ProviderDriver:
    timeout = float(config.get("timeout_seconds", 120))
    if name == "ollama":
        base_url = _required(config, "base_url", provider=name)
        return OllamaDriver(
            base_url=base_url,
            timeout=timeout,
            options=_ollama_options(config),
        )
    api_key_env = _required(config, "api_key_env", provider=name)
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValidationError(
            "Provider API key environment variable is not set",
            details={"provider": name, "api_key_env": api_key_env},
        )
    if name == "anthropic":
        return AnthropicDriver(
            base_url=_required(config, "base_url", provider=name),
            api_key=api_key,
            timeout=timeout,
        )
    return OpenAICompatibleDriver(
        base_url=_required(config, "base_url", provider=name),
        api_key=api_key,
        extra_headers={
            str(key): str(value)
            for key, value in config.get("headers", {}).items()
        },
        timeout=timeout,
    )


def _ollama_options(config: dict[str, Any]) -> dict[str, Any]:
    """Read the operator-supplied Ollama request options.

    Values are never echoed back in errors; only the key names are structural.
    """

    options = config.get("options", {})
    if not isinstance(options, dict):
        raise ValidationError(
            "Ollama provider options must be an object",
            details={"provider": "ollama"},
        )
    return {str(key): value for key, value in options.items()}


def _required(config: dict[str, Any], field: str, *, provider: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value:
        raise ValidationError(
            "Provider configuration is incomplete",
            details={"provider": provider, "missing": field},
        )
    return value


def _post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    raw = b""
    last_error: Exception | None = None
    for attempt in range(3):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            last_error = None
            break
        except urllib.error.HTTPError as exc:
            body = exc.read(2_000).decode("utf-8", errors="replace")
            last_error = ValidationError(
                "Provider request failed",
                details={"status": exc.code, "response": body},
            )
            if exc.code != 429 and exc.code < 500:
                raise last_error from exc
            if attempt == 2:
                raise last_error from exc
            retry_after = exc.headers.get("Retry-After")
            delay = (
                min(float(retry_after), 8.0)
                if retry_after and retry_after.replace(".", "", 1).isdigit()
                else float(2**attempt)
            )
            time.sleep(delay)
        except urllib.error.URLError as exc:
            last_error = ValidationError(
                "Provider is unreachable", details={"reason": str(exc.reason)}
            )
            if attempt == 2:
                raise last_error from exc
            time.sleep(float(2**attempt))
    if last_error:
        raise last_error
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("Provider returned invalid JSON") from exc
