#!/bin/bash
#
# Refresh the activity store.
#
#   1. ghstats-sync      -- the only step that touches the network
#   2. ghstats-reindex   -- rebuild the derived tables from the new commits
#
# That is the whole pipeline. Results are read with `ghstats-explore`, which
# queries the store directly, so there is nothing to render ahead of time.
#
# The reindex is what keeps AI-assistance attribution and Jira classification
# current: it reparses commit trailers and messages, so a sync that brings in
# new commits without it would leave them unclassified while every other
# number moved.
#
# Usage:
#   ./scripts/report.sh                # sync, then reindex
#   ./scripts/report.sh --skip-sync    # reindex only, no network
#
# Configured by environment variable; see the block below. GHSTATS_ORG is
# required. GHSTATS_VENV activates a virtualenv first, if you use one.
set -u

# .cache/ghstats.db is relative to the project root, so anchor to it rather
# than to wherever this was invoked from.
cd "$(dirname "$0")/.." || exit 1

SKIP_SYNC=0
for arg in "$@"; do
    case "$arg" in
        --skip-sync) SKIP_SYNC=1 ;;
        -h|--help)   awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next}
                          NR>1 {exit}' "$0"; exit 0 ;;
        *)           echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

# Configuration. Every value can be overridden from the environment, so
# retargeting this at another organization needs no edit to a tracked file:
#
#   GHSTATS_ORG=my-org GHSTATS_SINCE=2025-06-01 ./scripts/report.sh
#
ORG=${GHSTATS_ORG:?set GHSTATS_ORG to your GitHub organization, or edit this line}
SINCE=${GHSTATS_SINCE:-2025-01-01}

# Activate a virtualenv only if one was named. Sourcing a hardcoded path fails
# silently for anyone else -- there is no `set -e` here, so the run would carry
# on against whatever Python is on PATH and sync into the wrong store.
if [ -n "${GHSTATS_VENV:-}" ]; then
    if [ ! -f "$GHSTATS_VENV/bin/activate" ]; then
        echo "Error: GHSTATS_VENV=$GHSTATS_VENV has no bin/activate" >&2
        exit 1
    fi
    # shellcheck disable=SC1091
    source "$GHSTATS_VENV/bin/activate"
fi

for cmd in ghstats-sync ghstats-reindex; do
    command -v "$cmd" >/dev/null || {
        echo "Error: $cmd not on PATH. Run: pip install -e '.[dev]'" >&2
        exit 1
    }
done

# --- 1. Sync -------------------------------------------------------------
# A non-zero exit means at least one repo failed. Its watermark was not
# advanced, so the next run covers the gap -- worth a warning, not worth
# abandoning the reindex over one repo.
failed=0
if [ "$SKIP_SYNC" -eq 0 ]; then
    echo "==> Syncing $ORG (network)"
    if ! ghstats-sync --org "$ORG" --from "$SINCE" \
            --commit-batch "${GHSTATS_COMMIT_BATCH:-3}" \
            --concurrency "${GHSTATS_CONCURRENCY:-3}"; then
        echo "WARNING: sync reported repository failures; continuing with the" \
             "store as it stands. Those repos keep their old watermark." >&2
        failed=1
    fi
else
    echo "==> Skipping sync (--skip-sync)"
fi

# --- 2. Reindex ----------------------------------------------------------
# Derived tables only; offline and idempotent, so it runs either way.
echo "==> Rebuilding derived tables"
if ! ghstats-reindex; then
    echo "ERROR: reindex failed; the newest commits are unclassified." >&2
    exit 1
fi

# --- 3. SonarCloud -------------------------------------------------------
# Optional enrichment, and skipped rather than required when unconfigured: an
# organization with no SonarCloud account must still get a working report, and
# hard-requiring these would break every existing cron the day it was added.
# The explorer already tells "never synced" apart from "no Sonar project", so
# skipping degrades honestly instead of drawing a dash on every row.
if [ -n "${SONAR_ORG:-}" ] && [ -n "${SONAR_TOKEN:-}" ]; then
    if command -v ghstats-sonar >/dev/null; then
        echo "==> Reading SonarCloud quality gates (network)"
        if ! ghstats-sonar --sonar-org "$SONAR_ORG" --org "$ORG"; then
            echo "WARNING: SonarCloud sync failed; the explorer keeps the" \
                 "previous snapshot." >&2
        fi
    else
        echo "==> Skipping SonarCloud (ghstats-sonar not on PATH)"
    fi
else
    echo "==> Skipping SonarCloud (set SONAR_ORG and SONAR_TOKEN to enable)"
fi

echo "==> Done. Explore with:"
if [ -n "${GHSTATS_TZ:-}" ]; then
    echo "      ghstats-explore --timezone \"$GHSTATS_TZ\" --open"
else
    echo "      ghstats-explore --open"
fi
[ "$failed" -eq 0 ]
