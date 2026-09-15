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
 * **A recipe is a query with a name.** `recipe=<id>` loads a hand-written query
 * that reproduces a card (see `recipes.py`); the cards link here with it. Once
 * edited, the text travels as `sql=` beside the recipe id, so the page can say
 * what it was edited from and offer the way back.
 *
 * **Saved queries and history stay in this browser.** `localStorage`, per
 * origin -- so http://localhost and http://127.0.0.1 keep separate lists. Every
 * access is guarded: storage can be disabled, full, or throw in a private
 * window, and the page must work without it.
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
const SQL_PARAMS = ['org', 'from', 'to', 'tz', 'bots', 'repo', 'user'];
const SQL_EDITABLE = ['repo', 'user'];

let SQL_PAGE = null;

const SQL_SAVED_KEY = 'ghstats.sql.saved';
const SQL_HISTORY_KEY = 'ghstats.sql.history';
const SQL_HISTORY_MAX = 50;

function sqlStored(key) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || '[]');
    return Array.isArray(value) ? value.filter((e) => e && typeof e.sql === 'string') : [];
  } catch (err) {
    return [];
  }
}

function sqlStore(key, list) {
  try {
    localStorage.setItem(key, JSON.stringify(list));
    return true;
  } catch (err) {
    return false;
  }
}

/** Put a query at the top of the history, once. */
function rememberSql(text) {
  if (!text.trim()) return;
  const list = sqlStored(SQL_HISTORY_KEY).filter((entry) => entry.sql !== text);
  list.unshift({ sql: text, at: new Date().toISOString() });
  sqlStore(SQL_HISTORY_KEY, list.slice(0, SQL_HISTORY_MAX));
}

/** A name to offer when saving: the recipe it came from, else its first comment
 *  or first line. */
function sqlSuggestName(text, recipe) {
  if (recipe) return recipe.title + ' (edited)';
  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean);
  const comment = lines.find((line) => line.startsWith('--') && line.replace(/^-+\s*/, ''));
  const first = comment ? comment.replace(/^-+\s*/, '') : (lines[0] || 'query');
  return first.length > 60 ? first.slice(0, 57) + '…' : first;
}

function saveSql(page) {
  const text = page.editor.getValue();
  if (!text.trim()) return;
  const saved = sqlStored(SQL_SAVED_KEY);
  const current = saved.find((entry) => entry.sql === text);
  const name = (window.prompt('Save this query as', current ? current.name : sqlSuggestName(text, page.recipe)) || '').trim();
  if (!name) return;
  const clash = saved.find((entry) => entry.name === name);
  if (clash && clash.sql !== text && !window.confirm(`Replace the saved query “${name}”?`)) return;
  const next = [{ name, sql: text, at: new Date().toISOString() }]
    .concat(saved.filter((entry) => entry.name !== name));
  if (!sqlStore(SQL_SAVED_KEY, next)) {
    page.status.textContent = 'Could not save: this browser is not allowing local storage.';
    return;
  }
  page.status.textContent = `Saved as “${name}”.`;
  renderSqlLibrary(page);
}

function sqlAgo(iso) {
  const seconds = (Date.now() - Date.parse(iso)) / 1000;
  if (!(seconds >= 0)) return '';
  if (seconds < 90) return 'just now';
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 129600) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} d ago`;
}

/** First line worth showing for a query in a list: skip blank and header lines. */
function sqlGist(text) {
  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean);
  const code = lines.find((line) => !line.startsWith('--'));
  return (code || lines[0] || '').slice(0, 80);
}

// The schema changes only when the store is migrated, which means a restart:
// fetch it once per page load. Outside `api` on purpose, so a navigation that
// supersedes a render does not discard it.
let SQL_SCHEMA = null;

let SQL_RECIPES = null;

function sqlRecipes() {
  if (!SQL_RECIPES) {
    SQL_RECIPES = fetch('/api/sql/recipes').then((response) => {
      if (!response.ok) throw new Error(response.statusText);
      return response.json();
    }).then((body) => body.recipes);
    SQL_RECIPES.catch(() => { SQL_RECIPES = null; });
  }
  return SQL_RECIPES;
}

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
  const mine = GENERATION;
  const { params } = parseHash();
  const recipeId = params.get('recipe');
  let recipe = null;
  let missing = null;
  if (recipeId) {
    const all = await sqlRecipes();
    if (mine !== GENERATION) throw STALE;
    recipe = all.find((r) => r.id === recipeId) || null;
    if (!recipe) missing = recipeId;
  }
  const edited = params.get('sql');
  // Recorded before running, so a query that fails is not lost either.
  if (edited !== null) rememberSql(edited);
  const text = edited !== null ? edited : (recipe ? recipe.sql : null);

  if (!SQL_PAGE || !main.contains(SQL_PAGE.root)) SQL_PAGE = buildSqlPage(text);
  const page = SQL_PAGE;
  page.recipe = recipe;
  if (text !== null && page.editor.getValue() !== text) page.editor.setValue(text);
  syncSqlInputs(page);
  renderSqlRecipe(page, recipe, edited !== null && recipe !== null && edited !== recipe.sql, missing);
  renderSqlLibrary(page);

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
    dropSqlChart(page);
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
  const save = el('button', { class: 'more sql-save', text: 'Save', title: 'Ctrl+S — kept in this browser' });
  const strip = el('div', { class: 'sql-params' });
  const results = el('div', { class: 'sql-results' });

  const recipesHost = el('div', { class: 'sql-recipes' });
  const schemaHost = el('div', null, [el('div', { class: 'empty', text: 'Loading schema…' })]);
  const libraryHost = el('div', { class: 'sql-library' });
  const side = el('aside', { class: 'sql-side' }, [recipesHost, libraryHost, schemaHost]);
  const recipeBox = el('div', { class: 'sql-recipe', hidden: true });

  const editorCard = el('div', { class: 'card' }, [
    editorHost,
    el('div', { class: 'sql-bar' }, [run, save, status]),
    strip,
  ]);
  root.appendChild(side);
  root.appendChild(el('div', { class: 'sql-work' }, [recipeBox, editorCard, results]));
  show([crumb('SQL', 'read-only'), root]);

  const page = {
    root, side, recipesHost, libraryHost, schemaHost, recipeBox, status, strip, results,
    inputs: {}, editor: null, recipe: null, recipes: [],
  };
  const execute = () => runSql(page);
  page.editor = CodeMirror(editorHost, {
    // With nothing in the URL, pick up where the last query left off.
    value: text !== null ? text : ((sqlStored(SQL_HISTORY_KEY)[0] || {}).sql || SQL_STARTER),
    mode: 'text/x-sqlite',
    lineNumbers: true,
    matchBrackets: true,
    indentUnit: 2,
    tabSize: 2,
    viewportMargin: Infinity,
    extraKeys: {
      'Ctrl-Enter': execute,
      'Cmd-Enter': execute,
      'Ctrl-S': () => saveSql(page),
      'Cmd-S': () => saveSql(page),
      'Ctrl-Space': 'autocomplete',
    },
    hintOptions: { completeSingle: false, hint: sqlHint },
  });
  run.addEventListener('click', execute);
  save.addEventListener('click', () => saveSql(page));
  // Complete as you type an identifier, not only on Ctrl+Space.
  page.editor.on('inputRead', (editor, change) => {
    if (editor.state.completionActive || change.origin !== '+input') return;
    if (!/^[\w.:]$/.test(change.text.join(''))) return;
    const type = editor.getTokenTypeAt(editor.getCursor()) || '';
    if (/comment|string/.test(type)) return;
    editor.showHint();
  });

  sqlRecipes().then((all) => {
    if (SQL_PAGE !== page) return;
    page.recipes = all;
    renderSqlRecipeList(page);
  }, () => {});

  sqlSchema().then((schema) => {
    if (SQL_PAGE !== page) return;
    const tables = {};
    schema.tables.forEach((t) => { tables[t.name] = t.columns.map((c) => c.name); });
    page.schema = schema;
    page.editor.setOption('hintOptions', { completeSingle: false, hint: sqlHint, tables });
    renderSqlSide(page, schema);
  }, (err) => {
    clear(schemaHost);
    schemaHost.appendChild(el('div', { class: 'warn', text: 'Schema unavailable: ' + (err.message || err) }));
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
  const side = page.schemaHost;
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
        onclick: () => {
          page.editor.setValue(`SELECT *\nFROM ${table.name}\nLIMIT 100`);
          runSql(page, { recipe: null });
        },
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

/**
 * The recipes, by the card they explain. Picking one replaces the editor's
 * text; the query it replaced is a Back away.
 */
function renderSqlRecipeList(page) {
  const host = page.recipesHost;
  clear(host);
  if (!page.recipes.length) return;
  host.appendChild(el('div', { class: 'sql-head', text: 'Recipes' }));
  page.recipes.forEach((recipe) => {
    host.appendChild(el('button', {
      type: 'button',
      class: 'sql-recipe-item' + (page.recipe && page.recipe.id === recipe.id ? ' on' : ''),
      text: recipe.title,
      title: recipe.about,
      onclick: () => go(['sql'], { recipe: recipe.id, sql: null }),
    }));
  });
}

/**
 * Saved queries, then the history. Opening either runs it, as a link would;
 * a recipe id is dropped, since the text is no longer the recipe's.
 */
function renderSqlLibrary(page) {
  const host = page.libraryHost;
  clear(host);
  const open = (text) => go(['sql'], { sql: text, recipe: null });
  const current = page.editor ? page.editor.getValue() : null;

  const saved = sqlStored(SQL_SAVED_KEY);
  if (saved.length) {
    host.appendChild(el('div', { class: 'sql-head', text: 'Saved' }));
    saved.forEach((entry) => {
      host.appendChild(el('div', { class: 'sql-lib-row' }, [
        el('button', {
          type: 'button', class: 'sql-recipe-item' + (entry.sql === current ? ' on' : ''),
          text: entry.name, title: entry.sql.slice(0, 400), onclick: () => open(entry.sql),
        }),
        el('button', {
          type: 'button', class: 'sql-lib-x', text: '×', title: `Delete “${entry.name}”`,
          'aria-label': `Delete ${entry.name}`,
          onclick: () => {
            if (!window.confirm(`Delete the saved query “${entry.name}”?`)) return;
            sqlStore(SQL_SAVED_KEY, sqlStored(SQL_SAVED_KEY).filter((e) => e.name !== entry.name));
            renderSqlLibrary(page);
          },
        }),
      ]));
    });
  }

  const history = sqlStored(SQL_HISTORY_KEY);
  if (history.length) {
    const details = el('details', { class: 'sql-history' });
    details.appendChild(el('summary', { class: 'sql-head', text: `History (${history.length})` }));
    history.forEach((entry) => {
      details.appendChild(el('button', {
        type: 'button', class: 'sql-recipe-item sql-hist-item', title: entry.sql.slice(0, 400),
        onclick: () => open(entry.sql),
      }, [el('code', { text: sqlGist(entry.sql) }), el('span', { class: 'ago', text: sqlAgo(entry.at) })]));
    });
    details.appendChild(el('button', {
      type: 'button', class: 'sql-lib-clear', text: 'Clear history',
      onclick: () => {
        if (!window.confirm('Clear the query history in this browser?')) return;
        sqlStore(SQL_HISTORY_KEY, []);
        renderSqlLibrary(page);
      },
    }));
    host.appendChild(details);
  }
}

// What each filter the SQL page cannot bind is called on screen.
const SQL_UNBOUND = {
  team: 'team', project: 'Jira project', issue: 'issue', kinds: 'event kinds',
  q: 'text search', ai: 'AI',
};

/** The recipe being shown: its name, what it cannot reproduce, and whether
 *  the editor still holds it as written. */
function renderSqlRecipe(page, recipe, edited, missing) {
  const box = page.recipeBox;
  clear(box);
  renderSqlRecipeList(page);
  box.hidden = !recipe && !missing;
  if (missing) {
    box.appendChild(el('div', { class: 'warn', text: `No recipe called “${missing}”.` }));
    return;
  }
  if (!recipe) return;

  const head = el('div', { class: 'sql-recipe-head' }, [
    el('span', { class: 'kind-of', text: 'recipe' }),
    el('strong', { text: recipe.title }),
  ]);
  if (edited) {
    head.appendChild(el('span', { class: 'chip', text: 'edited' }));
    head.appendChild(el('button', {
      type: 'button', class: 'more', text: 'Back to the recipe',
      onclick: () => go(['sql'], { sql: null }),
    }));
  }
  box.appendChild(head);

  const f = currentFilters();
  const active = recipe.ignores.filter((name) => f[name]).map((name) => SQL_UNBOUND[name] || name);
  if (active.length) {
    box.appendChild(el('div', {
      class: 'warn',
      text: `The card applies the ${active.join(', ')} filter${active.length > 1 ? 's' : ''}, `
          + 'and this recipe does not, so their numbers can differ.',
    }));
  }
}

/** Write the editor's text into the hash, or re-run if it is already there. */
function runSql(page, changes) {
  const text = page.editor.getValue();
  const before = location.hash;
  const recipe = changes && 'recipe' in changes ? null : page.recipe;
  // The recipe as written travels as its id alone, so the URL stays short and
  // a later fix to the recipe reaches the bookmark.
  const sql = recipe && text === recipe.sql ? null : (text.trim() ? text : null);
  go(['sql'], Object.assign({ sql }, changes));
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
  dropSqlChart(page);
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

  // Table | Chart. Which one is showing, and how the chart is set up, lives in
  // the hash like everything else -- but is written with replaceState, so
  // changing an axis redraws from the rows in hand instead of re-running the
  // query.
  const { params } = parseHash();
  const chartView = params.get('view') === 'chart';
  const tabs = el('div', { class: 'sql-tabs', role: 'tablist' });
  const body = el('div');
  const tab = (label, isChart) => el('button', {
    type: 'button', role: 'tab', class: 'sql-tab' + (chartView === isChart ? ' on' : ''),
    'aria-selected': chartView === isChart ? 'true' : 'false', text: label,
    onclick: () => {
      replaceSqlHash({ view: isChart ? 'chart' : null });
      renderSqlResult(page, result);
    },
  });
  tabs.appendChild(tab('Table', false));
  tabs.appendChild(tab('Chart', true));

  card.appendChild(el('div', { class: 'sql-tools' }, [tabs, copy]));
  card.appendChild(body);
  page.results.appendChild(card);
  if (chartView) renderSqlChart(page, result, body);
  else body.appendChild(sqlTable(result));
}

function dropSqlChart(page) {
  if (!page.chart) return;
  const index = CHARTS.indexOf(page.chart);
  if (index >= 0) CHARTS.splice(index, 1);
  try { page.chart.destroy(); } catch (err) { /* already detached */ }
  page.chart = null;
}

/** Change the hash without the hashchange that would re-run the query. */
function replaceSqlHash(changes) {
  const { parts, params } = parseHash();
  for (const [k, v] of Object.entries(changes)) {
    if (v === null || v === undefined || v === '') params.delete(k);
    else params.set(k, v);
  }
  const query = params.toString();
  history.replaceState(null, '', '#/' + parts.map(encodeURIComponent).join('/') + (query ? '?' + query : ''));
}

const SQL_SERIES_MAX = 4;        // the palette's four slots
const SQL_DATEISH = /^\d{4}-\d{2}(-\d{2}([T ][\d:.]+Z?)?)?$/;

/** 'number', 'date' or 'text' for each column, from its non-null values. */
function sqlColumnKinds(result) {
  return result.columns.map((_, i) => {
    const values = result.rows.map((row) => row[i]).filter((v) => v !== null);
    if (values.length && values.every((v) => typeof v === 'number')) return 'number';
    if (values.length && values.every((v) => typeof v === 'string' && SQL_DATEISH.test(v))) return 'date';
    return 'text';
  });
}

/**
 * The chart settings from the hash, checked against this result's columns,
 * with a guess for whatever is missing: x is the first date or text column,
 * y the first numeric column other than x, and the type follows x -- a line over dates,
 * a scatter over numbers, bars over anything else.
 */
function sqlChartConfig(result, kinds) {
  const { params } = parseHash();
  const columns = result.columns;
  const numeric = columns.filter((_, i) => kinds[i] === 'number');

  let x = params.get('cx');
  if (!columns.includes(x)) {
    x = columns.find((_, i) => kinds[i] !== 'number') || columns[0];
  }
  const xKind = kinds[columns.indexOf(x)];

  let series = params.get('cs');
  if (!columns.includes(series) || series === x) series = null;

  let y = [];
  try { y = JSON.parse(params.get('cy') || '[]'); } catch (err) { y = []; }
  y = (Array.isArray(y) ? y : []).filter((name) => numeric.includes(name) && name !== x && name !== series);
  // One measure by default: a second is a click away, and two columns of
  // different scale on one axis flatten the smaller into the baseline.
  if (!y.length) y = numeric.filter((name) => name !== x && name !== series).slice(0, 1);
  y = y.slice(0, series ? 1 : SQL_SERIES_MAX);

  let type = params.get('ct');
  if (!['bar', 'line', 'scatter'].includes(type)) {
    type = xKind === 'date' ? 'line' : xKind === 'number' ? 'scatter' : 'bar';
  }
  if (type === 'scatter' && xKind !== 'number') type = 'bar';
  return { type, x, y, series, xKind };
}

/**
 * Rows into series. With a series column, one y split by that column's values,
 * in the order the rows introduce them; without, one series per y column.
 * Past the fourth series the rest become "Other". Rows that share an x within
 * a series are summed, and the notes say so.
 */
function sqlChartShape(result, config) {
  const col = (name) => result.columns.indexOf(name);
  const xi = col(config.x);
  const notes = [];
  const shape = { type: config.type, xTitle: config.x, yTitle: config.series ? config.y[0] : '', series: [], labels: [] };
  if (!config.y.length) return { shape, notes: ['Pick at least one numeric column to plot.'] };

  // (series name, y column index) for every row, in row order.
  const entries = [];
  if (config.series) {
    const si = col(config.series);
    const yi = col(config.y[0]);
    result.rows.forEach((row) => entries.push([row[si] === null ? 'NULL' : String(row[si]), yi, row]));
  } else {
    result.rows.forEach((row) => config.y.forEach((name) => entries.push([name, col(name), row])));
  }

  const names = [];
  entries.forEach(([name]) => { if (!names.includes(name)) names.push(name); });
  const kept = names.length > SQL_SERIES_MAX ? names.slice(0, SQL_SERIES_MAX - 1) : names;
  if (names.length > SQL_SERIES_MAX) {
    notes.push(`${names.length} series: the first ${SQL_SERIES_MAX - 1} are drawn, the other ${names.length - kept.length} combined as Other. ORDER BY decides which come first.`);
  }
  const seriesOf = (name) => (kept.includes(name) ? name : 'Other');
  const order = kept.concat(names.length > kept.length ? ['Other'] : []);

  if (config.type === 'scatter') {
    const points = new Map(order.map((name) => [name, []]));
    entries.forEach(([name, yi, row]) => {
      if (typeof row[xi] === 'number' && typeof row[yi] === 'number') {
        points.get(seriesOf(name)).push({ x: row[xi], y: row[yi] });
      }
    });
    shape.series = order.map((name) => ({ name, other: name === 'Other', points: points.get(name) }));
    return { shape, notes };
  }

  const labels = [];
  const seen = new Set();
  result.rows.forEach((row) => {
    const label = row[xi] === null ? 'NULL' : String(row[xi]);
    if (!seen.has(label)) { seen.add(label); labels.push(label); }
  });
  // Dates read left to right whatever order the query returned them in.
  if (config.xKind === 'date') labels.sort();
  const index = new Map(labels.map((label, i) => [label, i]));

  const values = new Map(order.map((name) => [name, labels.map(() => null)]));
  let summed = false;
  entries.forEach(([name, yi, row]) => {
    if (typeof row[yi] !== 'number') return;
    const line = values.get(seriesOf(name));
    const at = index.get(row[xi] === null ? 'NULL' : String(row[xi]));
    if (line[at] !== null) summed = true;
    line[at] = (line[at] || 0) + row[yi];
  });
  if (summed) notes.push(`Several rows share a ${config.x} value; their values are added together.`);
  if (config.type === 'bar' && config.series) shape.stacked = true;

  shape.labels = labels;
  shape.series = order.map((name) => ({ name, other: name === 'Other', values: values.get(name) }));
  return { shape, notes };
}

function renderSqlChart(page, result, host) {
  const kinds = sqlColumnKinds(result);
  const config = sqlChartConfig(result, kinds);
  const numeric = result.columns.filter((_, i) => kinds[i] === 'number');
  const redraw = (changes) => {
    replaceSqlHash(changes);
    renderSqlResult(page, result);
  };

  const select = (label, value, options, onChange) => {
    const box = el('select', { 'aria-label': label, onchange: () => onChange(box.value) });
    options.forEach(([v, text, disabled]) => box.appendChild(el('option', {
      value: v, text, selected: v === value, disabled: !!disabled,
    })));
    return el('label', { class: 'sql-ctl' }, [el('span', { text: label }), box]);
  };

  const controls = el('div', { class: 'sql-chart-controls' });
  controls.appendChild(select('type', config.type, [
    ['bar', 'bar'], ['line', 'line'],
    ['scatter', config.xKind === 'number' ? 'scatter' : 'scatter (needs a numeric x)', config.xKind !== 'number'],
  ], (v) => redraw({ ct: v })));
  controls.appendChild(select('x', config.x, result.columns.map((c) => [c, c]),
    (v) => redraw({ cx: v, ct: null, cy: null })));
  controls.appendChild(select('series', config.series || '', [['', 'none']].concat(
    result.columns.filter((c) => c !== config.x && !numeric.includes(c)).map((c) => [c, c])),
  (v) => redraw({ cs: v || null, cy: null })));

  const ys = el('div', { class: 'sql-ctl sql-ys' }, [el('span', { text: config.series ? 'y (one)' : 'y' })]);
  numeric.filter((c) => c !== config.x).forEach((c) => {
    const on = config.y.includes(c);
    ys.appendChild(el('button', {
      type: 'button', class: 'chip sql-y' + (on ? ' on' : ''), text: c,
      'aria-pressed': on ? 'true' : 'false',
      disabled: !on && !config.series && config.y.length >= SQL_SERIES_MAX ? true : null,
      onclick: () => {
        let next;
        if (config.series) next = [c];
        else next = on ? config.y.filter((n) => n !== c) : config.y.concat([c]);
        redraw({ cy: next.length ? JSON.stringify(next) : null });
      },
    }));
  });
  controls.appendChild(ys);
  host.appendChild(controls);

  if (!numeric.length) {
    host.appendChild(el('div', { class: 'empty', text: 'No numeric column to plot. Add a COUNT or SUM.' }));
    return;
  }

  const { shape, notes } = sqlChartShape(result, config);
  if (shape.series.length > 1) {
    const legendBox = el('div', { class: 'legend sql-legend' });
    const slots = ['--s1', '--s2', '--s3', '--s4'];
    shape.series.forEach((s, i) => {
      const swatch = el('i');
      swatch.style.background = s.other ? 'var(--ink-muted)' : `var(${slots[i]})`;
      legendBox.appendChild(el('span', { class: 'key' }, [swatch, s.name]));
    });
    host.appendChild(legendBox);
  }
  const chartHost = el('div');
  host.appendChild(chartHost);
  whenPlaced(chartHost, () => {
    if (!chartHost.isConnected) return;
    page.chart = sqlResultChart(chartHost, shape);
  });
  notes.forEach((text) => host.appendChild(el('div', { class: 'count-note', text })));
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
