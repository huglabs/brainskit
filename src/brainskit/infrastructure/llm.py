from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol
from urllib.parse import urlparse

from brainskit.domain.model import (
    ModelResponseError,
    NotConfiguredError,
    PolicyError,
    PrivacyMode,
    ValidationError,
    VaultConfig,
)
from brainskit.domain.privacy import resolve_branch_policy, strictest_privacy

SUPPORTED_PROVIDERS = {"anthropic", "openai", "openrouter", "ollama"}

#: Attempts `_post_json` makes before giving up on a retryable failure. Named
#: because the refusals quote it: "rate limited on all 3 attempts" is what
#: tells an operator that waiting a moment longer is not the missing step.
_HTTP_ATTEMPTS = 3

#: Request options sent to Ollama unless `providers.ollama.options` overrides
#: them. Ollama defaults `num_ctx` to 4096 regardless of what the model
#: advertises, which truncates judgment prompts long before the model's real
#: window; 16384 covers a bounded evidence bundle plus job scaffolding and
#: repair feedback without forcing a large KV-cache allocation. `temperature`
#: stays at 0 because judgment output is schema-bound.
DEFAULT_OLLAMA_OPTIONS: dict[str, Any] = {"temperature": 0, "num_ctx": 16384}

ANTHROPIC_VERSION = "2023-06-01"
#: Anthropic's `max_tokens` bounds thinking *and* response text together, and
#: thinking is on by default on the current frontier models. A digest or ingest
#: proposal that fit in 8192 before can now be truncated mid-JSON, which the
#: repair loop cannot recover from because the next attempt truncates too.
#: Override per vault with `providers.anthropic.max_tokens`.
DEFAULT_ANTHROPIC_MAX_TOKENS = 16384

#: JSON Schema keywords that constrained structured decoding accepts. Anything
#: outside this set is dropped before the schema is sent to a provider; the
#: complete schema is still enforced locally by the repair loop, so narrowing
#: what the provider sees can only cost a retry, never correctness. Keeping an
#: allow-list rather than a deny-list means an unverified keyword degrades to
#: "not sent" instead of "rejected by the API".
_STRUCTURED_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "allOf",
        "$ref",
        "$defs",
    }
)


class ProviderDriver(Protocol):
    def complete(
        self, prompt: str, *, model: str, output_schema: dict[str, Any] | None
    ) -> str: ...


class JobSpecs:
    def prompt(self, job: str, variables: dict[str, Any]) -> str:
        resource = files("brainskit").joinpath("jobs", f"{job}.md")
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
        resource = files("brainskit").joinpath(
            "jobs", "_output-schemas", f"{job}.json"
        )
        if not resource.is_file():
            return None
        schema: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8"))
        return schema


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
        policies: dict[str, Any] = {}
        missing: list[str] = []
        for branch in set(branches):
            try:
                policies[branch] = resolve_branch_policy(self.config, branch)
            except PolicyError:
                missing.append(branch)
        if missing:
            # One refusal naming every unresolved branch, not the first one
            # found: the router's contract is plural and callers pin it.
            raise PolicyError(
                "No privacy policy exists for one or more branches",
                details={"branches": sorted(missing)},
            )
        never_ingest = sorted(
            branch
            for branch, policy in policies.items()
            if policy.privacy == PrivacyMode.NEVER_INGEST
        )
        if never_ingest:
            raise PolicyError(
                "Branch policy forbids judgment ingestion",
                details={"branches": never_ingest},
            )
        # Runs after the never-ingest refusal, so the fold answers LOCAL_ONLY
        # or CLOUD. `on_empty` is unreachable -- `branches` defaults to
        # `["_inbox"]` and every branch resolved above -- but the answer is
        # stated because the doctrine makes it a required decision, not an
        # asserted invariant.
        effective_privacy = strictest_privacy(
            (policy.privacy for policy in policies.values()),
            on_empty=PrivacyMode.NEVER_INGEST,
        )
        mapping = self.config.job_models.get(job)
        if not mapping:
            raise NotConfiguredError(
                "No model is configured for this job", details={"job": job}
            )
        route_mapping = mapping.get(effective_privacy.value, mapping)
        if not isinstance(route_mapping, dict):
            raise NotConfiguredError(
                "Job privacy route is invalid",
                details={"job": job, "privacy": effective_privacy.value},
            )
        provider_name = route_mapping.get("provider")
        model = route_mapping.get("model")
        if provider_name not in SUPPORTED_PROVIDERS or not model:
            raise NotConfiguredError(
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
            raise NotConfiguredError(
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
        provider: str = "",
        api_key_env: str = "",
    ):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key
        self.extra_headers = extra_headers or {}
        self.timeout = timeout
        self.provider = provider
        self.api_key_env = api_key_env

    def complete(
        self, prompt: str, *, model: str, output_schema: dict[str, Any] | None
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if output_schema:
            # Strict decoding rejects a schema it cannot compile, so send the
            # projection rather than the vault's schema: `metadata` is optional
            # in the ingest schema and `uniqueItems` appears in the query one,
            # and either is enough for the API to refuse the whole request.
            projected = _structured_schema(output_schema)
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "brainskit_output",
                    "strict": _strictly_expressible(projected),
                    "schema": projected,
                },
            }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        response = _post_json(
            self.url,
            payload,
            headers,
            self.timeout,
            context=ProviderContext(
                provider=self.provider,
                api_key_env=self.api_key_env,
                model=model,
            ),
        )
        try:
            return str(response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelResponseError("Provider returned an unexpected response") from exc


class AnthropicDriver:
    """Messages API driver.

    Two things this deliberately does not send. `temperature` was removed on
    the current frontier models and is rejected with a 400 rather than ignored,
    and it never guaranteed determinism on the models that did accept it — the
    schema and the repair loop are what bound the output. `thinking` is left
    unset so each model applies its own default.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 120,
        max_tokens: int = DEFAULT_ANTHROPIC_MAX_TOKENS,
        api_key_env: str = "",
    ):
        self.url = base_url.rstrip("/") + "/messages"
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.api_key_env = api_key_env

    def complete(
        self, prompt: str, *, model: str, output_schema: dict[str, Any] | None
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        projected = _structured_schema(output_schema) if output_schema else None
        if projected is not None and _strictly_expressible(projected):
            payload["output_config"] = {
                "format": {"type": "json_schema", "schema": projected}
            }
            try:
                return self._text(self._send(payload))
            except ValidationError as exc:
                if not _rejected_structured_output(exc):
                    raise
            # A model that predates constrained decoding rejects the parameter
            # itself. Falling through to the prompt-described schema is weaker,
            # but the repair loop validates the result either way, so the job
            # degrades instead of failing on an operator's choice of model.
            payload.pop("output_config")
        if output_schema:
            payload["messages"] = [
                {"role": "user", "content": prompt + _schema_instruction(output_schema)}
            ]
        return self._text(self._send(payload))

    def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _post_json(
            self.url,
            payload,
            {
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            self.timeout,
            context=ProviderContext(
                provider="anthropic",
                api_key_env=self.api_key_env,
                model=str(payload.get("model", "")),
            ),
        )

    def _text(self, response: dict[str, Any]) -> str:
        """Read the message body, refusing to pass off a non-answer as one.

        `stop_reason` is checked before `content` because both terminal cases
        produce a response that parses fine and means nothing: a refusal
        carries an empty content list, and a truncation carries JSON that ends
        mid-token. Returning either would send the repair loop after output no
        retry can fix — three attempts, three identical outcomes, and an error
        naming the schema rather than the cause.
        """
        stop_reason = response.get("stop_reason")
        if stop_reason == "refusal":
            details = response.get("stop_details")
            category = details.get("category") if isinstance(details, dict) else None
            raise PolicyError(
                "The provider's safety classifiers declined this request",
                details={"provider": "anthropic", "category": category},
            )
        if stop_reason == "max_tokens":
            raise ModelResponseError(
                "Provider response was truncated by max_tokens",
                details={
                    "provider": "anthropic",
                    "max_tokens": self.max_tokens,
                    "remedy": "Raise providers.anthropic.max_tokens for this vault",
                },
            )
        try:
            blocks = response["content"]
            text = "".join(
                block["text"]
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:
            raise ModelResponseError("Provider returned an unexpected response") from exc
        if not text.strip():
            raise ModelResponseError(
                "Provider returned no text content",
                details={"provider": "anthropic", "stop_reason": stop_reason},
            )
        return text


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
            self.url,
            payload,
            {"Content-Type": "application/json"},
            self.timeout,
            context=ProviderContext(provider="ollama", model=model),
        )
        try:
            return str(response["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise ModelResponseError("Provider returned an unexpected response") from exc


def _structured_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Project a vault schema onto the subset constrained decoding accepts.

    Two rewrites, both required by strict structured output: every object must
    close `additionalProperties` and list *every* declared property in
    `required`, so an optional property becomes required-and-nullable rather
    than being dropped — dropping it would forbid a field the vault permits.
    Keywords outside the allow-list are removed; they are re-applied by the
    local validator, which is what actually enforces the contract.
    """

    if not isinstance(schema, dict):
        return schema
    projected: dict[str, Any] = {}
    for keyword, value in schema.items():
        if keyword not in _STRUCTURED_SCHEMA_KEYWORDS:
            continue
        if keyword == "properties" and isinstance(value, dict):
            projected[keyword] = {
                name: _structured_schema(entry) for name, entry in value.items()
            }
        elif keyword in {"items", "$defs"} and isinstance(value, dict):
            projected[keyword] = (
                {name: _structured_schema(entry) for name, entry in value.items()}
                if keyword == "$defs"
                else _structured_schema(value)
            )
        elif keyword in {"anyOf", "allOf"} and isinstance(value, list):
            projected[keyword] = [_structured_schema(entry) for entry in value]
        else:
            projected[keyword] = value
    properties = projected.get("properties")
    if isinstance(properties, dict):
        optional = sorted(set(properties) - set(schema.get("required", [])))
        for name in optional:
            properties[name] = _nullable(properties[name])
        projected["required"] = sorted(properties)
        projected["additionalProperties"] = False
    return projected


def _strictly_expressible(schema: Any) -> bool:
    """True when constrained decoding can represent this schema exactly.

    It cannot represent a free-form object: strict mode requires every object
    to close `additionalProperties`, and closing one that was deliberately open
    would forbid exactly the custom frontmatter `.brain/schema.json` exists to
    let a vault declare. The ingest schema has such an object (`metadata`), so
    this is the live case, not a hypothetical.

    A schema that fails here is still sent — as guidance rather than as a
    constraint — and the repair loop enforces the real contract afterwards.
    """

    if isinstance(schema, list):
        return all(_strictly_expressible(entry) for entry in schema)
    if not isinstance(schema, dict):
        return True
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else [declared]
    if "object" in types and "properties" not in schema:
        return False
    for keyword in ("properties", "$defs"):
        entry = schema.get(keyword)
        if isinstance(entry, dict) and not all(
            _strictly_expressible(value) for value in entry.values()
        ):
            return False
    for keyword in ("items", "anyOf", "allOf"):
        if keyword in schema and not _strictly_expressible(schema[keyword]):
            return False
    return True


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """Widen a schema so an omitted-but-required property can be sent as null."""

    if not isinstance(schema, dict):
        return schema
    declared = schema.get("type")
    if declared is None or declared == "null":
        return schema
    types = declared if isinstance(declared, list) else [declared]
    if "null" in types:
        return schema
    return {**schema, "type": [*types, "null"]}


def _schema_instruction(schema: dict[str, Any]) -> str:
    return "\n\nReturn only JSON matching this schema:\n" + json.dumps(
        schema, ensure_ascii=False
    )


def _rejected_structured_output(error: ValidationError) -> bool:
    """True when a 400 blames the structured-output parameter, not the payload.

    Narrow on purpose: any other 400 — an unknown model, a malformed message —
    must surface as itself rather than being retried in a weaker mode that
    fails again for the original reason and reports the wrong cause.
    """

    details = error.details
    if details.get("status") != 400:
        return False
    body = str(details.get("response", "")).lower()
    return "output_config" in body or "output config" in body


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
        raise NotConfiguredError(
            "Provider API key environment variable is not set",
            details={"provider": name, "api_key_env": api_key_env},
        )
    if name == "anthropic":
        return AnthropicDriver(
            base_url=_required(config, "base_url", provider=name),
            api_key=api_key,
            timeout=timeout,
            max_tokens=_positive_int(
                config, "max_tokens", DEFAULT_ANTHROPIC_MAX_TOKENS, provider=name
            ),
            api_key_env=api_key_env,
        )
    return OpenAICompatibleDriver(
        base_url=_required(config, "base_url", provider=name),
        api_key=api_key,
        extra_headers={
            str(key): str(value)
            for key, value in config.get("headers", {}).items()
        },
        timeout=timeout,
        provider=name,
        api_key_env=api_key_env,
    )


def _ollama_options(config: dict[str, Any]) -> dict[str, Any]:
    """Read the operator-supplied Ollama request options.

    Values are never echoed back in errors; only the key names are structural.
    """

    options = config.get("options", {})
    if not isinstance(options, dict):
        raise NotConfiguredError(
            "Ollama provider options must be an object",
            details={"provider": "ollama"},
        )
    return {str(key): value for key, value in options.items()}


def _positive_int(
    config: dict[str, Any], field: str, default: int, *, provider: str
) -> int:
    """Read an operator-supplied positive integer, rejecting a silent fallback.

    A mistyped budget must not quietly become the default: the symptom would be
    a truncation the operator already tried to fix.
    """

    if field not in config:
        return default
    value = config[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise NotConfiguredError(
            "Provider option must be a positive integer",
            details={"provider": provider, "field": field, "value": value},
        )
    return int(value)


def _required(config: dict[str, Any], field: str, *, provider: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value:
        raise NotConfiguredError(
            "Provider configuration is incomplete",
            details={"provider": provider, "missing": field},
        )
    return value


@dataclass(frozen=True)
class ProviderContext:
    """What an HTTP failure needs in order to name its own next step.

    `_post_json` sees a URL and a status code; an operator reading a 401 needs
    to hear *which* environment variable holds the rejected key, and one
    reading a 404 needs the model id the config routed the job to. Every field
    is optional so a driver built without it still works and simply reports
    less.
    """

    provider: str = ""
    api_key_env: str = ""
    model: str = ""


def _http_failure(
    status: int, body: str, context: ProviderContext
) -> ValidationError:
    """Code an HTTP failure by what the caller has to do differently.

    Every status used to be `validation_error` -- *"the request itself is wrong,
    fix it and send it again"* -- which is true of a 400 and false of every
    other status a provider returns. Twenty lines below, the `URLError` arm of
    the same `try` already made the argument this function generalises: an agent
    told to fix a well-formed request will rewrite it forever against a provider
    that is refusing its credentials, has retired its model, or is simply down.

    The split is one question: **is the failure about the bytes the caller
    sent?** A 400 says yes and stays `validation_error`. Credentials (401),
    entitlement (403), an identifier the provider does not know (404), an
    exhausted quota (429) and a provider erroring out (5xx) all say no, and are
    `not_configured` -- "stop retrying and tell the operator" -- because none of
    them is cleared by any request an agent can construct.

    Anything unlisted keeps `validation_error`. The default is deliberately the
    status quo, so a status nobody has reasoned about never changes meaning as
    a side effect of this table.
    """

    details: dict[str, Any] = {"status": status, "response": body}
    if context.provider:
        details["provider"] = context.provider
    if context.model:
        details["model"] = context.model
    key_env = context.api_key_env or "the provider's api_key_env"

    if status == 401:
        details["api_key_env"] = context.api_key_env
        details["hint"] = (
            f"The provider rejected the credential, not the request. Set a "
            f"valid key in {key_env} and run the job again; no change to the "
            f"request will clear this."
        )
        return NotConfiguredError("Provider rejected the API key", details=details)

    if status == 403:
        details["api_key_env"] = context.api_key_env
        details["hint"] = (
            f"The key in {key_env} authenticated but is not permitted to use "
            f"this model or endpoint. Check the account's entitlements, "
            f"billing and region -- rewriting the request will not clear this."
        )
        return NotConfiguredError(
            "Provider refused this key's access", details=details
        )

    if status == 404:
        details["hint"] = (
            f"The provider does not know this endpoint or model id. Check "
            f"job_models in .brain/config.json (routed to "
            f"{context.model or 'an unnamed model'}) and the provider's "
            f"base_url; model ids are retired without notice."
        )
        return NotConfiguredError(
            "Provider does not recognise the model or endpoint", details=details
        )

    if status == 429:
        details["hint"] = (
            f"Rate limited on all {_HTTP_ATTEMPTS} attempts, honouring "
            f"Retry-After. Wait for the quota window to reset, raise the "
            f"quota, or route this job to another model in job_models."
        )
        return NotConfiguredError(
            "Provider rate limit outlasted the retries", details=details
        )

    if status >= 500:
        details["hint"] = (
            f"The provider accepted the request and failed to answer it on all "
            f"{_HTTP_ATTEMPTS} attempts. Retry later or route this job to "
            f"another provider in job_models; the request body is not the cause."
        )
        return NotConfiguredError("Provider failed to answer", details=details)

    details["hint"] = (
        "The provider judged the request itself invalid; the response above "
        "says why. Fix the request and send it again."
    )
    return ValidationError("Provider rejected the request", details=details)


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    *,
    context: ProviderContext | None = None,
) -> dict[str, Any]:
    # `base_url` is operator configuration, and urlopen honours `file:` and
    # `data:` as readily as `https:`. A judgment job must reach a provider or
    # fail, never read a local file and pass it off as model output.
    scheme = urlparse(url).scheme
    if scheme not in {"http", "https"}:
        raise NotConfiguredError(
            "Provider base_url must be an http(s) URL",
            details={"scheme": scheme or "(none)"},
        )
    resolved = context or ProviderContext()
    raw = b""
    last_error: Exception | None = None
    for attempt in range(_HTTP_ATTEMPTS):
        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - scheme checked above
                request, timeout=timeout
            ) as response:
                raw = response.read()
            last_error = None
            break
        except urllib.error.HTTPError as exc:
            body = exc.read(2_000).decode("utf-8", errors="replace")
            last_error = _http_failure(exc.code, body, resolved)
            if exc.code != 429 and exc.code < 500:
                raise last_error from exc
            if attempt == _HTTP_ATTEMPTS - 1:
                raise last_error from exc
            retry_after = exc.headers.get("Retry-After")
            delay = (
                min(float(retry_after), 8.0)
                if retry_after and retry_after.replace(".", "", 1).isdigit()
                else float(2**attempt)
            )
            time.sleep(delay)
        except urllib.error.URLError as exc:
            # `not_configured`, not `validation_error`: the request is fine and
            # the provider is not answering. An agent told "fix the request and
            # send it again" will rewrite a well-formed body forever against a
            # provider that is simply not running.
            last_error = NotConfiguredError(
                "Provider is unreachable",
                details={
                    "reason": str(exc.reason),
                    "provider": resolved.provider,
                    "hint": (
                        f"Nothing answered on {_HTTP_ATTEMPTS} attempts. Check "
                        f"that the provider is running and that its base_url in "
                        f".brain/config.json points at it."
                    ),
                },
            )
            if attempt == _HTTP_ATTEMPTS - 1:
                raise last_error from exc
            time.sleep(float(2**attempt))
    if last_error:
        raise last_error
    try:
        decoded: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelResponseError("Provider returned invalid JSON") from exc
    return decoded
