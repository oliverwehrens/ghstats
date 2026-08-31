# Design: incremental, never-deleting GitHub cache

> **Historical design record.** Written while this was built, and kept for the reasoning
> rather than as current documentation. Measurements are from the one organization it was
> developed against, referred to below as the source organization. See the README for how
> the tool is configured today.

Status: awaiting sign-off. No code written yet.

> **Filenames below predate the package layout.** These are records of decisions
> taken when the modules sat in the project root; the names were not rewritten,
> so read them against this map:
> `sync.py` → `ghstats/sync.py`, `reindex.py` → `ghstats/reindex.py`,
> `cache_store.py` → `ghstats/store/json_cache.py`,
> `sqlite_store.py` → `ghstats/store/sqlite.py`,
> `sqlite_client.py` → `ghstats/clients/sqlite.py`,
> `offline_client.py` → `ghstats/clients/offline.py`,
> `activity_analyzer.py` → `ghstats/analysis.py`,
> `github_graphql.py` → `ghstats/github/graphql.py`,
> `import_cache.py` → `ghstats/tools/import_cache.py`.
> `main.py` and `generate_html_report.py` became `ghstats/reporting/`, which has
> since been removed: the per-user JSON, the plain-text report and the combined
> `report.html` are all gone, replaced by `ghstats-explore` querying the store
> directly. Where the text below describes them, it describes what once was.

## Goal

A cache that is **append-mostly and permanent**, refreshed by a **daily sync** that fetches
only what changed. Reports are generated **offline** from that cache with zero API calls.

Non-goals: real-time updates; multi-org support in a single store (one store holds one
organization); keeping the current REST/PyGithub fetch path working alongside the new one.

---

## Measurements that drove the design

All taken against the source organization on 2026-08-17.

| Measurement | Value |
|---|---|
| Active (non-archived) repos | 1198 |
| REST `list-commits` includes `stats`? | **No** — returns `stats: null` |
| GraphQL commit exposes `additions`/`deletions` inline? | **Yes**, cost 1 pt for 10 repos / 398 commits |
| Commits in org, last 14d | 6206 |
| PRs updated in org, last 1d / 14d | 927 / 5109 |
| Repos with PR activity but no push in window | 18 of 304 (**6%**) |
| Commits since 2026-01-01, top 10 repos alone | 4762 |

Three consequences:

1. **The dominant cost today is commit stats, not the repo scan.** `activity_cache.py:107`
   reads `commit.stats`, which PyGithub must fetch per-commit because the list response
   omits it. That is ~6200 API calls per 14-day window.
2. **The Events API and the Search API are both unusable here.** Events: 300-event cap,
   30-day horizon, 30s–6h latency, public-only — against 927 PR updates/day. Search: hard
   1000-result cap that **truncates silently** — against 5109 PR updates and 6206 commits
   per 14 days.
3. **A `pushed_at` repo gate is not worth it.** Reviews don't bump `pushed_at`, so it drops
   6% of PR/review activity, and with GraphQL it saves ~80 points out of 5000/hour.

---

## Architecture

Sync and reporting are separated. Today `main.py` does both, so `report.sh`'s 203 per-user
invocations each try to refresh shared org data — the TTL is the only thing suppressing
730k redundant API calls.

```
sync.py            (GraphQL, network)   → .cache/
main.py --offline  (no network)         → <user>_<org>.json          × 203
generate_html_report.py                 → report.html
```

`report.sh` becomes: sync once → 203 offline runs → HTML. Total API cost is ~170 points
regardless of user count.

### Transport

GraphQL for everything. PyGithub is dropped from the sync path (it has no real GraphQL
support); queries go over `requests`/`httpx`.

**Trap:** GraphQL returns HTTP 200 with `null` nodes plus a populated `errors` array on
partial failure. Status code is not the signal. Any non-empty `errors` = failed batch.

### Batch sizes (measured ceilings)

| Query | Batch | Cost | Fails at |
|---|---|---|---|
| commits + inline stats | **10 repos** | 1 pt | 20 repos → 502 timeout |
| PRs + nested reviews | **25 repos × 10 PRs × 20 reviews** | 3 pts | 50×20×20 → `RESOURCE_LIMITS_EXCEEDED` (21k nodes) |

Concurrency 3, honouring `Retry-After`. Every query requests
`rateLimit { remaining resetAt cost }`, and two independent limits are enforced:

**Primary — the hourly point budget.** `_await_budget()` runs before every query. Below a
200-point floor it sleeps until `resetAt`, in naps of at most 300s so it re-checks rather
than oversleeping, then clears the stale reading and resumes. If the budget is low and
`resetAt` is somehow unknown it waits and re-checks rather than proceeding — a rate limiter
must fail closed. Overshoot is bounded by concurrency (at most 3 in-flight queries can pass
the guard on a stale reading, against a 200-point floor and per-query costs of 1–3).

**Secondary — the concurrency/CPU limit.** Arrives as 403 (sometimes 429) with an
explanatory body while `remaining` still reads in the thousands, so it is invisible to the
primary guard. Detected by body marker, it sets a pause **shared by every thread** —
independent per-thread backoff is what provokes it — escalating on repeat hits up to 4×.
That wait does not count against the query's retry budget, since it is a pause rather than
a failed attempt.

Why the old REST path took ~24 hours: REST bills 5000 **requests**/hour, and
`commit.stats` cost one request *per commit* (`list-commits` returns `stats: null`). At
70k–115k commits that is 14–23 hours of pure rate-limit waiting before counting PRs at all.
GraphQL bills ~5000 **points**/hour and returns stats inline, so the same work costs ~2,300
points.

---

## On-disk schema

Two files per repo, replacing three. PRs and their reviews now arrive in one query on one
watermark, so splitting them risks a torn write leaving them permanently inconsistent —
there is no TTL to heal it.

```
.cache/
  sync_state.json
  repos/<org>/<repo>/
    commits.json
    pulls.json
```

```jsonc
// commits.json
{
  "schema_version": 1,
  "organization": "example-org",
  "repository": "account-service",
  "covered_from": "2025-01-01T00:00:00+00:00",
  "covered_to":   "2026-08-17T03:00:00+00:00",
  "commits": [
    { "oid": "...", "author_login": "octocat", "author_email": "...",
      "author_name": "...", "committed_date": "...", "authored_date": "...",
      "additions": 44, "deletions": 41, "message": "..." }
  ]
}
```

```jsonc
// pulls.json
{
  "schema_version": 1, "organization": "...", "repository": "...",
  "covered_from": "...", "covered_to": "...",
  "pulls": [
    { "number": 1036, "author_login": "...", "created_at": "...",
      "updated_at": "...", "merged_at": null, "state": "OPEN", "title": "...",
      "reviews": [ { "id": "...", "author_login": "...",
                     "submitted_at": "...", "state": "APPROVED", "body": "..." } ] }
  ]
}
```

**`covered_from` / `covered_to` are the point of the redesign.** A watermark alone records
when you last fetched, never what range the data holds — which turns today's loud,
temporary undercount bug into a silent permanent one. `covered_to` is the sync watermark;
`covered_from` lets the reader **refuse** a request for a range it doesn't hold.

Writes are atomic: write `<file>.tmp`, then `os.replace`.

`author_login` is nullable — GraphQL returns `null` for commits whose email isn't linked to
a GitHub account (`activity_cache.py:121` has the same gap today). `author_email` and
`author_name` are stored alongside so unlinked commits can be attributed later without a
second rebuild.

---

## Sync algorithm

Nightly, sweeping **all 1198 active repos** — no `pushed_at` gate.

### Two overlap windows

```python
COMMIT_OVERLAP = timedelta(days=14)
PR_OVERLAP     = timedelta(days=1)
```

These are **not** the same knob and must not be merged.

- **`COMMIT_OVERLAP` is a correctness parameter.** GitHub's `since` filters on the commit's
  own date, not when it landed on `main`. With squash/rebase merges the committer date is
  rewritten to merge time, so incremental works. With **merge commits — 15.7% of your
  history** — the branch's commits keep their original dates, so a June branch merged in
  September is invisible to `since=<September>`. Forever. 14 days of re-reading is what
  closes that hole.
- **`PR_OVERLAP` is just slack.** `updated_at` only moves forward, so `since last_sync`
  already catches every PR change. 1 day absorbs clock skew and partially-failed runs.
  Using 14 days here would re-pull ~5100 PRs nightly for no correctness gain.

### Commits

```graphql
organization(login: $org) {
  repositories(first: 10, orderBy: {field: PUSHED_AT, direction: DESC}, isArchived: false, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      name
      defaultBranchRef { target { ... on Commit {
        history(first: 100, since: $commit_since) {
          pageInfo { hasNextPage endCursor }
          nodes { oid additions deletions committedDate authoredDate message
                  author { name email user { login } } }
        }
      }}}
    }
  }
}
```

`commit_since = covered_to - COMMIT_OVERLAP`. Any repo reporting `hasNextPage` on its
history gets followed up individually with its own cursor — batching per-repo cursors into
one query gets unreadable, and follow-ups are rare after the initial rebuild.

Merge by `oid`.

### Pulls + reviews

Same repo batching at 25, `pullRequests(first: 10, orderBy: {field: UPDATED_AT, direction: DESC})`
with `reviews(last: 20)` nested. Paginate until `updated_at < covered_to - PR_OVERLAP`,
then stop.

**`reviews(last:)`, not `first:`.** The `reviews` connection has no `orderBy` and returns
oldest-first, so `first: N` yields the *oldest* N — on `review-service#1619` (12 reviews),
`first:5` returns May 27–29 while `last:5` returns May 29–Jun 16. Using `first:` would
systematically miss the newest reviews on busy PRs, which is precisely what an incremental
sync exists to catch. Any PR reporting `totalCount > 20` gets its reviews fetched separately
with full forward pagination, so the rebuild doesn't lose the oldest ones either.

Merge: **upsert pulls by `number`, reviews by `id`.** "Never delete" means never lose an
entity, not never overwrite a field — a PR created in January and merged in August must
have its `state` and `merged_at` replaced, or the record rots permanently.

### Merge rules summary

| Entity | Key | Rule |
|---|---|---|
| commit | `oid` | insert if absent, never overwrite |
| pull | `number` | upsert — newer record replaces older |
| review | `id` | upsert within its pull |

Nothing is ever removed. `covered_to` advances to sync start time only on success.

---

## Failure policy

Today a thrown exception writes `[]` to the cache (`github_client.py:192/280/377`). With no
TTL, one rate-limit blip would permanently zero a repo. You already have 403 empty commit
caches with no way to distinguish scar tissue from genuinely quiet repos — which is one
reason the existing cache is being rebuilt rather than migrated.

On any failed batch (transport error, non-empty `errors`, timeout):

- write nothing for that repo
- **do not advance `covered_to`**
- log it, continue with other repos
- **exit non-zero** at the end

Because the watermark never moved, the next night's run automatically covers the wider gap.
This is also what makes a laptop-hosted schedule safe: a skipped night is not a data gap,
just a longer next sync.

"Fetched, found zero" is explicit and distinct: an empty list **plus** an advanced
`covered_to`.

---

## Rebuild

`covered_from = 2025-01-01`. The existing `.cache/` is deleted, not migrated.

Migration was rejected because `covered_from`/`covered_to` would have to be *inferred* and
differ per data type: commits were bounded by `--until 2026-08-01`, but PRs and reviews were
collected with no upper bound (`github_client.py:262/363` check only the lower edge), so
their real `covered_to` is the fetch time. Three files, three derivations, all inferred from
a shell invocation rather than anything on disk — and it would carry the 403 poisoned
empties forward as fact.

Rebuild and sync are **one code path**. "Rebuild" is a sync against an empty cache.
`--from` is required when no `covered_from` exists on disk, inferred otherwise.

### Rebuild cost, computed from measured volume

Two earlier estimates in this document were extrapolations from unrepresentative samples
and both were wrong — the first too low (sized on commits, ignoring PR volume), the second
too high (extrapolated from the 30 busiest repos, which are wildly atypical). This one is
arithmetic over actual counts.

Volume from 2025-01-01, measured:

| | count |
|---|---|
| PRs created | 40,396 |
| Commits (all branches; default-branch-only is lower) | 114,721 |
| Active repos | 1,198 (56 with zero PRs) |
| Lifetime PRs, whole org | 116,492 — top 30 repos hold 41% |

Queries needed, at `--pull-page 50` / `COMMIT_PAGE 100`:

```
pulls      ~45,000 / 50   =  900   + 1198/5 batch queries  = 240   → ~1,150
commits    ~70,000 / 100  =  700   + 1198/3 batch queries  = 400   → ~1,100
repo enumeration                                                   →     12
                                                            total  → ~2,300
```

Points track queries almost 1:1 in practice (939 queries cost 941 points). So the rebuild
is roughly **2,300–2,800 points — inside a single 5000/hour window** — and at the measured
~0.5s/query with `--concurrency 3`, **about 30–60 minutes**.

Cost per repo is extremely skewed: the 30 busiest ran ~31 points each, while samples at
offsets 400 and 900 in the push-ordered list ran ~1 point each (15 repos = 17 and 14 points
respectively). Any estimate drawn from the head of that distribution will be far too high.

Two limits bite during a rebuild that never appear in nightly runs, both handled:

- **502 on commit batches.** GitHub applies `since` by walking the commit graph, so cost
  scales with history length, not rows returned. Ten repos is fine over a fortnight and
  times out over twenty months. Use `--commit-batch 3`.
- **403 secondary rate limit.** Separate from the point budget — it fires on request
  concurrency and server CPU while `rateLimit.remaining` still reads in the thousands. The
  client detects it, escalates a backoff shared across all threads, and retries; that wait
  deliberately does not count against the query's retry budget. Keep `--concurrency 3`.

---

## CLI changes

**`sync.py`** (new): `--org`, `--from`, `--cache-dir`, `--concurrency`, `--debug`.

**`main.py`**: always offline, never touches the network.

- Removed outright: `--cache-ttl`, `--refresh-cache`, `--no-cache`. No deprecation
  no-ops — a flag that silently does nothing is worse than one that errors.
- `--until` now defaults to *now*.
- Errors if the requested range falls outside `covered_from`/`covered_to` rather than
  silently under-reporting.
- Keeps `--debug-cache`.

**`repo_cache.py`**: deleted. The sync enumerates repos via GraphQL on every sweep, and the
offline reader discovers repos by globbing `.cache/repos/<org>/*/`. Flagging this as a
consequence rather than a decision you made — say if you'd rather keep it.

**`report.sh`**: `sync.py` once, then 203 × `main.py`, then the HTML.

---

## Report changes

`report.sh:10` currently hardcodes `--until 2026-08-01` — already 16 days stale. A perfect
nightly sync would still render a report frozen at August 1, so the "daily view" goal fails
on this line, not on the caching.

- `--since 2025-01-01`, `--until` omitted (defaults to now).
- The HTML shows `covered_to` and the sync timestamp prominently, so a stalled sync looks
  stalled instead of looking like a quiet fortnight.

---

## Accepted trade-offs

- **Archived repos keep their cache.** The sweep queries `isArchived: false`, but a repo that
  *becomes* archived retains its history. Otherwise archiving would retroactively erase last
  spring's work from everyone's stats.
- **Bots are stored, filtered at report time.** Much of the 927 PR-updates/day is Renovate.
  Storing it is cheap; discarding at fetch time is unrecoverable.
- **Slow drift is permanent.** Upsert handles change but not disappearance: force-pushed
  commits stay counted, renamed repos leave ghost directories. The TTL used to clean this up
  every 20 days. The escape hatch is an explicit per-repo rebuild — full reconciliation costs
  the same API calls as a rebuild, so it isn't worth building separately.
- **Commits are default-branch only**, unchanged from today.

---

## Implementation order

1. ✅ GraphQL client: auth, `errors`-array handling, `rateLimit` guard, retry/backoff.
2. ✅ Cache layer: schema v1, atomic write, merge rules, coverage-window read guard.
3. ✅ `sync.py`: repo enumeration → commits → pulls+reviews → `sync_state.json` → summary + exit code.
4. ✅ Dry-run against ~20 repos; verify merge-commit catch-up by syncing a stale window twice.
5. ✅ Full rebuild from 2025-01-01 — 1198 repos, 80MB, **2408 points, 32.7 min**, 1 repo failed
   (the one whose commit query always 502s; needs a targeted re-sync).
6. ✅ `main.py` offline conversion; REST path deleted.
7. ✅ `report.sh` rewiring; staleness banner in the HTML.
8. Schedule it.

Steps 1–4 are safe alongside the existing cache; step 5 is the irreversible one.

### Step 6 as built

New `offline_client.py` exposes the subset of the old `GitHubClient` interface that
`ActivityAnalyzer` uses, reading only the cache. The analyzer keeps its aggregation logic
untouched (four edits: dropped the two PyGithub imports, `commit.commit.author.date` →
`commit.date`, and a `quiet` flag — one progress line per repo across 1198 repos and 203
users is 243k lines of output).

Deleted, now fully unreferenced: `github_client.py`, `activity_cache.py`, `repo_cache.py`.
PyGithub dropped from `requirements.txt`. `check_inactive_users.py` was converted too — it
imported `GitHubClient` and had the same structural bug, hitting the API once per user
across every repo.

`main.py` runs a **coverage preflight** via `CacheStore.coverage_summary()` (~0.6s, one pass
over all 2395 files). The lower bound is a hard error with exit 2; the upper bound is clamped
to `covered_to` and reported. Every report carries a `cache` block recording
`covered_from`/`covered_to`/`effective_until`/`sync_state`, which is what step 7's staleness
banner will read.

Timing: **1.15s per user**, so ~4 min for all 203 with zero API calls.

### Validation: the new numbers are right, the old ones undercounted

Re-running six users over the old window and deep-diffing against the previously committed
JSON showed exact agreement for some users and differences for high-activity ones. Both
causes were traced:

**Repos archived since the old run (new reports less).** For one member, 7 repos
vanished — all confirmed `archived=true`, archived after 2026-08-04. The rebuild
queries `isArchived: false`, so repos archived *before* the rebuild were never fetched. The
"archived repos keep their cache" protection only applies going forward. Org-wide there are
1010 archived repos, **133 with pushes since 2025-01-01** and 47 since 2026-01-01 — see the
open question below.

**Stale narrow-window cache in the old run (new reports more).** On
`legal-identity-provider` the old report said 67 commits and the new says 80. Ruled out:
committer-vs-author date filtering (all 80 have both dates inside the window) and
late-landing merge commits (none landed after the old fetch date). Counting live from GitHub
today gives **80** — matching the new path exactly. The old figure was wrong, almost
certainly the silent-undercount this redesign exists to prevent: a `commits.json` written
for a narrower range, reused inside the 20-day TTL with nothing on disk recording what it
covered.

### Step 7 as built

`report.sh` is now sync-once → 203 offline reports → HTML, with `--skip-sync` for
rebuilding the HTML without touching the network and `--help` that works. A sync exit code
of non-zero warns rather than aborting: one repo failing out of 1198 kept its old watermark
and will be covered next run, which is not a reason to abandon the report.

`build_cache_banner()` in `generate_html_report.py` renders freshness at the top of the
page, reading the `cache` block `main.py` writes into every user's JSON:

| Age of oldest `covered_to` | Banner |
|---|---|
| ≤ 26h | green, "Data is current" |
| 26–72h | amber, "Data is behind" |
| > 72h | red, "Data is stale" |
| no `cache` key anywhere | red, "Unknown data freshness" |

It also reports failed repos from `sync_state`, absent cache files, and — importantly —
counts input JSONs that predate the offline pipeline. Stale per-user JSONs from the old REST
run stay on disk until overwritten, so a partial run silently blends old undercounted data
with new. The banner now says so explicitly.

### Member list

The user list was hand-maintained and had drifted badly: `users.txt` held 203 names while
the org had **218 members — 30 joiners missing from every report, 15 leavers still carried**.
Nothing in the repo populated it.

`sync.py` now fetches `organization.membersWithRole` (3 queries, 3 points) and writes
`members.txt`, reporting who joined and who left since the last run and recording the churn
in `sync_state.json`. `report.sh` reads `members.txt` by default (`GHSTATS_USERS` overrides).

`report.sh` also passes an **explicit list** of per-user JSONs rather than a glob. Someone
who leaves keeps their JSON on disk — nothing here deletes data — but drops out of the HTML
as soon as they leave the member list. Previously a departed member's stale file would have
been picked up by a `*_<org>.json` glob forever.

`last_successful_run` is only advanced by a complete clean sweep. Any partial run
(`--skip-*`, `--repos`, `--limit`) leaves it alone, so a members-only refresh cannot make
the staleness banner claim the repository data is fresh.

### Open question, still open

Archived repos are excluded from the sweep, so the rebuild silently dropped 133 repos that
hold real 2025–2026 history. That contradicts the permanence goal. A one-time
`--include-archived` backfill would recover it, and since archived repos never change they
would never need re-syncing. Roughly doubles the repo count for one run only.
