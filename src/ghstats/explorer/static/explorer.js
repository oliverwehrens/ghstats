/* ghstats explorer — client.
 *
 * Three rules hold this together:
 *
 * 1. The hash is the state. Every filter and every selection lives in the URL,
 *    so a view is bookmarkable and a colleague can be sent one. Nothing is kept
 *    in a variable that the URL does not also record.
 *
 * 2. All data reaches the DOM through `text()`. Commit messages, branch names
 *    and logins are arbitrary strings from a third party; building HTML out of
 *    them by concatenation is how a repository name becomes script.
 *
 * 3. Charts live in `charts.js` and are drawn by a vendored Chart.js. This
 *    file builds the cards around them and decides what each view asks for;
 *    it does not know how a bar is painted.
 */
'use strict';

const KINDS = ['commit', 'pull', 'merge', 'review'];
const KIND_LABEL = { commit: 'commits', pull: 'PRs opened', merge: 'merges', review: 'reviews' };
const KIND_VAR = { commit: '--s1', pull: '--s2', merge: '--s3', review: '--s4' };

let META = null;

/* -- tiny DOM helpers ---------------------------------------------------- */

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = String(v);
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, String(v));
    }
  }
  for (const child of [].concat(children || [])) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attrs) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    node.setAttribute(k, String(v));
  }
  return node;
}

const num = (n) => (n === null || n === undefined ? '–' : Number(n).toLocaleString());

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

/* -- routing ------------------------------------------------------------- */

function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/users';
  const [path, query] = raw.split('?');
  return {
    parts: path.split('/').filter(Boolean).map(decodeURIComponent),
    params: new URLSearchParams(query || ''),
  };
}

/** Merge changes into the current hash without losing the rest of it. */
function go(parts, changes) {
  const { params } = parseHash();
  for (const [k, v] of Object.entries(changes || {})) {
    if (v === null || v === undefined || v === '') params.delete(k);
    else params.set(k, v);
  }
  const query = params.toString();
  location.hash = '/' + parts.map(encodeURIComponent).join('/') + (query ? '?' + query : '');
}

function navigate(parts, changes) { go(parts, changes); }

/** The filter set the controls and the URL currently agree on. */
function currentFilters() {
  const { params } = parseHash();
  const out = {};
  for (const key of ['from', 'to', 'user', 'repo', 'team', 'project', 'issue',
                     'kinds', 'q', 'ai', 'bots', 'limit', 'offset', 'tz']) {
    const value = params.get(key);
    if (value) out[key] = value;
  }
  return out;
}

/** The event kinds the current URL asks for, in palette order. */
function activeKinds() {
  const chosen = currentFilters().kinds;
  if (!chosen) return KINDS.slice();
  const on = chosen.split(',').filter((k) => KINDS.includes(k));
  return on.length ? KINDS.filter((k) => on.includes(k)) : KINDS.slice();
}

/** Add or remove one kind, refusing to leave the selection empty. */
function toggleKind(kind) {
  const on = activeKinds();
  const next = on.includes(kind) ? on.filter((k) => k !== kind) : on.concat([kind]);
  if (!next.length) {
    // An empty stream is not a filter anyone means. Nothing changes, so no
    // hashchange fires -- put the checkbox that was just cleared back.
    syncFilterUI();
    return;
  }
  // All four is the default; recording it would just clutter URLs.
  go(parseHash().parts, { kinds: next.length < KINDS.length ? next.join(',') : null });
}

// On an entity endpoint the entity is a path segment, so these never travel as
// query parameters from the URL. `/api/events` is the exception: it has no
// path segment to carry them, and `entityFilter` passes them in explicitly.
const ENTITY_KEYS = ['user', 'repo', 'team', 'project', 'issue'];

function apiQuery(extra) {
  const params = new URLSearchParams();
  const f = Object.assign({}, currentFilters());
  ENTITY_KEYS.forEach((key) => delete f[key]);
  Object.assign(f, extra || {});     // an explicit entity outranks the URL
  for (const [k, v] of Object.entries(f)) if (v) params.set(k, v);
  return params.toString();
}

/**
 * The newest render, and the error an older one stops at.
 *
 * Two hash changes in quick succession leave two fetches in flight, and the
 * page belongs to whichever *finishes* last unless someone says otherwise --
 * so a slow org-wide view could land under a person's URL. A superseded fetch
 * throws here instead of returning, which stops its view before it can reach
 * `show`.
 */
let GENERATION = 0;
const STALE = new Error('superseded');

async function api(path, extra) {
  const mine = GENERATION;
  const query = apiQuery(extra);
  const response = await fetch('/api/' + path + (query ? '?' + query : ''));
  const body = await response.json();
  if (mine !== GENERATION) throw STALE;
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

/** `api` for the SQL page, whose query is a JSON body rather than a path. */
async function apiPost(path, payload, extra) {
  const mine = GENERATION;
  const query = apiQuery(extra);
  const response = await fetch('/api/' + path + (query ? '?' + query : ''), {
    method: 'POST',
    // The server refuses anything else: see `Handler.do_POST`.
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (mine !== GENERATION) throw STALE;
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

/* -- filter controls ---------------------------------------------------- */

function isoDay(d) { return d.toISOString().slice(0, 10); }

function applyPreset(days) {
  if (!days) { go(parseHash().parts, { from: null, to: null }); return; }
  const to = new Date();
  const from = new Date(to.getTime() - (days - 1) * 86400000);
  go(parseHash().parts, { from: isoDay(from), to: isoDay(to) });
}

function wireFilters() {
  document.querySelectorAll('.presets button').forEach((button) => {
    button.addEventListener('click', () => applyPreset(Number(button.dataset.days)));
  });

  const from = document.getElementById('from');
  const to = document.getElementById('to');
  from.addEventListener('change', () => go(parseHash().parts, { from: from.value || null }));
  to.addEventListener('change', () => go(parseHash().parts, { to: to.value || null }));

  document.querySelectorAll('.kinds input').forEach((box) => {
    box.addEventListener('change', () => toggleKind(box.value));
  });

  const bots = document.getElementById('bots');
  bots.addEventListener('change', () => go(parseHash().parts, { bots: bots.checked ? '1' : null }));

  const ai = document.getElementById('ai');
  ai.addEventListener('change', () => go(parseHash().parts, { ai: ai.value || null }));

  const q = document.getElementById('q');
  let timer = null;
  q.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => go(parseHash().parts, { q: q.value.trim() || null }), 320);
  });
}

/** Push URL state back into the controls, so a shared link renders honestly. */
function syncFilterUI() {
  const f = currentFilters();
  // A day view takes its window from the path, so the range controls would lie.
  const onDay = parseHash().parts[0] === 'day' && parseHash().parts.length === 2;
  document.getElementById('filters').classList.toggle('day-pinned', onDay);
  // Kinds, AI and text search bind no SQL parameter, so they would lie there too.
  document.getElementById('filters').classList.toggle('sql-pinned', parseHash().parts[0] === 'sql');
  document.getElementById('from').value = f.from || '';
  document.getElementById('to').value = f.to || '';
  document.getElementById('bots').checked = f.bots === '1';
  document.getElementById('ai').value = f.ai || '';
  const q = document.getElementById('q');
  if (document.activeElement !== q) q.value = f.q || '';

  const kinds = activeKinds();
  document.querySelectorAll('.kinds input').forEach((box) => {
    box.checked = kinds.includes(box.value);
  });

  let days = null;
  if (!f.from && !f.to) days = 0;
  else if (f.from && f.to) {
    const span = Math.round((Date.parse(f.to) - Date.parse(f.from)) / 86400000) + 1;
    if (f.to === isoDay(new Date()) && [1, 7, 30, 90].includes(span)) days = span;
  }
  document.querySelectorAll('.presets button').forEach((button) => {
    button.classList.toggle('on', days !== null && Number(button.dataset.days) === days);
  });
}

/* -- charts -------------------------------------------------------------- */

const tip = document.getElementById('tip');

function showTip(html, event) {
  clear(tip);
  tip.appendChild(html);
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  let x = event.clientX + 14;
  let y = event.clientY - box.height - 10;
  if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 14;
  if (y < 8) y = event.clientY + 16;
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}

function hideTip() { tip.hidden = true; }

/**
 * A clickable legend over all four kinds, dimming the ones filtered out.
 *
 * Chart.js would happily draw its own and hide datasets in local state, which
 * puts a second filter on the page that the URL knows nothing about. This one
 * writes the same `kinds` parameter the checkbox row writes, so a legend
 * click, a checkbox and a pasted link cannot disagree.
 */
function legend() {
  const on = activeKinds();
  const box = el('div', { class: 'legend' });
  KINDS.forEach((kind) => {
    const swatch = el('i');
    swatch.style.background = `var(${KIND_VAR[kind]})`;
    const button = el('button', {
      class: 'key' + (on.includes(kind) ? '' : ' off'),
      type: 'button',
      'aria-pressed': on.includes(kind) ? 'true' : 'false',
      title: on.includes(kind) ? `Hide ${KIND_LABEL[kind]}` : `Show ${KIND_LABEL[kind]}`,
      onclick: () => toggleKind(kind),
    }, [swatch, KIND_LABEL[kind]]);
    box.appendChild(button);
  });
  return box;
}

/* -- shared blocks ------------------------------------------------------ */

/**
 * A link to the SQL page with the recipe that reproduces a card, carrying the
 * filters the card was drawn with. `entity` supplies what the page's path
 * holds -- a repository, a person, a day -- since those are not query
 * parameters here.
 */
function sqlLink(recipe, entity, label) {
  const query = new URLSearchParams();
  for (const [k, v] of Object.entries(currentFilters())) {
    if (!ENTITY_KEYS.includes(k) && k !== 'offset' && k !== 'limit') query.set(k, v);
  }
  for (const [k, v] of Object.entries(entity || {})) if (v) query.set(k, v);
  query.set('recipe', recipe);
  return el('a', {
    class: 'sql-link', href: '#/sql?' + query.toString(),
    text: label || 'SQL', title: 'Open the query behind these numbers',
  });
}

/**
 * Headline counts.
 *
 * `omit` drops a tile that cannot say anything in this view: "people: 1" on a
 * person's page is noise where a number should be, and so is the repository
 * count on a repository's.
 */
function tiles(totals, omit, link) {
  const skip = omit || [];
  const box = el('div', { class: 'tiles' });
  const spec = [
    ['commits', totals.commits], ['PRs opened', totals.pulls],
    ['merged', totals.merges], ['reviews', totals.reviews],
    ['repositories', totals.repos, 'repos'], ['people', totals.people, 'people'],
  ];
  spec.forEach(([label, value, key]) => {
    if (key && skip.includes(key)) return;
    box.appendChild(el('div', { class: 'tile' }, [
      el('div', { class: 'n', text: num(value) }),
      el('div', { class: 'l', text: label }),
    ]));
  });
  box.appendChild(el('div', { class: 'tile pos' }, [
    el('div', { class: 'n', text: '+' + num(totals.lines_added) }),
    el('div', { class: 'l', text: 'lines added' }),
  ]));
  box.appendChild(el('div', { class: 'tile neg' }, [
    el('div', { class: 'n', text: '−' + num(totals.lines_removed) }),
    el('div', { class: 'l', text: 'lines removed' }),
  ]));
  if (!link) return box;
  return el('div', { class: 'tiles-wrap' }, [box, el('div', { class: 'sql-note' }, [link])]);
}

/**
 * Defer a chart until its card is on the page.
 *
 * Chart.js sizes itself from its container, and a container that is not in the
 * document yet has no size. The `isConnected` check covers the other end: a
 * fast second navigation can retire the card before the frame arrives, and
 * drawing into a detached canvas would register an instance nothing destroys.
 */
function whenPlaced(host, draw) {
  requestAnimationFrame(() => { if (host.isConnected) draw(host); });
}

function chartCard(bundle) {
  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', { text: 'Activity by day' }));
  card.appendChild(legend());
  const host = el('div');
  card.appendChild(host);
  whenPlaced(host, () =>
    dailyChart(host, bundle.by_day, (day) => navigate(['day', day])));
  return card;
}

/**
 * The same two questions the static report asks — which weekday, which hour —
 * against the same kinds, so a person's page and the org's page are read the
 * same way at two scales.
 */
function rhythmCard(bundle) {
  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', { text: 'Activity patterns (' + (META ? META.timezone : 'UTC') + ')' }));
  card.appendChild(legend());
  const wrap = el('div', { class: 'grid2' });
  const dows = el('div');
  const hours = el('div');
  dows.appendChild(el('div', { class: 'count-note', text: 'By day of week' }));
  hours.appendChild(el('div', { class: 'count-note', text: 'By hour of day' }));
  const dowHost = el('div');
  const hourHost = el('div');
  dows.appendChild(dowHost);
  hours.appendChild(hourHost);
  wrap.appendChild(dows);
  wrap.appendChild(hours);
  card.appendChild(wrap);
  whenPlaced(dowHost, () => weekdayChart(dowHost, bundle.rhythm));
  whenPlaced(hourHost, () => hourChart(hourHost, bundle.rhythm));
  return card;
}

/**
 * The window as a wall of days.
 *
 * The daily bars answer "how much, when"; this answers "how often, and with
 * what gaps" — a fortnight of silence is a shape here and a flat stretch of
 * axis there. It counts every kind the filters allow, not just commits, so it
 * does not quietly disagree with the chart above it.
 */
function calendarCard(bundle) {
  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', { text: 'Contribution calendar' }));
  const host = el('div');
  card.appendChild(host);
  const window_ = bundle.filters || {};
  whenPlaced(host, () => contributionCalendar(
    host, bundle.by_day, { from: window_.frm, to: window_.to },
    (day) => navigate(['day', day])));
  return card;
}

function rankTable(title, rows, opts) {
  const options = opts || {};
  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', { text: title }));
  if (!rows.length) {
    card.appendChild(el('div', { class: 'empty', text: 'Nothing in this window.' }));
    return card;
  }
  const table = el('table');
  const head = el('tr', null, [el('th', { text: options.label || 'name' })]);
  ['commits', 'PRs', 'merged', 'reviews', '+/−'].forEach((h) =>
    head.appendChild(el('th', { class: 'num', text: h })));
  table.appendChild(el('thead', null, [head]));

  const body = el('tbody');
  rows.forEach((row) => {
    const name = row[options.key || 'name'];
    const cell = el('td');
    if (options.link) {
      cell.appendChild(el('a', {
        href: '#', text: name,
        onclick: (e) => { e.preventDefault(); navigate(options.link(name)); },
      }));
    } else {
      cell.textContent = name;
    }
    const tr = el('tr', null, [cell]);
    [row.commits, row.pulls, row.merges, row.reviews].forEach((v) =>
      tr.appendChild(el('td', { class: 'num', text: num(v || 0) })));
    tr.appendChild(el('td', { class: 'num' }, [
      el('span', { class: 'diff' }, [
        el('span', { class: 'add', text: '+' + num(row.added || 0) }), ' ',
        el('span', { class: 'del', text: '−' + num(row.removed || 0) }),
      ]),
    ]));
    body.appendChild(tr);
  });
  table.appendChild(body);
  card.appendChild(table);
  return card;
}

/**
 * Pull request size against how much discussion it drew.
 *
 * Three readings of one question, because none of them is sufficient alone:
 * the trend says whether review attention is keeping up, the scatter says
 * which individual changes went through unexamined, and the table is the
 * numbers — a chart that cannot be read off is not evidence anyone can take
 * to a retro.
 *
 * **An unmeasured pull request is not a zero.** Anything synced before the
 * store learned to record PR size has no measurement at all, and drawing that
 * as "no lines, no comments" would invent a stretch of enormous, undiscussed
 * history. The card refuses to plot rather than guess, and says what to run.
 */
function pullSizeCard(bundle, entity) {
  const data = bundle.pulls;
  if (!data || !data.total) return null;

  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', null, ['PR size and discussion', sqlLink('pr-size-totals', entity)]));

  if (!data.measured) {
    card.appendChild(el('div', {
      class: 'warn',
      text: `None of the ${num(data.total)} pull requests in this window have `
          + 'been measured yet. Run ghstats-backfill-pulls --org '
          + ((META && META.org) || '<org>') + ' to size them.',
    }));
    return card;
  }

  const totals = data.totals;
  const box = el('div', { class: 'tiles' });
  [
    [num(totals.pulls), 'PRs measured'],
    [num(Math.round(totals.lines_median)), 'median lines changed'],
    [num(Math.round(totals.discussion_median)), 'median comments'],
    [totals.per_100_lines === null ? '–' : totals.per_100_lines.toFixed(1),
     'comments / 100 lines'],
    [Math.round((totals.undiscussed / totals.pulls) * 100) + '%',
     'drew no comment'],
  ].forEach(([value, label]) => {
    box.appendChild(el('div', { class: 'tile' }, [
      el('div', { class: 'n', text: value }),
      el('div', { class: 'l', text: label }),
    ]));
  });
  card.appendChild(box);

  // Said up front, not in a footnote: every number above is over the measured
  // subset, and a reader who does not know what fraction that is cannot tell a
  // real trend from a half-backfilled one.
  if (data.unmeasured) {
    card.appendChild(el('div', {
      class: 'warn',
      text: `${num(data.unmeasured)} of ${num(data.total)} pull requests are `
          + 'not measured yet and are left out of everything below. Run '
          + 'ghstats-backfill-pulls to include them.',
    }));
  }

  const trendHost = el('div');
  card.appendChild(el('div', { class: 'count-note',
    text: 'PRs opened per ' + data.granularity
        + ', against comments per 100 lines changed' }));
  card.appendChild(trendHost);
  whenPlaced(trendHost, () =>
    pullTrendChart(trendHost, data.buckets, data.granularity));

  const scatterHost = el('div');
  card.appendChild(el('div', { class: 'count-note',
    text: data.truncated
      ? `Each PR, newest ${num(data.points.length)} of ${num(data.measured)}`
      : 'Each PR: size against comments' }));
  card.appendChild(scatterHost);
  whenPlaced(scatterHost, () =>
    pullScatterChart(scatterHost, data.points, (p) => navigate(['day', p.day])));

  card.appendChild(el('div', { class: 'count-note',
    text: 'Comment counts are everything on the PR — conversation, inline '
        + 'review comments, and reviews that carried a message. Conversation '
        + 'and inline counts are GitHub totals with no author breakdown, so '
        + 'review bots contribute to them even when bots are filtered out.' }));

  card.appendChild(pullBucketTable(data));
  return card;
}

/** The trend as numbers. Oldest first, to read left-to-right like the chart. */
function pullBucketTable(data) {
  const table = el('table');
  const head = el('tr', null, [
    el('th', { text: data.granularity === 'week' ? 'week of' : 'month' }),
  ]);
  ['PRs', 'merged', 'median lines', 'median comments', 'comments',
   'per 100 lines', 'no comment'].forEach((h) =>
    head.appendChild(el('th', { class: 'num', text: h })));
  table.appendChild(el('thead', null, [head]));

  const body = el('tbody');
  data.buckets.forEach((b) => {
    body.appendChild(el('tr', null, [
      el('td', { text: b.bucket }),
      el('td', { class: 'num', text: num(b.pulls) }),
      el('td', { class: 'num', text: num(b.merged) }),
      el('td', { class: 'num', text: num(Math.round(b.lines_median)) }),
      el('td', { class: 'num', text: num(Math.round(b.discussion_median)) }),
      el('td', { class: 'num', text: num(b.discussion_total) }),
      el('td', { class: 'num',
                 text: b.per_100_lines === null ? '–' : b.per_100_lines.toFixed(1) }),
      el('td', { class: 'num', text: num(b.undiscussed) }),
    ]));
  });
  table.appendChild(body);
  return table;
}

function issueCard(bundle) {
  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', { text: 'Jira' }));
  const { projects, issues } = bundle.issues;
  if (!projects.length) {
    card.appendChild(el('div', {
      class: 'empty',
      text: META && !META.issues_present
        ? 'No issue references indexed. Run ghstats-reindex.'
        : 'No issue keys referenced in this window.',
    }));
    return card;
  }
  const wrap = el('div', { class: 'grid2' });

  const left = el('div');
  left.appendChild(el('div', { class: 'count-note', text: 'Projects' }));
  const pills = el('div', { class: 'pills' });
  projects.slice(0, 14).forEach((p) => {
    pills.appendChild(el('a', {
      class: 'chip issue', href: '#', text: `${p.project} · ${p.issues}`,
      title: `${p.refs} references across ${p.issues} issues`,
      onclick: (e) => { e.preventDefault(); navigate(['projects', p.project]); },
    }));
  });
  left.appendChild(pills);

  const right = el('div');
  right.appendChild(el('div', { class: 'count-note', text: 'Most-referenced issues' }));
  const list = el('div', { class: 'pills' });
  issues.slice(0, 14).forEach((i) => {
    list.appendChild(el('a', {
      class: 'chip issue', href: '#', text: `${i.issue} · ${i.refs}`,
      onclick: (e) => { e.preventDefault(); navigate(['issues', i.issue]); },
    }));
  });
  right.appendChild(list);

  wrap.appendChild(left);
  wrap.appendChild(right);
  card.appendChild(wrap);
  return card;
}

function aiCard(bundle) {
  const ai = bundle.ai;
  if (!ai.total) return null;
  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', { text: 'AI assistance' }));
  const share = ai.total ? Math.round((ai.assisted / ai.total) * 100) : 0;
  const box = el('div', { class: 'tiles' });
  box.appendChild(el('div', { class: 'tile' }, [
    el('div', { class: 'n', text: share + '%' }),
    el('div', { class: 'l', text: 'of commits assisted' }),
  ]));
  box.appendChild(el('div', { class: 'tile' }, [
    el('div', { class: 'n', text: num(ai.assisted) }),
    el('div', { class: 'l', text: 'assisted commits' }),
  ]));
  ai.by_tool.forEach((tool) => {
    box.appendChild(el('div', { class: 'tile' }, [
      el('div', { class: 'n', text: num(tool.commits) }),
      el('div', { class: 'l', text: tool.tool }),
    ]));
  });
  card.appendChild(box);
  return card;
}

/** The event stream: where every view bottoms out. */
/* -- event stream -------------------------------------------------------- */

const FORMATTERS = new Map();

/**
 * A formatter in the zone the store groups by, not the browser's.
 *
 * The charts bucket events with `local_date` in `--timezone`; a stream that
 * dated the same events in the reader's zone would put a row under a day
 * header the charts never counted it in. One zone for the whole page.
 */
function zoned(kind) {
  const zone = (META && META.timezone) || 'UTC';
  const key = zone + '|' + kind;
  if (FORMATTERS.has(key)) return FORMATTERS.get(key);
  const locale = kind === 'day' ? 'en-CA' : undefined;   // en-CA spells ISO
  const shape = kind === 'day'
    ? { year: 'numeric', month: '2-digit', day: '2-digit' }
    : { hour: '2-digit', minute: '2-digit', hour12: false };
  let format;
  try {
    format = new Intl.DateTimeFormat(locale, Object.assign({ timeZone: zone }, shape));
  } catch (err) {
    format = new Intl.DateTimeFormat(locale, shape);     // an unknown zone, as UTC does
  }
  FORMATTERS.set(key, format);
  return format;
}

function localDay(at) { return zoned('day').format(new Date(at)); }
function localTime(at) { return zoned('time').format(new Date(at)); }

/** Consecutive events split into local days. The stream is already ordered. */
function intoDays(events) {
  const days = [];
  events.forEach((event) => {
    const day = localDay(event.at);
    let bucket = days[days.length - 1];
    if (!bucket || bucket.day !== day) {
      bucket = { day, events: [] };
      days.push(bucket);
    }
    bucket.events.push(event);
  });
  return days;
}

/** One day's events by repository, in repository order. */
function intoRepos(events) {
  const groups = new Map();
  events.forEach((event) => {
    if (!groups.has(event.repo)) groups.set(event.repo, []);
    groups.get(event.repo).push(event);
  });
  return [...groups.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([repo, rows]) => ({ repo, events: rows }));
}

function dayHeader(bucket) {
  const head = el('div', { class: 'day-head' });
  head.appendChild(el('a', {
    class: 'd', href: '#', text: prettyDay(bucket.day, true),
    title: 'Open this day', onclick: (e) => { e.preventDefault(); navigate(['day', bucket.day]); },
  }));
  head.appendChild(el('span', {
    class: 'n',
    text: bucket.events.length === 1 ? '1 event' : `${num(bucket.events.length)} events`,
  }));
  return head;
}

function repoHeader(group) {
  const head = el('div', { class: 'repo-head' });
  head.appendChild(el('a', {
    href: '#', text: group.repo,
    onclick: (e) => { e.preventDefault(); navigate(['repos', group.repo]); },
  }));
  head.appendChild(el('span', { class: 'n', text: num(group.events.length) }));
  return head;
}

/**
 * The stream, banded by day.
 *
 * A flat list of a hundred rows reads as one undifferentiated run, and the
 * only thing separating Tuesday from Wednesday is a timestamp nobody parses at
 * a glance. Every day gets a full-bleed band; on a person's page each day is
 * then split by repository, because "what was she doing on Tuesday" is
 * answered by two repositories and eleven commits, not by eleven commits in
 * timestamp order.
 */
function renderStream(host, events, options) {
  clear(host);
  intoDays(events).forEach((bucket) => {
    host.appendChild(dayHeader(bucket));
    if (!options.byRepo) {
      bucket.events.forEach((event) => host.appendChild(eventRow(event, options)));
      return;
    }
    intoRepos(bucket.events).forEach((group) => {
      const box = el('div', { class: 'repo-group' });
      box.appendChild(repoHeader(group));
      group.events.forEach((event) =>
        box.appendChild(eventRow(event, { byRepo: true })));
      host.appendChild(box);
    });
  });
}

function eventsCard(bundle, baseParts, opts) {
  const options = opts || {};
  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', { text: 'Events' }));
  const page = bundle.events;
  const rows = page.events.slice();

  const note = el('div', { class: 'count-note' });
  const list = el('div', { class: 'stream' });
  const paint = () => {
    note.textContent = page.total
      ? `Showing ${num(page.offset + 1)}–${num(page.offset + rows.length)} of ` +
        `${num(page.total)}, newest first, ` +
        (options.byRepo ? 'by day and repository' : 'by day')
      : 'Nothing matches these filters.';
    renderStream(list, rows, options);
  };
  card.appendChild(note);
  card.appendChild(list);
  paint();

  if (page.offset + page.returned < page.total) {
    const button = el('button', { class: 'more', text: 'Load more' });
    let seen = page.offset + page.returned;
    button.addEventListener('click', async () => {
      button.disabled = true;
      button.textContent = 'Loading…';
      try {
        // `/api/events` rather than the entity endpoint: paging needs 100 more
        // rows, not a recount of every aggregate and chart series behind them.
        const more = await api('events', Object.assign(entityFilter(baseParts), {
          offset: seen,
        }));
        rows.push(...more.events);
        seen += more.returned;
        // Repainting rather than appending: the last day on the page may
        // continue into the next one, and its band and repository groups have
        // to grow rather than start again under a duplicate header.
        paint();
        if (seen >= more.total || !more.returned) { button.remove(); return; }
      } catch (err) {
        button.textContent = 'Failed: ' + (err.message || err);
        return;
      }
      button.disabled = false;
      button.textContent = 'Load more';
    });
    card.appendChild(button);
  }
  return card;
}

/**
 * The entity a detail view is about, as event-stream filter parameters.
 *
 * `['users', 'octocat']` becomes `{user: 'octocat'}`. Days pin both ends of the
 * window, because a day view is a one-day slice of the same stream.
 */
function entityFilter(parts) {
  const [head, name] = parts;
  if (head === 'users') return { user: name };
  if (head === 'repos') return { repo: name };
  if (head === 'teams') return { team: name };
  if (head === 'projects') return { project: name };
  if (head === 'issues') return { issue: name };
  if (head === 'days') return { from: name, to: name };
  return {};
}

function eventRow(event, opts) {
  const options = opts || {};
  const row = el('div', { class: 'ev ' + event.kind });
  row.appendChild(el('div', {
    class: 'badge',
    text: event.kind === 'pull' ? 'PR' : event.kind,
  }));

  const body = el('div', { class: 'body' });
  body.appendChild(el('div', { class: 'subj', text: event.subject }));

  const meta = el('div', { class: 'meta' });
  // The clock only: the date is on the band this row sits under.
  meta.appendChild(el('span', {
    class: 'when', title: new Date(event.at).toLocaleString(), text: localTime(event.at),
  }));
  if (event.actor) {
    meta.appendChild(el('a', {
      href: '#', text: event.actor,
      onclick: (e) => { e.preventDefault(); navigate(['users', event.actor]); },
    }));
  }
  if (!options.byRepo) {
    meta.appendChild(el('a', {
      href: '#', text: event.repo,
      onclick: (e) => { e.preventDefault(); navigate(['repos', event.repo]); },
    }));
  }
  meta.appendChild(el('a', {
    href: event.url, target: '_blank', rel: 'noopener noreferrer', text: event.label,
  }));
  if (event.kind === 'commit' && (event.additions || event.deletions)) {
    meta.appendChild(el('span', { class: 'diff' }, [
      el('span', { class: 'add', text: '+' + num(event.additions) }), ' ',
      el('span', { class: 'del', text: '−' + num(event.deletions) }),
    ]));
  }
  if (event.state && event.kind === 'review') {
    meta.appendChild(el('span', { class: 'chip', text: event.state.toLowerCase().replace('_', ' ') }));
  }
  event.issues.forEach((key) => {
    meta.appendChild(el('a', {
      class: 'chip issue', href: '#', text: key,
      onclick: (e) => { e.preventDefault(); navigate(['issues', key]); },
    }));
  });
  event.tools.forEach((tool) => meta.appendChild(el('span', { class: 'chip ai', text: tool })));

  body.appendChild(meta);
  row.appendChild(body);
  return row;
}

function crumb(title, kindOf, extra) {
  const box = el('div', { class: 'crumb' });
  box.appendChild(el('h1', { text: title }));
  if (kindOf) box.appendChild(el('span', { class: 'kind-of', text: kindOf }));
  [].concat(extra || []).forEach((node) => node && box.appendChild(node));
  return box;
}

/* -- views -------------------------------------------------------------- */

const main = document.getElementById('main');

function show(nodes) {
  destroyCharts();
  clear(main);
  [].concat(nodes).forEach((node) => node && main.appendChild(node));
}

/**
 * The People entry point, which is also the org-wide view.
 *
 * It carries the same three graphs a person's page does, over everyone: the
 * point of a personal rhythm is what it differs from, and comparing it against
 * a shape you have to remember from another page is not comparing.
 */
async function viewUsers() {
  const data = await api('users');
  const note = el('div', { class: 'count-note' });
  note.textContent = `${data.users.length} people active in this window` +
    (currentFilters().bots === '1' ? ', bots included' : ', bots excluded');
  show([
    crumb('People', 'entry point'),
    tiles(data.totals, null, sqlLink('activity-totals', null, 'SQL behind these counts')),
    chartCard(data),
    calendarCard(data),
    rhythmCard(data),
    note,
    rankTable('People', data.users, {
      label: 'person', key: 'login', link: (login) => ['users', login],
    }),
  ]);
}

async function viewUser(login) {
  const data = await api('users/' + encodeURIComponent(login));
  const extras = [];
  data.user.teams.forEach((team) => {
    extras.push(el('a', {
      class: 'chip', href: '#', text: team.slug,
      onclick: (e) => { e.preventDefault(); navigate(['teams', team.slug]); },
    }));
  });
  if (!data.user.member) {
    extras.push(el('span', { class: 'chip', text: 'not a current member' }));
  }

  show([
    crumb(login, 'person', extras),
    tiles(data.totals, ['people'], sqlLink('activity-totals', { user: login }, 'SQL behind these counts')),
    chartCard(data),
    calendarCard(data),
    rhythmCard(data),
    aiCard(data),
    el('div', { class: 'grid2' }, [
      rankTable('Repositories', data.by_repo, { label: 'repository', link: (n) => ['repos', n] }),
      reviewCard(data),
    ]),
    issueCard(data),
    eventsCard(data, ['users', login], { byRepo: true }),
  ]);
}

function reviewCard(data) {
  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', { text: 'Review partners' }));
  const wrap = el('div', { class: 'grid2' });

  const build = (title, rows) => {
    const side = el('div');
    side.appendChild(el('div', { class: 'count-note', text: title }));
    if (!rows.length) {
      side.appendChild(el('div', { class: 'empty', text: 'None.' }));
      return side;
    }
    const table = el('table');
    const body = el('tbody');
    rows.slice(0, 8).forEach((row) => {
      body.appendChild(el('tr', null, [
        el('td', null, [el('a', {
          href: '#', text: row.login,
          onclick: (e) => { e.preventDefault(); navigate(['users', row.login]); },
        })]),
        el('td', { class: 'num', text: num(row.reviews) }),
      ]));
    });
    table.appendChild(body);
    side.appendChild(table);
    return side;
  };

  wrap.appendChild(build('Reviewed their PRs', data.reviewers));
  wrap.appendChild(build('They reviewed', data.reviewed));
  card.appendChild(wrap);
  return card;
}

/**
 * The Repositories entry point. Carries the same PR size card a repository's
 * page does, over every repository, and a row per repository under it: one
 * repository's comments per hundred lines only means something against the
 * ones next to it.
 */
async function viewRepos() {
  const data = await api('repos');
  show([
    crumb('Repositories', 'entry point'),
    el('div', { class: 'count-note', text: `${data.repos.length} with activity in this window` }),
    rankTable('Repositories', data.repos, {
      label: 'repository', key: 'repo', link: (name) => ['repos', name],
    }),
    pullSizeCard(data),
    pullRepoTable(data.pulls),
  ]);
}

/**
 * PR size and discussion, one row per repository.
 *
 * A repository with pull requests and no measurements keeps its row, with
 * dashes rather than zeroes, so an un-backfilled repository reads as unknown
 * instead of as one where nothing is ever discussed.
 */
function pullRepoTable(data) {
  if (!data || !data.by_repo || !data.by_repo.length) return null;
  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', null, ['PR size and discussion by repository',
                                   sqlLink('pr-size-by-repo')]));

  const table = el('table');
  const head = el('tr', null, [el('th', { text: 'repository' })]);
  ['PRs', 'measured', 'merged', 'median lines', 'median comments',
   'per 100 lines', 'no comment'].forEach((h) =>
    head.appendChild(el('th', { class: 'num', text: h })));
  table.appendChild(el('thead', null, [head]));

  const body = el('tbody');
  data.by_repo.forEach((r) => {
    const measured = r.pulls > 0;
    const dash = (value) => (measured ? value : '–');
    body.appendChild(el('tr', null, [
      el('td', null, [el('a', {
        href: '#', text: r.repo,
        onclick: (e) => { e.preventDefault(); navigate(['repos', r.repo]); },
      })]),
      el('td', { class: 'num', text: num(r.total) }),
      el('td', { class: 'num', text: num(r.pulls) }),
      el('td', { class: 'num', text: dash(num(r.merged)) }),
      el('td', { class: 'num', text: dash(num(Math.round(r.lines_median))) }),
      el('td', { class: 'num', text: dash(num(Math.round(r.discussion_median))) }),
      el('td', { class: 'num',
                 text: r.per_100_lines === null ? '–' : r.per_100_lines.toFixed(1) }),
      el('td', { class: 'num',
                 text: dash(Math.round((r.undiscussed / r.pulls) * 100) + '%') }),
    ]));
  });
  table.appendChild(body);
  card.appendChild(table);
  return card;
}

async function viewRepo(name) {
  const data = await api('repos/' + encodeURIComponent(name));
  const extras = data.repo.teams.slice(0, 6).map((team) => el('a', {
    class: 'chip', href: '#', text: `${team.slug} · ${(team.permission || '').toLowerCase()}`,
    onclick: (e) => { e.preventDefault(); navigate(['teams', team.slug]); },
  }));

  const blocks = [crumb(name, 'repository', extras)];
  if (data.repo.unsyncable) {
    blocks.push(el('div', { class: 'warn', text: 'Never synced: ' + data.repo.unsyncable }));
  }
  const coverage = data.repo.coverage;
  if (coverage.length) {
    blocks.push(el('div', {
      class: 'count-note',
      text: 'Coverage ' + coverage.map((c) =>
        `${c.kind} ${c.covered_from.slice(0, 10)}→${c.covered_to.slice(0, 10)}`).join('   '),
    }));
  }

  blocks.push(
    tiles(data.totals, ['repos'], sqlLink('activity-totals', { repo: name }, 'SQL behind these counts')),
    chartCard(data),
    calendarCard(data),
    rhythmCard(data),
    aiCard(data),
    pullSizeCard(data, { repo: name }),
    el('div', { class: 'grid2' }, [
      rankTable('Contributors', data.by_actor, { label: 'person', link: (n) => ['users', n] }),
      issueCard(data),
    ]),
    eventsCard(data, ['repos', name]),
  );
  show(blocks);
}

async function viewTeams() {
  const data = await api('teams');
  const blocks = [crumb('Teams', 'entry point')];
  if (!data.teams.length) {
    blocks.push(el('div', {
      class: 'warn',
      text: 'No teams in the store. Run ghstats-sync (without --skip-teams) to fetch them.',
    }));
    show(blocks);
    return;
  }
  blocks.push(el('div', {
    class: 'count-note',
    text: `${data.teams.length} teams · ${data.unassigned} members on no team`,
  }));

  const card = el('div', { class: 'card' });
  const table = el('table');
  const head = el('tr', null, [el('th', { text: 'team' }), el('th', { text: 'parent' })]);
  ['members', 'repos'].forEach((h) => head.appendChild(el('th', { class: 'num', text: h })));
  table.appendChild(el('thead', null, [head]));
  const body = el('tbody');
  data.teams.forEach((team) => {
    body.appendChild(el('tr', null, [
      el('td', null, [el('a', {
        href: '#', text: team.name || team.slug,
        onclick: (e) => { e.preventDefault(); navigate(['teams', team.slug]); },
      })]),
      el('td', { class: 'num', text: team.parent_slug || '' }),
      el('td', { class: 'num', text: num(team.members) }),
      el('td', { class: 'num', text: num(team.repos) }),
    ]));
  });
  table.appendChild(body);
  card.appendChild(table);
  blocks.push(card);
  show(blocks);
}

async function viewTeam(slug) {
  const data = await api('teams/' + encodeURIComponent(slug));
  const team = data.team;

  const roster = el('div', { class: 'card' });
  roster.appendChild(el('h2', { text: `Members (${team.roster.length})` }));
  const table = el('table');
  const head = el('tr', null, [el('th', { text: 'person' })]);
  ['commits', 'PRs', 'merged', 'reviews'].forEach((h) =>
    head.appendChild(el('th', { class: 'num', text: h })));
  table.appendChild(el('thead', null, [head]));
  const body = el('tbody');
  team.roster.forEach((person) => {
    const cell = el('td', null, [el('a', {
      href: '#', text: person.login,
      onclick: (e) => { e.preventDefault(); navigate(['users', person.login]); },
    })]);
    if (!person.active) cell.appendChild(el('span', { class: 'chip', text: 'left' }));
    body.appendChild(el('tr', null, [
      cell,
      el('td', { class: 'num', text: num(person.commits) }),
      el('td', { class: 'num', text: num(person.pulls) }),
      el('td', { class: 'num', text: num(person.merges) }),
      el('td', { class: 'num', text: num(person.reviews) }),
    ]));
  });
  table.appendChild(body);
  roster.appendChild(table);

  const untracked = team.repos.filter((r) => !r.tracked).length;
  show([
    crumb(team.name || slug, 'team',
          team.parent_slug ? [el('span', { class: 'chip', text: 'in ' + team.parent_slug })] : null),
    el('div', {
      class: 'warn',
      text: 'Team membership is current-only — GitHub reports no history. ' +
            'A window that predates someone moving team credits their old work here.',
    }),
    team.description ? el('div', { class: 'count-note', text: team.description }) : null,
    tiles(data.totals),
    chartCard(data),
    calendarCard(data),
    rhythmCard(data),
    roster,
    aiCard(data),
    el('div', { class: 'grid2' }, [
      rankTable('Repositories worked in', data.by_repo, {
        label: 'repository', link: (n) => ['repos', n],
      }),
      issueCard(data),
    ]),
    el('div', {
      class: 'count-note',
      text: `${team.repos.length} repositories granted to this team` +
            (untracked ? ` (${untracked} not covered by the sweep, typically archived)` : ''),
    }),
    eventsCard(data, ['teams', slug]),
  ]);
}

async function viewProjects() {
  const data = await api('projects');
  const blocks = [crumb('Jira projects', 'entry point')];
  if (!data.projects.length) {
    blocks.push(el('div', {
      class: 'warn',
      text: META && !META.issues_present
        ? 'No issue references indexed yet. Run ghstats-reindex.'
        : 'No issue keys referenced in this window.',
    }));
    show(blocks);
    return;
  }
  const card = el('div', { class: 'card' });
  const table = el('table');
  table.appendChild(el('thead', null, [el('tr', null, [
    el('th', { text: 'project' }),
    el('th', { class: 'num', text: 'issues' }),
    el('th', { class: 'num', text: 'references' }),
  ])]));
  const body = el('tbody');
  data.projects.forEach((project) => {
    body.appendChild(el('tr', null, [
      el('td', null, [el('a', {
        href: '#', text: project.project,
        onclick: (e) => { e.preventDefault(); navigate(['projects', project.project]); },
      })]),
      el('td', { class: 'num', text: num(project.issues) }),
      el('td', { class: 'num', text: num(project.refs) }),
    ]));
  });
  table.appendChild(body);
  card.appendChild(table);
  blocks.push(card);
  show(blocks);
}

async function viewProject(key) {
  const data = await api('projects/' + encodeURIComponent(key));
  const extras = data.project.aliases.length
    ? [el('span', {
        class: 'chip',
        title: 'Misspellings folded onto this project',
        text: 'also written ' + data.project.aliases.join(', '),
      })]
    : [];
  show([
    crumb(data.project.key, 'jira project', extras),
    tiles(data.totals),
    chartCard(data),
    issueCard(data),
    el('div', { class: 'grid2' }, [
      rankTable('People', data.by_actor, { label: 'person', link: (n) => ['users', n] }),
      rankTable('Repositories', data.by_repo, { label: 'repository', link: (n) => ['repos', n] }),
    ]),
    eventsCard(data, ['projects', key]),
  ]);
}

async function viewIssue(key) {
  const data = await api('issues/' + encodeURIComponent(key));
  const extras = data.issue.project
    ? [el('a', {
        class: 'chip', href: '#', text: data.issue.project,
        onclick: (e) => { e.preventDefault(); navigate(['projects', data.issue.project]); },
      })]
    : [];
  const blocks = [crumb(data.issue.key, 'issue', extras)];
  if (!data.events.total) {
    blocks.push(el('div', {
      class: 'warn',
      text: 'Nothing references this key in the current window. Widen the date range.',
    }));
  }
  blocks.push(
    tiles(data.totals),
    chartCard(data),
    el('div', { class: 'grid2' }, [
      rankTable('People', data.by_actor, { label: 'person', link: (n) => ['users', n] }),
      rankTable('Repositories', data.by_repo, { label: 'repository', link: (n) => ['repos', n] }),
    ]),
    eventsCard(data, ['issues', key]),
  );
  show(blocks);
}

/**
 * The Day entry point.
 *
 * A date field on its own was a dead end. It opened on today, which a store a
 * week behind its organization has little or nothing for, and a date input
 * fires no `change` when the day picked is the day already showing -- so the
 * obvious move, opening the picker and clicking today, did nothing at all and
 * the section read as broken. So: a button that opens whatever the field
 * holds, a field that starts on the newest day the window actually has
 * activity for, and the window's days on the page as a chart and a calendar,
 * either of which opens a day when clicked.
 */
async function viewDayPicker() {
  const data = await api('users');
  const withData = (data.by_day || []).filter((d) => d.total > 0);
  // The newest day the window can answer for beats today, which a store that
  // is behind would open on and show nothing for.
  const start = withData.length ? withData[withData.length - 1].day
                                : isoDay(new Date());

  const card = el('div', { class: 'card' });
  card.appendChild(el('h2', { text: 'Pick a day' }));
  const input = el('input', { type: 'date', value: start, 'aria-label': 'Day to open' });
  const open = () => { if (input.value) navigate(['day', input.value]); };
  input.addEventListener('change', open);
  card.appendChild(el('div', { class: 'daypick' }, [
    input,
    el('button', { text: 'Open', onclick: open }),
  ]));
  card.appendChild(el('div', {
    class: 'count-note',
    text: 'Or click any day in the chart or the calendar below. '
      + 'A day is a slice of the whole organization, whatever the range above says.',
  }));

  show([
    crumb('Day', 'entry point'),
    card,
    chartCard(data),
    calendarCard(data),
  ]);
}

async function viewDay(day) {
  const data = await api('days/' + encodeURIComponent(day));
  const teamCard = el('div', { class: 'card' });
  teamCard.appendChild(el('h2', { text: 'By team' }));
  const table = el('table');
  const head = el('tr', null, [el('th', { text: 'team' })]);
  ['people', 'commits', 'PRs', 'merged', 'reviews'].forEach((h) =>
    head.appendChild(el('th', { class: 'num', text: h })));
  table.appendChild(el('thead', null, [head]));
  const body = el('tbody');
  data.day.by_team.forEach((row) => {
    const cell = el('td');
    if (row.team === '(no team)') cell.textContent = row.team;
    else {
      cell.appendChild(el('a', {
        href: '#', text: row.team,
        onclick: (e) => { e.preventDefault(); navigate(['teams', row.team]); },
      }));
    }
    body.appendChild(el('tr', null, [
      cell,
      el('td', { class: 'num', text: num(row.people) }),
      el('td', { class: 'num', text: num(row.commits) }),
      el('td', { class: 'num', text: num(row.pulls) }),
      el('td', { class: 'num', text: num(row.merges) }),
      el('td', { class: 'num', text: num(row.reviews) }),
    ]));
  });
  table.appendChild(body);
  teamCard.appendChild(table);
  teamCard.appendChild(el('div', {
    class: 'count-note',
    text: 'Someone on two teams counts under both, so these do not sum to the total.',
  }));

  const shift = (delta) => {
    const d = new Date(day + 'T00:00:00Z');
    d.setUTCDate(d.getUTCDate() + delta);
    return isoDay(d);
  };
  const nav = [
    el('a', { class: 'chip', href: '#', text: '← ' + shift(-1),
              onclick: (e) => { e.preventDefault(); navigate(['day', shift(-1)]); } }),
    el('a', { class: 'chip', href: '#', text: shift(1) + ' →',
              onclick: (e) => { e.preventDefault(); navigate(['day', shift(1)]); } }),
  ];

  show([
    crumb(day, 'day', nav),
    tiles(data.totals, null, sqlLink('activity-totals', { from: day, to: day }, 'SQL behind these counts')),
    teamCard,
    aiCard(data),
    el('div', { class: 'grid2' }, [
      rankTable('Repositories', data.by_repo, { label: 'repository', link: (n) => ['repos', n] }),
      rankTable('People', data.by_actor, { label: 'person', link: (n) => ['users', n] }),
    ]),
    issueCard(data),
    eventsCard(data, ['days', day]),
  ]);
}

/* -- search ------------------------------------------------------------- */

function wireSearch() {
  const input = document.getElementById('search');
  const panel = document.getElementById('suggest');
  let timer = null;

  const close = () => {
    panel.hidden = true;
    input.setAttribute('aria-expanded', 'false');
  };

  const render = (results) => {
    clear(panel);
    const groups = [
      ['People', results.users, (name) => ['users', name], (v) => v],
      ['Repositories', results.repos, (name) => ['repos', name], (v) => v],
      ['Teams', results.teams, (t) => ['teams', t.slug], (t) => t.name || t.slug],
      ['Jira projects', results.projects, (name) => ['projects', name], (v) => v],
      ['Issues', results.issues, (name) => ['issues', name], (v) => v],
    ];
    let any = false;
    groups.forEach(([title, items, target, label]) => {
      if (!items || !items.length) return;
      any = true;
      panel.appendChild(el('div', { class: 'grp', text: title }));
      items.forEach((item) => {
        panel.appendChild(el('button', {
          type: 'button', text: label(item),
          onclick: () => { close(); input.value = ''; navigate(target(item)); },
        }));
      });
    });
    if (!any) panel.appendChild(el('div', { class: 'grp', text: 'Nothing found' }));
    panel.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  };

  input.addEventListener('input', () => {
    clearTimeout(timer);
    const term = input.value.trim();
    if (term.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      try {
        render(await api('search', { q: term, limit: 8 }));
      } catch (err) { close(); }
    }, 180);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { close(); input.blur(); }
    if (e.key === 'Enter') {
      const first = panel.querySelector('button');
      if (first) first.click();
    }
  });

  document.addEventListener('click', (e) => {
    if (!panel.contains(e.target) && e.target !== input) close();
  });

  // `/` focuses search, the shortcut every developer tool has.
  document.addEventListener('keydown', (e) => {
    // TEXTAREA too: CodeMirror types into one, and `/` is a SQL operator.
    if (e.key === '/' && !['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
      e.preventDefault();
      input.focus();
    }
  });
}

/* -- chrome ------------------------------------------------------------- */

function renderFreshness() {
  const box = document.getElementById('freshness');
  clear(box);
  box.className = 'freshness';
  if (!META) return;

  const hours = META.stale_hours;
  let level = 'fresh-ok';
  let text = 'up to date';
  if (hours === null || hours === undefined) {
    level = 'fresh-stale';
    text = 'coverage unknown';
  } else if (hours > 72) {
    level = 'fresh-stale';
    text = `${Math.round(hours / 24)}d behind`;
  } else if (hours > 26) {
    level = 'fresh-warn';
    text = `${Math.round(hours)}h behind`;
  } else {
    text = `${Math.round(hours)}h behind`;
  }
  box.classList.add(level);
  box.appendChild(el('span', { class: 'dot' }));
  box.appendChild(el('span', { class: 'txt', text }));
  box.title = `Store covers ${(META.covered_from || '?').slice(0, 10)} → ` +
    `${(META.covered_to || '?').slice(0, 10)} · ${META.timezone}`;
}

function renderSideNote() {
  const note = document.getElementById('sidenote');
  clear(note);
  if (!META) return;
  const c = META.counts;
  [`${num(c.commits)} commits`, `${num(c.pulls)} PRs`, `${num(c.reviews)} reviews`,
   `${num(c.repos)} repos`, `${num(c.members)} members`,
   `${num(c.teams)} teams`, `${num(c.issues)} issues`].forEach((line) => {
    note.appendChild(el('div', { text: line }));
  });
}

function markTab() {
  const { parts } = parseHash();
  const head = parts[0] === 'issues' ? 'projects' : parts[0];
  document.querySelectorAll('.side a').forEach((link) => {
    link.classList.toggle('on', link.dataset.tab === head);
    // Preserve the active filters when switching entry point.
    const query = new URLSearchParams();
    for (const [k, v] of Object.entries(currentFilters())) {
      if (!['user', 'repo', 'team', 'project', 'issue', 'offset'].includes(k)) {
        query.set(k, v);
      }
    }
    const q = query.toString();
    link.setAttribute('href', `#/${link.dataset.tab}${q ? '?' + q : ''}`);
  });
}

/* -- dispatch ----------------------------------------------------------- */

const ROUTES = [
  [['users'], viewUsers],
  [['users', '*'], viewUser],
  [['repos'], viewRepos],
  [['repos', '*'], viewRepo],
  [['teams'], viewTeams],
  [['teams', '*'], viewTeam],
  [['projects'], viewProjects],
  [['projects', '*'], viewProject],
  [['issues', '*'], viewIssue],
  [['day'], viewDayPicker],
  [['day', '*'], viewDay],
  [['sql'], viewSql],
];

function resolve(parts) {
  for (const [pattern, view] of ROUTES) {
    if (pattern.length !== parts.length) continue;
    const ok = pattern.every((seg, i) => seg === '*' || seg === parts[i]);
    if (ok) return [view, parts.filter((_, i) => pattern[i] === '*')];
  }
  return [null, []];
}

async function render() {
  const mine = ++GENERATION;
  const { parts } = parseHash();
  syncFilterUI();
  markTab();

  const [view, args] = resolve(parts);
  if (!view) { show(crumb('Not found', String(parts.join('/')))); return; }

  main.classList.add('reloading');
  try {
    await view(...args);
  } catch (err) {
    if (err === STALE) return;
    show([
      crumb('Error', null),
      el('div', { class: 'warn', text: String(err.message || err) }),
    ]);
  } finally {
    // A render that has been overtaken leaves the newest one loading.
    if (mine === GENERATION) main.classList.remove('reloading');
  }
}

// No resize handler: Chart.js observes its own container, and the calendar is
// fixed-pitch and scrolls. A window drag used to mean refetching the view.

// Colour is baked into a chart when it is built, so a system theme flip needs
// a rebuild -- the same refetch a navigation does, and rare enough to cost
// nothing.
const DARK = window.matchMedia('(prefers-color-scheme: dark)');
if (DARK.addEventListener) DARK.addEventListener('change', () => render());

window.addEventListener('hashchange', render);

(async function start() {
  wireFilters();
  wireSearch();
  try {
    META = await api('meta');
    document.getElementById('org').textContent = META.org;
    document.title = `${META.org} · ghstats explorer`;
    renderFreshness();
    renderSideNote();
  } catch (err) {
    show([crumb('Cannot reach the store', null),
          el('div', { class: 'warn', text: String(err.message || err) })]);
    return;
  }
  if (!location.hash) applyPreset(30);
  else render();
})();
