"""GraphQL client for the GitHub API with rate-limit guarding and retry/backoff.

The REST path (PyGithub) is not used for syncing: `list-commits` omits the `stats`
object, so PyGithub must issue one extra request per commit to populate
additions/deletions. GraphQL returns them inline on the commit node.
"""
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

GITHUB_GRAPHQL_URL = 'https://api.github.com/graphql'

# Pause the sweep when the remaining hourly point budget drops below this.
RATE_LIMIT_FLOOR = 200

# HTTP statuses worth retrying. Heavy commit-history queries 502 fairly often.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Secondary rate limits arrive as 403 (sometimes 429) with an explanatory body,
# and are separate from the hourly point budget: they fire on request
# concurrency and server CPU time, so a wide-window rebuild trips them while
# `rateLimit.remaining` still reads in the thousands. They must back off rather
# than fail, and the backoff has to be shared -- independently retrying threads
# are what caused the limit in the first place.
SECONDARY_LIMIT_STATUSES = frozenset({403, 429})
SECONDARY_LIMIT_MARKERS = ('secondary rate limit', 'abuse detection')
SECONDARY_LIMIT_BACKOFF = 90
# Waits before giving up on a repo entirely. Six escalating pauses is
# roughly 30 minutes -- past that the org is throttled, not the query.
MAX_SECONDARY_WAITS = 6

# How long to wait when the budget is low but no reset time is known.
UNKNOWN_RESET_BACKOFF = 60

# GraphQL error types that a retry might clear. RESOURCE_LIMITS_EXCEEDED is not
# among them: it means the query asked for too many nodes and needs a smaller batch.
RETRYABLE_ERROR_TYPES = frozenset({'RATE_LIMITED'})


class GraphQLError(Exception):
    """A GraphQL response carried a non-empty `errors` array.

    GitHub returns HTTP 200 with null nodes plus a populated `errors` array on
    partial failure, so the status code alone is not a success signal.
    """

    def __init__(self, errors: List[Dict[str, Any]], label: Optional[str] = None):
        self.errors = errors
        self.label = label
        types = sorted({e.get('type', 'UNKNOWN') for e in errors})
        message = errors[0].get('message', '') if errors else ''
        super().__init__(
            f"{label or 'query'} failed [{', '.join(types)}] "
            f"({len(errors)} error(s)): {message}"
        )

    @property
    def types(self) -> set:
        """Set of GraphQL error type strings present in the response."""
        return {e.get('type', 'UNKNOWN') for e in self.errors}


class GraphQLTransportError(Exception):
    """The request never produced a usable GraphQL response."""


def _parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp from the API into an aware datetime."""
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class GitHubGraphQL:
    """Thread-safe GraphQL client.

    Rate-limit state is shared across threads behind a lock; HTTP sessions are
    per-thread because `requests.Session` is not documented as thread-safe.
    """

    def __init__(
        self,
        token: str,
        *,
        max_retries: int = 4,
        timeout: int = 90,
        rate_limit_floor: int = RATE_LIMIT_FLOOR,
        debug: bool = False,
    ):
        """Initialize the client.

        Args:
            token: GitHub token with org-wide read access.
            max_retries: Retry attempts per query for transient failures.
            timeout: Per-request timeout in seconds.
            rate_limit_floor: Pause when remaining points drop below this.
            debug: Print per-query cost and rate-limit state.
        """
        self._token = token
        self.max_retries = max_retries
        self.timeout = timeout
        self.rate_limit_floor = rate_limit_floor
        self.debug = debug

        self._local = threading.local()
        self._lock = threading.Lock()
        self._remaining: Optional[int] = None
        self._reset_at: Optional[datetime] = None
        self._points_spent = 0
        self._query_count = 0
        self._pause_until: float = 0.0
        self._secondary_hits = 0

    @property
    def points_spent(self) -> int:
        """Total GraphQL points consumed by this client."""
        return self._points_spent

    @property
    def query_count(self) -> int:
        """Total queries issued, excluding retries."""
        return self._query_count

    @property
    def remaining(self) -> Optional[int]:
        """Remaining hourly points, or None if not yet observed."""
        return self._remaining

    def _session(self) -> requests.Session:
        """Get (or create) this thread's HTTP session."""
        session = getattr(self._local, 'session', None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                'Authorization': f'Bearer {self._token}',
                'Accept': 'application/json',
                'User-Agent': 'ghstats-sync',
            })
            self._local.session = session
        return session

    @property
    def secondary_hits(self) -> int:
        """How many times a secondary rate limit was tripped."""
        return self._secondary_hits

    def _is_secondary_limit(self, response: requests.Response) -> bool:
        """Whether a response is a secondary rate limit rather than a real 403."""
        if response.status_code not in SECONDARY_LIMIT_STATUSES:
            return False
        body = response.text.lower()
        return any(marker in body for marker in SECONDARY_LIMIT_MARKERS)

    def _trip_secondary_limit(self, response: requests.Response) -> float:
        """Record a secondary rate limit and pause every thread.

        Returns:
            Seconds until the shared pause lifts.
        """
        delay = None
        for header in ('Retry-After', 'X-RateLimit-Reset'):
            value = response.headers.get(header)
            if not value:
                continue
            try:
                delay = (float(value) if header == 'Retry-After'
                         else float(value) - time.time())
            except ValueError:
                delay = None
            if delay and delay > 0:
                break
        if not delay or delay <= 0:
            delay = SECONDARY_LIMIT_BACKOFF

        with self._lock:
            self._secondary_hits += 1
            hits = self._secondary_hits
            # Escalate if we keep tripping: the first backoff was not enough.
            delay = delay * min(hits, 4)
            self._pause_until = max(self._pause_until, time.monotonic() + delay)
            resume = self._pause_until
        print(f'  Secondary rate limit (hit {hits}); pausing all requests '
              f'{delay:.0f}s', flush=True)
        return resume - time.monotonic()

    def _await_budget(self):
        """Block until the shared pause has lifted and points are above floor."""
        while True:
            with self._lock:
                pause = self._pause_until
            wait = pause - time.monotonic()
            if wait <= 0:
                break
            time.sleep(min(wait, 30))

        while True:
            with self._lock:
                remaining = self._remaining
                reset_at = self._reset_at
            if remaining is None or remaining > self.rate_limit_floor:
                return
            if reset_at is None:
                # Budget is low and we do not know when it refills. Wait and
                # re-check rather than charging ahead: failing open here would
                # burn the remaining budget and strand a multi-hour rebuild.
                print(f'  Rate limit low ({remaining} points), no reset time '
                      f'known; waiting {UNKNOWN_RESET_BACKOFF}s', flush=True)
                time.sleep(UNKNOWN_RESET_BACKOFF)
                continue
            wait = (reset_at - datetime.now(timezone.utc)).total_seconds()
            if wait <= 0:
                # Window has rolled over; let the next response refresh the state.
                with self._lock:
                    self._remaining = None
                return
            print(
                f"  Rate limit low ({remaining} points); "
                f"pausing {wait / 60:.1f} min until {reset_at.isoformat()}",
                flush=True,
            )
            time.sleep(min(wait + 1, 300))

    def _record_rate_limit(self, data: Optional[Dict[str, Any]]):
        """Update rate-limit state from a `rateLimit` block in the response."""
        if not data:
            return
        block = data.get('rateLimit')
        if not isinstance(block, dict):
            return
        with self._lock:
            if 'remaining' in block:
                self._remaining = block['remaining']
            if block.get('resetAt'):
                try:
                    self._reset_at = _parse_iso(block['resetAt'])
                except ValueError:
                    pass
            self._points_spent += block.get('cost') or 0

    def _sleep_backoff(self, attempt: int, response: Optional[requests.Response]):
        """Sleep before a retry, honouring Retry-After when present."""
        delay = None
        if response is not None:
            header = response.headers.get('Retry-After')
            if header:
                try:
                    delay = float(header)
                except ValueError:
                    delay = None
        if delay is None:
            delay = min(2 ** attempt, 30) + random.uniform(0, 1)
        time.sleep(delay)

    def query(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        *,
        label: Optional[str] = None,
        max_retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute a GraphQL query and return its `data` block.

        Args:
            query: GraphQL document.
            variables: Variable values for the document.
            label: Short name used in error messages and debug output.
            max_retries: Override the client default. Callers with a cheaper
                recovery path than retrying (a multi-repo batch that can be
                split) should lower this: an oversized query 502s
                deterministically, so retrying it whole is pure delay.

        Returns:
            The `data` object from the response.

        Raises:
            GraphQLError: The response carried a non-empty `errors` array.
            GraphQLTransportError: No usable response after retries.
        """
        self._await_budget()
        payload = {'query': query, 'variables': variables or {}}
        last_error: Optional[Exception] = None
        retries = self.max_retries if max_retries is None else max_retries

        # A secondary-limit pause is a wait, not a retry, so it gets its own
        # budget: charging it against `retries` would let a rate-limited batch
        # exhaust its attempts without the query ever having been evaluated.
        attempt = 0
        waits = 0

        while True:
            response = None
            try:
                response = self._session().post(
                    GITHUB_GRAPHQL_URL, json=payload, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = GraphQLTransportError(f"{label or 'query'}: {exc}")
                attempt += 1
                if attempt <= retries:
                    self._sleep_backoff(attempt, None)
                    continue
                raise last_error from exc

            # Checked before RETRY_STATUSES and before the generic non-200 raise:
            # a secondary limit is not a failure, and never a per-query concern.
            if self._is_secondary_limit(response):
                wait = self._trip_secondary_limit(response)
                last_error = GraphQLTransportError(
                    f"{label or 'query'}: secondary rate limit"
                )
                # Splitting the batch or failing the repo would not help: the
                # limit is about request rate, not query size.
                waits += 1
                if waits <= MAX_SECONDARY_WAITS:
                    time.sleep(max(wait, 0))
                    continue
                raise last_error

            if response.status_code in RETRY_STATUSES:
                last_error = GraphQLTransportError(
                    f"{label or 'query'}: HTTP {response.status_code}"
                )
                attempt += 1
                if attempt <= retries:
                    self._sleep_backoff(attempt, response)
                    continue
                raise last_error

            if response.status_code != 200:
                raise GraphQLTransportError(
                    f"{label or 'query'}: HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )

            try:
                body = response.json()
            except ValueError:
                # Gateway errors sometimes return an HTML page with a 200.
                last_error = GraphQLTransportError(
                    f"{label or 'query'}: non-JSON response: {response.text[:200]}"
                )
                attempt += 1
                if attempt <= retries:
                    self._sleep_backoff(attempt, response)
                    continue
                raise last_error

            data = body.get('data')
            self._record_rate_limit(data)

            errors = body.get('errors')
            if errors:
                error = GraphQLError(errors, label)
                attempt += 1
                if error.types & RETRYABLE_ERROR_TYPES and attempt <= retries:
                    last_error = error
                    self._sleep_backoff(attempt, None)
                    continue
                raise error

            if data is None:
                raise GraphQLTransportError(
                    f"{label or 'query'}: response had no data block"
                )

            with self._lock:
                self._query_count += 1
            if self.debug:
                with self._lock:
                    print(
                        f"    [gql] {label or 'query'} ok "
                        f"(remaining={self._remaining}, spent={self._points_spent})",
                        flush=True,
                    )
            return data
