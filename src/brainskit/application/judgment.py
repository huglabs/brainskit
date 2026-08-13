"""Running a judgment job and holding the model to its output schema.

Only the machinery lives here, not the jobs themselves. `ask`, `digest`,
`ingest`, `resurface` and the semantic half of `lint` each build their own
variables and then hand them to the same bounded repair loop, and that loop is
the thing worth having exactly one of: it is what guarantees a failed judgment
is reported as a failure rather than replaced with something plausible.

A leaf -- it needs the job specs and a provider, and knows nothing about the
vault's contents.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from brainskit.application.ports import JobSpecPort, JudgmentPort
from brainskit.application.schema import validate_schema
from brainskit.domain.model import (
    ModelResponseError,
    NotConfiguredError,
)


class JudgmentRunner:
    """Owns the one repair loop every schema-bound job shares."""

    def __init__(
        self, jobs: JobSpecPort | None, judgment: JudgmentPort | None
    ):
        self.jobs = jobs
        self.judgment = judgment


    def run(
        self,
        *,
        job: str,
        branches: list[str],
        variables: dict[str, Any],
        max_attempts: int = 3,
        validator: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        judgment = self.require()
        if not self.jobs:
            raise NotConfiguredError("Job specifications are not configured")
        schema = self.jobs.schema(job)
        if schema is None:
            raise NotConfiguredError(
                "Structured job has no output schema", details={"job": job}
            )
        feedback = ""
        last_failures: list[dict[str, Any]] = []
        last_response = ""
        for attempt in range(1, max_attempts + 1):
            response = judgment.run(
                job=job,
                branches=branches,
                variables={
                    **variables,
                    "repair_feedback": feedback,
                },
                output_schema=schema,
            )
            last_response = response
            try:
                parsed = json.loads(response)
            except json.JSONDecodeError as exc:
                last_failures = [
                    {
                        "path": "$",
                        "code": "json.invalid",
                        "message": str(exc),
                    }
                ]
            else:
                if not isinstance(parsed, dict):
                    last_failures = [
                        {
                            "path": "$",
                            "code": "schema.type",
                            "message": "Expected an object",
                        }
                    ]
                else:
                    last_failures = validate_schema(parsed, schema)
                    if not last_failures and validator:
                        last_failures = validator(parsed)
                    if not last_failures:
                        return parsed
            feedback = json.dumps(
                {
                    "attempt": attempt,
                    "invalid_output": last_response[-8_000:],
                    "validation_failures": last_failures,
                    "instruction": "Return a corrected JSON object only.",
                },
                ensure_ascii=False,
            )
        raise ModelResponseError(
            "Provider output remained invalid after repair attempts",
            details={
                "job": job,
                "attempts": max_attempts,
                "failures": last_failures,
            },
        )

    def require(self) -> JudgmentPort:
        """The configured provider, or a clear error naming what is missing."""
        if not self.judgment:
            raise NotConfiguredError("Judgment layer is not configured")
        return self.judgment
