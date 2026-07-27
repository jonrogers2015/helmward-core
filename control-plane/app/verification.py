"""Thin re-export shim.

The verification logic that used to live in this file has been extracted
verbatim to the standalone `attested` package (PyPI, published 2026-07-24)
and now lives there as the single source of truth. This file exists only so
that api.py, dispatch.py, and tests/test_verification.py don't need an import
line changed -- every name they import from `.verification` / `app.verification`
is simply re-exported from `attested`.

Do not add logic here. If a check type needs to change, change it in
`attested` and bump the pinned version in requirements.txt.
"""
from attested import (  # noqa: F401  (re-exported for backward-compatible imports)
    SPEC_SCHEMA,
    SUPPORTED_TYPES,
    SpecError,
    build_raw_command,
    evaluate_probe_result,
    validate_spec,
)

__all__ = [
    "SPEC_SCHEMA",
    "SUPPORTED_TYPES",
    "SpecError",
    "build_raw_command",
    "evaluate_probe_result",
    "validate_spec",
]
