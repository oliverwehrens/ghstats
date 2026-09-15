/* ghstats explorer — the SQL page.
 *
 * A read-only console over the store, for checking what a card's number is
 * made of. Loaded before `explorer.js`, and leans on its helpers (`el`, `go`,
 * `apiPost`, `show`) at call time.
 *
 * **The hash is still the state.** The query text travels as `sql=` next to
 * the filters, so a query is bookmarkable and Back returns to the previous one.
 * Typing does not write the hash -- Run does -- because every hash change
 * re-renders, and re-rendering on each keystroke would run half-typed SQL.
 *
 * **The editor survives re-renders.** A filter change re-runs the query, and
 * rebuilding CodeMirror for that would drop the cursor and the undo history.
 * The mounted page is kept in `SQL_PAGE` and reused while it is still on
 * screen; navigating elsewhere clears `main`, and the next visit builds afresh.
 */
'use strict';

const SQL_STARTER = [
  '-- Measured pull requests opened in the filter window, largest first.',
  '-- :from and :to follow the date filter as UTC; :bots is 1 when bots are shown.',
  'SELECT r.name AS repo, p.number, p.author_login,',
  '       m.additions + m.deletions AS lines,',
  '       m.comments + m.review_comments AS comments',
  'FROM pulls p',
  'JOIN repos r ON r.id = p.repo_id',
  'JOIN pull_metrics m USING (repo_id, number)',
  'WHERE (:from IS NULL OR p.created_at >= :from)',
  '  AND (:to IS NULL OR p.created_at < :to)',
  '  AND (:repo IS NULL OR r.name = :repo)',
  '  AND (:bots OR p.author_login IS NULL',
  '       OR p.author_login NOT IN (SELECT login FROM bot_logins))',
  'ORDER BY lines DESC',
  'LIMIT 50',
].join('\n');

// Result columns whose values name something the explorer has a page for.
const SQL_LINKS = {
  repo: 'repos', repo_name: 'repos', login: 'users', author_login: 'users',
};

// Parameters the strip shows, and the two it lets you set: repository and
// person have no control in the filter bar, because everywhere else they are
// the page you are on.
const SQL_PARAMS = ['from', 'to', 'tz', 'bots', 'repo', 'user'];
const SQL_EDITABLE = ['repo', 'user'];

let SQL_PAGE = null;

async function viewSql() {
  const { params } = parseHash();
  const text = params.get('sql');

  if (!SQL_PAGE || !main.contains(SQL_PAGE.root)) SQL_PAGE = buildSqlPage(text);
  const page = SQL_PAGE;
  if (text !== null && page.editor.getValue() !== text) page.editor.setValue(text);
  syncSqlInputs(page);

  if (text === null) {
    renderSqlParams(page, null);
    clear(page.results);
    page.status.textContent = 'Press Run or Ctrl+Enter to run the query.';
    return;
  }

  page.status.textContent = 'Running…';
  const f = currentFilters();
  let result;
  try {
    result = await apiPost('sql', { sql: text }, { repo: f.repo, user: f.user });
  } catch (err) {
    if (err === STALE) throw err;
    renderSqlParams(page, null);
    clear(page.results);
    page.status.textContent = '';
    page.results.appendChild(el('div', { class: 'warn sql-error', text: String(err.message || err) }));
    return;
  }
  renderSqlResult(page, result);
}

function buildSqlPage(text) {
  const root = el('div', { class: 'sql-page' });
  const editorHost = el('div', { class: 'sql-editor' });
  const status = el('span', { class: 'sql-status' });
  const run = el('button', { class: 'sql-run', text: 'Run', title: 'Ctrl+Enter' });
  const strip = el('div', { class: 'sql-params' });
  const results = el('div', { class: 'sql-results' });

  const editorCard = el('div', { class: 'card' }, [
    editorHost,
    el('div', { class: 'sql-bar' }, [run, status]),
    strip,
  ]);
  root.appendChild(editorCard);
  root.appendChild(results);
  show([crumb('SQL', 'read-only'), root]);

  const page = { root, status, strip, results, inputs: {}, editor: null };
  const execute = () => runSql(page);
  page.editor = CodeMirror(editorHost, {
    value: text === null ? SQL_STARTER : text,
    mode: 'text/x-sqlite',
    lineNumbers: true,
    matchBrackets: true,
    indentUnit: 2,
    tabSize: 2,
    viewportMargin: Infinity,
    extraKeys: {
      'Ctrl-Enter': execute,
      'Cmd-Enter': execute,
      'Ctrl-Space': 'autocomplete',
    },
    hintOptions: { completeSingle: false },
  });
  run.addEventListener('click', execute);

  SQL_EDITABLE.forEach((name) => {
    const input = el('input', {
      type: 'search', spellcheck: 'false', autocomplete: 'off',
      placeholder: 'any', 'aria-label': `:${name}`,
    });
    input.addEventListener('change', () => go(['sql'], { [name]: input.value.trim() || null }));
    page.inputs[name] = input;
  });
  renderSqlParams(page, null);
  // CodeMirror measured itself before the card had a width.
  requestAnimationFrame(() => page.editor.refresh());
  return page;
}

/** Write the editor's text into the hash, or re-run if it is already there. */
function runSql(page) {
  const text = page.editor.getValue();
  const before = location.hash;
  go(['sql'], { sql: text.trim() ? text : null });
  // An unchanged hash fires no hashchange, and Run on the same text should
  // still run -- the store may have been synced since.
  if (location.hash === before) render();
}

function syncSqlInputs(page) {
  const f = currentFilters();
  SQL_EDITABLE.forEach((name) => {
    const input = page.inputs[name];
    if (document.activeElement !== input) input.value = f[name] || '';
  });
}

/**
 * The values each parameter was bound to, as reported by the server.
 *
 * Shown from the response rather than from the filter bar, so the strip says
 * what the rows on screen were computed with even while a new run is pending.
 */
function renderSqlParams(page, bound) {
  clear(page.strip);
  SQL_PARAMS.forEach((name) => {
    const item = el('span', { class: 'sql-param' }, [el('code', { text: ':' + name })]);
    if (SQL_EDITABLE.includes(name)) {
      item.appendChild(page.inputs[name]);
    } else {
      const value = bound ? bound[name] : undefined;
      item.appendChild(value === null || value === undefined
        ? el('span', { class: 'null', text: bound ? 'NULL' : '–' })
        : el('span', { class: 'v', text: String(value) }));
    }
    page.strip.appendChild(item);
  });
}

function renderSqlResult(page, result) {
  renderSqlParams(page, result.parameters);
  clear(page.results);
  page.status.textContent =
    `${num(result.row_count)} row${result.row_count === 1 ? '' : 's'} · ${num(result.elapsed_ms)} ms`;

  const card = el('div', { class: 'card' });
  if (result.truncated) {
    card.appendChild(el('div', {
      class: 'warn',
      text: `Showing the first ${num(result.row_count)} rows. Add a LIMIT or narrow the window to see the rest.`,
    }));
  }
  if (!result.columns.length) {
    card.appendChild(el('div', { class: 'empty', text: 'The statement returned no columns.' }));
    page.results.appendChild(card);
    return;
  }

  const copy = el('button', { class: 'more sql-copy', text: 'Copy as TSV' });
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(sqlTsv(result));
      copy.textContent = 'Copied';
    } catch (err) {
      copy.textContent = 'Copy failed';
    }
    setTimeout(() => { copy.textContent = 'Copy as TSV'; }, 1500);
  });
  card.appendChild(el('div', { class: 'sql-tools' }, [copy]));
  card.appendChild(sqlTable(result));
  page.results.appendChild(card);
}

function sqlTable(result) {
  const numeric = result.columns.map((_, i) => result.rows.some((row) => typeof row[i] === 'number')
    && result.rows.every((row) => row[i] === null || typeof row[i] === 'number'));

  const head = el('tr');
  result.columns.forEach((name, i) =>
    head.appendChild(el('th', { class: numeric[i] ? 'num' : null, text: name })));

  const body = el('tbody');
  result.rows.forEach((row) => {
    const tr = el('tr');
    row.forEach((value, i) => tr.appendChild(sqlCell(result.columns[i], value, numeric[i])));
    body.appendChild(tr);
  });
  if (!result.rows.length) {
    body.appendChild(el('tr', null, [el('td', {
      class: 'empty', colspan: result.columns.length, text: 'No rows.',
    })]));
  }
  return el('div', { class: 'sql-table' }, [el('table', null, [el('thead', null, [head]), body])]);
}

function sqlCell(column, value, numeric) {
  const td = el('td', { class: numeric ? 'num' : null });
  if (value === null) {
    td.appendChild(el('span', { class: 'null', text: 'NULL' }));
    return td;
  }
  const text = String(value);
  const target = SQL_LINKS[column];
  if (target && text) {
    td.appendChild(el('a', {
      href: '#', text,
      onclick: (e) => { e.preventDefault(); navigate([target, text], { sql: null }); },
    }));
  } else {
    td.textContent = text;
  }
  // Long text is clipped to keep rows one line high; the whole value is a
  // hover away.
  if (text.length > 60) td.title = text;
  return td;
}

/** Rows as tab-separated text. Tabs and newlines inside a value become spaces,
 *  since a spreadsheet paste has no quoting to protect them. */
function sqlTsv(result) {
  const clean = (v) => (v === null ? '' : String(v).replace(/[\t\r\n]+/g, ' '));
  return [result.columns.map(clean).join('\t')]
    .concat(result.rows.map((row) => row.map(clean).join('\t')))
    .join('\n') + '\n';
}
