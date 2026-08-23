#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
JAVA_DIR="$PROJECT_ROOT/java"

# --- Defaults ---
DRY_RUN=false
VERSION=""

# --- Usage ---
usage() {
    cat <<EOF
Usage: $(basename "$0") [--dry-run] <version>

Release adk-libpetri Java to Maven Central.

Release profile lives in java/pom.xml: sources, javadocs, GPG signing,
and central-publishing-maven-plugin. This script only drives that
profile; Maven handles build, test, sign, bundle, upload, and publish.

Prerequisites:
  - GPG signing key available to gpg-agent
  - ~/.m2/settings.xml with <server id="central"> credentials
  - gh CLI authenticated (for GitHub release)

Arguments:
  version       Release version (e.g. 0.4.0)

Options:
  --dry-run     Build, test, and sign (mvn clean verify -Prelease); skip upload, tag, release
  -h, --help    Show this help

Example:
  $(basename "$0") 0.4.0
  $(basename "$0") --dry-run 0.4.0
EOF
}

# --- Parse args ---
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

# Reject anything versions:set would happily accept but Central would not.
# Without this, `release-java.sh foo` stamps <version>foo</version>.
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]; then
    echo "Error: '$VERSION' is not a semver release version (e.g. 0.4.0)" >&2
    exit 1
fi

if [[ "$VERSION" == *-SNAPSHOT ]]; then
    echo "Error: Maven Central rejects -SNAPSHOT versions." >&2
    exit 1
fi

# --- Helpers ---
info()  { echo "==> $*"; }
error() { echo "Error: $*" >&2; exit 1; }

# Extract the CHANGELOG.md section for the given version (between the heading
# containing `<v>` and the next `## `). Prints empty string if there is none.
#
# Matches the version as a space-delimited token rather than by exact string
# equality, so it finds it in a dated, language-prefixed heading such as
# `## Java 0.4.0 - 2026-08-21`. Exact equality never matched a real heading and
# silently sent every release to `gh --generate-notes`.
changelog_section() {
    awk -v v="$1" '
        BEGIN { gsub(/\./, "\\.", v); re = " " v " " }
        /^## / && $0 ~ re { p = 1; next }
        p && /^## / { exit }
        p
    ' "$PROJECT_ROOT/CHANGELOG.md"
}

# --- Validate prerequisites ---
info "Validating prerequisites"

# Clean working tree (untracked files are OK)
if ! git -C "$PROJECT_ROOT" diff --quiet || ! git -C "$PROJECT_ROOT" diff --cached --quiet; then
    error "Working tree has uncommitted changes. Commit or stash first."
fi

# GPG key available
if ! gpg --list-secret-keys --keyid-format SHORT 2>/dev/null | grep -q sec; then
    error "No GPG secret key found. Import your signing key first."
fi

# Central credentials in settings.xml
if ! grep -q '<id>central</id>' ~/.m2/settings.xml 2>/dev/null; then
    error "No <server id=\"central\"> found in ~/.m2/settings.xml"
fi

# gh CLI authenticated
if ! gh auth status >/dev/null 2>&1; then
    error "GitHub CLI not authenticated. Run 'gh auth login' first."
fi

# Check tag doesn't already exist (unless dry-run)
if [[ "$DRY_RUN" == false ]]; then
    if git -C "$PROJECT_ROOT" rev-parse "java/v${VERSION}" >/dev/null 2>&1; then
        error "Tag java/v${VERSION} already exists."
    fi
fi

# The release notes come from CHANGELOG.md, and this script never writes it:
# dating the section is a separate commit made before running this. Catch a
# missing section here rather than after the tag has already been pushed.
if [[ -z "$(changelog_section "$VERSION" | tr -d '[:space:]')" ]]; then
    error "No CHANGELOG.md section for ${VERSION}. Date its heading first (e.g. '## Java ${VERSION} - YYYY-MM-DD')."
fi

# `git push origin HEAD` below pushes whatever is checked out, so make sure
# that is main and that it is not behind the remote.
if [[ "$DRY_RUN" == false ]]; then
    BRANCH="$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref HEAD)"
    if [[ "$BRANCH" != "main" ]]; then
        error "On branch '${BRANCH}'; releases are cut from main."
    fi
    git -C "$PROJECT_ROOT" fetch --quiet origin main
    BEHIND="$(git -C "$PROJECT_ROOT" rev-list --count HEAD..origin/main)"
    if [[ "$BEHIND" != "0" ]]; then
        error "main is ${BEHIND} commit(s) behind origin/main. Pull first."
    fi
fi

# --- Set version ---
info "Setting Java version to ${VERSION}"
cd "$JAVA_DIR"
./mvnw versions:set -DnewVersion="$VERSION" -DgenerateBackupPoms=false -q

# --- Commit the version bump ---
cd "$PROJECT_ROOT"
git add java/pom.xml
git diff --cached --quiet || git commit -m "release: java ${VERSION}"

# --- Build / Deploy ---
cd "$JAVA_DIR"
if [[ "$DRY_RUN" == true ]]; then
    info "Dry run: building, testing, and signing (no upload)"
    GOAL=verify
else
    info "Building, testing, signing, and publishing to Maven Central"
    GOAL=deploy
fi

if ! ./mvnw clean "$GOAL" -Prelease; then
    info "Build failed — version commit remains, fix and retry or reset"
    exit 1
fi

if [[ "$DRY_RUN" == true ]]; then
    info "Dry run complete. Java artifacts in java/target/."
    info "Note: version commit created. Run 'git reset HEAD~1' to undo if needed."
    exit 0
fi

# --- Tag and release ---
cd "$PROJECT_ROOT"
info "Creating tag java/v${VERSION}"
git tag -a "java/v${VERSION}" -m "Release java ${VERSION}"

info "Pushing commit and tag"
git push origin HEAD
git push origin "java/v${VERSION}"

info "Creating GitHub release"
NOTES=$(changelog_section "$VERSION")
if [[ -z "${NOTES// }" ]]; then
    gh release create "java/v${VERSION}" \
        --title "Java v${VERSION}" \
        --generate-notes
else
    gh release create "java/v${VERSION}" \
        --title "Java v${VERSION}" \
        --notes "$NOTES"
fi

info "Released Java v${VERSION} to Maven Central and GitHub."
