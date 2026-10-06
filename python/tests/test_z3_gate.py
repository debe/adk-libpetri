"""Fails the build when z3 is missing on a machine that must have it.

Every ``requires_z3`` test skips silently without the binary, which would let
CI go green with no proof run. With ``REQUIRE_Z3=1`` (set in CI) this fails.
"""

from __future__ import annotations

import os

import libpetri as lp
import pytest


def test_z3_is_available_where_required() -> None:
    if os.environ.get("REQUIRE_Z3") != "1":
        pytest.skip("REQUIRE_Z3 not set")
    assert lp.z3_available(), "z3 binary not found (PATH or LIBPETRI_Z3) but REQUIRE_Z3=1"
