/* Admin panel — four screens, no framework, no build step.
 *
 * Every Telegram API call below is optional-chained. Telegram Desktop and older Android
 * builds lag the Bot API by several versions, and a panel that throws on a missing
 * HapticFeedback is worse than one that simply doesn't buzz.
 *
 * Two rules about painting, both learned from using this on a phone:
 *
 * A screen never blanks to a spinner for data it already has. Reads are cached, a tab
 * paints from that cache instantly, and the re-check happens behind the refresh icon.
 * A mutation re-renders in place at the same scroll offset, because a toggle that throws
 * you back to the top of the list is a toggle nobody uses twice.
 */

'use strict';

// Resolved in boot(), not at load time: the bridge script is fetched from telegram.org,
// and on a slow connection it can still be in flight when this file executes. Reading it
// once at module scope turns that race into a permanent "open this from Telegram".
let tg = null;
const API = '/panel/api';

const state = {
  boot: null,          // /bootstrap payload
  tab: 'dashboard',
  detail: null,        // {type, ...} when a detail view is pushed over a tab
  period: 'today',
  groupsQuery: '',
  alertFilter: { type: '', unit: '' },
  alertItems: [],
  alertNext: null,
  alertLoaded: false,  // the feed is paged, so it caches itself rather than by URL
  scroll: {},          // tab -> last scroll offset, restored when you come back to it
  // Bumped on every navigation. A background re-check that resolves after the user has
  // moved on compares this before touching the DOM, so a slow response can't repaint a
  // screen the user already left.
  epoch: 0,
};

// ── theme ───────────────────────────────────────────────────────────────────────
// Telegram exposes the palette twice: as CSS variables on some clients and as the
// themeParams object everywhere. Mapping the object onto our own names covers both, and
// means app.css never has to know which client it is running on.

const THEME_KEYS = {
  bg_color: '--tg-theme-bg-color',
  text_color: '--tg-theme-text-color',
  hint_color: '--tg-theme-hint-color',
  link_color: '--tg-theme-link-color',
  button_color: '--tg-theme-button-color',
  button_text_color: '--tg-theme-button-text-color',
  secondary_bg_color: '--tg-theme-secondary-bg-color',
  section_bg_color: '--tg-theme-section-bg-color',
  section_separator_color: '--tg-theme-section-separator-color',
  destructive_text_color: '--tg-theme-destructive-text-color',
  accent_text_color: '--tg-theme-accent-text-color',
};

function applyTheme() {
  const params = (tg && tg.themeParams) || {};
  const root = document.documentElement;
  for (const [key, cssVar] of Object.entries(THEME_KEYS)) {
    if (params[key]) root.style.setProperty(cssVar, params[key]);
  }
}

// ── helpers ─────────────────────────────────────────────────────────────────────

const $ = (sel) => document.querySelector(sel);

function el(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

/** Escape anything that came from the database before it reaches innerHTML.
 *  Group titles and admin names are typed by people; a truck named `<img onerror>` would
 *  otherwise execute inside the panel of whoever opened the Groups tab. */
function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function num(value) {
  return Number(value || 0).toLocaleString('en-US');
}

function haptic(style) {
  try { tg.HapticFeedback.impactOccurred(style || 'light'); } catch (_) { /* older client */ }
}
function hapticSelect() {
  try { tg.HapticFeedback.selectionChanged(); } catch (_) { }
}
function hapticResult(ok) {
  try { tg.HapticFeedback.notificationOccurred(ok ? 'success' : 'error'); } catch (_) { }
}

function confirmAction(message) {
  return new Promise((resolve) => {
    if (tg && tg.showConfirm) tg.showConfirm(message, resolve);
    else resolve(window.confirm(message));
  });
}

/** Failures only. Telegram's showAlert is a modal with a button to dismiss, which is the
 *  right weight for "that unit is already linked" and far too much for "Saved". */
function alertMessage(message) {
  if (tg && tg.showAlert) tg.showAlert(message);
  else window.alert(message);
}

let toastTimer = null;

/** Successes. Says the write landed without asking for a tap to acknowledge it. */
function toast(text) {
  let node = $('#toast');
  if (!node) {
    node = el('<div class="toast" id="toast" role="status" aria-live="polite"></div>');
    document.body.appendChild(node);
  }
  node.textContent = text;
  requestAnimationFrame(() => node.classList.add('is-on'));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('is-on'), 1800);
}

/** Every request carries the signed initData; the server re-verifies it each time.
 *  A 401 means the credential expired, which no individual screen can recover from, so
 *  it takes over the whole page rather than failing one list quietly. */
async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options);
  opts.headers['X-Telegram-Init-Data'] = (tg && tg.initData) || '';
  if (opts.body) opts.headers['Content-Type'] = 'application/json';

  const res = await fetch(API + path, opts);
  if (res.status === 401) { showBlocker('expired'); throw new Error('unauthenticated'); }

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || 'Something went wrong.');
    err.code = data.error;
    err.data = data;
    throw err;
  }
  return data;
}

async function post(path, body) {
  return api(path, { method: 'POST', body: JSON.stringify(body || {}) });
}

// ── the read cache ──────────────────────────────────────────────────────────────
//
// Four tabs over six endpoints, and moving between them used to refetch everything and
// blank the screen while it waited. What is on screen is almost always still true, so
// the cache serves it immediately and the network call becomes a confirmation rather
// than a prerequisite.
//
// Nothing here ages out on a timer: a mutation invalidates exactly what it changed, and
// the refresh control drops the lot.

const cache = new Map();

async function load(path) {
  if (cache.has(path)) return cache.get(path);
  const data = await api(path);
  cache.set(path, data);
  return data;
}

function invalidate(prefix) {
  for (const key of Array.from(cache.keys())) {
    if (key.startsWith(prefix)) cache.delete(key);
  }
}

/** A screen's reads, plus the promise to re-check them if they came from cache. */
async function screenData(paths, opts) {
  const fromCache = paths.every((p) => cache.has(p));
  const data = await Promise.all(paths.map(load));
  if (fromCache && !opts.silent) revalidate(paths);
  return data;
}

async function revalidate(paths) {
  const epoch = state.epoch;
  busy(true);
  try {
    const fresh = await Promise.all(paths.map((p) => api(p)));
    let changed = false;
    paths.forEach((p, i) => {
      if (JSON.stringify(cache.get(p)) !== JSON.stringify(fresh[i])) {
        cache.set(p, fresh[i]);
        changed = true;
      }
    });
    // silent, so the re-render cannot start another round of this. And never while the
    // user is in a field on this screen — nobody asked for this refresh, and taking the
    // focus out of the search box mid-word to apply it is not a trade worth making.
    if (changed && epoch === state.epoch && !isTyping()) {
      renderCurrent({ keep: true, silent: true });
    }
  } catch (_) {
    // A failed background check leaves the cached screen exactly as it is. The user did
    // not ask for this request and must not be shown an error for it.
  } finally {
    busy(false);
  }
}

function isTyping() {
  const active = document.activeElement;
  return !!active && $('#main').contains(active)
    && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName);
}

let busyCount = 0;
function busy(on) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  const btn = $('#refresh');
  if (btn) btn.classList.toggle('is-busy', busyCount > 0);
}

// ── time ────────────────────────────────────────────────────────────────────────
//
// Every timestamp the server sends is already converted to Eastern — and then serialized
// with its offset, which means `new Date(iso)` renders it in the *phone's* zone. On a
// phone set to anything but Eastern the panel disagreed with the alert cards in the
// group chats it describes, and by a whole day around midnight. So the zone is named
// explicitly here, matching utils/webapp/api.py.

const ET = 'America/New_York';

function fmt(locale, opts) {
  // A WebView built without full ICU throws on a named zone. Falling back to the
  // device's own zone is wrong by the offset, but it is readable, and it only happens
  // where the alternative is a screen that doesn't render at all.
  try { return new Intl.DateTimeFormat(locale, Object.assign({ timeZone: ET }, opts)); }
  catch (_) { return new Intl.DateTimeFormat(locale, opts); }
}

const TIME_FMT = fmt('en-US', { hour: 'numeric', minute: '2-digit' });
const DATE_FMT = fmt('en-US', { month: 'short', day: 'numeric' });
// en-CA gives YYYY-MM-DD, which is only ever compared to another string from this same
// formatter — it is a bucket key, not something anyone reads.
const DAY_KEY_FMT = fmt('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit' });

function fmtTime(iso) {
  return TIME_FMT.format(new Date(iso));
}

/** Day bucket label for the feed's sticky headers, in Eastern time. */
function dayLabel(iso) {
  const key = DAY_KEY_FMT.format(new Date(iso));
  const now = Date.now();
  if (key === DAY_KEY_FMT.format(new Date(now))) return 'Today';
  if (key === DAY_KEY_FMT.format(new Date(now - 86400000))) return 'Yesterday';
  return DATE_FMT.format(new Date(iso));
}

// ── screen chrome ───────────────────────────────────────────────────────────────

const TITLES = {
  dashboard: 'Dashboard', groups: 'Groups', alerts: 'Alerts', admins: 'Admins',
};

function showBlocker(kind) {
  $('#app').hidden = true;
  const blocker = $('#blocker');
  blocker.hidden = false;
  if (kind === 'expired') {
    $('#blocker-emoji').textContent = '⌛';
    $('#blocker-title').textContent = 'Session expired';
    $('#blocker-text').textContent =
      'Close the panel and open it again from the bot to continue.';
    const action = $('#blocker-action');
    action.hidden = false;
    action.textContent = 'Close';
    action.onclick = () => tg && tg.close && tg.close();
  }
  if (kind === 'outside') showDiagnostics();
}

/** Print what the launch actually handed us.
 *
 *  "No credential" has several causes that look identical on screen — the bridge script
 *  never loaded, the client is too old to send one, or the button that opened this app
 *  isn't a kind that carries one — and they have different fixes. Rather than guess from
 *  a screenshot, show the facts. */
function showDiagnostics() {
  const w = window.Telegram;
  const app = w && w.WebApp;
  let unsafe = '(none)';
  try {
    const u = app && app.initDataUnsafe;
    unsafe = u && Object.keys(u).length ? JSON.stringify(u).slice(0, 220) : '(empty)';
  } catch (e) { unsafe = '(unreadable)'; }

  const lines = [
    `Telegram object : ${w ? 'yes' : 'NO — bridge script did not load'}`,
    `WebApp object   : ${app ? 'yes' : 'no'}`,
    `platform        : ${(app && app.platform) || '—'}`,
    `version         : ${(app && app.version) || '—'}`,
    `initData length : ${app && app.initData ? app.initData.length : 0}`,
    `initDataUnsafe  : ${unsafe}`,
  ];
  const box = $('#blocker-diag');
  box.textContent = lines.join('\n');
  box.hidden = false;
}

// Placeholders in the shape of the content they are standing in for, so the screen does
// not reflow when the data lands. Only ever shown when there is nothing cached — which,
// after the first visit to a tab, is never.

function skRows(n) {
  let html = '<div class="rows">';
  for (let i = 0; i < n; i++) {
    html += `<div class="row"><div class="row-main">
        <div class="sk sk-line" style="width:${52 + (i % 3) * 12}%"></div>
        <div class="sk sk-line sk-sm" style="width:${30 + (i % 2) * 10}%"></div>
      </div></div>`;
  }
  return html + '</div>';
}

const SKELETONS = {
  dashboard: () => `<div>
      <div class="sk" style="height:44px;border-radius:10px;margin-bottom:12px"></div>
      <div class="card"><div class="sk" style="height:46px;width:40%;margin:4px auto"></div>
        <div class="sk sk-sm" style="width:30%;margin:12px auto 2px"></div></div>
      <div class="tiles">
        <div class="sk" style="height:66px;border-radius:12px"></div>
        <div class="sk" style="height:66px;border-radius:12px"></div>
        <div class="sk" style="height:66px;border-radius:12px"></div>
      </div>
      <div class="card"><div class="sk sk-sm" style="width:28%;margin-bottom:14px"></div>
        <div class="sk sk-line"></div><div class="sk sk-line"></div>
        <div class="sk sk-line"></div></div>
    </div>`,
  groups: () => `<div><div class="sk" style="height:44px;border-radius:10px;
      margin-bottom:12px"></div>${skRows(5)}</div>`,
  alerts: () => `<div><div class="sk" style="height:34px;border-radius:17px;width:70%;
      margin-bottom:14px"></div>${skRows(6)}</div>`,
  admins: () => `<div>${skRows(3)}</div>`,
  detail: () => `<div>
      <div class="card"><div class="sk sk-line"></div><div class="sk sk-line"
        style="width:70%"></div><div class="sk sk-line" style="width:55%"></div></div>
      <div class="card"><div class="sk sk-sm" style="width:30%;margin-bottom:12px"></div>
        <div class="sk" style="height:34px;border-radius:17px"></div></div>
    </div>`,
};

function setSkeleton(kind) {
  const html = (SKELETONS[kind] || (() => '<div class="loading"><span class="spinner"></span></div>'))();
  $('#main').innerHTML = html;
}

/** Put a screen on the page.
 *
 *  `keep` is what makes a toggle feel like a toggle: the node is rebuilt from new data
 *  but the page stays exactly where it was. `restore` is the other direction — coming
 *  back to a tab or out of a detail view, to where that tab was left. */
function render(node, opts) {
  opts = opts || {};
  const y = opts.keep ? window.scrollY : (opts.restore || 0);
  if (!opts.keep) node.classList.add('screen-enter');

  const main = $('#main');
  main.innerHTML = '';
  main.appendChild(node);

  // Twice, and neither one is redundant. The first works because layout is available as
  // soon as the node is in the tree; the second catches a client that hadn't finished
  // laying out a long list yet. rAF alone is not enough — it does not run at all while
  // the WebView is in the background, and coming back to a screen scrolled to the top
  // is exactly what this call exists to prevent.
  window.scrollTo(0, y);
  requestAnimationFrame(() => window.scrollTo(0, y));
}

function rememberScroll() {
  if (!state.detail) state.scroll[state.tab] = window.scrollY;
}

/** Push a detail view over the current tab.
 *
 *  history.pushState is here for Android's hardware back button: without a history entry
 *  to pop, it closes the whole Mini App from a detail screen, which reads as a crash. */
function pushDetail(detail) {
  rememberScroll();
  state.detail = detail;
  state.epoch++;
  history.pushState({ detail: true }, '');
  if (tg && tg.BackButton) tg.BackButton.show();
  renderCurrent();
}

function popDetail() {
  state.detail = null;
  state.epoch++;
  if (tg && tg.BackButton) tg.BackButton.hide();
  hideMainButton();
  renderCurrent({ restore: state.scroll[state.tab] || 0 });
}

function selectTab(tab) {
  // Tapping the tab you are already on is a scroll-to-top everywhere else; matching that
  // is free, and it is the fastest way back up a long feed.
  if (tab === state.tab && !state.detail) {
    window.scrollTo({ top: 0, behavior: 'smooth' });
    return;
  }
  rememberScroll();
  state.tab = tab;
  state.detail = null;
  state.epoch++;
  if (tg && tg.BackButton) tg.BackButton.hide();
  hideMainButton();
  document.querySelectorAll('.tab').forEach((btn) => {
    const on = btn.dataset.tab === tab;
    btn.classList.toggle('is-active', on);
    btn.setAttribute('aria-current', on ? 'page' : 'false');
  });
  renderCurrent({ restore: state.scroll[tab] || 0 });
}

const DETAIL_VIEWS = { group: groupDetail, admin: adminDetail, addadmin: addAdminScreen };
const TAB_VIEWS = {
  dashboard: dashboardScreen, groups: groupsScreen,
  alerts: alertsScreen, admins: adminsScreen,
};

function renderCurrent(opts) {
  opts = opts || {};
  $('#screen-title').textContent = state.detail ? state.detail.title : TITLES[state.tab];

  const view = state.detail ? DETAIL_VIEWS[state.detail.type] : TAB_VIEWS[state.tab];
  if (!view) { popDetail(); return; }

  // A skeleton belongs to a screen that has nothing to show yet. Re-rendering after a
  // toggle, or restoring a cached tab, must not flash one.
  if (!opts.keep && !hasCachedScreen()) setSkeleton(state.detail ? 'detail' : state.tab);

  view(opts).catch((e) => {
    if (e.message === 'unauthenticated') return;
    render(errorScreen(e.message), opts);
  });
}

/** Whether the current screen can paint without waiting for the network. */
function hasCachedScreen() {
  if (state.detail) return cache.has('/groups') || cache.has('/admins');
  if (state.tab === 'alerts') return state.alertLoaded;
  return screenPaths().every((p) => cache.has(p));
}

function screenPaths() {
  if (state.tab === 'dashboard') return [statsPath()];
  if (state.tab === 'groups') return ['/groups', '/units'];
  if (state.tab === 'admins') return ['/admins'];
  return [];
}

function errorScreen(message) {
  const wrap = el(`<div class="empty"><span class="empty-emoji">⚠️</span>
      ${esc(message)}</div>`);
  const retry = el('<button class="btn btn-secondary" style="margin-top:16px">Try again</button>');
  retry.onclick = () => { haptic(); refreshAll(); };
  wrap.appendChild(retry);
  return wrap;
}

/** The refresh control: drop everything and rebuild the current screen from the wire. */
function refreshAll() {
  haptic();
  cache.clear();
  resetFeed();
  state.epoch++;
  busy(true);
  setSkeleton(state.detail ? 'detail' : state.tab);
  renderCurrent({ silent: true });
  // The screens resolve their own promises; this only clears the spinning state once
  // the work they queued has had a turn.
  setTimeout(() => busy(false), 400);
}

// ── Telegram's main button ──────────────────────────────────────────────────────
//
// Used for the one screen that is a form. It is the control a Telegram user expects to
// submit with, it sits above the keyboard rather than under it, and it cannot scroll
// out of reach. Where the client is too old to have it, the in-page button stays.

let mainButtonHandler = null;

function showMainButton(text, onClick) {
  const mb = tg && tg.MainButton;
  if (!mb || !mb.setText || !mb.show) return false;
  try {
    if (mainButtonHandler && mb.offClick) mb.offClick(mainButtonHandler);
    mainButtonHandler = onClick;
    mb.setText(text);
    if (mb.onClick) mb.onClick(onClick);
    mb.show();
    return true;
  } catch (_) {
    mainButtonHandler = null;
    return false;
  }
}

function hideMainButton() {
  const mb = tg && tg.MainButton;
  if (!mb) return;
  try {
    if (mainButtonHandler && mb.offClick) mb.offClick(mainButtonHandler);
    mainButtonHandler = null;
    if (mb.hide) mb.hide();
  } catch (_) { }
}

// ── screen 1: dashboard ─────────────────────────────────────────────────────────

const PERIODS = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days']];

function statsPath() {
  return `/stats?period=${encodeURIComponent(state.period)}`;
}

async function dashboardScreen(opts) {
  const [data] = await screenData([statsPath()], opts);
  const wrap = el('<div></div>');

  const seg = el('<div class="segmented" role="tablist"></div>');
  PERIODS.forEach(([key, label]) => {
    const b = el(`<button${key === state.period ? ' class="is-active"' : ''}
        aria-selected="${key === state.period}">${label}</button>`);
    b.onclick = () => { hapticSelect(); state.period = key; state.epoch++; renderCurrent(); };
    seg.appendChild(b);
  });
  wrap.appendChild(seg);

  // The muted warning comes before the numbers on purpose: a silent group is a problem
  // the numbers themselves cannot show, because its alerts are missing from them.
  if (data.groups.muted > 0) {
    const n = data.groups.muted;
    const card = el(`<button class="warn-card">
        <span style="font-size:22px">🔕</span>
        <div><b>${n} group${n > 1 ? 's are' : ' is'} muted</b>
          <small>${esc(data.groups.muted_titles.join(', '))}</small></div>
      </button>`);
    card.onclick = () => { haptic(); state.groupsQuery = ''; selectTab('groups'); };
    wrap.appendChild(card);
  }

  // Every number on this screen is a question with a list behind it, so every number is
  // a way into that list.
  const headline = el(`<button class="card headline">
      <div class="headline-number">${num(data.totals.total)}</div>
      <div class="headline-label">alerts · ${esc(data.period.label)}</div>
    </button>`);
  headline.onclick = () => { haptic(); openAlerts({}); };
  wrap.appendChild(headline);

  const tiles = el('<div class="tiles"></div>');
  [
    [data.totals.units, 'Units', false, () => selectTab('groups')],
    [data.totals.speeding, 'Speeding', false, () => openAlerts({ type: 'speeding' })],
    // A crash count is only worth colouring when there is one to colour.
    [data.totals.crashes, 'Crashes', data.totals.crashes > 0,
     () => openAlerts({ type: 'crash' })],
  ].forEach(([value, label, alarm, go]) => {
    const tile = el(`<button class="tile${alarm ? ' tile-alarm' : ''}">
        <div class="tile-value">${num(value)}</div>
        <div class="tile-label">${label}</div></button>`);
    tile.onclick = () => { haptic(); go(); };
    tiles.appendChild(tile);
  });
  wrap.appendChild(tiles);

  if (data.by_day && data.by_day.length > 1) wrap.appendChild(trendCard(data.by_day));

  if (data.top_units.length) {
    const peak = data.top_units[0].total || 1;
    const card = el(`<div class="card"><div class="card-head">
        <div class="card-title">Top units</div>
        <span class="card-head-note">tap to filter</span></div></div>`);
    data.top_units.forEach((u) => {
      const row = el(`<button class="bar-row">
          <span class="bar-name">🚛 ${esc(u.vehicle_number)}</span>
          <span class="bar-track"><span class="bar-fill"
                style="width:${Math.round((u.total / peak) * 100)}%"></span></span>
          <span class="bar-count">${num(u.total)}</span></button>`);
      row.onclick = () => { haptic(); openAlerts({ unit: u.vehicle_number }); };
      card.appendChild(row);
    });
    wrap.appendChild(card);
  }

  if (data.by_type.length) {
    const peak = data.by_type[0].total || 1;
    const card = el(`<div class="card"><div class="card-head">
        <div class="card-title">By type</div>
        <span class="card-head-note">tap to filter</span></div></div>`);
    data.by_type.forEach((t) => {
      const row = el(`<button class="bar-row">
          <span class="bar-name">${esc(t.emoji)} ${esc(t.label)}</span>
          <span class="bar-track"><span class="bar-fill"
                style="width:${Math.round((t.total / peak) * 100)}%"></span></span>
          <span class="bar-count">${num(t.total)}</span></button>`);
      row.onclick = () => { haptic(); openAlerts({ type: t.event_type }); };
      card.appendChild(row);
    });
    wrap.appendChild(card);
  }

  if (!data.totals.total) {
    wrap.appendChild(el(`<div class="empty"><span class="empty-emoji">✅</span>
      No alerts in this period.</div>`));
  }
  render(wrap, opts);
}

/** Alerts per day, with the value for one column readable underneath it.
 *
 *  The columns used to carry a `title` attribute, which needs a mouse to show. On the
 *  device this panel is actually used on, the number behind a bar was unreachable. */
function trendCard(byDay) {
  const peak = Math.max(...byDay.map((d) => d.total), 1);
  const card = el('<div class="card"><div class="card-title">Per day</div></div>');
  const trend = el('<div class="trend"></div>');
  const readout = el('<div class="trend-read"></div>');

  // 30 columns cannot each carry a legible label, and "M T W T F S S M T W…" repeated
  // four times says nothing anyway. Roughly six markers, evenly spaced.
  const every = Math.max(1, Math.round(byDay.length / 6));

  byDay.forEach((d, i) => {
    const when = new Date(d.day + 'T12:00:00');
    const narrow = when.toLocaleDateString('en-US', { weekday: 'narrow' });
    const short = when.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    const label = byDay.length <= 10 ? narrow
      : (i % every === 0 || i === byDay.length - 1 ? String(when.getDate()) : '');

    const col = el(`<button class="trend-col" aria-label="${esc(short)}: ${d.total}">
        <div class="trend-bar" style="height:${Math.round((d.total / peak) * 100)}%"></div>
        <div class="trend-day">${esc(label)}</div></button>`);
    col.onclick = () => {
      hapticSelect();
      trend.querySelectorAll('.trend-col').forEach((c) => c.classList.remove('is-on'));
      col.classList.add('is-on');
      readout.textContent = `${short} · ${num(d.total)} alert${d.total === 1 ? '' : 's'}`;
    };
    trend.appendChild(col);
  });

  const busiest = byDay.reduce((a, b) => (b.total > a.total ? b : a), byDay[0]);
  readout.textContent = `Busiest: ${new Date(busiest.day + 'T12:00:00')
    .toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} · ${num(busiest.total)}`;

  card.appendChild(trend);
  card.appendChild(readout);
  return card;
}

// ── screen 2: groups ────────────────────────────────────────────────────────────

async function groupsScreen(opts) {
  const [groups, roster] = await screenData(['/groups', '/units'], opts);
  const wrap = el('<div></div>');

  const searchWrap = el(`<div class="search-wrap">
      <span class="search-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/>
        <path d="m20 20-3.5-3.5"/></svg></span>
      <input class="input" placeholder="Search groups or units" inputmode="search"
             value="${esc(state.groupsQuery)}">
      <button class="search-clear" aria-label="Clear search" hidden>✕</button>
    </div>`);
  const search = searchWrap.querySelector('input');
  const clear = searchWrap.querySelector('.search-clear');
  wrap.appendChild(searchWrap);

  const head = el('<div class="list-head"></div>');
  wrap.appendChild(head);

  const listWrap = el('<div></div>');
  wrap.appendChild(listWrap);

  function renderList(query) {
    listWrap.innerHTML = '';
    clear.hidden = !query;
    const q = query.trim().toLowerCase();
    const matches = (g) => !q
      || (g.title || '').toLowerCase().includes(q)
      || (g.vehicle_number || '').toLowerCase().includes(q);
    const filtered = groups.filter(matches);
    const muted = groups.filter((g) => !g.enabled).length;

    // The noun agrees with the last number in the phrase — "1 of 6 groups", not
    // "1 of 6 group".
    const shown = q ? `${num(filtered.length)} of ${num(groups.length)}` : num(filtered.length);
    const counted = q ? groups.length : filtered.length;
    head.innerHTML = `<span>${shown} group${counted === 1 ? '' : 's'}</span>`
      + (muted ? `<span style="color:var(--destructive)">${num(muted)} muted</span>` : '');

    if (!groups.length) {
      listWrap.appendChild(el(`<div class="empty"><span class="empty-emoji">👥</span>
        No groups registered yet.<br>Add the bot to a truck's group to get started.</div>`));
    } else if (!filtered.length) {
      listWrap.appendChild(el(`<div class="empty"><span class="empty-emoji">🔍</span>
        No groups match "${esc(query)}".</div>`));
    } else {
      const rows = el('<div class="rows"></div>');
      filtered.forEach((g) => rows.appendChild(groupRow(g)));
      listWrap.appendChild(rows);
    }

    if (state.boot.crash_group_id) {
      listWrap.appendChild(el(`<div class="note">💥 Crash alerts go to a dedicated chat
        configured in the deployment settings, not listed here.</div>`));
    }

    // Samsara units with no group at all — the coverage gap no per-group screen can show,
    // since a missing group has nowhere in the groups list to appear.
    if (roster.available) {
      const unconnected = roster.units.filter((u) => !u.linked
        && (!q || u.name.toLowerCase().includes(q)));
      if (unconnected.length) {
        const card = el(`<div class="card">
            <div class="card-title">Not connected to a group (${unconnected.length})</div>
          </div>`);
        const chips = el('<div class="chips"></div>');
        unconnected.forEach((u) => {
          chips.appendChild(el(`<span class="chip is-static">${esc(u.name)}</span>`));
        });
        card.appendChild(chips);
        card.appendChild(el(`<div class="note">These units have no Telegram group receiving
          their alerts. Add the bot to that unit's group chat, then run /setunit there.</div>`));
        listWrap.appendChild(card);
      }
    }
  }

  /** One row: the name on its own line so a long one is readable, the badges under it,
   *  and the mute switch on the row itself — muting used to cost two taps and a screen
   *  change, which is a lot for the control people reach for most. */
  function groupRow(g) {
    const badge = g.is_main
      ? '<span class="badge badge-main">ALL UNITS</span>'
      : `<span class="badge badge-unit">${esc(g.vehicle_number || 'no unit')}</span>`;
    const filter = g.filter_mode === 'all' ? 'All events'
      : `${g.event_types.length} event type${g.event_types.length === 1 ? '' : 's'}`;

    const row = el(`<div class="row"></div>`);
    const main = el(`<button class="row-main">
        <div class="row-title">${esc(g.title || 'Untitled group')}</div>
        <div class="row-meta">${badge}
          <span class="row-sub">${filter} · ${num(g.alerts_7d)} this week</span></div>
      </button>`);
    main.onclick = () => {
      haptic();
      pushDetail({ type: 'group', id: g.telegram_group_id, title: g.title || 'Group' });
    };
    row.appendChild(main);

    const sw = el(`<button class="switch${g.enabled ? ' is-on' : ''}" role="switch"
        aria-checked="${!!g.enabled}" aria-label="Alerts for this group"></button>`);
    // Redrawing the list keeps the "N muted" count above it honest. The search field is
    // outside listWrap, so whatever was typed — and the focus — survives.
    sw.onclick = () => toggleGroupEnabled(g, sw, () => renderList(search.value));
    row.appendChild(sw);
    return row;
  }

  search.oninput = () => { state.groupsQuery = search.value; renderList(search.value); };
  clear.onclick = () => {
    haptic();
    state.groupsQuery = '';
    search.value = '';
    renderList('');
    search.focus();
  };
  renderList(state.groupsQuery);

  render(wrap, opts);
}

/** Flip a group's alerts, updating the row rather than the screen.
 *
 *  The cached group object is the same object the list was built from, so writing the
 *  new value into it keeps the cache and the DOM in step without a refetch. Only the
 *  dashboard is dropped, because its muted count just changed. */
async function toggleGroupEnabled(g, sw, after) {
  const next = !g.enabled;
  sw.classList.toggle('is-on', next);
  sw.setAttribute('aria-checked', String(next));
  sw.disabled = true;
  haptic();
  try {
    await post(`/groups/${g.telegram_group_id}/enabled`, { enabled: next });
    g.enabled = next;
    invalidate('/stats');
    hapticResult(true);
    toast(next ? 'Alerts on' : 'Alerts muted');
    if (after) after();
  } catch (e) {
    sw.classList.toggle('is-on', !next);
    sw.setAttribute('aria-checked', String(!next));
    hapticResult(false);
    alertMessage(e.message);
  } finally {
    sw.disabled = false;
  }
}

async function groupDetail(opts) {
  const id = state.detail.id;
  const [groups, roster] = await screenData(['/groups', '/units'], opts);
  const g = groups.find((x) => x.telegram_group_id === id);
  if (!g) { popDetail(); return; }

  const wrap = el('<div></div>');

  // — alerts on/off —
  const card = el('<div class="card"></div>');
  const field = el(`<div class="field"><span class="field-label">Alerts</span></div>`);
  const status = el(`<span class="field-value ${g.enabled ? 'is-on' : 'is-off'}"
      style="margin-right:10px">${g.enabled ? 'On' : 'Muted'}</span>`);
  const sw = el(`<button class="switch${g.enabled ? ' is-on' : ''}" role="switch"
      aria-checked="${!!g.enabled}" aria-label="Alerts"></button>`);
  sw.onclick = async () => {
    await toggleGroupEnabled(g, sw);
    status.textContent = g.enabled ? 'On' : 'Muted';
    status.className = `field-value ${g.enabled ? 'is-on' : 'is-off'}`;
  };
  field.appendChild(status);
  field.appendChild(sw);
  card.appendChild(field);
  card.appendChild(el(`<div class="field"><span class="field-label">Alerts this week</span>
      <span class="field-value">${num(g.alerts_7d)}</span></div>`));
  card.appendChild(el(`<div class="field"><span class="field-label">Chat ID</span>
      <span class="field-value">${esc(String(id))}</span></div>`));
  wrap.appendChild(card);

  // — unit —
  if (!g.is_main) {
    const unitCard = el('<div class="card"><div class="card-title">Unit</div></div>');
    if (roster.available) {
      // A picker, not a text box: the roster's spelling is the only one alert routing
      // matches, so handing over the exact strings removes the whole class of typo.
      const select = el('<select class="input" aria-label="Unit"></select>');
      select.appendChild(el(`<option value="">— choose a unit —</option>`));
      roster.units.forEach((u) => {
        const taken = u.linked && u.name !== g.vehicle_number ? ' (already linked)' : '';
        const sel = u.name === g.vehicle_number ? ' selected' : '';
        select.appendChild(el(
          `<option value="${esc(u.name)}"${sel}>${esc(u.name)}${taken}</option>`));
      });
      select.onchange = () => saveUnit(id, select.value);
      const selectWrap = el('<div class="select-wrap"></div>');
      selectWrap.appendChild(select);
      unitCard.appendChild(selectWrap);
    } else {
      const input = el(`<input class="input" value="${esc(g.vehicle_number || '')}"
                         placeholder="e.g. 1234" aria-label="Unit">`);
      unitCard.appendChild(input);
      const save = el('<button class="btn btn-block" style="margin-top:10px">Save unit</button>');
      save.onclick = () => saveUnit(id, input.value);
      unitCard.appendChild(save);
      unitCard.appendChild(el(`<div class="note note-warn">${esc(roster.reason)}</div>`));
    }
    wrap.appendChild(unitCard);
  }

  // — event filter —
  const evCard = el('<div class="card"><div class="card-title">Event types</div></div>');
  const chips = el('<div class="chips"></div>');
  const note = el('<div class="note"></div>');
  evCard.appendChild(chips);
  evCard.appendChild(note);

  /** Redraw just the chips from the group's current filter.
   *
   *  Each tap is still a round trip — the collapse rule lives on the server and is
   *  deliberately not reimplemented here — but the response carries the authoritative
   *  list, so the answer lands on these chips instead of rebuilding the screen. */
  function drawChips() {
    chips.innerHTML = '';
    const selected = new Set(g.event_types);
    const all = g.filter_mode === 'all';

    const allChip = el(`<button class="chip${all ? ' is-on' : ''}"
        aria-pressed="${all}">All types</button>`);
    allChip.onclick = () => toggleEvent(g, { action: 'all' }, drawChips);
    chips.appendChild(allChip);

    state.boot.event_types.forEach((t) => {
      const on = all || selected.has(t.type);
      const chip = el(`<button class="chip${on ? ' is-on' : ''}" aria-pressed="${on}"
          >${esc(t.emoji)} ${esc(t.label)}</button>`);
      chip.onclick = () => toggleEvent(g, { action: 'toggle', event_type: t.type }, drawChips);
      chips.appendChild(chip);
    });

    note.textContent = all
      ? 'Receiving every event type, including any added later.'
      : 'Only the highlighted types are delivered to this group.';
  }
  drawChips();
  wrap.appendChild(evCard);

  // — danger zone —
  if (state.boot.me.is_super && !g.is_main) {
    const zone = el('<div class="danger-zone"></div>');
    const btn = el('<button class="btn btn-danger btn-block">Remove group</button>');
    btn.onclick = async () => {
      const ok = await confirmAction(
        `Remove "${g.title || 'this group'}"? It will stop receiving alerts until the bot is re-added.`);
      if (!ok) return;
      try {
        await post(`/groups/${id}/remove`, { confirm: true });
        invalidate('/groups');
        invalidate('/units');
        invalidate('/stats');
        hapticResult(true);
        toast('Group removed');
        popDetail();
      } catch (e) { hapticResult(false); alertMessage(e.message); }
    };
    zone.appendChild(btn);
    wrap.appendChild(zone);
  }

  render(wrap, opts);
}

async function saveUnit(id, unit) {
  if (!unit) return;
  busy(true);
  try {
    const res = await post(`/groups/${id}/unit`, { unit });
    invalidate('/groups');
    invalidate('/units');
    hapticResult(true);
    if (res.note) alertMessage(res.note);
    else toast('Unit saved');
    renderCurrent({ keep: true });
  } catch (e) {
    hapticResult(false);
    // The server sends "did you mean" candidates with a 409; showing them is the whole
    // point of the endpoint returning them rather than a bare rejection.
    const hint = (e.data && e.data.suggestions && e.data.suggestions.length)
      ? `\n\nDid you mean: ${e.data.suggestions.join(', ')}`
      : '';
    alertMessage(e.message + hint);
    renderCurrent({ keep: true });
  } finally {
    busy(false);
  }
}

async function toggleEvent(g, body, redraw) {
  haptic();
  try {
    const res = await post(`/groups/${g.telegram_group_id}/events`, body);
    // The response carries the authoritative list — the collapse rule that turns a full
    // allowlist back into "all types" lives in next_event_filter on the server, and is
    // deliberately not reimplemented here where it would drift.
    g.event_types = res.event_types;
    g.filter_mode = res.filter_mode;
    hapticResult(true);
    redraw();
  } catch (e) { hapticResult(false); alertMessage(e.message); }
}

// ── screen 3: alerts ────────────────────────────────────────────────────────────

// The feed is the one screen that pages, so it keeps its own loaded items rather than
// caching by URL. These hold the nodes a "load more" appends into, so a second page is
// added to the list instead of rebuilding it — rebuilding meant the list you were
// reading jumped back to the top the moment it grew.
let feedNode = null;
let feedMore = null;
let feedRows = null;
let feedDay = null;
let feedLoading = false;

function resetFeed() {
  state.alertItems = [];
  state.alertNext = null;
  state.alertLoaded = false;
}

function openAlerts(filter) {
  state.alertFilter = Object.assign({ type: '', unit: '' }, filter || {});
  resetFeed();
  state.scroll.alerts = 0;
  // Not selectTab: changing the filter while already on this tab has to re-render, and
  // selectTab treats "the tab you are on" as a scroll-to-top and returns.
  if (state.tab === 'alerts' && !state.detail) {
    state.epoch++;
    setSkeleton('alerts');
    renderCurrent();
  } else {
    selectTab('alerts');
  }
}

/** "All", then the four types that account for nearly every alert, then the rest of the
 *  catalog. Crash is named literally because it is deliberately absent from
 *  GROUP_FILTER_TYPES — a crash is never a per-group filter — but it is the one alert
 *  anybody opens this feed to find. */
function alertFilterChips() {
  const catalog = (state.boot && state.boot.event_types) || [];
  const byType = new Map(catalog.map((t) => [t.type, t]));
  const out = [{ type: '', label: 'All', emoji: '' },
               { type: 'crash', label: 'Crash', emoji: '💥' }];
  const seen = new Set(['', 'crash']);
  ['speeding', 'hard_brake', 'cell_phone'].forEach((t) => {
    if (byType.has(t)) { out.push(byType.get(t)); seen.add(t); }
  });
  catalog.forEach((t) => { if (!seen.has(t.type)) out.push(t); });
  return out;
}

/** Four tones over sixteen event types. The point is not to encode the type in colour —
 *  the label already does that — it is that a crash cannot look like a phone call in a
 *  column of sixty rows. */
function alertTone(type) {
  if (type === 'crash') return 'crash';
  if (type === 'speeding') return 'speeding';
  if (/brake|harsh|collision|stop_sign|near_miss|unsafe/.test(type)) return 'brake';
  if (/cell_phone|inattentive|drowsy|drowsiness|seat_belt|cam/.test(type)) return 'phone';
  return '';
}

function alertRow(item) {
  const sev = item.severity
    ? ` · ${esc(String(item.severity).replace(/^./, (c) => c.toUpperCase()))}` : '';
  const filtered = !!state.alertFilter.unit;
  const tag = filtered ? 'div' : 'button';

  const row = el(`<${tag} class="row alert-row" data-tone="${alertTone(item.event_type)}">
      <span class="alert-icon" aria-hidden="true">${esc(item.emoji)}</span>
      <div class="row-main" style="pointer-events:none">
        <div class="row-title">${esc(item.label)}</div>
        <div class="row-sub"><span class="alert-unit">${esc(item.vehicle_number)}</span>${sev}</div>
      </div>
      <span class="row-side">${esc(fmtTime(item.occurred_at))}</span>
    </${tag}>`);
  // Tapping a row narrows the feed to that unit, which is the follow-up question the
  // row itself raises. Already filtered by unit, there is nothing to narrow to.
  if (!filtered) row.onclick = () => { haptic(); openAlerts({ unit: item.vehicle_number }); };
  return row;
}

function appendAlerts(items) {
  items.forEach((item) => {
    const day = dayLabel(item.occurred_at);
    if (day !== feedDay) {
      feedDay = day;
      feedNode.appendChild(el(`<div class="day-header">${esc(day)}</div>`));
      feedRows = el('<div class="rows"></div>');
      feedNode.appendChild(feedRows);
    }
    feedRows.appendChild(alertRow(item));
  });
}

async function alertsScreen(opts) {
  if (!state.alertLoaded) {
    const params = new URLSearchParams({ limit: '50' });
    if (state.alertFilter.type) params.set('type', state.alertFilter.type);
    if (state.alertFilter.unit) params.set('unit', state.alertFilter.unit);
    const data = await api(`/alerts?${params.toString()}`);
    state.alertItems = data.items;
    state.alertNext = data.next;
    state.alertLoaded = true;
  }

  const wrap = el('<div></div>');

  const bar = el('<div class="filter-bar"></div>');
  // First, not last: the bar scrolls, and appended after sixteen type chips the one
  // filter the user set themselves was off the right edge — invisible, and with no way
  // to clear it without knowing to scroll for it.
  if (state.alertFilter.unit) {
    const chip = el(`<button class="chip is-on">🚛 ${esc(state.alertFilter.unit)} ✕</button>`);
    chip.onclick = () => { haptic(); openAlerts({ type: state.alertFilter.type }); };
    bar.appendChild(chip);
  }
  alertFilterChips().forEach((f) => {
    const on = state.alertFilter.type === f.type;
    const label = f.emoji ? `${esc(f.emoji)} ${esc(f.label)}` : esc(f.label);
    const chip = el(`<button class="chip${on ? ' is-on' : ''}" aria-pressed="${on}">${label}</button>`);
    chip.onclick = () => {
      hapticSelect();
      openAlerts({ type: f.type, unit: state.alertFilter.unit });
    };
    bar.appendChild(chip);
  });
  wrap.appendChild(bar);

  if (!state.alertItems.length) {
    wrap.appendChild(el(`<div class="empty"><span class="empty-emoji">🔕</span>
      No alerts match this filter.</div>`));
    render(wrap, opts);
    return;
  }

  feedNode = el('<div></div>');
  feedMore = el('<div></div>');
  feedDay = null;
  feedRows = null;
  wrap.appendChild(feedNode);
  wrap.appendChild(feedMore);

  appendAlerts(state.alertItems);
  drawMore();
  render(wrap, opts);
}

function drawMore() {
  feedMore.innerHTML = '';
  if (!state.alertNext) {
    if (state.alertItems.length > 20) {
      feedMore.appendChild(el(`<div class="note" style="text-align:center">
        That's every alert in this filter.</div>`));
    }
    return;
  }

  const more = el(`<button class="btn btn-secondary btn-block"
      style="margin-top:14px">Load more</button>`);
  more.onclick = () => loadMoreAlerts(more);
  feedMore.appendChild(more);

  // Auto-load when the button comes into view, so reading down a long feed doesn't stop
  // every fifty rows. The button is still a button: it is the control on a client
  // without IntersectionObserver, and the one that works when the WebView is in the
  // background, where the observer is suspended along with every animation frame.
  if (typeof IntersectionObserver === 'function') {
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { io.disconnect(); loadMoreAlerts(more); }
    }, { rootMargin: '300px' });
    io.observe(more);
  }
}

async function loadMoreAlerts(button) {
  if (feedLoading || !state.alertNext) return;
  feedLoading = true;
  button.textContent = 'Loading…';
  busy(true);
  try {
    const params = new URLSearchParams({ limit: '50' });
    if (state.alertFilter.type) params.set('type', state.alertFilter.type);
    if (state.alertFilter.unit) params.set('unit', state.alertFilter.unit);
    params.set('before_ts', state.alertNext.before_ts);
    params.set('before_id', state.alertNext.before_id);

    const data = await api(`/alerts?${params.toString()}`);
    state.alertItems = state.alertItems.concat(data.items);
    state.alertNext = data.next;
    // Appended, not re-rendered: the rows already on screen stay exactly where they are.
    appendAlerts(data.items);
    drawMore();
  } catch (e) {
    button.textContent = 'Load more';
    if (e.message !== 'unauthenticated') alertMessage(e.message);
  } finally {
    feedLoading = false;
    busy(false);
  }
}

// ── screen 4: admins ────────────────────────────────────────────────────────────

async function adminsScreen(opts) {
  const [admins] = await screenData(['/admins'], opts);
  const wrap = el('<div></div>');

  const active = admins.filter((a) => a.is_active).length;
  wrap.appendChild(el(`<div class="list-head">
      <span>${num(admins.length)} admin${admins.length === 1 ? '' : 's'}</span>
      <span>${num(active)} active</span></div>`));

  const rows = el('<div class="rows"></div>');
  admins.forEach((a) => {
    const badges = (a.is_super ? '<span class="badge badge-super">SUPER</span>' : '')
      + (a.is_active ? '' : '<span class="badge badge-muted">INACTIVE</span>');
    const handle = a.username ? `@${esc(a.username)}` : `ID ${esc(String(a.telegram_id))}`;
    const row = el(`<button class="row">
        <div class="row-main">
          <div class="row-title">${esc(a.full_name || 'Unknown')}</div>
          <div class="row-meta">${badges}<span class="row-sub">${handle}</span></div>
        </div>
        <span class="row-chevron">›</span>
      </button>`);
    row.onclick = () => {
      haptic();
      pushDetail({ type: 'admin', id: a.id, title: a.full_name || 'Admin' });
    };
    rows.appendChild(row);
  });
  wrap.appendChild(rows);

  if (state.boot.me.is_super) {
    const add = el('<button class="btn btn-block" style="margin-top:14px">+ Add admin</button>');
    add.onclick = () => { haptic(); pushDetail({ type: 'addadmin', title: 'Add admin' }); };
    wrap.appendChild(add);
  } else {
    wrap.appendChild(el('<div class="note">Only a super admin can change this list.</div>'));
  }
  render(wrap, opts);
}

async function adminDetail(opts) {
  const [admins] = await screenData(['/admins'], opts);
  const a = admins.find((x) => x.id === state.detail.id);
  if (!a) { popDetail(); return; }

  const isSuper = state.boot.me.is_super;
  const isSelf = a.telegram_id === state.boot.me.telegram_id;
  const wrap = el('<div></div>');

  wrap.appendChild(el(`<div class="card">
      <div class="field"><span class="field-label">Name</span>
        <span class="field-value">${esc(a.full_name || '—')}</span></div>
      <div class="field"><span class="field-label">Username</span>
        <span class="field-value">${a.username ? '@' + esc(a.username) : '—'}</span></div>
      <div class="field"><span class="field-label">Telegram ID</span>
        <span class="field-value">${esc(String(a.telegram_id))}</span></div>
      <div class="field"><span class="field-label">Role</span>
        <span class="field-value">${a.is_super ? 'Super admin' : 'Admin'}</span></div>
      <div class="field"><span class="field-label">Status</span>
        <span class="field-value ${a.is_active ? 'is-on' : 'is-off'}"
          >${a.is_active ? 'Active' : 'Inactive'}</span></div>
    </div>`));

  if (!isSuper) {
    wrap.appendChild(el('<div class="note">Only a super admin can change this.</div>'));
    render(wrap, opts);
    return;
  }

  // Guards render as disabled buttons with the reason spelled out, rather than being
  // hidden. The bot answers with these exact sentences, so the rules are already known to
  // exist — hiding the control just makes the panel look broken.
  const actions = el('<div class="card"></div>');

  const activeBtn = el(`<button class="btn btn-secondary btn-block"
      style="margin-bottom:10px">${a.is_active ? 'Deactivate' : 'Activate'}</button>`);
  if (a.is_super) disable(activeBtn, "Super admins can't be deactivated here.");
  else if (isSelf && a.is_active) disable(activeBtn, "You can't deactivate yourself.");
  else activeBtn.onclick = () => mutateAdmin(a.id, { is_active: !a.is_active },
                                             a.is_active ? 'Deactivated' : 'Activated');
  actions.appendChild(activeBtn);

  if (!a.is_super) {
    const promote = el('<button class="btn btn-secondary btn-block" style="margin-bottom:10px">⭐ Make super admin</button>');
    if (!a.is_active) disable(promote, 'Activate this admin before promoting them.');
    else promote.onclick = async () => {
      if (await confirmAction(`Make ${a.full_name || 'this admin'} a super admin?`)) {
        mutateAdmin(a.id, { is_super: true }, 'Promoted');
      }
    };
    actions.appendChild(promote);
  }
  wrap.appendChild(actions);

  const remove = el('<button class="btn btn-danger btn-block">Remove admin</button>');
  if (isSelf) disable(remove, "You can't remove yourself.");
  else if (a.is_super) disable(remove, "Super admins can't be removed here.");
  else remove.onclick = async () => {
    if (!await confirmAction(`Remove ${a.full_name || 'this admin'}?`)) return;
    try {
      await post(`/admins/${a.id}/remove`, { confirm: true });
      invalidate('/admins');
      hapticResult(true);
      toast('Admin removed');
      popDetail();
    } catch (e) { hapticResult(false); alertMessage(e.message); }
  };
  const zone = el('<div class="danger-zone"></div>');
  zone.appendChild(remove);
  wrap.appendChild(zone);

  render(wrap, opts);
}

function disable(button, reason) {
  button.classList.add('is-guarded');
  button.setAttribute('aria-disabled', 'true');
  button.title = reason;
  // Kept tappable on purpose: the reason is the useful part, and a button that does
  // nothing at all when pressed reads as a broken panel.
  button.onclick = () => alertMessage(reason);
}

async function mutateAdmin(id, body, done) {
  busy(true);
  try {
    await post(`/admins/${id}/update`, body);
    invalidate('/admins');
    hapticResult(true);
    if (done) toast(done);
    renderCurrent({ keep: true });
  } catch (e) { hapticResult(false); alertMessage(e.message); }
  finally { busy(false); }
}

async function addAdminScreen(opts) {
  const wrap = el(`<div>
      <div class="card">
        <div class="card-title">Telegram ID</div>
        <input class="input" id="new-admin-id" inputmode="numeric" autocomplete="off"
               placeholder="e.g. 123456789">
        <div class="note">The person has to have pressed /start on this bot at least
          once, so the bot knows who that id belongs to.</div>
      </div>
    </div>`);
  const input = wrap.querySelector('#new-admin-id');

  const submit = async () => {
    const value = input.value.trim();
    if (!/^\d+$/.test(value)) {
      hapticResult(false);
      alertMessage('A Telegram ID is a number — check the value and try again.');
      return;
    }
    busy(true);
    try {
      await post('/admins', { telegram_id: Number(value) });
      invalidate('/admins');
      hapticResult(true);
      toast('Admin added');
      popDetail();
    } catch (e) { hapticResult(false); alertMessage(e.message); }
    finally { busy(false); }
  };

  // Telegram's own submit control where the client has one; otherwise a button in the
  // page, so this screen is never left without a way to finish.
  if (!showMainButton('Add admin', submit)) {
    const save = el('<button class="btn btn-block">Add admin</button>');
    save.onclick = submit;
    wrap.appendChild(save);
  }

  render(wrap, opts);
  input.focus();
}

// ── boot ────────────────────────────────────────────────────────────────────────

/** Wait for the bridge script, which is fetched from telegram.org and may still be in
 *  flight. Short budget: if it isn't there in two seconds it isn't coming, and a
 *  dispatcher staring at a blank panel is worse than being told what went wrong. */
async function waitForTelegram(ms = 2000) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (window.Telegram && window.Telegram.WebApp) return window.Telegram.WebApp;
    await new Promise((r) => setTimeout(r, 50));
  }
  return (window.Telegram && window.Telegram.WebApp) || null;
}

/** Day headers stick underneath the topbar, so the topbar's real height has to be a
 *  number the stylesheet can use — it varies with the client's font scaling. */
function measureTopbar() {
  const bar = document.querySelector('.topbar');
  if (bar && bar.offsetHeight) {
    document.documentElement.style.setProperty('--topbar-h', `${bar.offsetHeight}px`);
  }
}

async function boot() {
  tg = await waitForTelegram();

  // ready() before the credential is read, not after: it is how the client is told the
  // page is up, and some clients only settle the launch parameters once it has been
  // called. Cheap to do first, and it removes a whole class of "empty on first paint".
  if (tg) {
    applyTheme();
    try { tg.ready(); } catch (_) { /* older client */ }
  }

  // No initData means nothing here can be authorized — every request would 401. Say so
  // once, with the diagnostics, instead of failing four screens in a row.
  if (!tg || !tg.initData) { showBlocker('outside'); return; }

  tg.expand();
  if (tg.setHeaderColor) { try { tg.setHeaderColor('secondary_bg_color'); } catch (_) { } }
  if (tg.disableVerticalSwipes) { try { tg.disableVerticalSwipes(); } catch (_) { } }
  if (tg.onEvent) {
    tg.onEvent('themeChanged', applyTheme);
    tg.onEvent('backButtonClicked', () => history.back());
    tg.onEvent('viewportChanged', measureTopbar);
  }
  window.addEventListener('popstate', () => { if (state.detail) popDetail(); });
  window.addEventListener('resize', measureTopbar);

  document.querySelectorAll('.tab').forEach((btn) => {
    btn.onclick = () => { hapticSelect(); selectTab(btn.dataset.tab); };
  });
  $('#refresh').onclick = refreshAll;

  // Shown before the first request rather than after it. Bootstrap over a truck-stop
  // connection is a second or two, and a blank white screen for that long is the part
  // of a Mini App that gets described as "it didn't open".
  $('#app').hidden = false;
  setSkeleton('dashboard');
  measureTopbar();

  try {
    state.boot = await load('/bootstrap');
  } catch (e) {
    if (e.message === 'unauthenticated') return;
    render(errorScreen(e.message), {});
    return;
  }

  $('#company-name').textContent = state.boot.company.name;
  measureTopbar();
  renderCurrent();
}

boot();
