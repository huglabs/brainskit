"""A provider being down is not a malformed request.

Six sites raised "Provider is unreachable" / "Docker command failed" as
`validation_error`, whose documented meaning is *"the request itself is wrong --
fix it and send it again"*. An agent that reads its own error codes will keep
rewriting a well-formed request against a provider that is simply not running.

`not_configured` is the code that means "stop retrying and tell the operator".
The change is safe by construction: `NotConfiguredError` subclasses
`ValidationError`, so every `except` and every exit code is unchanged -- only
`.code` sharpens.

Two things that first pass missed, and this file now covers.

**The HTTP arm never got the fix.** Only `URLError` -- nothing answered at all --
was recoded. An HTTP response with a status was still `validation_error`
whatever the status said, so a rejected API key, a retired model id, an
exhausted quota and a provider returning 500 all told the caller to rewrite a
request that was never the problem. There was no HTTP status case in this file,
which is why it shipped.

**Wrapping downgraded the fix.** The sharper classes *subclass*
`ValidationError`, so `except ValidationError: raise ValidationError(...)` --
two clauses in `integrations.py` that exist only to attach which container and
which ports -- caught `_docker`'s `not_configured` and re-raised it as
`validation_error`. The same stopped Docker daemon answered differently
depending on the path, and the only path this file exercised was the one that
stayed correct. The tests below drive `up("postgres")`, which is what
`bk integration up postgres` actually calls.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from brainskit.domain.model import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fix_integrations import policy


def _http_error(status: int, body: bytes = b'{"error":"x"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://provider.invalid/v1", status, "boom", {}, io.BytesIO(body)
    )


DAEMON_STOPPED = "Cannot connect to the Docker daemon. Is the docker daemon running?"


class ProviderOutageIsNotConfiguredTest(unittest.TestCase):
    def test_an_unreachable_provider_reports_not_configured(self) -> None:
        from brainskit.infrastructure import llm

        with mock.patch.object(
            llm.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ), mock.patch.object(llm.time, "sleep"):
            with self.assertRaises(ValidationError) as raised:
                llm._post_json(
                    "http://127.0.0.1:9/none",
                    {},
                    headers={},
                    timeout=1,
                )
        self.assertEqual(raised.exception.code, "not_configured")

    def test_the_error_is_still_a_validation_error_subclass(self) -> None:
        """The compatibility guarantee that makes this change safe."""

        from brainskit.domain.model import NotConfiguredError

        self.assertTrue(issubclass(NotConfiguredError, ValidationError))

    def test_a_missing_docker_binary_reports_not_configured(self) -> None:
        from brainskit.infrastructure import integrations

        with mock.patch.object(
            integrations.subprocess, "run", side_effect=OSError("no docker")
        ):
            with self.assertRaises(ValidationError) as raised:
                integrations._docker(["ps"])
        self.assertEqual(raised.exception.code, "not_configured")

    def test_a_failing_docker_command_reports_not_configured(self) -> None:
        from brainskit.infrastructure import integrations

        completed = mock.Mock(returncode=1, stderr="daemon not running", stdout="")
        with mock.patch.object(integrations.subprocess, "run", return_value=completed):
            with self.assertRaises(ValidationError) as raised:
                integrations._docker(["ps"])
        self.assertEqual(raised.exception.code, "not_configured")

    def test_a_genuinely_invalid_request_is_still_validation_error(self) -> None:
        """Control: the sharpening must not swallow real input errors.

        If everything became `not_configured`, the code would carry no
        information at all -- the same failure as everything being
        `validation_error`, pointed the other way. A malformed *request* keeps
        its own code.
        """

        from brainskit.domain.privacy import Consumer

        with self.assertRaises(ValidationError) as raised:
            Consumer.parse("everyone")
        self.assertEqual(raised.exception.code, "validation_error")

    def test_a_bad_base_url_is_configuration_not_a_bad_request(self) -> None:
        """Already correct before this task, and pinned so it stays that way."""

        from brainskit.infrastructure import llm

        with self.assertRaises(ValidationError) as raised:
            llm._post_json("ftp://example.invalid/x", {}, headers={}, timeout=1)
        self.assertEqual(raised.exception.code, "not_configured")


class HttpStatusCarriesItsOwnRemedyTest(unittest.TestCase):
    """S2-4: the HTTP arm of the same `try`, which the first pass skipped.

    The split these pin is one question -- *is the failure about the bytes the
    caller sent?* A 400 says yes. A rejected credential, a model id the provider
    has retired, an exhausted quota and a provider erroring out all say no, and
    no request an agent can construct will clear any of them.
    """

    def raise_status(self, status: int, **context: str) -> ValidationError:
        from brainskit.infrastructure import llm

        resolved = llm.ProviderContext(**context) if context else None
        with mock.patch.object(
            llm.urllib.request, "urlopen", side_effect=_http_error(status)
        ), mock.patch.object(llm.time, "sleep"):
            with self.assertRaises(ValidationError) as raised:
                llm._post_json(
                    "http://provider.invalid/v1",
                    {},
                    headers={},
                    timeout=1,
                    context=resolved,
                )
        return raised.exception

    def test_the_caller_is_only_told_to_fix_the_request_when_it_is_the_request(
        self,
    ) -> None:
        """The whole table at once, so the partition is visible in one place.

        Asserted as a partition rather than status by status: a regression that
        moved *any* status across the line fails here, including one that swept
        everything into `not_configured`, which would carry as little
        information as the `validation_error` it replaced.
        """

        not_the_request = {401: "credential", 403: "entitlement",
                           404: "unknown model", 429: "quota", 500: "provider",
                           503: "provider", 529: "provider"}
        the_request = {400: "malformed", 413: "too large", 422: "unprocessable"}

        observed = {
            status: self.raise_status(status).code
            for status in {**not_the_request, **the_request}
        }
        self.assertEqual(
            observed,
            {
                **dict.fromkeys(not_the_request, "not_configured"),
                **dict.fromkeys(the_request, "validation_error"),
            },
        )

    def test_a_rejected_key_names_the_variable_that_holds_it(self) -> None:
        """The remedy, not the code: a 401 an operator can act on.

        `not_configured` says "tell the operator"; this asserts the refusal says
        *what to tell them*. The canary name can only appear by having been
        threaded from the provider config through the driver to the failure.
        """

        failure = self.raise_status(
            401, provider="openrouter", api_key_env="CANARY_KEY_ENV_a1b2"
        )
        self.assertEqual(failure.code, "not_configured")
        self.assertIn("CANARY_KEY_ENV_a1b2", failure.details["hint"])
        self.assertEqual(failure.details["provider"], "openrouter")

    def test_an_unknown_model_names_the_model_and_where_it_is_configured(
        self,
    ) -> None:
        """A 404 is almost always a model id `job_models` still routes to."""

        failure = self.raise_status(
            404, provider="anthropic", model="claude-canary-retired-20240229"
        )
        self.assertEqual(failure.code, "not_configured")
        self.assertIn("claude-canary-retired-20240229", failure.details["hint"])
        self.assertIn("job_models", failure.details["hint"])

    def test_an_exhausted_quota_says_retrying_is_already_spent(self) -> None:
        """A 429 that survived the retries is not cleared by retrying again."""

        from brainskit.infrastructure import llm

        failure = self.raise_status(429, provider="openai")
        self.assertEqual(failure.code, "not_configured")
        self.assertIn(str(llm._HTTP_ATTEMPTS), failure.details["hint"])

    def test_the_status_stays_readable_to_the_structured_output_fallback(
        self,
    ) -> None:
        """The hard constraint, pinned through the behaviour that depends on it.

        `AnthropicDriver` degrades to a prompt-described schema when a model
        rejects `output_config`, and it recognises that case by reading
        `details["status"] == 400`. Recoding 400 would not fail loudly -- the
        driver would simply stop degrading and start failing on an operator's
        choice of model. So this asserts the *degradation*, not the detail.
        """

        from brainskit.infrastructure import llm

        answered = {
            "content": [{"type": "text", "text": '{"ok": true}'}],
            "stop_reason": "end_turn",
        }
        sent: list[dict[str, Any]] = []

        def respond(request: Any, timeout: float = 0) -> Any:
            import json as _json

            payload = _json.loads(request.data)
            sent.append(payload)
            if "output_config" in payload:
                raise _http_error(400, b'{"error":"unexpected parameter output_config"}')
            return mock.MagicMock(
                __enter__=lambda _: mock.Mock(
                    read=lambda: _json.dumps(answered).encode()
                ),
                __exit__=lambda *_: False,
            )

        driver = llm.AnthropicDriver(
            base_url="https://api.anthropic.invalid/v1",
            api_key="k",
            api_key_env="ANTHROPIC_API_KEY",
        )
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=respond):
            text = driver.complete(
                "hi",
                model="claude-old",
                output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
            )

        self.assertEqual(text, '{"ok": true}')
        self.assertTrue("output_config" in sent[0])
        self.assertNotIn("output_config", sent[1])

    def test_a_401_through_a_real_driver_still_reports_not_configured(self) -> None:
        """End to end: config -> `_create_driver` -> `complete` -> the refusal.

        `_post_json` cannot name an environment variable it was never told
        about, so this pins the threading as well as the classification.
        """

        from brainskit.infrastructure import llm

        os.environ["CANARY_PROVIDER_KEY_c3d4"] = "sk-not-a-real-key"
        self.addCleanup(os.environ.pop, "CANARY_PROVIDER_KEY_c3d4", None)
        driver = llm._create_driver(
            "openrouter",
            {
                "base_url": "https://openrouter.invalid/api/v1",
                "api_key_env": "CANARY_PROVIDER_KEY_c3d4",
            },
        )
        with mock.patch.object(
            llm.urllib.request, "urlopen", side_effect=_http_error(401)
        ):
            with self.assertRaises(ValidationError) as raised:
                driver.complete("hi", model="some/model", output_schema=None)

        self.assertEqual(raised.exception.code, "not_configured")
        self.assertIn(
            "CANARY_PROVIDER_KEY_c3d4", raised.exception.details["hint"]
        )


class DockerOutageReadsTheSameFromEveryPathTest(unittest.TestCase):
    """S2-5: the regression the additive-subclass strategy introduced.

    These drive `up("postgres")` -- what `bk integration up postgres` calls --
    rather than `_docker`, because `_docker` was the one path that stayed
    correct. The assertion is *parity*: one outage, one remedy, whichever
    wrapper happens to be between the caller and the daemon. A test naming
    `not_configured` per path would keep passing if a future wrapper downgraded
    one of them and someone updated that path's expectation to match.
    """

    def integrations(self) -> Any:
        from brainskit.infrastructure.integrations import NativeIntegrations
        from brainskit.infrastructure.vault import FileVault

        configured = policy()
        configured["integrations"]["postgres"] = {
            "enabled": True,
            "managed": True,
            "options": {
                "password_env": "CANARY_PG_PASSWORD_e5f6",
                "consumer": "local",
                "port": 55432,
            },
        }
        os.environ["CANARY_PG_PASSWORD_e5f6"] = "pw"
        self.addCleanup(os.environ.pop, "CANARY_PG_PASSWORD_e5f6", None)
        temporary = tempfile.TemporaryDirectory(prefix="outage-codes-")
        self.addCleanup(temporary.cleanup)
        return NativeIntegrations(
            FileVault.initialize(Path(temporary.name), configured)
        )

    def up_with_daemon_stopped(self, *, container_exists: bool) -> ValidationError:
        """`bk integration up postgres` against a Docker daemon that is down.

        The two arms differ only in whether the container was created before:
        absent takes `docker run`, present-but-stopped takes `docker start`.
        Both wrapped `_docker`'s refusal, and both downgraded it.
        """

        from brainskit.infrastructure import integrations as module

        inspected = {
            "State": {"Status": "exited", "Running": False},
            "Config": {"Image": "postgres:16"},
            "NetworkSettings": {"Ports": {"5432/tcp": [{"HostPort": "55432"}]}},
        }
        stopped = mock.Mock(returncode=1, stdout="", stderr=DAEMON_STOPPED)
        with mock.patch.object(
            module.shutil, "which", return_value="/usr/local/bin/docker"
        ), mock.patch.object(
            module,
            "_docker_container_inspect",
            return_value=inspected if container_exists else None,
        ), mock.patch.object(
            module, "_container_drift", return_value=[]
        ), mock.patch.object(
            module.subprocess, "run", return_value=stopped
        ):
            with self.assertRaises(ValidationError) as raised:
                self.integrations().up("postgres")
        return raised.exception

    def test_one_outage_reads_the_same_through_every_path(self) -> None:
        from brainskit.infrastructure import integrations as module

        stopped = mock.Mock(returncode=1, stdout="", stderr=DAEMON_STOPPED)
        with mock.patch.object(module.subprocess, "run", return_value=stopped):
            with self.assertRaises(ValidationError) as raised:
                module._docker(["ps"])

        observed = {
            "_docker": raised.exception.code,
            "up/docker run": self.up_with_daemon_stopped(container_exists=False).code,
            "up/docker start": self.up_with_daemon_stopped(container_exists=True).code,
        }
        self.assertEqual(len(set(observed.values())), 1, observed)
        self.assertEqual(observed["_docker"], "not_configured", observed)

    def test_the_wrapper_still_attaches_the_context_it_exists_for(self) -> None:
        """Preserving the class must not cost the diagnosis it was added for."""

        failure = self.up_with_daemon_stopped(container_exists=True)
        self.assertEqual(failure.details["integration"], "postgres")
        self.assertIn("container", failure.details)
        self.assertEqual(failure.details["reason"], "Docker command failed")
        # Merged from the wrapped refusal, which is where the daemon's own words
        # survive -- the diagnosis is worthless without them.
        self.assertIn(DAEMON_STOPPED, failure.details["response"])

    def test_a_wrapper_never_widens_the_remedy_it_was_handed(self) -> None:
        """The invariant stated directly, independent of what `_docker` raises.

        The wrappers rebuild the class from the instance they caught, so a
        genuine `validation_error` stays one and every sharper class survives.
        Pinned against all five, because the next subclass added to the taxonomy
        must survive these clauses without anyone remembering they exist.
        """

        from brainskit.domain.model import (
            ConflictError,
            ModelResponseError,
            NotConfiguredError,
            RefusalError,
        )
        from brainskit.infrastructure import integrations as module

        for error_class in (
            ValidationError,
            NotConfiguredError,
            ConflictError,
            RefusalError,
            ModelResponseError,
        ):
            with self.subTest(error_class.__name__):
                with mock.patch.object(
                    module, "_docker", side_effect=error_class("docker said no")
                ):
                    with self.assertRaises(ValidationError) as raised:
                        module._start_managed_container(
                            "postgres",
                            "brainkit-postgres",
                            {"image": "postgres:16", "ports": {"5432/tcp": ["55432"]}},
                        )
                self.assertIs(type(raised.exception), error_class)
                self.assertEqual(raised.exception.code, error_class.code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
