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

Stamps __version__ (the package's one version source), builds the sdist and
wheel, checks them with twine, tests the installed wheel with
scripts/test-python-wheel.sh (the check CI runs on every push), then uploads
with twine, tags python/v<version>, pushes and creates the GitHub release from
the CHANGELOG section. Publishing is local, from a developer machine; there is
no publish-on-tag workflow and no token in GitHub secrets.

Prerequisites:
  - python3 with the 'build' and 'twine' modules
  - PyPI credentials: a [pypi] section in ~/.pypirc, or TWINE_USERNAME and
    TWINE_PASSWORD
  - gh CLI authenticated, with an active account that can create releases
    here, and git able to push to origin (both checked before the upload)
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

have_pypi_auth() {
    if [[ -n "${TWINE_USERNAME:-}" && -n "${TWINE_PASSWORD:-}" ]]; then
        return 0
    fi
    [[ -f ~/.pypirc ]] && grep -q '^\[pypi\]' ~/.pypirc
}

info "Validating prerequisites"
if ! git -C "$PROJECT_ROOT" diff --quiet || ! git -C "$PROJECT_ROOT" diff --cached --quiet; then
    error "Working tree has uncommitted changes. Commit or stash first."
fi
python3 -c "import build, twine" 2>/dev/null || error "python3 needs the 'build' and 'twine' modules"
command -v z3 >/dev/null 2>&1 || [[ -n "${LIBPETRI_Z3:-}" ]] || error "z3 not found on PATH (the wheel test runs with REQUIRE_Z3=1)"
if [[ "$DRY_RUN" == false ]]; then
    have_pypi_auth || error "No PyPI credentials: a [pypi] section in ~/.pypirc, or TWINE_USERNAME/TWINE_PASSWORD"
    gh auth status >/dev/null 2>&1 || error "GitHub CLI not authenticated. Run 'gh auth login' first."
    if git -C "$PROJECT_ROOT" rev-parse "python/v${VERSION}" >/dev/null 2>&1; then
        error "Tag python/v${VERSION} already exists."
    fi
    BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD)"
    [[ "$BRANCH" == "main" ]] || error "On branch '${BRANCH}'; releases are cut from main."
    git -C "$PROJECT_ROOT" fetch --quiet origin main
    BEHIND="$(git -C "$PROJECT_ROOT" rev-list --count HEAD..origin/main)"
    [[ "$BEHIND" == "0" ]] || error "main is ${BEHIND} commit(s) behind origin/main. Pull first."
    # The PyPI upload cannot be undone, so prove the steps after it (push,
    # GitHub release) are allowed before it runs. With several gh accounts the
    # active one may lack push rights to this repository.
    git -C "$PROJECT_ROOT" push --dry-run --quiet origin HEAD 2>/dev/null \
        || error "git cannot push to origin. Check the credentials git uses (with several gh accounts: 'gh auth switch')."
    [[ "$(gh repo view --json viewerPermission --jq .viewerPermission 2>/dev/null)" =~ ^(ADMIN|MAINTAIN|WRITE)$ ]] \
        || error "The active gh account cannot create releases here. Run 'gh auth switch'."
fi
if [[ -z "$(changelog_section "$VERSION" | tr -d '[:space:]')" ]]; then
    error "No CHANGELOG.md section for Python ${VERSION}. Date its heading first ('## Python ${VERSION} - YYYY-MM-DD')."
fi

info "Setting Python version to ${VERSION}"
# pyproject.toml reads the version from __version__ ([tool.hatch.version]).
sed -i.bak -E "s/^__version__ = \"[^\"]+\"/__version__ = \"${VERSION}\"/" "$PY_DIR/src/adk_libpetri/__init__.py"
rm -f "$PY_DIR/src/adk_libpetri/__init__.py.bak"
cd "$PROJECT_ROOT"
git add python/src/adk_libpetri/__init__.py
STAMPED=false
if ! git diff --cached --quiet; then
    git commit -m "release: python ${VERSION}"
    STAMPED=true
fi

info "Building sdist and wheel"
cd "$PY_DIR"
rm -rf dist
python3 -m build
python3 -m twine check dist/*
WHEEL="dist/adk_libpetri-${VERSION}-py3-none-any.whl"
[[ -f "$WHEEL" && -f "dist/adk_libpetri-${VERSION}.tar.gz" ]] \
    || error "dist/ does not hold ${VERSION}'s wheel and sdist: $(ls dist)"

info "Testing the built wheel"
if ! "$SCRIPT_DIR/test-python-wheel.sh" "$WHEEL"; then
    [[ "$STAMPED" == true ]] && info "Tests failed against the built wheel; the version commit remains"
    exit 1
fi

if [[ "$DRY_RUN" == true ]]; then
    info "Dry run complete. Artifacts in python/dist/."
    if [[ "$STAMPED" == true ]]; then
        info "Note: version commit created. Run 'git reset HEAD~1' to undo if needed."
    else
        info "__version__ was already ${VERSION}: no commit was made."
    fi
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
PRERELEASE=()
[[ "$VERSION" =~ (a|b|rc)[0-9]+$ ]] && PRERELEASE=(--prerelease)
gh release create "python/v${VERSION}" --title "Python v${VERSION}" "${PRERELEASE[@]}" \
    --notes "$(changelog_section "$VERSION")"

info "Released Python v${VERSION} to PyPI and GitHub."
