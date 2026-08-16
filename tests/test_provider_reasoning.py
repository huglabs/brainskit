"""An empty completion is a cause, not a schema violation.

A reasoning model that spends its whole completion budget thinking returns a
well-formed response whose `content` is the empty string. `OpenAICompatibleDriver`
passed that straight back, so the repair loop went after it as if the model had
written malformed JSON: three attempts, three empty strings, and a final
`model_response_invalid` naming `json.invalid -- Expecting value: line 1
column 1 (char 0)`. The cause is in `finish_reason`, which nothing read.

Observed live against OpenRouter, which is what motivated this file.
`nvidia/nemotron-3-super-120b-a12b:free` burned 832 completion tokens, all of
them reasoning, and returned `finish_reason: "error"` with empty content --
6m46s of wall clock spent reporting the wrong cause. The sibling
`nvidia/nemotron-3-nano-30b-a3b:free` answered the same prompt in 12.7s, and in
3.8s once reasoning was suppressed, with 898 reasoning tokens dropping to 0.

The Anthropic driver already had this guard: `_text` checks `stop_reason`
before `content` precisely because both terminal cases "produce a response that
parses fine and means nothing". The OpenAI-compatible driver had no equivalent.
That asymmetry is what shipped.

Suppression is opt-in per provider, and deliberately passed through rather than
modelled -- the shape belongs to the provider. It is also a preference, never a
correctness requirement, so an endpoint that refuses to skip reasoning (live
case: `openai/gpt-oss-20b:free`, *"Reasoning is mandatory for this endpoint"*)
must still answer the job rather than fail on an operator's choice of model.
"""

from __future__ import annotations

import io
import json as _json
import sys
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

from brainskit.domain.model import NotConfiguredError, ValidationError
from brainskit.infrastructure import llm

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _http_error(status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://provider.invalid/v1", status, "boom", {}, io.BytesIO(body)
    )


def _completion(content: Any, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}]
    }


def _driver(**extra: Any) -> llm.OpenAICompatibleDriver:
    return llm.OpenAICompatibleDriver(
        base_url="https://openrouter.invalid/api/v1",
        api_key="k",
        provider="openrouter",
        api_key_env="OPENROUTER_API_KEY",
        **extra,
    )


def _responder(sent: list[dict[str, Any]], *responses: Any):
    """Reply with each response in turn, recording what was sent."""

    queue = list(responses)

    def respond(request, *_args, **_kwargs):
        sent.append(_json.loads(request.data))
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return mock.MagicMock(
            __enter__=lambda _: mock.Mock(read=lambda: _json.dumps(reply).encode()),
            __exit__=lambda *_: False,
        )

    return respond


class EmptyCompletionNamesItsCauseTest(unittest.TestCase):
    def test_an_empty_completion_is_refused_rather_than_returned(self) -> None:
        sent: list[dict[str, Any]] = []
        with mock.patch.object(
            llm.urllib.request,
            "urlopen",
            side_effect=_responder(sent, _completion("", finish_reason="error")),
        ):
            with self.assertRaises(ValidationError) as raised:
                _driver().complete("hi", model="m", output_schema=None)

        self.assertEqual(raised.exception.code, "model_response_invalid")
        self.assertEqual(raised.exception.details["finish_reason"], "error")

    def test_the_refusal_names_the_knob_that_fixes_it(self) -> None:
        """The remedy has to name the config key, or it is just a diagnosis."""

        with mock.patch.object(
            llm.urllib.request,
            "urlopen",
            side_effect=_responder([], _completion("")),
        ):
            with self.assertRaises(ValidationError) as raised:
                _driver().complete("hi", model="m", output_schema=None)

        remedy = raised.exception.details["remedy"]
        self.assertIn("providers.openrouter.reasoning", remedy)

    def test_whitespace_only_content_counts_as_empty(self) -> None:
        """`"\\n"` parses as JSON no better than `""` does."""

        with mock.patch.object(
            llm.urllib.request,
            "urlopen",
            side_effect=_responder([], _completion("  \n ")),
        ):
            with self.assertRaises(ValidationError):
                _driver().complete("hi", model="m", output_schema=None)

    def test_a_null_content_does_not_become_the_string_none(self) -> None:
        """`str(None)` is `"None"` -- a 4-character non-answer the loop would
        have chased. Only `finish_reason` distinguishes it from real output."""

        with mock.patch.object(
            llm.urllib.request,
            "urlopen",
            side_effect=_responder([], _completion(None)),
        ):
            with self.assertRaises(ValidationError) as raised:
                _driver().complete("hi", model="m", output_schema=None)

        self.assertEqual(raised.exception.code, "model_response_invalid")

    def test_real_output_still_comes_back_untouched(self) -> None:
        with mock.patch.object(
            llm.urllib.request,
            "urlopen",
            side_effect=_responder([], _completion('{"ok": true}')),
        ):
            text = _driver().complete("hi", model="m", output_schema=None)

        self.assertEqual(text, '{"ok": true}')


class ReasoningSuppressionIsOptInTest(unittest.TestCase):
    def test_nothing_is_sent_when_the_operator_configured_nothing(self) -> None:
        """Absent means absent. A model that reasons by default keeps doing so."""

        sent: list[dict[str, Any]] = []
        with mock.patch.object(
            llm.urllib.request,
            "urlopen",
            side_effect=_responder(sent, _completion("ok")),
        ):
            _driver().complete("hi", model="m", output_schema=None)

        self.assertNotIn("reasoning", sent[0])

    def test_the_configured_control_reaches_the_provider(self) -> None:
        sent: list[dict[str, Any]] = []
        with mock.patch.object(
            llm.urllib.request,
            "urlopen",
            side_effect=_responder(sent, _completion("ok")),
        ):
            _driver(reasoning={"enabled": False, "exclude": True}).complete(
                "hi", model="m", output_schema=None
            )

        self.assertEqual(sent[0]["reasoning"], {"enabled": False, "exclude": True})

    def test_an_endpoint_that_requires_reasoning_still_answers(self) -> None:
        """Live case: gpt-oss-20b 400s with "Reasoning is mandatory for this
        endpoint". Suppression is a preference, so the job must survive it."""

        sent: list[dict[str, Any]] = []
        responder = _responder(
            sent,
            _http_error(
                400, b'{"error":{"message":"Reasoning is mandatory for this endpoint"}}'
            ),
            _completion('{"ok": true}'),
        )
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=responder):
            text = _driver(reasoning={"enabled": False}).complete(
                "hi", model="m", output_schema=None
            )

        self.assertEqual(text, '{"ok": true}')
        self.assertIn("reasoning", sent[0])
        self.assertNotIn("reasoning", sent[1])

    def test_an_unrelated_400_is_not_retried_without_reasoning(self) -> None:
        """Retrying a 400 that meant something else drops a parameter the
        operator asked for, then fails again for the original reason."""

        sent: list[dict[str, Any]] = []
        responder = _responder(
            sent, _http_error(400, b'{"error":{"message":"unknown model foo"}}')
        )
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=responder):
            with self.assertRaises(ValidationError):
                _driver(reasoning={"enabled": False}).complete(
                    "hi", model="m", output_schema=None
                )

        self.assertEqual(len(sent), 1)

    def test_a_reasoning_400_without_the_option_set_is_not_swallowed(self) -> None:
        """The detector must not fire on a request that never carried it."""

        sent: list[dict[str, Any]] = []
        responder = _responder(
            sent,
            _http_error(400, b'{"error":{"message":"reasoning is mandatory"}}'),
        )
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=responder):
            with self.assertRaises(ValidationError):
                _driver().complete("hi", model="m", output_schema=None)

        self.assertEqual(len(sent), 1)


class ReasoningConfigTest(unittest.TestCase):
    def test_the_option_is_threaded_from_provider_config(self) -> None:
        """End to end: config -> `_create_driver` -> the wire."""

        import os

        os.environ["CANARY_REASONING_KEY_9f2a"] = "sk-not-a-real-key"
        self.addCleanup(os.environ.pop, "CANARY_REASONING_KEY_9f2a", None)
        driver = llm._create_driver(
            "openrouter",
            {
                "base_url": "https://openrouter.invalid/api/v1",
                "api_key_env": "CANARY_REASONING_KEY_9f2a",
                "reasoning": {"enabled": False, "exclude": True},
            },
        )
        sent: list[dict[str, Any]] = []
        with mock.patch.object(
            llm.urllib.request,
            "urlopen",
            side_effect=_responder(sent, _completion("ok")),
        ):
            driver.complete("hi", model="m", output_schema=None)

        self.assertEqual(sent[0]["reasoning"], {"enabled": False, "exclude": True})

    def test_a_non_object_reasoning_value_is_a_configuration_error(self) -> None:
        with self.assertRaises(NotConfiguredError):
            llm._reasoning_option({"reasoning": "off"})

    def test_an_absent_key_reads_as_none_not_an_empty_object(self) -> None:
        """`{}` is a real instruction to some providers; absent is not."""

        self.assertIsNone(llm._reasoning_option({}))
        self.assertEqual(llm._reasoning_option({"reasoning": {}}), {})


if __name__ == "__main__":
    unittest.main()
