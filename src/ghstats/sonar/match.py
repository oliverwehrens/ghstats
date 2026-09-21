"""Which SonarCloud project belongs to which GitHub repository.

**Match against the real project list; never derive a key and assume it.**
SonarCloud's GitHub auto-provisioning names projects `<org>_<repo>`, but a
project created by hand is often just `<repo>`, and an organization that has
been running for a few years holds both spellings plus a handful that follow
neither. Deriving `<org>_<repo>` and querying it one repository at a time turns
every deviation into a silent "no Sonar project", which is indistinguishable
from the truth and therefore never gets noticed.

Listing the organization is one paginated call and returns the authoritative
key for every project it holds. Matching against that list distinguishes "this
repository has no Sonar project" from "the key convention here is not the one
assumed" -- and `MatchResult.assumed` carries the key that *would* have been
queried, which is what makes the second case diagnosable.

The rules reach the pairs that follow *some* convention. The ones that follow
none -- a project renamed, misspelled, or created under a name with no
relationship to the repository -- are written down by hand in
the map file (see `sonar/overrides.py`) and outrank every rule here. That map
is deliberately not a similarity score: two names reading alike is not evidence
they are the same codebase, and a wrong pairing reports another project's gate
with nothing downstream able to tell.

Pure: no HTTP, no store. Everything here is a function of two lists and a map.
"""
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

# Precedence, most specific first. A repository takes the first rule that
# produces an unclaimed project.
#
#   override  from the map file    what a person wrote down
#   prefixed  <sonar_org>_<repo>   what GitHub auto-provisioning creates
#   bare      <repo>               what a hand-made project usually is
#   suffix    *_<repo>             any other prefix, compared case-blind
#
# `override` is first because it is the only one anybody stated outright; the
# rest are inferences from a naming convention. `suffix` is last because it is
# the only rule that can match a project belonging to a different prefix
# entirely -- another organization's import, typically -- and the two exact
# rules should always win over it.
RULES = ('override', 'prefixed', 'bare', 'suffix')


class Match(NamedTuple):
    """One repository resolved to one Sonar project."""

    repo: str
    project_key: str
    rule: str


class Ambiguity(NamedTuple):
    """A repository that several projects could have claimed."""

    repo: str
    chosen: str
    rejected: List[str]


class MatchResult(NamedTuple):
    """What matching one organization's projects against its repos produced.

    Attributes:
        matched: Repositories resolved to a project.
        unmatched: Repositories no project claimed, in name order.
        assumed: For each unmatched repository, the `<org>_<repo>` key that a
            convention-based implementation would have queried. Printed by the
            sync so a naming mismatch is visible rather than inferred.
        orphans: Project keys that matched no repository. Kept because a Sonar
            project with no repository beside it is the other half of the same
            evidence.
        ambiguous: Repositories where more than one project was a candidate,
            with the one precedence chose and the ones it did not.
        unknown_overrides: Entries in the map file naming a project the
            organization does not hold, as (repo, key) pairs. Reported rather
            than raised: a project deleted or renamed in SonarCloud should not
            cost the repository the gate the ordinary rules would have found
            for it, but a line in a hand-written map that quietly does nothing
            is worth saying out loud.
        conflicting_overrides: Entries naming a project another override
            already took, as (repo, key) pairs. One project describes one
            codebase; the loser falls through to the ordinary rules.
    """

    matched: List[Match]
    unmatched: List[str]
    assumed: Dict[str, str]
    orphans: List[str]
    ambiguous: List[Ambiguity]
    unknown_overrides: List[Tuple[str, str]]
    conflicting_overrides: List[Tuple[str, str]]


def assumed_key(sonar_org: str, repo: str) -> str:
    """The project key SonarCloud's GitHub provisioning would have created."""
    return f'{sonar_org}_{repo}'


def _suffix(key: str) -> str:
    """The part of a project key after its last underscore, lowercased."""
    return key.rsplit('_', 1)[-1].lower()


def match_projects(repos: Iterable[str], projects: Sequence[Dict[str, object]],
                   sonar_org: str,
                   overrides: Optional[Dict[str, str]] = None) -> MatchResult:
    """Resolve repositories to Sonar projects by precedence.

    Deterministic in every respect: repositories are considered in name order
    and a tie between candidate projects is broken by key order, so the same
    two inputs always produce the same matching. An ambiguity is resolved and
    *reported* rather than raising -- two projects that both look like one
    repository is a fact about the Sonar organization, not a fault in the
    tool, and refusing to run over it would make the whole column unavailable
    because of one stray project.

    **Overrides are applied in a pass of their own, before any rule runs.**
    Claiming them as they came up in name order would let an inferred match on
    an earlier repository take a project that a later repository names
    outright, which would make the result depend on alphabetical accident. A
    person who wrote the pair down outranks a convention either way.

    Args:
        repos: GitHub repository names.
        projects: Dicts carrying at least `key`, as `SonarClient.projects`
            returns them.
        sonar_org: The SonarCloud organization key, used for the `prefixed`
            rule and for `assumed`.
        overrides: Repository name -> project key, from the map file.
            An entry naming a project the organization does not hold is
            collected into `unknown_overrides` and otherwise ignored, so the
            repository still gets whatever the ordinary rules find.

    Returns:
        A `MatchResult`.
    """
    keys = [str(p['key']) for p in projects if p.get('key')]
    by_key = {key: key for key in keys}

    by_suffix: Dict[str, List[str]] = {}
    for key in keys:
        by_suffix.setdefault(_suffix(key), []).append(key)
    for bucket in by_suffix.values():
        bucket.sort()

    matched: List[Match] = []
    unmatched: List[str] = []
    assumed: Dict[str, str] = {}
    ambiguous: List[Ambiguity] = []
    unknown_overrides: List[Tuple[str, str]] = []
    conflicting_overrides: List[Tuple[str, str]] = []
    claimed: set = set()

    # Materialized once: `repos` is an Iterable, and reading it twice -- once
    # for the override pass and once for the rules -- would leave the second
    # pass empty if a caller handed over a generator.
    names = sorted(repos)
    tracked = set(names)
    resolved: Dict[str, str] = {}
    for repo, key in sorted((overrides or {}).items()):
        # An override for a repository the store does not hold is not an
        # error: the map outlives the repository list, and a line for a repo
        # that has not been synced yet should simply wait rather than warn.
        if repo not in tracked:
            continue
        if key not in by_key:
            unknown_overrides.append((repo, key))
            continue
        if key in claimed:
            # Two overrides naming one project. One project describes one
            # codebase, so the first in name order keeps it; the second is
            # reported and falls through to the ordinary rules rather than
            # one of the pair silently winning.
            conflicting_overrides.append((repo, key))
            continue
        claimed.add(key)
        resolved[repo] = key

    for repo in names:
        if repo in resolved:
            matched.append(Match(repo=repo, project_key=resolved[repo],
                                 rule='override'))
            continue
        candidates: List[str] = []
        prefixed = assumed_key(sonar_org, repo)
        if prefixed in by_key:
            candidates.append(prefixed)
        if repo in by_key and repo not in candidates:
            candidates.append(repo)
        for key in by_suffix.get(repo.lower(), ()):
            if key not in candidates:
                candidates.append(key)

        # A project already taken by an earlier repository cannot be taken
        # again: one project describes one codebase. This can only bite
        # through the `suffix` rule, or where a repository is literally named
        # `<org>_<other repo>`.
        free = [key for key in candidates if key not in claimed]
        if not free:
            unmatched.append(repo)
            assumed[repo] = prefixed
            continue

        chosen = free[0]
        claimed.add(chosen)
        if chosen == prefixed:
            rule = 'prefixed'
        elif chosen == repo:
            rule = 'bare'
        else:
            rule = 'suffix'
        matched.append(Match(repo=repo, project_key=chosen, rule=rule))
        if len(free) > 1:
            ambiguous.append(Ambiguity(repo=repo, chosen=chosen,
                                       rejected=free[1:]))

    orphans = sorted(key for key in keys if key not in claimed)
    return MatchResult(matched=matched, unmatched=unmatched, assumed=assumed,
                       orphans=orphans, ambiguous=ambiguous,
                       unknown_overrides=unknown_overrides,
                       conflicting_overrides=conflicting_overrides)


def rows(result: MatchResult, projects: Sequence[Dict[str, object]],
         gates: Dict[str, str]) -> List[Dict[str, Optional[str]]]:
    """Turn a matching plus the fetched facts into store rows.

    Every project gets a row, matched or not: an orphan is evidence about the
    key convention and costs one row to keep.

    Args:
        result: What `match_projects` returned.
        projects: The project dicts that were matched.
        gates: Project key -> gate status, as `SonarClient.gate_statuses`
            returns it. A key absent here has no gate result, which is stored
            as NULL rather than guessed at.

    Returns:
        Dicts shaped for `SyncStore.replace_sonar_projects`.
    """
    by_repo = {m.project_key: m for m in result.matched}
    out: List[Dict[str, Optional[str]]] = []
    for project in projects:
        key = project.get('key')
        if not key:
            continue
        key = str(key)
        match = by_repo.get(key)
        out.append({
            'project_key': key,
            'name': project.get('name'),
            'repo_name': match.repo if match else None,
            'match_rule': match.rule if match else None,
            'last_analysis': project.get('last_analysis'),
            'gate_status': gates.get(key),
        })
    return out
