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

from brainskit.application.ports import (
    JobSpecPort,
    JudgmentPort,
    JudgmentRoute,
    JudgmentRoutePort,
)
from brainskit.application.schema import validate_schema
from brainskit.domain.model import (
    ModelResponseError,
    NotConfiguredError,
    PolicyError,
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

    def route_for(self, *, job: str, branches: list[str]) -> JudgmentRoute | None:
        """Where the configured provider would route `job`, if it can say.

        None for a port that only runs (a substitute), which a caller treats
        as the strictest route. Refusals propagate: they are the ones `run`
        would raise for the same arguments.
        """

        judgment = self.require()
        if not isinstance(judgment, JudgmentRoutePort):
            return None
        return judgment.route_for(job=job, branches=branches)

    def refuse_without_evidence(
        self, *, job: str, branches: list[str], withheld: int, nothing: str, next_step: str
    ) -> None:
        """The router's refusal of an empty bundle, in words that fit it.

        With no evidence, `branches` is the `_inbox` fallback, so a cloud-mapped
        job on a vault whose inbox is `local-only` was refused as "Local-only
        content can only be routed to Ollama" -- about content that does not
        exist. The refusal stands; only what it says changes. A route that
        accepts the empty bundle returns, and the job runs as it always has.
        """

        try:
            self.route_for(job=job, branches=branches)
        except PolicyError as exc:
            hint = next_step
            if exc.details.get("privacy") == "local-only":
                hint += (
                    "; with no evidence the job routes under the _inbox policy, "
                    "which is local-only, so to run it anyway map "
                    f"job_models.{job}.local-only to a local provider"
                )
            raise PolicyError(
                f"{nothing}, and nothing was sent to any model",
                details={"job": job, "withheld_sources": withheld, "hint": hint},
            ) from exc

    def consumer_for(self, *, job: str, branches: list[str]) -> str:
        """The boundary of the model `job` reaches over `local`-visible branches.

        The router answers, not a copy of it: `local` only when it routes those
        branches to a model on this machine. Its privacy refusal means the job
        is mapped to a cloud provider for this evidence, and a port that cannot
        say is assumed to be one -- both answer `cloud`, which only ever
        narrows. A caller reads its evidence again under the answer, and `run`
        routes that narrower bundle itself, so the refusal still stands behind
        it. Refusals that are not about privacy (`NotConfiguredError`)
        propagate before any prompt exists.
        """

        try:
            route = self.route_for(job=job, branches=branches)
        except PolicyError:
            return "cloud"
        return "local" if route is not None and route.local else "cloud"

    def require(self) -> JudgmentPort:
        """The configured provider, or a clear error naming what is missing."""
        if not self.judgment:
            raise NotConfiguredError("Judgment layer is not configured")
        return self.judgment
