# GitHub User Activity Reporter

Collects GitHub activity across an organization — commits, pull requests, reviews and
lines changed — into a permanent local store, and serves an interactive explorer over it.

Work happens in two phases that never overlap:

```
ghstats-sync      network   →  .cache/ghstats.db      incremental, permanent
ghstats-reindex   offline   →  derived tables         trailers, identities, issues, bots
ghstats-explore   offline   →  http://127.0.0.1:8765  interactive, read-only
```

`ghstats-sync` is the only thing that talks to GitHub. Everything downstream is a pure
function of whatever the last sync wrote, so asking it anything costs nothing.


See [docs/incremental-cache.md](docs/incremental-cache.md) for why the sync is incremental,
[docs/sqlite-store.md](docs/sqlite-store.md) for why the store is SQLite, and
[docs/explorer.md](docs/explorer.md) for the explorer.

## Layout

```
src/ghstats/
    sync.py                the network sweep -- the only caller of GitHub
    reindex.py             rebuild derived tables from stored commits
    analysis.py            activity metrics from client output
    github/graphql.py      GraphQL client: rate limits, retries, backoff
    store/sqlite.py        the store: repos, coverage, commits, pulls, reviews
    store/json_cache.py    retired per-repo JSON layout, still readable
    clients/sqlite.py      read-only view over the store
    clients/offline.py     the same interface over the JSON cache
    explorer/queries.py    slices of the store, as functions -- no HTTP
    explorer/server.py     loopback JSON API      (ghstats-explore)
    explorer/static/       the single-page UI, and a vendored Chart.js
    tools/                 inactivity report, migration, verification
tests/                     unittest suite, no network
docs/                      design notes
scripts/report.sh          sync then reindex -- the whole pipeline
```

## Requirements

- **Python 3.10 or newer** — CI runs 3.10 through 3.13. Check yours with
  `python3 --version`; macOS ships one, and `brew install python` or
  [python.org](https://www.python.org/downloads/) gets a newer one.
- **pip**, which every supported Python bundles — `python3 -m pip --version` confirms it.
- **git**, to clone this repository. The [`gh` CLI](https://cli.github.com/) is optional and
  used only as the last place a token is looked for.

Two third-party libraries are needed, both installed for you by the step below:

| Library | Why |
|---|---|
| `requests` | the GraphQL calls in `ghstats-sync` |
| `python-dateutil` | parsing and shifting the timestamps everything downstream compares |

The explorer adds nothing to that list: it serves itself from the standard library's
`http.server`, and Chart.js is vendored into the package rather than fetched.

## Installation

Install into a virtualenv, so the `ghstats-*` commands and those two libraries stay out of
your system Python:

```bash
git clone https://github.com/oliverwehrens/ghstats.git
cd ghstats

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e '.[dev]'            # editable, plus pytest
```

`-e` keeps the installed commands pointing at the working tree, so editing `src/ghstats/`
takes effect with no reinstall. `[dev]` adds pytest — drop it (`pip install -e .`) if you
only mean to run the tools. `pip install -r requirements.txt` still works and does the same
thing; that file is just `-e .`.

Check it took:

```bash
ghstats-sync --help                     # the console scripts are on PATH
python -m unittest discover -s tests    # 221 tests, no network, no token
```

Every new shell needs `source .venv/bin/activate` again. `report.sh` can do it for you:
point `GHSTATS_VENV` at the virtualenv and it activates before running — which is what makes
it work from cron, where nothing has activated anything.

<details>
<summary>With <code>uv</code> instead of pip</summary>

```bash
uv venv && source .venv/bin/activate
uv pip install -e '.[dev]'
```

</details>

A token is needed for `ghstats-sync` only. It is read from `--token`, then `GITHUB_TOKEN`, then
`gh auth token`. Required scopes: `repo` (private repositories) and `read:org`.

## Quick start

```bash
export GHSTATS_ORG=my-org           # required; the rest have defaults
./scripts/report.sh                 # sync, then rebuild the derived tables
./scripts/report.sh --skip-sync     # reindex only, no network

ghstats-explore --open              # read the results
```

`report.sh` is the nightly job: it syncs, then reindexes, and that is all. There is
nothing to render, because the explorer queries the store directly. It works from any
directory -- it anchors itself to the project root.

It is configured entirely by environment variable, so retargeting it needs no edit to a
tracked file:

| Variable | Default | Meaning |
|---|---|---|
| `GHSTATS_ORG` | — | **Required.** GitHub organization to sync |
| `GHSTATS_SINCE` | `2025-01-01` | Coverage floor, `YYYY-MM-DD` |
| `GHSTATS_TZ` | `UTC` | Only used in the `ghstats-explore` line it prints at the end |
| `GHSTATS_VENV` | — | Virtualenv to activate first, if you use one |
| `GHSTATS_CONCURRENCY` | `3` | Passed to `ghstats-sync --concurrency` |
| `GHSTATS_COMMIT_BATCH` | `3` | Passed to `ghstats-sync --commit-batch` |

The script checks that `ghstats-sync` and `ghstats-reindex` are on `PATH` before it
starts, rather than failing after a long network sweep.

## Configuring for your organization

Nothing organization-specific is baked into the code. Four things are worth setting up,
and only the first is needed to get a report at all.

**1. The organization itself.** `GHSTATS_ORG` for `report.sh`, or `--org` on each command.

**2. Your Jira projects** — needed only if you want the explorer's issue views.
`JIRA_PROJECT_SEED` in `src/ghstats/store/sqlite.py` **ships empty**, because a key
prefix means nothing outside the org that uses it. Find your own and adopt them:

```bash
ghstats-reindex --unknown-issues        # ranks key-shaped prefixes no project claims
```

```sql
INSERT INTO jira_projects (key, canonical) VALUES ('ORB','ORB');
-- A misspelling folds onto what it meant, so both spellings are one issue:
INSERT INTO jira_projects (key, canonical) VALUES ('BILLNIG','BILLING');
```

Then `ghstats-reindex`. Because this is derived offline, adopting a project reclassifies
all the history you already hold — no re-sync, no API call.

**3. Your bots.** `ghstats-reindex` derives most automations from evidence (any login seen
with a `[bot]` suffix, plus its bare form). `BOT_LOGINS` in `src/ghstats/store/sqlite.py`
ships with the automations common to any org — `semantic-release-bot`, Renovate's
auto-approver, the Cursor and Copilot reviewers — and your own CI identities belong
alongside them. The query that finds the ones evidence cannot reach is in the comment
above the list.

**4. Repositories that cannot be fetched.** `UNSYNCABLE_REPOS` ships empty. If a repository
genuinely never syncs, `ghstats-inactive` exits 2 and names it; record it there with a
reason so the error does not fire nightly and stop being read.

Adding an **AI assistant** needs nothing org-specific: `AI_TOOL_SEED` already covers Claude,
Cursor and Copilot, matched on trailer email rather than display name.

## ghstats-sync

Sweeps every active repository and merges what changed into `.cache/ghstats.db`.

```bash
ghstats-sync --org my-org --from 2025-01-01
```

| Option | Meaning |
|---|---|
| `--org` | Organization (required) |
| `--from` | Coverage floor, `YYYY-MM-DD`. Required the first time a repo is seen; also triggers a backfill if earlier than current coverage |
| `--token` | Token override (else `GITHUB_TOKEN`, else `gh auth token`) |
| `--db` | Store path (default `.cache/ghstats.db`) |
| `--concurrency` | Parallel requests (default 3) |
| `--commit-batch` | Repos per commit query (default 10; use **3** for a wide window) |
| `--pull-batch` / `--pull-page` | Repos and PRs per pull query (default 5 / 50) |
| `--repos` | Sync only these repositories |
| `--limit` | Sync only the N most recently pushed |
| `--members-file` | Where to write the member list (default `members.txt`) |
| `--skip-members` / `--skip-teams` / `--skip-commits` / `--skip-pulls` | Skip a sweep |
| `--dry-run` | Fetch and report, write nothing |
| `--debug` | Per-query output |

Exit codes: `0` clean, `1` at least one repository failed, `2` bad invocation.

A failed repository writes nothing and **does not advance its watermark**, so the next run
covers the gap automatically. A skipped night is not a data gap, just a longer next sync.

### Member list

`ghstats-sync` writes `members.txt` from the organization's actual membership and reports who
joined and left since the last run. `report.sh` reads that file, so joiners and leavers are
picked up without anyone editing a list by hand.

### Teams

`ghstats-sync` also sweeps the organization's teams, their rosters and their repository
grants — a whole org's teams cost a handful of GraphQL points. That is what lets the explorer
answer "what did this group change on Tuesday" without anyone maintaining a roster file by
hand. Churn is reported like member churn.

GitHub reports **current** membership with no history, so a window predating someone's move
credits their old work to their new team. Right for last week, wrong across a reorg — the
explorer says so on screen.

## Refusing to undercount

Every command that reads a window checks the store can answer it, and **exits 2 rather
than reporting a subset** — silently undercounting is the failure this design exists to
prevent. `ghstats-inactive` is where you will meet the checks:

- **`--since` predates coverage.** The store does not hold the range you asked for. The
  fix it suggests is re-running `ghstats-sync` with an earlier `--from`.
- **A repository has no usable data at all**, named in the error. A repository that
  genuinely cannot be fetched belongs in `store.sqlite.UNSYNCABLE_REPOS` with a reason —
  the list ships empty, since which repositories are unfetchable is a property of your
  organization. One real example: a diagrams repository whose commit query returned
  HTTP 502 at every batch size and had never synced once.
- **No store at all**, or a file that is not one. Reported as a missing prerequisite,
  pointing at `ghstats-sync`, rather than as a traceback.

`ghstats-explore` does not refuse, because it never takes a window: it shows what the
store holds and how stale it is, so an empty week reads as an empty week and a stalled
sync reads as a stalled sync.

## ghstats-explore

Serves an interactive explorer over the store. Offline, read-only, no Python dependencies
and no network — the chart library is vendored into the package, not fetched.

```bash
ghstats-explore --timezone Europe/Berlin --open
```

| Option | Meaning |
|---|---|
| `--db` | Store path (default `.cache/ghstats.db`) |
| `--org` | Organization; inferred when the store holds one |
| `--timezone` | Zone that day and hour grouping use (default `UTC`) |
| `--host` / `--port` | Bind address and port (default `127.0.0.1:8765`) |
| `--open` | Open a browser once the server is up |

Five entry points, each bottoming out in the actual commits, PRs and reviews:

| View | Answers |
|---|---|
| **People** | What did this person do — repos, issues, review partners, AI share |
| **Repositories** | What changed here, and who changed it |
| **Teams** | What a group changed, rolled up and per member |
| **Jira** | A project's issues, or everything referencing one key |
| **Day** | One local day, cross-cut by team, repository and person |

They share one filterable event stream, so every chart drills into the same place; clicking
a bar, or a square in the contribution calendar, opens that day. The URL hash holds the
whole state, so any view can be bookmarked or sent to someone.

Three graphs recur: activity by day, a contribution calendar, and activity patterns by
weekday and hour. **People** draws them over the whole organization and a person's page
draws the same three over one person, because a personal rhythm only means something
against the one it sits inside. The event stream underneath is banded by day, and on a
person's page each day is split by repository.

The store is opened `mode=ro`, so a sync can run while it serves and no handler can write to
the file. It binds to loopback and checks the `Host` header — there is no auth because there
is no remote listener.

**A pull request is two events, not one:** opened on one day, merged on another. "What did she
work on" means the opening; "what changed on Tuesday" means the merge. Collapsing both onto
`created_at` answers the second question with the wrong day.

## ghstats-reindex

Rebuilds the derived tables from data the store already holds. Offline, idempotent, 0.3s.

```bash
ghstats-reindex                # rebuild trailers, identities and the tool list
ghstats-reindex --unresolved   # author identities awaiting a hand mapping
```

```bash
ghstats-reindex --unknown-issues   # key-shaped prefixes matching no Jira project
```

`ghstats-reindex` parses co-author trailers to attribute AI-assisted commits, so adding a new
assistant is an `INSERT` into `ai_tools` plus a reindex — **never a re-sync**. That is the
payoff for storing the whole commit message: the classification can change retroactively
over history already collected. Jira keys work the same way: adopt a project with an
`INSERT` into `jira_projects` and twenty months of history reclassifies offline.

Two of the derived tables exist because the naive version of each is quietly wrong:

- **`issue_refs`** cannot come from an open `[A-Z]{2,}-[0-9]+`. That pattern matches a great
  many distinct prefixes across a real corpus of commit messages and most are not issues —
  `ISO-8601`, `AES-256`, `PSR-4`, `ADR-001`. `jira_projects` decides instead, and it maps alias →
  canonical because one project there arrived under five spellings: a transposition, a
  dropped leading letter, a zero typed for an O.
- **`bot_logins`** cannot be `LIKE '%bot%'` — in that same org it would have caught one of its
  most prolific committers, whose login merely contains the substring — and cannot be
  `NOT LIKE '%[bot]'` either, because GraphQL spells the same account `renovate[bot]` on a
  commit and `renovate` on a pull request. Renovate alone opened more PRs there than any human.

Details and the queries for extending both are in [docs/explorer.md](docs/explorer.md).

`report.sh` runs it after every sync. Skipping it would leave the newest commits
unclassified while every other number moved.

## Other tools

```bash
# Members with no activity in a window (offline; run ghstats-sync first)
ghstats-inactive --org my-org --since 2025-01-01 --output inactive.txt
```

`ghstats-import-cache` migrates a `.cache/` tree from the JSON layout into the store;
`ghstats-verify-overlap` is described under Tests.

The explorer shows how fresh the store is on load — a stalled sync otherwise looks exactly
like a quiet fortnight.

## How the store works

One SQLite file, `.cache/ghstats.db`. `repos`, `coverage`, `commits`, `pulls`, `reviews`,
`members` and the `teams` tables hold what was fetched; `commit_trailers`, `identities`,
`ai_tools`, `issue_refs`, `jira_projects` and `bot_logins` are derived and rebuilt by
`ghstats-reindex` without touching the network. Full schema in
[docs/sqlite-store.md](docs/sqlite-store.md).

`connect()` migrates an older store in place. Every migration is additive — new tables only,
no `ALTER`, no rewrite — because a store this size represents hours of API budget and a
migration that required a re-sync would not get used. A version *newer* than the code
understands is still refused rather than guessed at.

The `coverage` table is the point of the design — one row per repository and kind:

```sql
SELECT covered_from, covered_to FROM coverage WHERE repo_id = ? AND kind = 'commits';
```

A bare "last fetched" timestamp records when data was collected but never what range it
holds, so a report asking for a wider window gets a silent undercount. Storing both lets
the reader refuse instead.

**Nothing is ever deleted.** Merges are:

| Entity | Key | Rule |
|---|---|---|
| commit | `(repo_id, oid)` | insert if absent, never overwritten |
| pull | `(repo_id, number)` | upsert — a PR merged months after it opened must refresh |
| review | `id` | upsert, unioned rather than replaced |

Commits key on `(repo_id, oid)` rather than `oid` alone because a great many commits lived in
two repositories each in the org this was built against, where one repo was a fork of another.

Each repository is one transaction covering both its rows and its `coverage` row: either
the new data and the advanced watermark both land, or neither does. Fetching runs at
`--concurrency`; writes serialize through a single connection.

Timestamps are ISO-8601 UTC at whole-second precision, ending in `Z`. SQLite compares
`TEXT` lexically, so a second spelling or a second precision silently breaks every range
query — `'...43Z' <= '...43.869494+00:00'` is false. `covered_to` is floored, which
understates coverage rather than overstating it.

### The two overlap windows

```python
COMMIT_OVERLAP = timedelta(days=14)
PR_OVERLAP     = timedelta(days=1)
```

These are **not** the same knob. `COMMIT_OVERLAP` is a correctness parameter: GitHub's
`since` filters on a commit's own date, not on when it landed on the default branch. Squash
and rebase merges rewrite the committer date, but a true merge commit preserves the
branch's original dates — so a June branch merged in September would be invisible to an
incremental fetch, permanently. Re-reading a fortnight closes that.

`PR_OVERLAP` is only slack: `updated_at` moves forward monotonically, so `since last_sync`
already catches every change.

### Archived repositories

The sweep queries `isArchived: false`, but a repository that *becomes* archived keeps its
cached history. Otherwise archiving would retroactively erase past work from everyone's
stats.

## Rate limits

GraphQL bills points, not requests, and every query reports its own budget. Two independent
limits are enforced:

- **Hourly point budget** — checked before every query; below a 200-point floor the client
  sleeps until `resetAt`. If the budget is low and the reset time is unknown it waits rather
  than proceeding.
- **Secondary rate limit** — arrives as HTTP 403 while the point budget still reads healthy,
  because it fires on request concurrency and server CPU. The backoff is shared across all
  threads; independent per-thread retries are what provoke it.

A nightly sweep of a large organization costs roughly 170–650 points of the 5000/hour budget
and takes a few minutes.

## Troubleshooting

**"predates cache coverage"** — the store does not hold the range you asked for. Re-run
`ghstats-sync` with an earlier `--from`.

**`HTTP 502` on commit batches** — the batch is too large for the window. GitHub applies
`since` by walking the commit graph, so cost scales with history length, not rows returned.
Ten repositories is fine over a fortnight and times out over twenty months. Use
`--commit-batch 3`.

**`403 secondary rate limit`** — lower `--concurrency`. The client already backs off and
retries; persistent hits mean the sweep is too aggressive for the token.

**A repository failed to sync** — its watermark did not move, so the next run covers it. To
retry immediately: `ghstats-sync --org <org> --repos <name> --from <date>`.

**"no store here yet"** — nothing has synced into `--db` yet, or it points at the wrong
path. Run `ghstats-sync --org <org> --from <YYYY-MM-DD>` first; only the sync creates a
store.

**The explorer says a derived table is empty** — the sync ran but the reindex did not. Run
`ghstats-reindex`, or `./scripts/report.sh --skip-sync` to do it without touching the
network.

## Tests

```bash
python -m unittest discover -s tests    # 221 tests, no network
pytest                                  # the same suite, if you installed [dev]
```

The suite needs no token, no network and no fixtures to download — a consequence of only
`ghstats-sync` talking to GitHub. CI runs it on Python 3.10 through 3.13
(`.github/workflows/test.yml`).

`ghstats-verify-overlap` checks the merge-commit guarantee against the live API by rewinding a
repository's watermark, deleting commits inside and outside the overlap, and confirming that
exactly the in-window ones come back:

```bash
ghstats-verify-overlap --org my-org review-service --cache-dir /tmp/ghverify
```

**It predates the SQLite store and does not currently run:** it still reads the JSON cache
and passes `--cache-dir` to the sync, which now takes `--db`. It is kept because the
guarantee it checks is worth checking, but expect to port it first.

## License

Apache-2.0 — see [LICENSE](LICENSE).

The explorer bundles Chart.js 4.4.7, which is MIT licensed; its notice is in
[`src/ghstats/explorer/static/vendor/README.md`](src/ghstats/explorer/static/vendor/README.md).
