"""One ``verify()`` per property, every verdict must be ``proven`` (Java ``SmtProofs``).

``is_proven()`` is the strong form on purpose: libpetri downgrades a verdict
it cannot back to ``unknown``, and ``not is_violated()`` would accept that,
letting a claimed proof rot silently.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import libpetri as lp
import pytest

requires_z3 = pytest.mark.skipif(not lp.z3_available(), reason="z3 binary not available")


def assert_each_proven(net: Any, properties: Mapping[str, lp.SmtProperty], **options: Any) -> None:
    failures = {}
    for label, prop in properties.items():
        result = lp.verify(net, prop, **options)
        if not result.is_proven():
            failures[label] = f"{result.verdict}\n{result.report}"
    assert not failures, f"every property must be Proven, one verify() each: {failures}"


def assert_all_proven(result: lp.SubnetVerificationResult) -> None:
    failures = {
        pr.property.description(): f"{pr.result.verdict}\n{pr.result.report}"
        for pr in result.property_results()
        if not pr.result.is_proven()
    }
    assert not failures, f"every property must be Proven: {failures}"
