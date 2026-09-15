/* ghstats explorer — graphs.
 *
 * Chart.js draws the plots. It is vendored under `static/vendor/` rather than
 * linked from a CDN because everything downstream of `ghstats-sync` is offline
 * by contract, and a chart that only appears when jsdelivr is reachable would
 * make looking at the results the one step that needs the network. See
 * `static/vendor/README.md`.
 *
 * Four rules keep the library from becoming a second source of truth:
 *
 * 1. Colour is read from the CSS custom properties at build time, never
 *    written here. The four event kinds own the four palette slots by
 *    identity, so filtering the stream never repaints the survivors, and dark
 *    mode is a rebuild against different variables rather than an inversion.
 *
 * 2. The legend is DOM, not Chart.js. A Chart.js legend hides datasets in
 *    local state, which would park a second invisible filter next to the
 *    checkbox row; the one in `explorer.js` toggles the same `kinds` parameter
 *    the checkboxes write, so the URL still describes what is on screen.
 *
 * 3. Instances are registered and destroyed on every view swap. Chart.js keeps
 *    a ResizeObserver per canvas, and a detached canvas holding a live
 *    observer is a leak that only shows itself after twenty navigations.
 *
 * 4. Animation is off, so `new Chart()` paints inside the call. Chart.js
 *    otherwise defers the first paint to a shared animation loop, and a view
 *    swapped away from while that loop still owns a frame can leave the loop
 *    waiting on a frame that never arrives -- after which no chart on the page
 *    draws again until a reload. The loop is also the wrong shape for this
 *    data: an org-wide year is ~3,500 bars, whose grow-in tweens some 21,000
 *    properties a frame to say something the numbers already said.
 *
 * The contribution calendar is deliberately *not* a Chart.js chart. It is a
 * run of days on a week grid with month rules — layout, not a plot — and the
 * matrix plugin that could draw it costs another dependency to end up with
 * less control over the thing GitHub renders as a plain grid.
 */
'use strict';

const CHARTS = [];

function destroyCharts() {
  while (CHARTS.length) {
    try { CHARTS.pop().destroy(); } catch (err) { /* already detached */ }
  }
}

/* -- theme --------------------------------------------------------------- */

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** The palette the charts are currently drawn against. Re-read per build. */
function theme() {
  return {
    commit: cssVar('--s1'), pull: cssVar('--s2'),
    merge: cssVar('--s3'), review: cssVar('--s4'),
    grid: cssVar('--grid'), axis: cssVar('--baseline'),
    tick: cssVar('--ink-muted'), ink: cssVar('--ink'), ink2: cssVar('--ink-2'),
    surface: cssVar('--surface-1'), border: cssVar('--border'),
  };
}

const ALPHA_CACHE = new Map();

/**
 * A palette colour at partial opacity, for area fills.
 *
 * Resolved through the browser rather than parsed here: the palette is
 * hex today but `color-mix()` and `oklch()` are both legal in a stylesheet and
 * neither survives a substring. A probe element makes the browser hand back
 * `rgb(r, g, b)` whatever the notation was.
 */
function alpha(color, opacity) {
  const key = color + '|' + opacity;
  if (ALPHA_CACHE.has(key)) return ALPHA_CACHE.get(key);
  const probe = document.createElement('span');
  probe.style.cssText = 'display:none';
  probe.style.color = color;
  document.body.appendChild(probe);
  const parts = getComputedStyle(probe).color.match(/[\d.]+/g);
  probe.remove();
  const out = parts ? `rgba(${parts[0]}, ${parts[1]}, ${parts[2]}, ${opacity})` : color;
  ALPHA_CACHE.set(key, out);
  return out;
}

/* -- shared chart furniture ---------------------------------------------- */

function tooltipStyle(t) {
  return {
    backgroundColor: t.surface, titleColor: t.ink, bodyColor: t.ink2,
    footerColor: t.tick, borderColor: t.border, borderWidth: 1,
    cornerRadius: 8, padding: 9, boxWidth: 10, boxHeight: 10, boxPadding: 4,
    usePointStyle: true, caretSize: 5,
    titleFont: { size: 12, weight: 650 },
    bodyFont: { size: 12 },
    footerFont: { size: 11, weight: 400 },
  };
}

function valueAxis(t, stacked) {
  return {
    stacked: !!stacked,
    beginAtZero: true,
    border: { display: false },
    grid: { color: t.grid, drawTicks: false },
    ticks: {
      color: t.tick, padding: 8, maxTicksLimit: 6, precision: 0,
      callback: (v) => Number(v).toLocaleString(),
    },
  };
}

function categoryAxis(t, stacked, ticks) {
  return {
    stacked: !!stacked,
    border: { color: t.axis },
    grid: { display: false },
    ticks: Object.assign({ color: t.tick, padding: 5, maxRotation: 0 }, ticks || {}),
  };
}

/** A responsive canvas in a box of fixed height, appended to `host`. */
function canvasHost(host, height, label) {
  const box = el('div', { class: 'canvas-box' });
  box.style.height = height + 'px';
  const canvas = el('canvas', { role: 'img', 'aria-label': label || '' });
  box.appendChild(canvas);
  host.appendChild(box);
  return canvas;
}

function noData(host, text) {
  host.appendChild(el('div', { class: 'empty', text: text || 'No activity.' }));
}

const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday',
                   'Friday', 'Saturday'];
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * `2026-08-18` as `Tue 18 Aug 2026`, or `Tuesday 18 Aug 2026` when `long`.
 *
 * Built from the parts rather than handed to `toLocaleDateString`: these
 * strings are already local days in the store's zone, and a formatter would
 * shift them into the reader's before printing.
 */
function prettyDay(iso, long) {
  const [y, m, d] = iso.split('-').map(Number);
  const weekday = DAY_NAMES[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
  return `${long ? weekday : weekday.slice(0, 3)} ${d} ${MONTH_NAMES[m - 1]} ${y}`;
}

/* -- activity by day ------------------------------------------------------ */

/**
 * Daily counts as stacked bars, one stack per local day.
 *
 * The bars are the primary drill-down — a spike is only useful once you can
 * click it — so the whole column is a hit target for that day's view.
 */
function dailyChart(host, days, onPick) {
  clear(host);
  if (!days.length) { noData(host, 'No activity in this window.'); return; }

  const t = theme();
  const kinds = activeKinds();
  const byDay = new Map(days.map((d) => [d.day, d]));
  const total = days.reduce((sum, d) => sum + kinds.reduce((s, k) => s + (d[k] || 0), 0), 0);
  const canvas = canvasHost(host, 250,
    `Activity by day: ${num(total)} events across ${days.length} days`);

  CHARTS.push(new Chart(canvas, {
    type: 'bar',
    data: {
      labels: days.map((d) => d.day),
      datasets: kinds.map((kind) => ({
        label: KIND_LABEL[kind],
        data: days.map((d) => d[kind] || 0),
        backgroundColor: t[kind],
        borderRadius: 3,
        borderSkipped: 'start',
        maxBarThickness: 34,
        categoryPercentage: 0.92,
        barPercentage: 0.98,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      onHover: (event, active, chart) => {
        chart.canvas.style.cursor = active.length ? 'pointer' : 'default';
      },
      onClick: (event, active, chart) => {
        const hit = active.length ? active
          : chart.getElementsAtEventForMode(event, 'index', { intersect: false }, true);
        if (hit.length) onPick(days[hit[0].index].day);
      },
      plugins: {
        legend: { display: false },
        tooltip: Object.assign(tooltipStyle(t), {
          callbacks: {
            title: (items) => prettyDay(items[0].label),
            label: (item) => ` ${item.dataset.label}: ${num(item.parsed.y)}`,
            footer: (items) => {
              const day = byDay.get(items[0].label);
              const lines = [];
              if (day && (day.added || day.removed)) {
                lines.push(`+${num(day.added)} / −${num(day.removed)} lines`);
              }
              lines.push('Click to open this day');
              return lines;
            },
          },
          filter: (item) => item.parsed.y > 0,
        }),
      },
      scales: {
        x: categoryAxis(t, true, {
          autoSkip: true, maxTicksLimit: 14,
          callback(index) { return (this.getLabelForValue(index) || '').slice(5); },
        }),
        y: valueAxis(t, true),
      },
    },
  }));
}

/* -- rhythm --------------------------------------------------------------- */

/** True when every series in a rhythm map is flat zero. */
function rhythmIsEmpty(map, keys, kinds) {
  return !kinds.some((kind) => keys.some((key) => (map[kind] || {})[key]));
}

/**
 * Day-of-week counts, one grouped bar per kind.
 *
 * Grouped rather than stacked: the question this answers is whether reviews
 * follow commits through the week, and a stack buries the small series under
 * the commit column.
 */
function weekdayChart(host, rhythm) {
  clear(host);
  const days = rhythm.days_of_week || DAY_NAMES.slice(1).concat(DAY_NAMES[0]);
  const kinds = activeKinds();
  if (rhythmIsEmpty(rhythm.by_day_of_week, days, kinds)) { noData(host); return; }

  const t = theme();
  const canvas = canvasHost(host, 210, 'Events by day of week');

  CHARTS.push(new Chart(canvas, {
    type: 'bar',
    data: {
      labels: days,
      datasets: kinds.map((kind) => ({
        label: KIND_LABEL[kind],
        data: days.map((d) => (rhythm.by_day_of_week[kind] || {})[d] || 0),
        backgroundColor: t[kind],
        borderRadius: 3,
        borderSkipped: 'start',
        maxBarThickness: 22,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: Object.assign(tooltipStyle(t), {
          callbacks: { label: (item) => ` ${item.dataset.label}: ${num(item.parsed.y)}` },
          filter: (item) => item.parsed.y > 0,
        }),
      },
      scales: {
        x: categoryAxis(t, false, { callback(i) { return this.getLabelForValue(i).slice(0, 3); } }),
        y: valueAxis(t, false),
      },
    },
  }));
}

/**
 * Hour-of-day counts, one filled line per kind.
 *
 * A line rather than bars: 24 points is a shape — a morning peak, a lunch
 * trough, an evening tail — and bars ask the eye to compare 96 rectangles when
 * the answer is a silhouette.
 */
function hourChart(host, rhythm) {
  clear(host);
  const hours = Array.from({ length: 24 }, (_, h) => String(h));
  const kinds = activeKinds();
  if (rhythmIsEmpty(rhythm.by_hour, hours, kinds)) { noData(host); return; }

  const t = theme();
  const canvas = canvasHost(host, 210,
    `Events by hour of day, ${META ? META.timezone : 'UTC'}`);

  CHARTS.push(new Chart(canvas, {
    type: 'line',
    data: {
      labels: hours.map((h) => h.padStart(2, '0') + ':00'),
      datasets: kinds.map((kind) => ({
        label: KIND_LABEL[kind],
        data: hours.map((h) => (rhythm.by_hour[kind] || {})[h] || 0),
        borderColor: t[kind],
        backgroundColor: alpha(t[kind], 0.16),
        borderWidth: 2,
        fill: true,
        tension: 0.38,
        pointRadius: 2,
        pointHoverRadius: 4.5,
        pointBackgroundColor: t.surface,
        pointBorderColor: t[kind],
        pointBorderWidth: 1.5,
      })),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: Object.assign(tooltipStyle(t), {
          callbacks: { label: (item) => ` ${item.dataset.label}: ${num(item.parsed.y)}` },
          filter: (item) => item.parsed.y > 0,
        }),
      },
      scales: {
        x: categoryAxis(t, false, { autoSkip: true, maxTicksLimit: 12 }),
        y: valueAxis(t, false),
      },
    },
  }));
}

/* -- contribution calendar ------------------------------------------------ */

const CELL = 12;                 // square edge
const PITCH = CELL + 3;          // edge plus gutter
const GUTTER_L = 30;             // room for Mon / Wed / Fri
const GUTTER_T = 17;             // room for the month rules

function addDays(iso, n) {
  const [y, m, d] = iso.split('-').map(Number);
  const at = new Date(Date.UTC(y, m - 1, d + n));
  return at.toISOString().slice(0, 10);
}

/** Monday index of a day: Monday 0 … Sunday 6, matching the grid's rows. */
function weekdayIndex(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7;
}

/**
 * Quartile cut-points over the days that actually had activity.
 *
 * Fixed thresholds would render a quiet person's whole year in the lightest
 * step and a busy repository's in the darkest. The ramp describes the slice it
 * is drawn for, which is why the legend says "Less / More" and not a number.
 */
function intensitySteps(totals) {
  const sorted = totals.filter((v) => v > 0).sort((a, b) => a - b);
  if (!sorted.length) return [1, 2, 3];
  const at = (p) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * p))];
  return [at(0.25), at(0.5), at(0.75)];
}

function intensityLevel(value, steps) {
  if (!value) return 0;
  if (value <= steps[0]) return 1;
  if (value <= steps[1]) return 2;
  if (value <= steps[2]) return 3;
  return 4;
}

/**
 * A GitHub-shaped year of days: weeks as columns, Monday at the top.
 *
 * The window is whatever the filters ask for, padded out to whole weeks so the
 * columns line up; days outside it are left blank rather than drawn as zeroes,
 * because "no data here" and "nothing happened" are different claims.
 */
function contributionCalendar(host, days, range, onPick) {
  clear(host);
  if (!days.length) { noData(host, 'No activity in this window.'); return; }

  const counts = new Map(days.map((d) => [d.day, d]));
  const first = range.from || days[0].day;
  const last = range.to || days[days.length - 1].day;
  if (last < first) { noData(host, 'No activity in this window.'); return; }

  const start = addDays(first, -weekdayIndex(first));
  const end = addDays(last, 6 - weekdayIndex(last));
  const weeks = Math.round((Date.parse(end) - Date.parse(start)) / 86400000 + 1) / 7;
  const steps = intensitySteps(days.map((d) => d.total));

  const width = GUTTER_L + weeks * PITCH + 2;
  const height = GUTTER_T + 7 * PITCH + 2;
  const root = svg('svg', {
    class: 'cal', width, height, viewBox: `0 0 ${width} ${height}`,
    role: 'grid', 'aria-label': `Contribution calendar, ${first} to ${last}`,
  });

  // Every other row, starting at Monday: seven labels at this pitch collide.
  [0, 2, 4].forEach((row) => {
    const label = svg('text', {
      class: 'wd', x: GUTTER_L - 7, y: GUTTER_T + row * PITCH + CELL - 2.5,
      'text-anchor': 'end',
    });
    label.textContent = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][row];
    root.appendChild(label);
  });

  const cells = [];
  let lastMonthLabel = -99;
  let previousMonth = null;

  for (let week = 0; week < weeks; week++) {
    const columnStart = addDays(start, week * 7);
    const month = columnStart.slice(5, 7);
    // One rule per month, but only where the previous one has cleared out.
    if (month !== previousMonth && week - lastMonthLabel >= 3) {
      const label = svg('text', { class: 'mon', x: GUTTER_L + week * PITCH, y: 10 });
      label.textContent = MONTH_NAMES[Number(month) - 1];
      root.appendChild(label);
      lastMonthLabel = week;
    }
    previousMonth = month;

    for (let row = 0; row < 7; row++) {
      const day = addDays(columnStart, row);
      if (day < first || day > last) continue;
      const entry = counts.get(day);
      const value = entry ? entry.total : 0;
      const cell = svg('rect', {
        class: 'lv' + intensityLevel(value, steps),
        x: GUTTER_L + week * PITCH, y: GUTTER_T + row * PITCH,
        width: CELL, height: CELL, rx: 2.5,
        role: 'gridcell', tabindex: -1,
        'data-day': day, 'data-column': week, 'data-row': row,
      });
      const title = svg('title', {});
      title.textContent = `${prettyDay(day)}: ${num(value)} events`;
      cell.appendChild(title);

      const readout = () => {
        const box = el('div');
        box.appendChild(el('div', { class: 'th', text: prettyDay(day) }));
        // The series carries quiet days as zeroes, so an entry is not proof
        // that anything happened on it.
        if (!entry || !entry.total) {
          box.appendChild(el('div', { class: 'hint', text: 'Nothing recorded' }));
          return box;
        }
        KINDS.forEach((kind) => {
          if (!entry[kind]) return;
          const row2 = el('div', { class: 'row' });
          const key = el('i');
          key.style.background = `var(${KIND_VAR[kind]})`;
          row2.appendChild(key);
          row2.appendChild(el('span', { text: KIND_LABEL[kind] }));
          row2.appendChild(el('b', { text: num(entry[kind]) }));
          box.appendChild(row2);
        });
        box.appendChild(el('div', { class: 'hint', text: 'Click to open this day' }));
        return box;
      };

      cell.addEventListener('pointermove', (e) => showTip(readout(), e));
      cell.addEventListener('pointerleave', hideTip);
      cell.addEventListener('click', () => onPick(day));
      cells.push(cell);
      root.appendChild(cell);
    }
  }

  if (cells.length) roveGrid(root, cells, onPick);

  const scroller = el('div', { class: 'cal-scroll' }, [root]);
  host.appendChild(scroller);
  host.appendChild(calendarKey(steps));
  // A long window is drawn right-to-left in importance: the recent end matters
  // most, so that is the end the scroller opens on.
  scroller.scrollLeft = scroller.scrollWidth;
}

/**
 * One tab stop for the whole grid, arrows within it.
 *
 * A year is 365 focusable squares; making each of them a tab stop would bury
 * everything below the calendar. Left and right move a week, up and down move
 * a day — the reading directions of the grid itself.
 */
function roveGrid(root, cells, onPick) {
  let current = cells[cells.length - 1];
  current.setAttribute('tabindex', 0);

  const focus = (cell) => {
    if (!cell) return;
    current.setAttribute('tabindex', -1);
    current = cell;
    current.setAttribute('tabindex', 0);
    current.focus();
    const box = current.getBoundingClientRect();
    showTip(el('div', null, [
      el('div', { class: 'th', text: prettyDay(current.getAttribute('data-day')) }),
      el('div', { class: 'hint', text: 'Enter opens this day' }),
    ]), { clientX: box.left + box.width / 2, clientY: box.top });
  };

  const at = (column, row) => cells.find((c) =>
    Number(c.getAttribute('data-column')) === column &&
    Number(c.getAttribute('data-row')) === row);

  root.addEventListener('keydown', (event) => {
    const column = Number(current.getAttribute('data-column'));
    const row = Number(current.getAttribute('data-row'));
    const moves = {
      ArrowLeft: [column - 1, row], ArrowRight: [column + 1, row],
      ArrowUp: [column, row - 1], ArrowDown: [column, row + 1],
    };
    if (moves[event.key]) {
      const [c, r] = moves[event.key];
      const next = r < 0 ? at(c - 1, 6) : r > 6 ? at(c + 1, 0) : at(c, r);
      if (next) { event.preventDefault(); focus(next); }
      return;
    }
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      onPick(current.getAttribute('data-day'));
    }
  });
  root.addEventListener('blur', hideTip, true);
}

function calendarKey(steps) {
  const box = el('div', { class: 'cal-key' });
  box.appendChild(el('span', { text: 'Less' }));
  [0, 1, 2, 3, 4].forEach((level) => {
    const swatch = el('i', { class: 'lv' + level });
    if (level) {
      const upper = level < 4 ? steps[level - 1] : null;
      swatch.title = upper ? `up to ${num(upper)} events a day` : 'the busiest days';
    } else {
      swatch.title = 'no activity';
    }
    box.appendChild(swatch);
  });
  box.appendChild(el('span', { text: 'More' }));
  return box;
}

/* -- pull request size against discussion --------------------------------- */

/**
 * Does review attention keep up with the size of what is being shipped?
 *
 * Two plots, because the question has two shapes and one chart cannot hold
 * both. The trend is bars of how many pull requests a bucket held against a
 * line of how much discussion each hundred changed lines drew; the scatter is
 * one dot per pull request, which is where the outliers live — the
 * four-thousand-line change nobody commented on is a point in the bottom
 * right, and no aggregate will ever show it to you.
 *
 * **Colour follows the same identities as everywhere else.** The four
 * categorical slots belong to the four event kinds, so a count of pull
 * requests is drawn in the `pull` colour and a count of discussion in the
 * `review` colour. Picking a fresh hue here would make "orange" mean PRs on
 * one card and something else on the next.
 */
function pullTrendChart(host, buckets, granularity) {
  clear(host);
  if (!buckets.length) { noData(host, 'No measured pull requests in this window.'); return; }

  const t = theme();
  const pulls = buckets.reduce((sum, b) => sum + b.pulls, 0);
  const canvas = canvasHost(host, 250,
    `Pull requests per ${granularity}: ${num(pulls)} across ${buckets.length} ${granularity}s`);

  CHARTS.push(new Chart(canvas, {
    data: {
      labels: buckets.map((b) => b.bucket),
      datasets: [
        {
          type: 'bar',
          label: 'PRs opened',
          data: buckets.map((b) => b.pulls),
          backgroundColor: t.pull,
          borderRadius: 3,
          maxBarThickness: 34,
          categoryPercentage: 0.92,
          barPercentage: 0.98,
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: 'comments per 100 lines',
          // `null` rather than 0 for a bucket that changed no lines: the ratio
          // is undefined there, and a zero would draw a dip that reads as "the
          // reviews stopped" when nothing was shipped to review.
          data: buckets.map((b) => b.per_100_lines),
          borderColor: t.review,
          backgroundColor: alpha(t.review, 0.14),
          borderWidth: 2,
          pointRadius: 2,
          pointHoverRadius: 4,
          tension: 0.25,
          spanGaps: true,
          fill: true,
          yAxisID: 'y1',
          order: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: Object.assign(tooltipStyle(t), {
          callbacks: {
            title: (items) => (granularity === 'week'
              ? 'Week of ' + prettyDay(items[0].label)
              : items[0].label),
            label: (item) => {
              const b = buckets[item.dataIndex];
              if (item.dataset.type === 'bar') {
                return ` ${num(b.pulls)} PRs, ${num(b.merged)} merged`;
              }
              return b.per_100_lines === null ? ' no lines changed'
                : ` ${b.per_100_lines.toFixed(1)} comments / 100 lines`;
            },
            footer: (items) => {
              const b = buckets[items[0].dataIndex];
              return [
                `median ${num(Math.round(b.lines_median))} lines, `
                + `${num(Math.round(b.discussion_median))} comments per PR`,
                `${num(b.undiscussed)} of ${num(b.pulls)} drew no comment`,
              ];
            },
          },
        }),
      },
      scales: {
        x: categoryAxis(t, false, {
          autoSkip: true, maxTicksLimit: 14,
          callback(index) {
            const label = this.getLabelForValue(index) || '';
            return granularity === 'week' ? label.slice(5) : label;
          },
        }),
        y: Object.assign(valueAxis(t, false), {
          position: 'left',
          title: { display: true, text: 'PRs opened', color: t.tick,
                   font: { size: 11 } },
        }),
        y1: Object.assign(valueAxis(t, false), {
          position: 'right',
          grid: { display: false },
          ticks: { color: t.tick, padding: 8, maxTicksLimit: 6,
                   callback: (v) => Number(v).toLocaleString() },
          title: { display: true, text: 'comments / 100 lines', color: t.tick,
                   font: { size: 11 } },
        }),
      },
    },
  }));
}

/**
 * One dot per pull request: how big it was against how much it was discussed.
 *
 * **The size axis is logarithmic.** Pull request size spans four orders of
 * magnitude in any real repository — a typo fix and a generated-client bump
 * sit on the same axis — and on a linear scale every ordinary change collapses
 * into a stripe against the left edge while one lockfile refresh owns the rest
 * of the width.
 *
 * **A zero-line pull request is plotted at 1.** Log scales have no zero, and
 * dropping those rows would quietly hide the reverts and branch merges that
 * change nothing and still get argued about.
 */
function pullScatterChart(host, points, onPick) {
  clear(host);
  if (!points.length) { noData(host, 'No measured pull requests in this window.'); return; }

  const t = theme();
  const canvas = canvasHost(host, 260,
    `Pull request size against discussion: ${num(points.length)} pull requests`);

  const split = (merged) => points
    .filter((p) => p.merged === merged)
    .map((p) => ({ x: Math.max(p.lines, 1), y: p.discussion, p }));

  CHARTS.push(new Chart(canvas, {
    type: 'scatter',
    data: {
      datasets: [
        {
          label: 'merged',
          data: split(true),
          backgroundColor: alpha(t.merge, 0.55),
          borderColor: t.merge,
          borderWidth: 1,
          pointRadius: 3.5,
          pointHoverRadius: 6,
        },
        {
          label: 'not merged',
          data: split(false),
          backgroundColor: alpha(t.pull, 0.45),
          borderColor: t.pull,
          borderWidth: 1,
          pointRadius: 3.5,
          pointHoverRadius: 6,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'nearest', intersect: true },
      onHover: (event, active, chart) => {
        chart.canvas.style.cursor = active.length ? 'pointer' : 'default';
      },
      onClick: (event, active, chart) => {
        if (!active.length || !onPick) return;
        const item = active[0];
        onPick(chart.data.datasets[item.datasetIndex].data[item.index].p);
      },
      plugins: {
        legend: { display: false },
        tooltip: Object.assign(tooltipStyle(t), {
          callbacks: {
            title: (items) => {
              const p = items[0].raw.p;
              return p.repo + '#' + p.number + ' · ' + p.author;
            },
            label: (item) => {
              const p = item.raw.p;
              return [
                p.title.length > 70 ? p.title.slice(0, 69) + '…' : p.title,
                `+${num(p.added)} / −${num(p.removed)} in ${num(p.files)} files`,
                `${num(p.discussion)} comments `
                + `(${num(p.conversation)} thread, ${num(p.inline)} inline, `
                + `${num(p.reviews)} review)`,
              ];
            },
            footer: () => 'Click to open the day it opened',
          },
        }),
      },
      scales: {
        x: {
          type: 'logarithmic',
          border: { color: t.axis },
          grid: { color: t.grid, drawTicks: false },
          ticks: {
            color: t.tick, padding: 5, maxRotation: 0,
            callback: (v) => {
              // Only the decade marks. Chart.js otherwise labels 2, 3, 4… on a
              // log axis, which is nine labels per decade of unreadable axis.
              const log = Math.log10(v);
              return Number.isInteger(log) ? Number(v).toLocaleString() : '';
            },
          },
          title: { display: true, text: 'lines changed', color: t.tick,
                   font: { size: 11 } },
        },
        y: Object.assign(valueAxis(t, false), {
          title: { display: true, text: 'comments', color: t.tick,
                   font: { size: 11 } },
        }),
      },
    },
  }));
}

/* -- SQL page ------------------------------------------------------------- */

/**
 * A chart of a query result, as shaped by `sqlChartShape` on the SQL page.
 *
 * Deliberately plain: one value axis (two measures of different scale belong
 * in two queries, not on a second axis), series coloured by the four palette
 * slots in the order the rows introduce them, and anything past the fourth
 * already folded into a muted "Other" before it gets here.
 *
 * Returns the instance so the page can destroy it when the result is redrawn
 * without a view swap.
 */
function sqlResultChart(host, shape) {
  clear(host);
  if (!shape.series.length) { noData(host, 'Nothing to plot.'); return null; }

  const t = theme();
  const slots = [t.commit, t.pull, t.merge, t.review];
  const colour = (s, i) => (s.other ? t.tick : slots[i]);
  const canvas = canvasHost(host, 320,
    `${shape.type} chart of ${shape.series.map((s) => s.name).join(', ')} by ${shape.xTitle}`);

  const scatter = shape.type === 'scatter';
  const datasets = shape.series.map((s, i) => {
    const c = colour(s, i);
    if (scatter) {
      return {
        label: s.name, data: s.points,
        backgroundColor: alpha(c, 0.55), borderColor: c, borderWidth: 1,
        pointRadius: 4, pointHoverRadius: 6,
      };
    }
    if (shape.type === 'line') {
      return {
        type: 'line', label: s.name, data: s.values,
        borderColor: c, backgroundColor: c, borderWidth: 2,
        pointRadius: shape.labels.length > 60 ? 0 : 2.5, pointHoverRadius: 5,
        tension: 0.2, spanGaps: false,
      };
    }
    return {
      type: 'bar', label: s.name, data: s.values, backgroundColor: c,
      borderRadius: 3, borderSkipped: 'start', maxBarThickness: 34,
      categoryPercentage: 0.9, barPercentage: 0.96,
      // A 2px surface gap between stacked segments and neighbouring bars.
      borderColor: t.surface, borderWidth: shape.stacked ? { top: 2 } : 0,
    };
  });

  const format = (v) => (v === null || v === undefined ? '–'
    : Number.isInteger(v) ? num(v) : Number(v).toLocaleString(undefined, { maximumFractionDigits: 3 }));

  const chart = new Chart(canvas, {
    type: scatter ? 'scatter' : 'bar',
    data: scatter ? { datasets } : { labels: shape.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: scatter ? { mode: 'nearest', intersect: true } : { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: Object.assign(tooltipStyle(t), {
          callbacks: scatter ? {
            title: (items) => `${shape.xTitle} ${format(items[0].parsed.x)}`,
            label: (item) => ` ${item.dataset.label}: ${format(item.parsed.y)}`,
          } : {
            title: (items) => `${shape.xTitle}: ${items[0].label}`,
            label: (item) => ` ${item.dataset.label}: ${format(item.parsed.y)}`,
          },
        }),
      },
      scales: {
        x: scatter ? {
          type: 'linear',
          border: { color: t.axis },
          grid: { color: t.grid, drawTicks: false },
          ticks: { color: t.tick, padding: 5, maxRotation: 0, callback: (v) => Number(v).toLocaleString() },
          title: { display: true, text: shape.xTitle, color: t.tick, font: { size: 11 } },
        } : Object.assign(categoryAxis(t, shape.stacked, { autoSkip: true, maxTicksLimit: 16 }), {
          title: { display: true, text: shape.xTitle, color: t.tick, font: { size: 11 } },
        }),
        y: Object.assign(valueAxis(t, shape.stacked), {
          beginAtZero: !scatter,
          ticks: { color: t.tick, padding: 8, maxTicksLimit: 6, callback: (v) => Number(v).toLocaleString() },
          title: { display: !!shape.yTitle, text: shape.yTitle || '', color: t.tick, font: { size: 11 } },
        }),
      },
    },
  });
  CHARTS.push(chart);
  return chart;
}
