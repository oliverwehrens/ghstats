# Design: SQLite as the cache

> Examples and observations below come from the one organization this was developed
> against, referred to as the source organization. Nothing org-specific ships in the
> code — see [Configuring for your organization](../README.md#configuring-for-your-organization).

Status: **complete**. `sync.py` writes SQLite, the JSON cache is retired, and
every per-user report is byte-identical to what the JSON pipeline produced, in a
fraction of the runtime.

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

Replace the per-repo JSON cache with a single SQLite database, so that a new metric is a
`WHERE` clause rather than an edit across several files and a re-run of one process per
member.

The immediate driver is counting AI-assisted commits, but that feature needs no schema at
all — the trailers are already in the data. The real goal is **asking questions that
weren't designed for in advance**.

Non-goals: changing what `sync.py` fetches; changing the per-user JSON contract or the HTML
report (both stay untouched, deliberately — see Phase 3); supporting a second organization;
concurrent readers from more than one process.

---

## Observations that drove the design

Taken against the source organization's cache **before** the untruncation re-sync, so the
trailer counts there were floors, not totals.

| Observation | What the cache showed |
|---|---|
| Duplicate oids | The same commit lives in more than one repository wherever one repo is a fork of another |
| `author_login` NULL | A small but material share of commits carry no linked GitHub account |
| Email → login | No email mapped to more than one login |
| Author identities | Distinctly more of them than the organization has members |
| Bot-authored commits | An agent, a release bot and a dependency bot between them are a large minority of the corpus |
| Claude co-author trailers | One address, `noreply@anthropic.com`, arriving under many display names |
| Truncation | A sizeable share of commit messages and review bodies sat exactly at the old length caps |

Four consequences:

1. **Commits key on `(repo_id, oid)`, never `oid` alone.** Some commits live in two repos
   each (`smart-product-review` / `smart-review-en` are forks). A bare `oid` primary key
   silently discards one copy and undercounts whichever repo loses the race.
2. **Email is a clean natural key for identity.** No email maps to two logins anywhere in
   the corpus, so `author_email → canonical_login` is a function. Identity resolution is
   a lookup table, not a conflict-resolution problem.
3. **AI attribution matches on trailer *email*, never display name.** `Claude`,
   `Claude Opus 4.8 (1M context)`, `Claude Fable 5` and every other model name resolve to
   `noreply@anthropic.com`. Matching names is a treadmill that loses a little accuracy on
   every model release.
4. **Bots are a large share of the data and belong in a column, not in the analyzer.**
   `is_bot` is a property of an identity. Hardcoding a name list into `activity_analyzer.py`
   is how it becomes wrong silently.

### After the re-sync

Re-measured from `ghstats.db` once Phase 0 landed. The truncated figures above were floors,
and badly low ones:

| Measurement | Truncated → full |
|---|---|
| Commits with a `noreply@anthropic.com` trailer | the message cap had hidden the great majority of them |
| Commits with a `cursoragent@cursor.com` trailer | same shape, same direction |
| Commits | a few added by activity in the intervening days, none lost |
| Oids in more than one repo | unchanged |
| Commits with NULL `author_login` | unchanged |

That gap is the retrospective justification for untruncating before migrating. Had the
import run against the old cache, `commit_trailers` would have been wrong by that margin
and nothing downstream would have flagged it.

One repository fails its commit query with HTTP 502 every time and holds zero commits — it
has never once synced, before or after. It is the single repo without commit coverage, and
**Phase 4's loud check will block on it forever** unless it gets an explicit
known-unsyncable exemption.

---

## Schema

```sql
CREATE TABLE repos (
  id    INTEGER PRIMARY KEY,
  org   TEXT NOT NULL,
  name  TEXT NOT NULL,
  UNIQUE (org, name)
);

-- The invariant the whole project exists to protect.
CREATE TABLE coverage (
  repo_id      INTEGER NOT NULL REFERENCES repos(id),
  kind         TEXT    NOT NULL CHECK (kind IN ('commits','pulls')),
  covered_from TEXT    NOT NULL,          -- ISO-8601 UTC
  covered_to   TEXT    NOT NULL,
  PRIMARY KEY (repo_id, kind)
) WITHOUT ROWID;

CREATE TABLE commits (
  repo_id        INTEGER NOT NULL REFERENCES repos(id),
  oid            TEXT    NOT NULL,
  author_login   TEXT,                    -- NULL where no account is linked
  author_name    TEXT,
  author_email   TEXT,
  committed_date TEXT    NOT NULL,
  authored_date  TEXT,
  additions      INTEGER NOT NULL DEFAULT 0,
  deletions      INTEGER NOT NULL DEFAULT 0,
  message        TEXT    NOT NULL DEFAULT '',
  PRIMARY KEY (repo_id, oid)
);

CREATE TABLE pulls (
  repo_id      INTEGER NOT NULL REFERENCES repos(id),
  number       INTEGER NOT NULL,
  author_login TEXT,
  title        TEXT    NOT NULL DEFAULT '',
  state        TEXT    NOT NULL CHECK (state IN ('OPEN','CLOSED','MERGED')),
  created_at   TEXT    NOT NULL,
  updated_at   TEXT,
  merged_at    TEXT,
  closed_at    TEXT,
  PRIMARY KEY (repo_id, number)
);

-- Schema 5. How big a pull request was and how much it was discussed.
--
-- A side table rather than columns on `pulls`, because the migration ladder is
-- additive and because "seen" and "measured" are genuinely different states:
-- everything collected before schema 5 has a `pulls` row and no measurement
-- until `ghstats-backfill-pulls` has run. Reads treat a missing row as
-- unmeasured, never as zero.
CREATE TABLE pull_metrics (
  repo_id         INTEGER NOT NULL,
  number          INTEGER NOT NULL,
  additions       INTEGER NOT NULL DEFAULT 0,
  deletions       INTEGER NOT NULL DEFAULT 0,
  changed_files   INTEGER NOT NULL DEFAULT 0,
  comments        INTEGER NOT NULL DEFAULT 0,   -- conversation tab
  review_comments INTEGER NOT NULL DEFAULT 0,   -- inline, summed over reviews
  measured_at     TEXT    NOT NULL,
  PRIMARY KEY (repo_id, number),
  FOREIGN KEY (repo_id, number) REFERENCES pulls(repo_id, number)
) WITHOUT ROWID;

CREATE TABLE reviews (
  id           TEXT    PRIMARY KEY,       -- GraphQL global id, verified unique
  repo_id      INTEGER NOT NULL,
  pull_number  INTEGER NOT NULL,
  author_login TEXT,
  submitted_at TEXT,
  state        TEXT    NOT NULL
                 CHECK (state IN ('APPROVED','CHANGES_REQUESTED',
                                  'COMMENTED','DISMISSED')),
  body         TEXT    NOT NULL DEFAULT '',
  FOREIGN KEY (repo_id, pull_number) REFERENCES pulls(repo_id, number)
);

CREATE TABLE members (
  login      TEXT PRIMARY KEY,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE sync_runs (
  id           INTEGER PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  repos_synced INTEGER,
  repos_failed INTEGER,
  points       INTEGER,
  seconds      REAL,
  complete     INTEGER NOT NULL DEFAULT 1
);
```

`complete` marks a full sweep. A partial one (`--repos`, `--limit`, any `--skip-*`) must
not read as fresh coverage of the whole organization — the job the old
`last_successful_run` field did.

`commits`, not `commit` — `COMMIT` is a SQLite keyword and the singular form needs quoting
everywhere.

**Trap: timestamps need one spelling *and* one precision.** An earlier draft of this
document claimed the import was "a straight copy with no parsing step to get wrong". That
was wrong. SQLite compares `TEXT` lexically, and the JSON cache mixed both variations:

```
'2025-01-01T00:00:00Z'        > '2025-01-01T00:00:00+00:00'   -> 1
'2026-08-17T13:48:43.869494Z' > '2026-08-17T13:48:43Z'        -> 0
```

Both answers are wrong for the same instant. GraphQL emits `Z` at whole-second precision
for every event; `cache_store.iso()` wrote `+00:00` for watermarks and gave `covered_to`
microseconds. Measured across the cache: every commit timestamp is `Z` at whole seconds,
every watermark is `+00:00`, and every `covered_to` carries microseconds.

Canonical form is therefore `YYYY-MM-DDTHH:MM:SSZ`. Events copy verbatim; only watermarks
convert, and `covered_to` is **floored** — claiming slightly less coverage than is held,
never more. `strftime` and `julianday` accept every spelling, which is exactly what makes
this quiet: only range comparisons break, and only sometimes.

**Trap: repositories whose names begin with a dot.** The source org has `.github` and
`.github-private`. `cache_store.list_repos` uses `iterdir()` and always saw them; the
`glob` module skips leading-dot entries, so any verification script written with `glob`
will silently drop every dot-prefixed repository. The importer uses `iterdir()` for this
reason.

### Derived tables

Pure functions of `commits.message`. A `reindex` command rebuilds both offline, with no
network access — which is the point. A new AI tool ships, you add one row to a match list
and reindex; you never re-sync.

```sql
CREATE TABLE commit_trailers (
  repo_id INTEGER NOT NULL,
  oid     TEXT    NOT NULL,
  name    TEXT    NOT NULL,   -- 'Claude Opus 4.8 (1M context)', 'Cursor', ...
  email   TEXT    NOT NULL,   -- 'noreply@anthropic.com'  <- match on this
  PRIMARY KEY (repo_id, oid, email, name),
  FOREIGN KEY (repo_id, oid) REFERENCES commits(repo_id, oid)
) WITHOUT ROWID;

CREATE TABLE identities (
  email           TEXT PRIMARY KEY,
  canonical_login TEXT NOT NULL,
  is_bot          INTEGER NOT NULL DEFAULT 0
);
```

`identities` is auto-seeded from `(author_email → author_login)` and hand-corrected
afterwards. Seeding covers the great majority of commits; the rest are the NULL-login rows,
and a generated stub sorted by commit count puts the worst offender first — a single
unmapped address can hold a large slice of one person's history.

`is_bot` seeds from logins ending in `[bot]` **plus an explicit list**. `semantic-release-bot`,
`goreleaserbot` and an org's own release automation have no brackets and would otherwise
pass as people.

**Trap:** the test must not be a substring match on `bot`. One person in the source
organization has a login containing that substring and years of commits behind it, and
`LIKE '%bot%'` erases them from every human metric.

Trailer emails live in a third derived table, `ai_tools`, with a `v_commit_ai` view joining
the three. A table rather than a module constant, so that adding an assistant is an
`INSERT` plus a reindex — no code change, no re-sync.

### What Phase 2 measured

Claude, Cursor and GitHub Copilot all appear, Claude by a wide margin the most. Commits
exceed trailers because a session spanning two models leaves two trailer lines; count with
`COUNT(DISTINCT repo_id \|\| oid)`.

The AI-assisted share of non-bot commits climbs steeply quarter over quarter, from
effectively nothing at the start of the record to a large minority of commits by its end.

Two parser findings, both now under test:

1. **Some commits repeat an identical trailer** (squash merges concatenating messages),
   several times over in the worst cases. The primary key already declares those the same
   fact, so the insert is `OR IGNORE`. Raw trailer instances therefore overstate the count.
2. **Trailers are not reliably at column zero.** A few commits indent the whole body.
   Anchoring the pattern at `^` made the count depend on formatting, so leading whitespace
   is tolerated. GitHub's own attribution would miss these, but the question here is whether
   an assistant was involved, not how the byline renders.

A tail of author identities remains unresolved, covering every commit with no linked
account, and one address can dominate that tail on its own. One member shows the cost
directly: a large body of AI-assisted commits on the linked account, and more stranded
under a second address whose domain was typo'd, so they attach to no login at all.

### Indexes

```sql
CREATE INDEX ix_commits_author_date ON commits (author_login, committed_date);
CREATE INDEX ix_commits_repo_date   ON commits (repo_id, committed_date);
CREATE INDEX ix_reviews_author_date ON reviews (author_login, submitted_at);
CREATE INDEX ix_pulls_author_date   ON pulls (author_login, created_at);
CREATE INDEX ix_trailers_email      ON commit_trailers (email);
```

Size is a non-issue and should not be designed around: the full database sits orders of
magnitude below SQLite's default ceiling. Query shape and indexes are the only things at
this scale that matter.

---

## Read path

`activity_analyzer.py` calls `client.get_user_commits(repo, ...)` **per repo** — a
file-shaped interface, and it stays that way. `OfflineClient` keeps every method signature
and is warmed by **three queries per user** (commits, pulls, reviews for that login), then
serves the per-repo calls from memory.

This deliberately leaves the `GROUP BY` rewrite on the table for later. Changing the
storage layer and the aggregation logic in one move means that when a number shifts, there
is no way to tell which change caused it.

Windows are filtered in Python with the same `_in_window`, and the record classes are
imported from `offline_client` rather than reimplemented. Both could be pushed into SQL
and eventually should be; during the gate, identical-by-construction beats independent.

**Ordering turned out to be part of the contract.** The analyzer only accumulates, so
within-repo order cannot change a count — but `by_hour` and `by_day_of_week` take their
*key order* from processing order, and `json.dump` preserves it. The client's `ORDER BY`
clauses reproduce how `cache_store` sorted its JSON: commits by `(committed_date, oid)`,
pulls by `number`, reviews by `(submitted_at, id)`.

### Gate result

`main.py --sqlite` selects the store, so both paths run side by side. Across every member,
every activity field — `commits`, `pull_requests`, `reviews`, `activity_by_date`,
`active_repos`, `repositories_analyzed` — is **byte-identical**, key order included, and the
run is an order of magnitude faster.

The fields that differ are all provenance rather than activity:

| field | cause |
|---|---|
| `date_range.until`, `cache.covered_to`, `cache.effective_until` | `covered_to` is floored to whole seconds |
| `cache.requested_until` | wall-clock at the moment of the run; differs between any two runs |
| `cache.sync_state.last_run` | canonical `Z` spelling rather than `+00:00` |
| `cache.sync_state.members_joined` / `members_left` | not carried by `sync_runs` yet — Phase 5 |

Notably the floored `covered_to` moved **no** activity number, which is the reasoning in
the timestamp trap holding up under test: flooring a ceiling cannot exclude a whole-second
event, and every event timestamp is whole seconds.

The AI metric, once Phase 2 lands:

```sql
SELECT c.author_login,
       strftime('%Y-%m', c.committed_date) AS month,
       COUNT(*)                            AS commits,
       COUNT(t.oid)                        AS ai_assisted
FROM commits c
LEFT JOIN commit_trailers t
  ON t.repo_id = c.repo_id AND t.oid = c.oid
 AND t.email IN ('noreply@anthropic.com','cursoragent@cursor.com')
GROUP BY 1, 2;
```

## Write path

One transaction per repo: rows and the `coverage` upsert commit together, or neither does.
A failed repo rolls back and its watermark does not advance, so the next run covers the
gap — the same guarantee as the old tmp-file + `os.replace`, but stated directly instead of
implied by keeping data and watermark in one file.

WAL mode. Fetch concurrency is unchanged; **writes serialize through a single connection**
behind one lock in `sqlite_store.SyncStore`.

**Trap: `INSERT OR IGNORE` ignores far more than duplicates.** It suppresses *every*
constraint violation, so a malformed record would be dropped exactly as quietly as a
re-fetched one — the silent-data-loss failure mode this project keeps running into. Commits
use `ON CONFLICT(repo_id, oid) DO NOTHING`, which ignores only the conflict actually
expected and lets a `NOT NULL` violation roll the repository back. Found by a test that
tried to prove the rollback worked and discovered nothing had been raised.

### Verification

The new write path was checked against the JSON-derived import rather than trusted: three
repositories synced into an empty database, then compared row for row. Every commit, pull
and review row came out identical; none differed.

A second run over the same repositories added nothing, confirming the insert-only merge is
idempotent, and recorded `complete = 0` for the `--repos`-scoped sweep.

## Membership and run history

`members` replaces `members.txt` as the source of truth. Unlike the file it keeps
`first_seen` across a departure and return, and marks a leaver inactive rather than
forgetting them — so churn accumulates instead of being overwritten nightly.

`sync.py` still exports `members.txt` on every run, so `report.sh` and any shell tooling
keep reading a line-per-login file. The export matched the previously committed file
exactly on first run.

`sync_runs` appends one row per run, replacing a `sync_state.json` that held only the most
recent one.

---

## The coverage invariant, and the bug in it

`read_items()` in `cache_store.py` is **dead code** — nothing outside that module and its
tests calls it. The per-repo `CoverageError` the README describes is not in the report
path. `offline_client` calls `load()` directly and filters windows itself.

The live guard is `coverage_summary()` at `main.py:129`, and its arithmetic is right:
`covered_from` is the **max** floor across repos (the binding constraint) and `covered_to`
the **min** ceiling (the staleness edge).

**Trap:** `coverage_summary` collects `missing` and `unreadable` lists and `main.py` reads
neither. A repo with no `commits.json` contributes zero rows and no error. With an empty
cache, `report.sh` produces a clean-looking report for every member showing near-zero
activity, and
`report.sh` runs `main.py --quiet`, which suppresses the only warning that exists.

In SQL this is `LEFT JOIN coverage ... WHERE covered_from IS NULL` — one query. It was a
pre-existing bug rather than one the migration introduced.

**Closed in Phase 4.** `sqlite_store.unusable_pairs()` takes either store's
`coverage_summary`, so the JSON and SQLite paths apply one rule, and `main.py` exits 2 with
the offending pairs named. Both stores independently report the same single gap: the one
repository whose commit query never succeeds.

An error nobody can clear is an error everybody learns to ignore, so a repository that
genuinely cannot be fetched is named in `sqlite_store.UNSYNCABLE_REPOS` with its reason
rather than left to fail nightly. The list ships empty; the source organization had a
single entry. Unreadable files are never exempt: that is a fault, not a standing condition.

### The hole Phase 4 turned up

`import_cache.py` deletes and rebuilds the database on every run, which would have silently
discarded every hand-corrected row in `identities` — the entire mechanism for attributing
the commits with no linked account. The loss would have surfaced as a contributor's
numbers quietly dropping again, months later.

The importer now rescues those rows before replacing the store and reinstates them
afterwards, reporting how many it carried. `--discard-curated` opts out. An unreadable or
absent prior store rescues nothing rather than blocking the import.

This matters less once Phase 5 makes `sync.py` the writer and re-imports stop happening,
but "the curation survives" is a property worth having regardless of how the data arrived.

---

## Phases

| # | Work | Network |
|---|---|---|
| ~~**0**~~ | ~~full re-sync with untruncated fields~~ — **done**, a handful of repos failed and all but one recovered on retry | yes |
| ~~**1**~~ | ~~schema + `import_cache.py`~~ — **done**, `sqlite_store.py` + `import_cache.py`, seconds | no |
| ~~**2**~~ | ~~trailer parse, identity auto-seed, `reindex`~~ — **done**, `reindex.py`, sub-second | no |
| ~~**3**~~ | ~~`OfflineClient` on SQLite~~ — **gate passed**, every user identical, far faster | no |
| ~~**4**~~ | ~~coverage check in SQL; close the silent-missing-repo gap~~ — **done** | no |
| ~~**5**~~ | ~~`sync.py` writes SQLite; JSON cache retired~~ — **done**, rows verified identical against the import | yes |

**Phase 3 is the gate.** Success is every per-user JSON file coming out **byte-identical**
to what Phase 0 produced. If they differ, the database is wrong and nothing proceeds until
they match. Keeping the JSON contract (Phase 3) and re-syncing to JSON first (Phase 0) exist
solely to provide this oracle — without them, a new storage layer is validated against
nothing.

### Why Phase 0 runs on the old code

The alternative — cut `sync.py` over to SQLite first and re-sync straight into the database —
skips the importer, and is wrong. It makes the first-ever execution of brand-new write code
an hour-long, rate-limited, whole-organization network run with no fallback if the schema
turns out wrong, and getting it wrong costs another one. Phase 0 buys the expensive network
data once in a format that already works, after which every schema iteration is a local
offline import that can run as many times as it takes.

The importer is not throwaway: it is the migration, and it generates the test fixtures.

---

## Decided elsewhere

- `.cache/ghstats.db`, covered by the existing `.cache/` gitignore rule.
- `PRAGMA user_version` for schema migrations.
- `members.txt` keeps being written by `sync.py` as a plain export, so `report.sh`'s shell
  `while read` loop needs no change. The database is the source of truth; the file is a view.
- `sync_runs` and `members` retain history that `sync_state.json` currently discards every
  night — it holds only the most recent run's joiners and leavers.
