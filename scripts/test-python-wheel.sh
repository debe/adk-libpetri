#!/usr/bin/env bash
set -euo pipefail

# Test a built adk-libpetri wheel the way it ships: installed into a fresh
# venv, with the test suite run from a copy outside python/, so the source
# tree cannot shadow the installed package. CI runs this on every push and
# scripts/release-python.sh runs it before uploading, so a release tests
# exactly what CI tested.

usage() {
    cat <<EOF
Usage: $(basename "$0") <wheel>

Installs <wheel> with its [dev] extras into a fresh venv and runs the Python
test suite against it with REQUIRE_Z3=1 (so the z3 binary must be on PATH).
EOF
}

if [[ $# -ne 1 || "$1" == -h || "$1" == --help ]]; then
    usage
    [[ $# -eq 1 ]] && exit 0
    exit 1
fi

WHEEL="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
[[ -f "$WHEEL" ]] || { echo "Error: no wheel at $1" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY_DIR="$PROJECT_ROOT/python"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> Installing $(basename "$WHEEL") into a fresh venv"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q --upgrade pip
"$WORK/venv/bin/pip" install -q "${WHEEL}[dev]"

# The suite finds spec/ (conformance) and docs/diagrams (readme_diagrams)
# relative to the tests' parent's parent, so mirror that layout.
mkdir -p "$WORK/python" "$WORK/docs/diagrams"
cp -R "$PY_DIR/tests" "$WORK/python/tests"
cp "$PY_DIR/pyproject.toml" "$WORK/python/pyproject.toml"
cp -R "$PROJECT_ROOT/spec" "$WORK/spec"
cp -R "$PROJECT_ROOT/docs/diagrams/dot" "$PROJECT_ROOT/docs/diagrams/hero" "$WORK/docs/diagrams/"

INSTALLED="$("$WORK/venv/bin/python" -c 'import adk_libpetri, pathlib; print(pathlib.Path(adk_libpetri.__file__).parent)')"
case "$INSTALLED" in
    "$WORK"/venv/*) ;;
    *) echo "Error: adk_libpetri imports from $INSTALLED, not the installed wheel" >&2; exit 1 ;;
esac

echo "==> Running the test suite against the installed wheel"
cd "$WORK/python"
REQUIRE_Z3=1 "$WORK/venv/bin/pytest" -q tests -p no:cacheprovider
