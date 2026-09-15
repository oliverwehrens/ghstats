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

// The schema changes only when the store is migrated, which means a restart:
// fetch it once per page load. Outside `api` on purpose, so a navigation that
// supersedes a render does not discard it.
let SQL_SCHEMA = null;

function sqlSchema() {
  if (!SQL_SCHEMA) {
    SQL_SCHEMA = fetch('/api/sql/schema').then((response) => {
      if (!response.ok) throw new Error(response.statusText);
      return response.json();
    });
    SQL_SCHEMA.catch(() => { SQL_SCHEMA = null; });     // retry on the next visit
  }
  return SQL_SCHEMA;
}

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

  const side = el('aside', { class: 'sql-side' }, [el('div', { class: 'empty', text: 'Loading schema…' })]);

  const editorCard = el('div', { class: 'card' }, [
    editorHost,
    el('div', { class: 'sql-bar' }, [run, status]),
    strip,
  ]);
  root.appendChild(side);
  root.appendChild(el('div', { class: 'sql-work' }, [editorCard, results]));
  show([crumb('SQL', 'read-only'), root]);

  const page = { root, side, status, strip, results, inputs: {}, editor: null };
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
    hintOptions: { completeSingle: false, hint: sqlHint },
  });
  run.addEventListener('click', execute);
  // Complete as you type an identifier, not only on Ctrl+Space.
  page.editor.on('inputRead', (editor, change) => {
    if (editor.state.completionActive || change.origin !== '+input') return;
    if (!/^[\w.:]$/.test(change.text.join(''))) return;
    const type = editor.getTokenTypeAt(editor.getCursor()) || '';
    if (/comment|string/.test(type)) return;
    editor.showHint();
  });

  sqlSchema().then((schema) => {
    if (SQL_PAGE !== page) return;
    const tables = {};
    schema.tables.forEach((t) => { tables[t.name] = t.columns.map((c) => c.name); });
    page.schema = schema;
    page.editor.setOption('hintOptions', { completeSingle: false, hint: sqlHint, tables });
    renderSqlSide(page, schema);
  }, (err) => {
    clear(side);
    side.appendChild(el('div', { class: 'warn', text: 'Schema unavailable: ' + (err.message || err) }));
  });

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

/**
 * Keywords, tables and columns from CodeMirror's SQL hint, plus what it cannot
 * know: the store's functions, and `:parameters` once a colon is typed.
 */
function sqlHint(editor, options) {
  const cursor = editor.getCursor();
  const before = editor.getLine(cursor.line).slice(0, cursor.ch);
  const word = (before.match(/:?\w*$/) || [''])[0];
  const schema = SQL_PAGE && SQL_PAGE.schema;

  if (word.startsWith(':')) {
    const typed = word.slice(1).toLowerCase();
    return {
      list: SQL_PARAMS.filter((name) => name.startsWith(typed)).map((name) => ':' + name),
      from: CodeMirror.Pos(cursor.line, cursor.ch - word.length),
      to: cursor,
    };
  }

  const base = CodeMirror.hint.sql(editor, options)
    || { list: [], from: CodeMirror.Pos(cursor.line, cursor.ch - word.length), to: cursor };
  if (schema && word && !before.endsWith('.' + word)) {
    const typed = word.toLowerCase();
    const seen = new Set(base.list.map((item) => (typeof item === 'string' ? item : item.text)));
    schema.functions.forEach((f) => {
      if (f.name.startsWith(typed) && !seen.has(f.name)) base.list.unshift(f.name + '(');
    });
  }
  return base;
}

function sqlInsert(page, text) {
  page.editor.replaceSelection(text);
  page.editor.focus();
}

/**
 * The schema panel: every table and view, what a row is, and what each column
 * counts. Clicking a column writes its name at the cursor; a table offers its
 * name, or a first look at its rows.
 */
function renderSqlSide(page, schema) {
  const side = page.side;
  clear(side);

  const find = el('input', {
    type: 'search', class: 'sql-find', placeholder: 'Find a table or column',
    spellcheck: 'false', autocomplete: 'off', 'aria-label': 'Find a table or column',
  });
  side.appendChild(find);

  const tableBlocks = schema.tables.map((table) => {
    const details = el('details', { class: 'sql-tbl' });
    details.appendChild(el('summary', null, [
      el('span', { class: 'n', text: table.name }),
      table.kind === 'view' ? el('span', { class: 'chip', text: 'view' }) : null,
    ]));
    details.appendChild(el('div', { class: 'about', text: table.about }));
    details.appendChild(el('div', { class: 'acts' }, [
      el('button', { type: 'button', text: 'insert name', onclick: () => sqlInsert(page, table.name) }),
      el('button', {
        type: 'button', text: 'preview rows',
        onclick: () => { page.editor.setValue(`SELECT *\nFROM ${table.name}\nLIMIT 100`); runSql(page); },
      }),
    ]));
    const list = el('ul', { class: 'cols' });
    const items = table.columns.map((column) => {
      const item = el('li', null, [
        el('div', { class: 'head' }, [
          el('button', {
            type: 'button', class: 'col', text: column.name,
            title: 'Insert at the cursor', onclick: () => sqlInsert(page, column.name),
          }),
          el('span', { class: 'type', text: (column.type || '').toLowerCase() + (column.pk ? ' · key' : '') }),
        ]),
        column.about ? el('div', { class: 'about', text: column.about }) : null,
      ]);
      list.appendChild(item);
      return { item, name: column.name.toLowerCase() };
    });
    details.appendChild(list);
    side.appendChild(details);
    return { details, name: table.name.toLowerCase(), items };
  });

  const functions = el('details', { class: 'sql-tbl' }, [el('summary', null, [el('span', { class: 'n', text: 'Functions' })])]);
  schema.functions.forEach((f) => functions.appendChild(el('div', { class: 'ref' }, [
    el('button', { type: 'button', class: 'col', text: f.signature, onclick: () => sqlInsert(page, f.name + '(') }),
    el('div', { class: 'about', text: f.about }),
  ])));
  const params = el('details', { class: 'sql-tbl' }, [el('summary', null, [el('span', { class: 'n', text: 'Parameters' })])]);
  schema.parameters.forEach((p) => params.appendChild(el('div', { class: 'ref' }, [
    el('button', { type: 'button', class: 'col', text: ':' + p.name, onclick: () => sqlInsert(page, ':' + p.name) }),
    el('div', { class: 'about', text: p.about }),
  ])));
  side.appendChild(functions);
  side.appendChild(params);

  find.addEventListener('input', () => {
    const needle = find.value.trim().toLowerCase();
    tableBlocks.forEach(({ details, name, items }) => {
      if (!needle) {
        details.hidden = false;
        details.open = false;
        items.forEach(({ item }) => { item.hidden = false; });
        return;
      }
      const tableHit = name.includes(needle);
      let columnHit = false;
      items.forEach(({ item, name: column }) => {
        const hit = column.includes(needle);
        columnHit = columnHit || hit;
        item.hidden = !tableHit && !hit;
      });
      details.hidden = !tableHit && !columnHit;
      details.open = columnHit;
    });
  });
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
