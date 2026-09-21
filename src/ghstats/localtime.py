"""Which zone the tools *display* in.

The store is UTC end to end and stays that way: instants are comparable, the
columns are indexable, and a row means the same thing wherever it is read.
This module is about the other end -- the point where an instant becomes a day
band, an hour bucket or a line on a terminal, which is a question about the
reader, not about the data.

The answer defaults to the machine's own zone rather than UTC. A page that
groups a Berlin team's evening commits into the next UTC day, or a histogram
that puts their stand-up an hour before it happens, is not neutral -- it is
quietly wrong in a way that reads as a real finding about how people work.
"""
import os
from datetime import timezone
from pathlib import Path

try:                                     # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:                      # pragma: no cover - 3.8 fallback
    ZoneInfo = None                      # type: ignore

UTC = 'UTC'


def local_zone_name() -> str:
    """The machine's IANA zone name, or `UTC` when it cannot be named.

    A fixed offset read from `datetime.now().astimezone()` would not do: the
    window these stores cover spans DST transitions, so grouping needs a zone
    with a history, which means a name `zoneinfo` can resolve. Each source
    below yields one. When none of them does, UTC is the honest answer rather
    than a guess -- and every caller states the zone it settled on, so a UTC
    fallback says so instead of passing for local time that is an hour out.
    """
    candidates = []
    env = os.environ.get('TZ')
    if env:
        # `TZ=:Europe/Berlin` is a legal spelling; the colon is a hint to the C
        # library about where to look, and zoneinfo will not accept it.
        candidates.append(env.lstrip(':'))
    link = Path('/etc/localtime')
    try:
        if link.is_symlink():
            parts = link.resolve().parts
            if 'zoneinfo' in parts:
                candidates.append('/'.join(parts[parts.index('zoneinfo') + 1:]))
    except OSError:
        pass
    try:
        candidates.append(Path('/etc/timezone').read_text().strip())
    except OSError:
        pass

    for name in candidates:
        if name and ZoneInfo is not None:
            try:
                ZoneInfo(name)
            except Exception:
                continue
            return name
    return UTC


def zone(name: str):
    """Resolve a zone name to a `tzinfo`, falling back to UTC rather than failing.

    A bad `--timezone` should not make a tool refuse to start; it should render
    UTC and be visibly wrong in the header, which the reader can recover from.
    """
    if not name or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc
