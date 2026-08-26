"""Running the six jobs on a subscription, through a CLI instead of an endpoint.

The subscription an operator already pays for is reachable only through the CLI
that owns the session. The desktop applications are MCP *clients* — they expose
no endpoint, so there is nothing for a `base_url` to point at, and this is the
one driver family that spawns a process rather than making a request.

Everything here pins a consequence of that, and each one is a way the naive
version fails silently rather than loudly:

- the prompt travels on **stdin**, because an evidence bundle in `argv` hits
  `ARG_MAX` only on the large jobs — the worst size to discover in production;
- the operator's own settings, hooks and MCP servers are **excluded**, or every
  job inherits unpredictable behaviour and a much larger token floor;
- API-key variables are **stripped**, or a stray key bills the API account
  instead of the subscription that was chosen;
- `local-only` evidence is still **refused**, because the process is local and
  the model is not.

Nothing here spawns a real CLI: the suite has to pass on a machine with neither
installed.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from brainskit.domain.model import (
    ModelResponseError,
    NotConfiguredError,
    PolicyError,
    PrivacyMode,
    ValidationError,
    VaultConfig,
)
from brainskit.infrastructure import llm

RESOLVED = {"claude": "/fake/bin/claude", "codex": "/fake/bin/codex"}


def _completed(
    stdout: str = "", stderr: str = "", code: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


class _CliCase(unittest.TestCase):
    """Fakes `which` and a signed-in CLI so no real binary is ever needed."""

    def setUp(self) -> None:
        llm._CLI_AUTH.clear()
        self.addCleanup(llm._CLI_AUTH.clear)
        which = mock.patch.object(
            llm.shutil, "which", side_effect=lambda name: RESOLVED.get(Path(name).name)
        )
        which.start()
        self.addCleanup(which.stop)
        for path in RESOLVED.values():
            llm._CLI_AUTH[path] = ""
        self.calls: list[dict[str, Any]] = []

    def spawn(self, result: subprocess.CompletedProcess[str] | Exception, *, answer: str = ""):
        """Patch the one `subprocess.run` the driver uses, recording the call."""

        def fake(argv, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append({"argv": list(argv), **kwargs})
            if answer:
                Path(kwargs["cwd"], "answer.txt").write_text(answer, encoding="utf-8")
            if isinstance(result, Exception):
                raise result
            return result

        return mock.patch.object(llm.subprocess, "run", side_effect=fake)

    @property
    def argv(self) -> list[str]:
        return self.calls[-1]["argv"]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }


class IsolationTest(_CliCase):
    def test_claude_never_inherits_the_operators_settings_or_mcp_servers(self) -> None:
        """Measured at ~11x the token floor when inherited, and unpredictable.

        Left alone, a judgment job picks up whatever hooks, plugins, project
        instructions and MCP servers the operator happens to have configured.
        """

        driver = llm.ClaudeCodeDriver()
        with self.spawn(_completed(json.dumps({"result": "{}"}))):
            driver.complete("hi", model=llm.CLI_DEFAULT_MODEL, output_schema=None)
        self.assertIn("--strict-mcp-config", self.argv)
        index = self.argv.index("--setting-sources")
        self.assertEqual(self.argv[index + 1], "")

    def test_codex_runs_under_an_isolated_home_that_only_borrows_the_credential(
        self,
    ) -> None:
        driver = llm.CodexDriver()
        with self.spawn(_completed(), answer="{}"):
            driver.complete("hi", model=llm.CLI_DEFAULT_MODEL, output_schema=None)
        home = Path(self.calls[-1]["env"]["CODEX_HOME"])
        self.assertTrue(home.is_dir())
        self.assertNotEqual(home, Path.home() / ".codex")
        self.assertEqual(home.stat().st_mode & 0o777, 0o700)

    def test_codex_never_runs_commands_and_never_touches_a_real_directory(self) -> None:
        """A judgment job reasons over evidence it was handed. Nothing more."""

        driver = llm.CodexDriver()
        with self.spawn(_completed(), answer="{}"):
            driver.complete("hi", model=llm.CLI_DEFAULT_MODEL, output_schema=None)
        index = self.argv.index("--sandbox")
        self.assertEqual(self.argv[index + 1], "read-only")
        self.assertTrue(str(self.calls[-1]["cwd"]).find("brainskit-cli-") != -1)

    def test_api_key_variables_are_stripped_from_the_child(self) -> None:
        """A stray key bills the API account instead of the chosen subscription.

        `*_BASE_URL` is worse: it silently redirects the CLI to a different
        backend, so the job runs and answers from somewhere nobody chose.
        """

        polluted = {
            "ANTHROPIC_API_KEY": "sk-ant-x",
            "ANTHROPIC_BASE_URL": "http://localhost:11434",
            "PATH": "/usr/bin",
        }
        with mock.patch.dict(llm.os.environ, polluted, clear=True):
            driver = llm.ClaudeCodeDriver()
            with self.spawn(_completed(json.dumps({"result": "{}"}))):
                driver.complete("hi", model=llm.CLI_DEFAULT_MODEL, output_schema=None)
        env = self.calls[-1]["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_BASE_URL", env)
        self.assertIn("PATH", env)

    def test_the_prompt_travels_on_stdin_and_never_in_argv(self) -> None:
        """`ARG_MAX` is only reached by the large jobs — the worst ones to lose."""

        prompt = "EVIDENCE " * 20_000
        driver = llm.ClaudeCodeDriver()
        with self.spawn(_completed(json.dumps({"result": "{}"}))):
            driver.complete(prompt, model=llm.CLI_DEFAULT_MODEL, output_schema=None)
        self.assertIn("EVIDENCE", self.calls[-1]["input"])
        self.assertFalse([arg for arg in self.argv if "EVIDENCE" in arg])


class StructuredOutputTest(_CliCase):
    def test_codex_gets_a_real_schema_file_holding_the_projected_schema(self) -> None:
        """This CLI takes `--output-schema`, so decoding stays constrained."""

        seen: dict[str, Any] = {}

        driver = llm.CodexDriver()
        with self.spawn(_completed(), answer='{"ok": true}') as run:
            run.side_effect = self._capture_schema(seen, run.side_effect)
            driver.complete("hi", model=llm.CLI_DEFAULT_MODEL, output_schema=self.schema())
        index = self.argv.index("--output-schema")
        self.assertTrue(self.argv[index + 1].endswith("schema.json"))
        self.assertEqual(seen["schema"], llm._structured_schema(self.schema()))

    def _capture_schema(self, seen: dict[str, Any], inner):  # type: ignore[no-untyped-def]
        def wrapper(argv, **kwargs):  # type: ignore[no-untyped-def]
            path = Path(argv[argv.index("--output-schema") + 1])
            seen["schema"] = json.loads(path.read_text(encoding="utf-8"))
            return inner(argv, **kwargs)

        return wrapper

    def test_a_schema_strict_decoding_cannot_express_is_asked_for_instead(self) -> None:
        """Found by running a real ingest, which failed with a 400.

        `--output-schema` has no lenient mode — unlike the HTTP drivers, which
        downgrade a non-strict schema to guidance. The shipped ingest schema is
        exactly such a case (a deliberately open `metadata` object), so handing
        the flag over would fail *the vault's most important job* every time.
        """

        from brainskit.infrastructure.llm import JobSpecs

        schema = JobSpecs().schema("ingest")
        assert schema is not None
        self.assertFalse(llm._strictly_expressible(llm._structured_schema(schema)))

        driver = llm.CodexDriver()
        with self.spawn(_completed(), answer='{"operations": []}'):
            driver.complete("hi", model=llm.CLI_DEFAULT_MODEL, output_schema=schema)
        self.assertNotIn("--output-schema", self.argv)
        self.assertIn('"operations"', self.calls[-1]["input"])

    def test_a_constant_carries_the_type_strict_validators_demand(self) -> None:
        """`{"const": "upsert"}` alone is rejected outright by OpenAI.

        "Invalid schema ... schema must have a 'type' key" — a whole-request
        400, not a degraded result. The type is derived from the constant, so
        it can only restate what the constant already says.
        """

        projected = llm._structured_schema(
            {
                "type": "object",
                "properties": {"action": {"const": "upsert"}, "n": {"const": 3}},
                "required": ["action", "n"],
            }
        )
        self.assertEqual(projected["properties"]["action"]["type"], "string")
        self.assertEqual(projected["properties"]["n"]["type"], "integer")

    def test_claude_asks_for_the_shape_because_it_cannot_be_given_one(self) -> None:
        """The honest difference from every HTTP driver here.

        There is no schema flag, so the shape is requested in the prompt and the
        repair loop absorbs a miss. Silently dropping the schema would leave the
        job asking for free-form prose and failing three times over.
        """

        driver = llm.ClaudeCodeDriver()
        with self.spawn(_completed(json.dumps({"result": "{}"}))):
            driver.complete("hi", model=llm.CLI_DEFAULT_MODEL, output_schema=self.schema())
        sent = self.calls[-1]["input"]
        self.assertIn('"additionalProperties": false', sent)
        self.assertNotIn("--output-schema", self.argv)

    def test_a_fenced_answer_is_unwrapped_rather_than_failing_to_parse(self) -> None:
        """A CLI without constrained decoding will sometimes add a code fence.

        Unwrapped in the driver, not in the shared parser: every HTTP provider
        returns bare JSON because the schema was enforced on the wire, and
        loosening the parser for all of them would hide a real failure.
        """

        driver = llm.ClaudeCodeDriver()
        fenced = '```json\n{"ok": true}\n```'
        with self.spawn(_completed(json.dumps({"result": fenced}))):
            out = driver.complete("hi", model="sonnet", output_schema=self.schema())
        self.assertEqual(json.loads(out), {"ok": True})

    def test_bare_json_is_returned_untouched(self) -> None:
        self.assertEqual(llm._unfence('{"ok": true}'), '{"ok": true}')


class ModelSelectionTest(_CliCase):
    def test_the_default_sentinel_omits_the_flag_entirely(self) -> None:
        """"Whatever the CLI is set to" is a real answer no model id expresses."""

        driver = llm.ClaudeCodeDriver()
        with self.spawn(_completed(json.dumps({"result": "{}"}))):
            driver.complete("hi", model=llm.CLI_DEFAULT_MODEL, output_schema=None)
        self.assertNotIn("--model", self.argv)

    def test_a_named_model_is_passed_through(self) -> None:
        driver = llm.CodexDriver()
        with self.spawn(_completed(), answer="{}"):
            driver.complete("hi", model="gpt-5.5-codex", output_schema=None)
        self.assertEqual(self.argv[self.argv.index("--model") + 1], "gpt-5.5-codex")


class PreflightTest(_CliCase):
    def test_a_missing_binary_is_refused_at_construction_naming_the_command(
        self,
    ) -> None:
        """So the first job refuses clearly, rather than a digest dying midway."""

        with mock.patch.object(llm.shutil, "which", return_value=None):
            with self.assertRaises(NotConfiguredError) as caught:
                llm.ClaudeCodeDriver()
        self.assertEqual(caught.exception.details["executable"], "claude")

    def test_a_signed_out_cli_is_refused_with_the_command_that_fixes_it(self) -> None:
        llm._CLI_AUTH.clear()
        with mock.patch.object(
            llm.ClaudeCodeDriver, "_describe", return_value=(False, "", "no session")
        ):
            with self.assertRaises(NotConfiguredError) as caught:
                llm.ClaudeCodeDriver()
        self.assertIn("claude", caught.exception.details["hint"])

    def test_the_verdict_is_asked_once_per_executable_not_once_per_job(self) -> None:
        """`_create_driver` runs per job; a process start each time is not free."""

        llm._CLI_AUTH.clear()
        with mock.patch.object(
            llm.ClaudeCodeDriver, "_describe", return_value=(True, "max plan", "")
        ) as describe:
            llm.ClaudeCodeDriver()
            llm.ClaudeCodeDriver()
        self.assertEqual(describe.call_count, 1)

    def test_codex_reports_its_verdict_from_stderr(self) -> None:
        """Observed live: `codex login status` leaves stdout empty.

        Reading stdout first looks correct, yields nothing, and then raised
        IndexError on the empty line list.
        """

        with mock.patch.object(
            llm, "_run_cli", return_value=_completed(stderr="Logged in using ChatGPT")
        ):
            signed_in, detail, _ = llm.CodexDriver._describe("/fake/bin/codex")
        self.assertTrue(signed_in)
        self.assertIn("ChatGPT", detail)

    def test_a_silent_but_successful_status_is_still_signed_in(self) -> None:
        with mock.patch.object(llm, "_run_cli", return_value=_completed()):
            signed_in, detail, _ = llm.CodexDriver._describe("/fake/bin/codex")
        self.assertTrue(signed_in)
        self.assertTrue(detail)

    def test_claude_auth_status_is_parsed_as_one_document(self) -> None:
        """It is pretty-printed, unlike the CLI's line-per-object stream output."""

        payload = json.dumps(
            {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"},
            indent=2,
        )
        with mock.patch.object(llm, "_run_cli", return_value=_completed(payload)):
            signed_in, detail, _ = llm.ClaudeCodeDriver._describe("/fake/bin/claude")
        self.assertTrue(signed_in)
        self.assertIn("max", detail)

    def test_an_api_key_session_is_reported_rather_than_passed_silently(self) -> None:
        """It works, and bills per token — which is what this provider avoids."""

        payload = json.dumps({"loggedIn": True, "authMethod": "apiKey"})
        with mock.patch.object(llm, "_run_cli", return_value=_completed(payload)):
            _signed_in, detail, _ = llm.ClaudeCodeDriver._describe("/fake/bin/claude")
        self.assertIn("per token", detail)

    def test_a_status_probe_never_reads_the_terminal(self) -> None:
        """Observed live: the wizard hung on its own provider screen.

        `bk init` asks this question at a terminal with the operator's next
        answers already queued on it. A child that inherits stdin either
        consumes them or blocks forever waiting for input nobody knows it
        wants — and the symptom is a wizard that simply stops.
        """

        with mock.patch.object(llm.subprocess, "run", return_value=_completed()) as run:
            llm._run_cli("/fake/bin/codex", ["login", "status"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_a_status_probe_never_raises(self) -> None:
        """A CLI that hangs is a fact for a screen, exactly as a down ollama is."""

        with mock.patch.object(
            llm.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)
        ):
            self.assertIsNone(llm._run_cli("/fake/bin/codex", ["login", "status"]))

    def test_status_and_the_preflight_cannot_disagree(self) -> None:
        """One `_describe`, two readers — a screen must never promise more."""

        with mock.patch.object(
            llm.CodexDriver, "_describe", return_value=(False, "", "signed out")
        ):
            status = llm.cli_status("codex")
            llm._CLI_AUTH.clear()
            with self.assertRaises(NotConfiguredError):
                llm.CodexDriver()
        self.assertFalse(status.usable)
        self.assertIn("signed out", status.note)

    def test_asking_about_a_provider_that_is_not_a_cli_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            llm.cli_status("openrouter")


class FailureTest(_CliCase):
    def test_a_non_zero_exit_keeps_the_tail_of_the_output(self) -> None:
        """A CLI prints progress first and the cause last."""

        driver = llm.ClaudeCodeDriver()
        with self.spawn(_completed(stderr="noise\n" * 500 + "the real cause", code=2)):
            with self.assertRaises(NotConfiguredError) as caught:
                driver.complete("hi", model="sonnet", output_schema=None)
        self.assertIn("the real cause", caught.exception.details["stderr"])
        self.assertEqual(caught.exception.details["exit_code"], 2)

    def test_a_timeout_names_the_setting_that_raises_it(self) -> None:
        driver = llm.ClaudeCodeDriver(timeout=5)
        with self.spawn(subprocess.TimeoutExpired("claude", 5)):
            with self.assertRaises(NotConfiguredError) as caught:
                driver.complete("hi", model="sonnet", output_schema=None)
        self.assertIn("timeout_seconds", caught.exception.details["hint"])

    def test_an_empty_answer_is_refused_rather_than_parsed(self) -> None:
        """Returning "" sends the repair loop after a violation it cannot fix."""

        driver = llm.ClaudeCodeDriver()
        with self.spawn(_completed(json.dumps({"result": ""}))):
            with self.assertRaises(ModelResponseError):
                driver.complete("hi", model="sonnet", output_schema=None)

    def test_claudes_own_error_result_is_surfaced_as_one(self) -> None:
        driver = llm.ClaudeCodeDriver()
        payload = json.dumps({"is_error": True, "subtype": "error_max_turns", "result": "x"})
        with self.spawn(_completed(payload)):
            with self.assertRaises(ModelResponseError) as caught:
                driver.complete("hi", model="sonnet", output_schema=None)
        self.assertEqual(caught.exception.details["subtype"], "error_max_turns")

    def test_a_run_with_no_final_message_does_not_return_the_event_log(self) -> None:
        """The flag is always passed, so a missing file means no answer was written."""

        driver = llm.CodexDriver()
        with self.spawn(_completed(stdout="tokens used\n14,080")):
            with self.assertRaises(ModelResponseError):
                driver.complete("hi", model="gpt-5.5-codex", output_schema=None)

    def test_output_that_is_not_the_documented_envelope_is_reported(self) -> None:
        driver = llm.ClaudeCodeDriver()
        with self.spawn(_completed("not json at all")):
            with self.assertRaises(ModelResponseError):
                driver.complete("hi", model="sonnet", output_schema=None)


class RoutingTest(_CliCase):
    def test_the_factory_builds_them_with_no_key_and_no_endpoint(self) -> None:
        """Neither exists: the CLI owns the session and is spawned, not fetched."""

        self.assertIsInstance(llm._create_driver("claude-code", {}), llm.ClaudeCodeDriver)
        self.assertIsInstance(llm._create_driver("codex", {}), llm.CodexDriver)

    def test_they_are_supported_providers(self) -> None:
        self.assertTrue(llm.SUBSCRIPTION_CLI_PROVIDERS <= llm.SUPPORTED_PROVIDERS)

    def test_local_only_evidence_is_still_refused(self) -> None:
        """The process is local; the model is not.

        The most plausible misreading of this whole feature is that a CLI on
        your own machine keeps private branches at home. It does not, and the
        router refuses it for the same reason it refuses every non-Ollama
        provider.
        """

        policy = self._policy()
        # Forced back to `local-only` by hand, which is exactly the state an
        # operator reaches by editing `.brain/config.json` after choosing a CLI
        # provider. The wizard no longer writes this combination -- it maps the
        # working branches to `cloud` when the provider is not Ollama -- so
        # asserting on a wizard-shaped policy would test nothing.
        policy["branches"]["10-work"]["privacy"] = PrivacyMode.LOCAL_ONLY.value
        router = llm.PolicyJudgmentRouter(
            VaultConfig.from_dict(policy), llm.JobSpecs()
        )
        # `local-only` reaches the provider check; `never-ingest` is refused
        # before one is even chosen. Both must refuse, for different reasons.
        with self.assertRaises(PolicyError) as caught:
            router.run(job="ingest", branches=["10-work"], variables={"source": "x"})
        self.assertEqual(caught.exception.details["provider"], "claude-code")
        with self.assertRaises(PolicyError):
            router.run(job="ingest", branches=["90-private"], variables={"source": "x"})

    def _policy(self) -> dict[str, Any]:
        """Built through the wizard's own assembly, not by hand.

        Which also pins the other half: a CLI choice must produce a policy the
        domain accepts, with a provider block carrying neither `base_url` nor
        `api_key_env` — there is no endpoint and no key to name.
        """

        import tempfile

        from brainskit.interfaces import onboarding

        with tempfile.TemporaryDirectory() as name:
            environment = onboarding.Environment(
                vault=Path(name),
                workspace=Path(name),
                is_git_repo=False,
                has_agent_dir=False,
                language="English",
            )
            policy = onboarding._assemble(
                environment,
                onboarding.ModelChoice(
                    provider="claude-code",
                    model=llm.CLI_DEFAULT_MODEL,
                    base_url="",
                ),
                dict(onboarding.PRESETS[0].branches),
                [],
            )
        self.assertEqual(policy["providers"], {"claude-code": {}})
        private = [
            name
            for name, policy_ in policy["branches"].items()
            if policy_["privacy"] == PrivacyMode.NEVER_INGEST.value
        ]
        self.assertEqual(private, ["90-private"])
        # The other half of the same decision: a CLI provider is cloud, so the
        # working branches must be too, or no job can run at all.
        self.assertEqual(
            policy["branches"]["10-work"]["privacy"], PrivacyMode.CLOUD.value
        )
        return policy


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
