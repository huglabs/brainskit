"""JSON Schema validation, kept out of the domain layer.

`jsonschema` was the domain's only third-party dependency, imported for this one
engine while every caller already lived in `application/`. A domain layer that
reaches for a vendor library to answer a question its own callers ask is a layer
boundary that exists on paper only, so the engine moved to the side that was
already using it. `domain/model.py` now imports nothing outside the standard
library.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, NoReturn

from jsonschema import FormatChecker  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable


@lru_cache(maxsize=32)
def _compiled_validator(canonical: str) -> Any:
    """Compile a validator once per distinct schema.

    `lint` validates every wiki page against the same `.brain/schema.json`, and
    compiling it per page made the cost of a lint scale with the page count for
    no reason. Keyed on the canonical serialization rather than object identity
    because the vault re-reads and re-parses the schema from disk, so the same
    schema is a different object on every call.

    A schema that fails `check_schema` raises here on every call: `lru_cache`
    does not memoize exceptions, which is what we want — an invalid schema must
    keep being reported, not be reported once and then silently pass.
    """

    schema = json.loads(canonical)
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return validator_class(
        schema,
        format_checker=FormatChecker(),
        registry=Registry(retrieve=_deny_remote_schema),  # type: ignore[call-arg]
    )


def _validator_for_schema(schema: dict[str, Any]) -> Any:
    try:
        canonical = json.dumps(schema, sort_keys=True)
    except (TypeError, ValueError):
        # Not serializable, so not cacheable. Compiling directly keeps the
        # caller's behaviour identical rather than turning a validation
        # question into a caching error.
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        return validator_class(
            schema,
            format_checker=FormatChecker(),
            registry=Registry(retrieve=_deny_remote_schema),  # type: ignore[call-arg]
        )
    return _compiled_validator(canonical)


def validate_schema(
    value: Any, schema: dict[str, Any], path: str = "$"
) -> list[dict[str, str]]:
    """Validate a value against its declared JSON Schema draft and formats."""

    try:
        validator = _validator_for_schema(schema)
        errors = sorted(
            validator.iter_errors(value),
            key=lambda error: (
                tuple(str(part) for part in error.absolute_path),
                str(error.validator),
                error.message,
            ),
        )
    except SchemaError as exc:
        return [
            {
                "path": path,
                "code": "schema.invalid",
                "message": exc.message,
            }
        ]
    except Unresolvable as exc:
        return [
            {
                "path": path,
                "code": "schema.unresolvable_ref",
                "message": str(exc),
            }
        ]
    return [
        {
            "path": _schema_error_path(path, list(error.absolute_path)),
            "code": f"schema.{error.validator or 'invalid'}",
            "message": error.message,
        }
        for error in errors
    ]


def _deny_remote_schema(uri: str) -> NoReturn:
    raise NoSuchResource(ref=uri)  # type: ignore[call-arg]


def _schema_error_path(root: str, parts: list[Any]) -> str:
    result = root
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(part)):
            result += f".{part}"
        else:
            escaped = str(part).replace("\\", "\\\\").replace('"', '\\"')
            result += f'["{escaped}"]'
    return result
