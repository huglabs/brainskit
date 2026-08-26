"""Where a provider key comes from, and what must never be able to see it.

`providers.<name>.api_key_env` names an environment variable. That is the right
contract for a server and an incomplete one for a laptop: `bk init` can ask for
a key, but it cannot export one into the shell that runs the next command, so
onboarding used to end at *"now set OPENROUTER_API_KEY"* -- a step with no
feedback and no way to tell whether it worked.

The store closes that gap, and everything here is about the two properties that
make it safe rather than merely convenient:

- **The environment always wins.** A file that could override an exported
  variable would let a key typed once on a laptop redirect a scripted
  deployment to another account, and *both* paths would still produce working
  requests -- so the failure would never surface as a failure.
- **The secret reaches exactly one file.** Not the policy, which
  `bk init --print-config` prints; not the summary, which lands in bug reports;
  not the CLI's own JSON output.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brainskit.domain.model import NotConfiguredError, ValidationError
from brainskit.infrastructure import credentials, llm
from brainskit.infrastructure.credentials import CredentialStore

CANARY = "CANARY_PROVIDER_KEY_7b31"


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "brainskit" / "credentials.json"
        self.store = CredentialStore(self.path)

    def test_the_environment_wins_over_a_stored_value(self) -> None:
        """The property that keeps a laptop from redirecting a deployment."""

        self.store.remember(CANARY, "from-the-file")
        self.assertEqual(
            self.store.lookup(CANARY, {CANARY: "from-the-environment"}),
            "from-the-environment",
        )

    def test_a_stored_value_answers_when_the_environment_does_not(self) -> None:
        self.store.remember(CANARY, "from-the-file")
        self.assertEqual(self.store.lookup(CANARY, {}), "from-the-file")

    def test_an_empty_exported_variable_is_not_an_answer(self) -> None:
        """`export X=` is how a shell says "unset", not "authenticate as ''"."""

        self.store.remember(CANARY, "from-the-file")
        self.assertEqual(self.store.lookup(CANARY, {CANARY: ""}), "from-the-file")

    def test_listing_names_never_discloses_a_value(self) -> None:
        self.store.remember(CANARY, "sk-or-v1-secret")
        self.assertEqual(self.store.names(), [CANARY])
        self.assertNotIn("sk-or-v1-secret", json.dumps(self.store.names()))

    def test_the_file_and_its_directory_are_readable_only_by_this_account(
        self,
    ) -> None:
        self.store.remember(CANARY, "sk-or-v1-secret")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_a_directory_widened_after_creation_is_narrowed_again(self) -> None:
        """Re-applied on every write, exactly as the vault registry does.

        A umask or an unrelated tool can widen the directory after it exists,
        which is precisely the state the mode is there to prevent.
        """

        self.store.remember(CANARY, "one")
        os.chmod(self.path.parent, 0o755)  # noqa: S103 -- the state under test
        self.store.remember(CANARY, "two")
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_remembering_twice_replaces_rather_than_appends(self) -> None:
        self.store.remember(CANARY, "one")
        self.store.remember(CANARY, "two")
        self.assertEqual(self.store.stored(CANARY), "two")
        self.assertEqual(self.store.names(), [CANARY])

    def test_forgetting_reports_whether_anything_was_held(self) -> None:
        self.store.remember(CANARY, "one")
        self.assertTrue(self.store.forget(CANARY))
        self.assertFalse(self.store.forget(CANARY))
        self.assertEqual(self.store.stored(CANARY), "")

    def test_an_empty_value_is_refused_rather_than_stored(self) -> None:
        """Storing "" would read as configured and fail as unauthenticated."""

        with self.assertRaises(ValidationError):
            self.store.remember(CANARY, "   ")

    def test_a_missing_file_is_empty_rather_than_an_error(self) -> None:
        self.assertEqual(self.store.names(), [])
        self.assertEqual(self.store.lookup(CANARY, {}), "")

    def test_a_corrupt_file_is_reported_rather_than_read_as_empty(self) -> None:
        """Silently reading as empty would present a stored key as never set."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValidationError) as caught:
            self.store.names()
        self.assertIn(str(self.path), json.dumps(caught.exception.details))

    def test_no_temporary_file_survives_a_write(self) -> None:
        self.store.remember(CANARY, "one")
        leftovers = [p.name for p in self.path.parent.iterdir() if p != self.path]
        self.assertEqual(leftovers, [])


class LocationTest(unittest.TestCase):
    def test_it_sits_beside_the_vault_registry_under_xdg(self) -> None:
        environment = {"XDG_CONFIG_HOME": "/xdg"}
        self.assertEqual(
            credentials.default_credentials_path(environment),
            Path("/xdg/brainskit/credentials.json"),
        )

    def test_a_relative_xdg_value_is_ignored_per_the_specification(self) -> None:
        path = credentials.default_credentials_path({"XDG_CONFIG_HOME": "relative"})
        self.assertEqual(path, Path.home() / ".config/brainskit/credentials.json")

    def test_there_is_no_legacy_name_fallback(self) -> None:
        """The registry honours a pre-0.5 `brainkit/` file; this must not.

        Nothing wrote a credential under the old name, so a fallback would only
        widen the set of places a secret might be found.
        """

        with tempfile.TemporaryDirectory() as name:
            legacy = Path(name) / "brainkit"
            legacy.mkdir()
            (legacy / "credentials.json").write_text("{}", encoding="utf-8")
            path = credentials.default_credentials_path({"XDG_CONFIG_HOME": name})
        self.assertEqual(path.parent.name, "brainskit")


class DriverLookupTest(unittest.TestCase):
    """End to end: config -> `_create_driver` -> the credential that is used."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "credentials.json"
        patched = mock.patch.object(
            credentials, "default_credentials_path", return_value=self.path
        )
        patched.start()
        self.addCleanup(patched.stop)
        os.environ.pop(CANARY, None)

    def config(self) -> dict[str, str]:
        return {
            "base_url": "https://openrouter.invalid/api/v1",
            "api_key_env": CANARY,
        }

    def test_a_stored_key_configures_a_driver_with_nothing_exported(self) -> None:
        """The whole point: `bk init` asked, and the next command works."""

        CredentialStore(self.path).remember(CANARY, "sk-or-stored")
        driver = llm._create_driver("openrouter", self.config())
        self.assertEqual(driver.api_key, "sk-or-stored")

    def test_an_exported_key_still_wins_at_the_driver(self) -> None:
        CredentialStore(self.path).remember(CANARY, "sk-or-stored")
        os.environ[CANARY] = "sk-or-exported"
        self.addCleanup(os.environ.pop, CANARY, None)
        driver = llm._create_driver("openrouter", self.config())
        self.assertEqual(driver.api_key, "sk-or-exported")

    def test_neither_source_answering_still_refuses_with_both_remedies(self) -> None:
        with self.assertRaises(NotConfiguredError) as caught:
            llm._create_driver("openrouter", self.config())
        hint = str(caught.exception.details.get("hint", ""))
        self.assertIn(CANARY, hint)
        self.assertIn("bk credential set", hint)

    def test_the_command_that_hint_names_actually_exists(self) -> None:
        """A remedy naming a command nobody implemented is worse than none."""

        from brainskit.interfaces import cli

        parser = cli.build_parser()
        self.assertIn("credential", cli._command_help_index(parser))
        self.assertIn(
            "usage: bk credential", cli._subcommand_help(parser, "credential")
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
