"""A provider being down is not a malformed request.

Six sites raised "Provider is unreachable" / "Docker command failed" as
`validation_error`, whose documented meaning is *"the request itself is wrong --
fix it and send it again"*. An agent that reads its own error codes will keep
rewriting a well-formed request against a provider that is simply not running.

`not_configured` is the code that means "stop retrying and tell the operator".
The change is safe by construction: `NotConfiguredError` subclasses
`ValidationError`, so every `except` and every exit code is unchanged -- only
`.code` sharpens.
"""

from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from brainskit.domain.model import ValidationError


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

        from brainskit.application.privacy import _validate_consumer

        with self.assertRaises(ValidationError) as raised:
            _validate_consumer("everyone")
        self.assertEqual(raised.exception.code, "validation_error")

    def test_a_bad_base_url_is_configuration_not_a_bad_request(self) -> None:
        """Already correct before this task, and pinned so it stays that way."""

        from brainskit.infrastructure import llm

        with self.assertRaises(ValidationError) as raised:
            llm._post_json("ftp://example.invalid/x", {}, headers={}, timeout=1)
        self.assertEqual(raised.exception.code, "not_configured")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
