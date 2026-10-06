#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY_DIR="$PROJECT_ROOT/python"

DRY_RUN=false
VERSION=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [--dry-run] <version>

Release adk-libpetri Python to PyPI (package adk-libpetri).

Builds the sdist and wheel, checks them, installs the wheel into a fresh venv
and runs the test suite against the installed copy (from outside python/, so
the source tree cannot shadow it), then uploads with twine. Publishing is
local, from a developer machine; there is no publish-on-tag workflow and no
token in GitHub secrets.

Prerequisites:
  - python3 with the 'build' and 'twine' modules
  - PyPI credentials: ~/.pypirc or TWINE_USERNAME/TWINE_PASSWORD
  - gh CLI authenticated (for the GitHub release)
  - z3 on PATH (the suite's Z3 gate runs with REQUIRE_Z3=1)

Arguments:
  version       Release version (e.g. 0.1.0)

Options:
  --dry-run     Stamp, build, check and test; skip upload, tag and release
  -h, --help    Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *) VERSION="$1"; shift ;;
    esac
done

if [[ -z "$VERSION" ]]; then
    echo "Error: version argument required" >&2
    usage >&2
    exit 1
fi
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?$ ]]; then
    echo "Error: '$VERSION' is not a PEP 440 release version (e.g. 0.1.0)" >&2
    exit 1
fi

info()  { echo "==> $*"; }
error() { echo "Error: $*" >&2; exit 1; }

# The CHANGELOG section headed `## Python <version> - YYYY-MM-DD`, up to the
# next `## `. Language-qualified, so a Java section of the same number is
# never picked up.
changelog_section() {
    awk -v v="$1" '
        BEGIN { gsub(/\./, "\\.", v); re = "Python " v " " }
        /^## / && $0 ~ re { p = 1; next }
        p && /^## / { exit }
        p
    ' "$PROJECT_ROOT/CHANGELOG.md"
}

info "Validating prerequisites"
if ! git -C "$PROJECT_ROOT" diff --quiet || ! git -C "$PROJECT_ROOT" diff --cached --quiet; then
    error "Working tree has uncommitted changes. Commit or stash first."
fi
python3 -c "import build, twine" 2>/dev/null || error "python3 needs the 'build' and 'twine' modules"
if [[ "$DRY_RUN" == false ]]; then
    if [[ ! -f ~/.pypirc && -z "${TWINE_PASSWORD:-}" ]]; then
        error "No PyPI credentials: ~/.pypirc or TWINE_USERNAME/TWINE_PASSWORD"
    fi
    gh auth status >/dev/null 2>&1 || error "GitHub CLI not authenticated. Run 'gh auth login' first."
    if git -C "$PROJECT_ROOT" rev-parse "python/v${VERSION}" >/dev/null 2>&1; then
        error "Tag python/v${VERSION} already exists."
    fi
    BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD)"
    [[ "$BRANCH" == "main" ]] || error "On branch '${BRANCH}'; releases are cut from main."
    git -C "$PROJECT_ROOT" fetch --quiet origin main
    BEHIND="$(git -C "$PROJECT_ROOT" rev-list --count HEAD..origin/main)"
    [[ "$BEHIND" == "0" ]] || error "main is ${BEHIND} commit(s) behind origin/main. Pull first."
fi
if [[ -z "$(changelog_section "$VERSION" | tr -d '[:space:]')" ]]; then
    error "No CHANGELOG.md section for Python ${VERSION}. Date its heading first ('## Python ${VERSION} - YYYY-MM-DD')."
fi

info "Setting Python version to ${VERSION}"
sed -i.bak -E "s/^version = \"[^\"]+\"/version = \"${VERSION}\"/" "$PY_DIR/pyproject.toml"
sed -i.bak -E "s/^__version__ = \"[^\"]+\"/__version__ = \"${VERSION}\"/" "$PY_DIR/src/adk_libpetri/__init__.py"
rm -f "$PY_DIR/pyproject.toml.bak" "$PY_DIR/src/adk_libpetri/__init__.py.bak"
cd "$PROJECT_ROOT"
git add python/pyproject.toml python/src/adk_libpetri/__init__.py
git diff --cached --quiet || git commit -m "release: python ${VERSION}"

info "Building sdist and wheel"
cd "$PY_DIR"
rm -rf dist
python3 -m build
python3 -m twine check dist/*

info "Testing the built wheel in a fresh venv"
WORK="$(mktemp -d)"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q "$(ls dist/*.whl)[dev]"
# Outside python/, so the source tree cannot shadow the installed copy; the
# conformance suite finds spec/ relative to the tests' parent's parent.
mkdir -p "$WORK/python"
cp -R "$PY_DIR/tests" "$WORK/python/tests"
cp "$PY_DIR/pyproject.toml" "$WORK/python/pyproject.toml"
cp -R "$PROJECT_ROOT/spec" "$WORK/spec"
if ! (cd "$WORK/python" && REQUIRE_Z3=1 "$WORK/venv/bin/pytest" -q tests -p no:cacheprovider); then
    info "Tests failed against the built wheel; version commit remains"
    exit 1
fi

if [[ "$DRY_RUN" == true ]]; then
    info "Dry run complete. Artifacts in python/dist/."
    info "Note: version commit created. Run 'git reset HEAD~1' to undo if needed."
    exit 0
fi

info "Uploading to PyPI"
python3 -m twine upload dist/*

cd "$PROJECT_ROOT"
info "Creating tag python/v${VERSION}"
git tag -a "python/v${VERSION}" -m "Release python ${VERSION}"
git push origin HEAD
git push origin "python/v${VERSION}"

info "Creating GitHub release"
gh release create "python/v${VERSION}" --title "Python v${VERSION}" \
    --notes "$(changelog_section "$VERSION")"

info "Released Python v${VERSION} to PyPI and GitHub."
