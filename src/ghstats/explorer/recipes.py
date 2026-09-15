"""Hand-written SQL that reproduces the explorer's cards.

A card's number is SQL plus Python -- medians, buckets and ratios are computed
after the query -- so no statement the explorer runs *is* the number. A recipe
is one self-contained query that is, written to be read: `recipes/*.sql`, each
opening with a comment header the SQL page shows as its explanation.

    -- title: PR size and discussion: the headline numbers
    -- card: pr-size
    -- ignores: team, project, issue, q, ai
    --
    -- Free text explaining what the card counts and why.

`card` names the card that links to it. `ignores` lists the filters the card
applies and the recipe does not, so the page can say when a number may differ.

**Kept honest by test.** `tests/test_recipes.py` runs every recipe against a
fixture store and compares it with `queries.py` under several filter sets. A
change to how a card counts that does not update its recipe fails there.
"""
from pathlib import Path
from typing import Any, Dict, List

RECIPES = Path(__file__).resolve().parent / 'recipes'

# Filters a card may honour that no recipe binds as a parameter.
UNBOUND_FILTERS = ('team', 'project', 'issue', 'kinds', 'q', 'ai')

_FIELDS = ('title', 'card', 'ignores')


class RecipeError(ValueError):
    """A recipe file whose header is malformed."""


def parse(name: str, text: str) -> Dict[str, Any]:
    """Read one recipe's header. The SQL is returned whole, header included."""
    fields: Dict[str, str] = {}
    about: List[str] = []
    in_header = True
    for line in text.splitlines():
        if not line.startswith('--'):
            break
        body = line[2:].strip() if line[2:3] != ' ' else line[3:].rstrip()
        if in_header:
            key, sep, value = body.partition(':')
            if sep and key.strip() in _FIELDS:
                fields[key.strip()] = value.strip()
                continue
            in_header = False
            if not body:
                continue
        about.append(body)

    missing = [f for f in ('title', 'card') if not fields.get(f)]
    if missing:
        raise RecipeError(f'{name}: header lacks {", ".join(missing)}')
    ignores = [f.strip() for f in fields.get('ignores', '').split(',') if f.strip()]
    unknown = sorted(set(ignores) - set(UNBOUND_FILTERS))
    if unknown:
        raise RecipeError(f'{name}: ignores unknown filters {unknown}')
    return {
        'id': name,
        'title': fields['title'],
        'card': fields['card'],
        'ignores': ignores,
        'about': '\n'.join(about).strip(),
        'sql': text,
    }


def load() -> List[Dict[str, Any]]:
    """Every recipe, by file name."""
    return [parse(path.stem, path.read_text(encoding='utf-8'))
            for path in sorted(RECIPES.glob('*.sql'))]


def get(recipe_id: str) -> Dict[str, Any]:
    path = RECIPES / f'{recipe_id}.sql'
    if '/' in recipe_id or not path.is_file():
        raise KeyError(recipe_id)
    return parse(recipe_id, path.read_text(encoding='utf-8'))
