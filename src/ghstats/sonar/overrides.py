"""The hand-written repository -> Sonar project map, read from a file.

**A file rather than a constant, because it is organization data and not
code.** Which repository answers to which project key is a property of one
company's history; putting it in `store/sqlite.py` would mean every correction
showed up as a source diff, and a working tree that could not be pulled without
a merge conflict over somebody else's mapping. `members.txt` and `repos.txt`
are kept out of the repository for the same reason, and this file sits beside
them in `.gitignore`.

The format is one pair per line, `#` starts a comment:

    # confirmed in SonarCloud, 2026-09-21
    legal-trustedshopscore   = legal-trusted-shops-core
    ca-push-notification-api = ca-push-notifications-api

Plain text rather than JSON or TOML, for two reasons. Comments: the map wants
them, because every line in it is a claim somebody has to be able to justify
later, and JSON has none. And the floor is Python 3.10, which has no `tomllib`
-- TOML would mean a third dependency for a file with one shape.

**Missing is fine; malformed is not.** An absent file means no overrides, which
is the normal case and says nothing. A line that cannot be read is a mapping
somebody wrote and believes is in effect, so it fails loudly with the line
number rather than being skipped into silence.
"""
import os
from typing import Dict, List, Tuple

# Looked for in the working directory, like `members.txt`. Overridable with
# `--map-file` for anyone keeping their organization data somewhere else.
DEFAULT_MAP_FILE = 'sonar-projects.txt'

COMMENT = '#'
SEPARATOR = '='


class MapFileError(ValueError):
    """The map file exists but could not be read as a mapping.

    Carries every bad line rather than the first: correcting a file one error
    per run is a poor way to spend an afternoon.
    """

    def __init__(self, path: str, problems: List[Tuple[int, str]]):
        self.path = path
        self.problems = problems
        detail = '\n'.join(f'  line {n}: {why}' for n, why in problems)
        super().__init__(f'{path}: {len(problems)} bad line(s)\n{detail}')


def load(path: str) -> Dict[str, str]:
    """Read the map, or return an empty one if the file is not there.

    Args:
        path: Map file location.

    Returns:
        Repository name -> Sonar project key.

    Raises:
        MapFileError: The file exists and holds a line that is not a pair, or
            names one repository twice.
        OSError: The file exists and could not be read.
    """
    if not os.path.exists(path):
        return {}

    mapping: Dict[str, str] = {}
    seen: Dict[str, int] = {}
    problems: List[Tuple[int, str]] = []

    with open(path, 'r', encoding='utf-8') as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.split(COMMENT, 1)[0].strip()
            if not line:
                continue
            if SEPARATOR not in line:
                problems.append((number, f'no {SEPARATOR!r} in {line!r}; '
                                         f'expected `repo = project-key`'))
                continue
            repo, key = (part.strip() for part in line.split(SEPARATOR, 1))
            if not repo or not key:
                problems.append((number, f'empty name in {line!r}'))
                continue
            # Two lines for one repository is a contradiction, not a
            # correction: whichever won would be an accident of file order.
            if repo in seen:
                problems.append((number, f'{repo!r} already mapped on line '
                                         f'{seen[repo]}'))
                continue
            seen[repo] = number
            mapping[repo] = key

    if problems:
        raise MapFileError(path, problems)
    return mapping
