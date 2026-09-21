"""REST client for the SonarCloud Web API.

Two calls answer everything the explorer shows:

    /api/projects/search     one paginated list of the organization's projects,
                             each with its key and last analysis date
    /api/measures/search     `alert_status` for up to 100 projects at a time

Deliberately small. See the package docstring for why this does not reuse the
GraphQL client's machinery.
"""
import time
from typing import Any, Dict, List, Optional, Sequence

import requests

SONARCLOUD_URL = 'https://sonarcloud.io'

# Projects per page of `/api/projects/search`. 500 is the API's ceiling.
PROJECT_PAGE = 500

# `/api/measures/search` accepts at most 100 project keys per call. Exceeding
# it is a 400, not a truncation, so the batch size is a hard limit rather than
# a tuning knob.
MEASURE_BATCH = 100

# Statuses worth retrying. 429 is SonarCloud's throttle; the 5xx family is the
# usual transient.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

RETRY_BACKOFF = 2.0

# A defensive ceiling on pagination. The API reports a total and this client
# stops when it has that many, so this only fires if `paging.total` disagrees
# with what is actually returned -- in which case looping forever is worse
# than stopping short and saying so.
MAX_PAGES = 100


class SonarError(Exception):
    """The Sonar API could not be read."""


class SonarAuthError(SonarError):
    """The token was rejected, or lacks permission for the endpoint.

    Separate from `SonarError` because the caller can do something about it:
    `/api/projects/search` requires organization administration on some
    accounts, and a plain user token falls back to the public projects
    endpoint rather than failing the run.
    """


class SonarClient:
    """A thin, synchronous SonarCloud client.

    Single-threaded by design: the whole sweep is a handful of requests, and
    the concurrency that makes the GitHub sync fast would here only add a way
    to trip a rate limit.
    """

    def __init__(self, token: str, *, base_url: str = SONARCLOUD_URL,
                 timeout: int = 30, max_retries: int = 3, debug: bool = False):
        """Initialize the client.

        Args:
            token: A SonarCloud user token.
            base_url: Server root. Overridable so a SonarQube instance is a
                configuration change rather than a code change; nothing else
                here is SonarCloud-specific except the `organization`
                parameter, which SonarQube ignores.
            timeout: Per-request timeout in seconds.
            max_retries: Attempts per request for transient failures.
            debug: Print each request and its status.
        """
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.max_retries = max_retries
        self.debug = debug
        self.request_count = 0
        self._session = requests.Session()
        # SonarCloud accepts the token as a bearer credential. The older
        # scheme -- HTTP Basic with the token as the username and an empty
        # password -- still works, but puts the secret through a second
        # encoding for no gain.
        self._session.headers.update({
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json',
            'User-Agent': 'ghstats-sonar',
        })

    def get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Issue one GET, retrying transient failures.

        Raises:
            SonarAuthError: 401 or 403.
            SonarError: Any other non-2xx, or an unreadable body.
        """
        url = f'{self.base_url}{path}'
        last: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.get(url, params=params,
                                             timeout=self.timeout)
            except requests.RequestException as exc:
                last = str(exc)
                if attempt >= self.max_retries:
                    break
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue

            self.request_count += 1
            if self.debug:
                print(f'  GET {path} {params} -> {response.status_code}')

            if response.status_code in (401, 403):
                raise SonarAuthError(
                    f'{path}: HTTP {response.status_code} -- token rejected or '
                    f'not permitted for this endpoint')
            if response.status_code in RETRY_STATUSES:
                last = f'HTTP {response.status_code}'
                if attempt >= self.max_retries:
                    break
                # `Retry-After` is the server saying how long it wants; obey it
                # rather than the local schedule when it is present.
                after = response.headers.get('Retry-After')
                delay = RETRY_BACKOFF * (attempt + 1)
                if after:
                    try:
                        delay = max(delay, float(after))
                    except ValueError:
                        pass
                time.sleep(delay)
                continue
            if not response.ok:
                raise SonarError(f'{path}: HTTP {response.status_code} '
                                 f'{response.text[:200]}')
            try:
                return response.json()
            except ValueError as exc:
                raise SonarError(f'{path}: response was not JSON: {exc}') from exc

        raise SonarError(f'{path}: giving up after {self.max_retries + 1} '
                         f'attempts ({last})')

    def projects(self, organization: str) -> List[Dict[str, Any]]:
        """Every project in the organization, with its last analysis date.

        **Two endpoints, because the first needs a permission not every token
        has.** `/api/projects/search` is the authoritative list and carries
        `lastAnalysisDate`, but it is an administrative endpoint: a plain user
        token gets 403. `/api/components/search_projects` is public and returns
        the same projects, with `analysisDate` where the server supplies it.
        Falling back costs nothing when the first call works and keeps the tool
        usable when it does not.

        Returns:
            Dicts of `key`, `name` and `last_analysis` (None if never analysed).
        """
        try:
            return self._page('/api/projects/search',
                              {'organization': organization},
                              date_field='lastAnalysisDate')
        except SonarAuthError:
            if self.debug:
                print('  /api/projects/search denied; falling back to '
                      '/api/components/search_projects')
            return self._page('/api/components/search_projects',
                              {'organization': organization},
                              date_field='analysisDate')

    def _page(self, path: str, params: Dict[str, Any],
              *, date_field: str) -> List[Dict[str, Any]]:
        """Walk a paginated `components` response to the end."""
        out: List[Dict[str, Any]] = []
        page = 1
        while page <= MAX_PAGES:
            body = self.get(path, {**params, 'p': page, 'ps': PROJECT_PAGE})
            components = body.get('components') or []
            for component in components:
                key = component.get('key')
                if not key:
                    continue
                out.append({
                    'key': key,
                    'name': component.get('name'),
                    'last_analysis': component.get(date_field),
                })
            total = (body.get('paging') or {}).get('total')
            if not components:
                break
            if total is not None and len(out) >= total:
                break
            page += 1
        return out

    def gate_statuses(self, keys: Sequence[str]) -> Dict[str, str]:
        """Quality gate status per project key.

        Asks only for `alert_status`. The call costs the same with more metric
        keys, but a column nobody displays is a column nobody validates -- see
        `docs/sonar.md`.

        Returns:
            Mapping of project key to `OK` / `ERROR` / `WARN` / `NONE`. A
            project with no gate result is absent rather than defaulted, so
            the caller can tell "no result" from "failing".
        """
        out: Dict[str, str] = {}
        for start in range(0, len(keys), MEASURE_BATCH):
            batch = list(keys[start:start + MEASURE_BATCH])
            if not batch:
                continue
            body = self.get('/api/measures/search', {
                'projectKeys': ','.join(batch),
                'metricKeys': 'alert_status',
            })
            for measure in body.get('measures') or []:
                if measure.get('metric') != 'alert_status':
                    continue
                component = measure.get('component')
                value = measure.get('value')
                if component and value:
                    out[component] = value
        return out
