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
  tab: 'fleet',
  detail: null,        // the detail view on top, or null when a tab is showing
  details: [],         // the stack under it: Admins -> one admin is two deep
  // The Report tab's one control. Last week rather than a rolling window by default:
  // this screen's purpose is the file at the bottom of it, and last week is the only
  // period that is both complete and the one a safety manager is asked about.
  period: 'last_week',
  groupsQuery: '',
  groupsFilter: 'active',  // which of the three Groups sections is showing
  // Expanded state of the "show the rest" control on the Report screen.
  typesOpen: false,
  // An unsaved event-type selection on a group screen: {id, picked}. See groupDetail.
  filterDraft: null,
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

  // Two surface levels are the whole visual structure of this panel: the page is
  // secondary_bg_color and the cards on it are bg_color. A client that sends neither, or
  // sends both the same colour, would give us one flat sheet again — which is exactly
  // what the old stylesheet had, because section_bg_color is not sent by Telegram
  // Desktop or most Android builds and fell back to the page colour.
  //
  // Mixing toward the text colour darkens a light theme and lightens a dark one, so the
  // one declaration covers both.
  if (!params.secondary_bg_color || params.secondary_bg_color === params.bg_color) {
    root.style.setProperty('--surface-page',
                           'color-mix(in srgb, var(--tg-text) 5%, var(--tg-bg))');
  } else {
    root.style.removeProperty('--surface-page');
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

// ── icons ───────────────────────────────────────────────────────────────────────
// Line icons in currentColor at one stroke weight, so a tile takes its colour from CSS and
// nothing here depends on an emoji font. The emoji stay in the bot's own messages.

const svg = (body, w) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="${w || 2}" stroke-linecap="round" stroke-linejoin="round"
  aria-hidden="true">${body}</svg>`;

const ICON = {
  warn:    svg('<path d="M12 3.5 2.8 19.5h18.4L12 3.5Z"/><path d="M12 10v4.2M12 17.2v.1"/>'),
  gauge:   svg('<path d="M4 16a8 8 0 1 1 16 0"/><path d="m12 16 4-5"/>'),
  stop:    svg('<path d="M8.5 3.5h7l5 5v7l-5 5h-7l-5-5v-7l5-5Z"/>'),
  accel:   svg('<path d="M5 19 19 5"/><path d="M10 5h9v9"/>'),
  turn:    svg('<path d="M9 7H4v5"/><path d="M4 7c4-4 12-3 14 3v7"/>'),
  car:     svg('<path d="M5 16V12l2-5h10l2 5v4"/><path d="M3 16h18"/><circle cx="8" cy="17" r="1.6"/><circle cx="16" cy="17" r="1.6"/>'),
  phone:   svg('<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/>'),
  lock:    svg('<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/>'),
  ban:     svg('<circle cx="12" cy="12" r="8.5"/><path d="m6 6 12 12"/>'),
  eye:     svg('<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>'),
  moon:    svg('<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/>'),
  camera:  svg('<path d="M4 8h3l1.5-2h7L17 8h3v11H4V8Z"/><circle cx="12" cy="13.5" r="3.2"/>'),
  parking: svg('<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M10 16V8h3a2.5 2.5 0 0 1 0 5h-3"/>'),
  link:    svg('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
  chevron: svg('<path d="m9 6 6 6-6 6"/>'),
  check:   svg('<path d="m5 12.5 4.5 4.5L19 7.5"/>', 2.6),
  plus:    svg('<path d="M12 5v14M5 12h14"/>', 2.4),
  pause:   svg('<circle cx="12" cy="12" r="8.5"/><path d="M9.5 9.5v5M14.5 9.5v5"/>'),
  up:      svg('<path d="M12 19V5M6 11l6-6 6 6"/>'),
  down:    svg('<path d="M12 5v14M6 13l6 6 6-6"/>'),
  trash:   svg('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12.2a1 1 0 0 0 1 .8h8a1 1 0 0 0 1-.8L18 7M9 7V4.8A.8.8 0 0 1 9.8 4h4.4a.8.8 0 0 1 .8.8V7"/>'),
};

/** [icon, colour token] per catalog row. Colour is a hue, not a severity: red is anything
 *  that can hurt somebody, orange is how the truck is being driven, purple is the driver,
 *  teal is the hardware. A type that is not listed gets a plain warning in the accent. */
const EVENT_STYLE = {
  speeding:                    ['gauge',   '--ev-red'],
  crash:                       ['warn',    '--ev-red'],
  forward_collision_warning:   ['warn',    '--ev-red'],
  near_miss:                   ['warn',    '--ev-red'],
  stop_sign_violation:         ['stop',    '--ev-red'],
  hard_brake:                  ['stop',    '--ev-orange'],
  harsh_acceleration:          ['accel',   '--ev-orange'],
  harsh_turn:                  ['turn',    '--ev-orange'],
  harsh_event:                 ['warn',    '--ev-orange'],
  cell_phone:                  ['phone',   '--ev-purple'],
  seat_belt_violation:         ['lock',    '--ev-purple'],
  no_seat_belt:                ['ban',     '--ev-purple'],
  inattentive_driving:         ['eye',     '--ev-purple'],
  drowsy_driving:              ['moon',    '--ev-purple'],
  road_facing_cam_obstruction: ['camera',  '--ev-teal'],
  tailgating:                  ['car',     '--ev-blue'],
  unsafe_parking:              ['parking', '--ev-blue'],
};

function eventStyle(type) {
  return EVENT_STYLE[chipTokenFor(type)] || EVENT_STYLE[type] || ['warn', '--ev-blue'];
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
 *  right weight for "that unit is already linked" and far too much for "Saved".
 *
 *  An expired credential is filtered out here rather than at a dozen call sites. api()
 *  has already taken the whole screen with the session-expired blocker by the time this
 *  would run, and every mutation that used to forget the check stacked a modal reading
 *  literally "unauthenticated" on top of it. */
function alertMessage(message) {
  if (message === 'unauthenticated') return;
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
  // Authenticated, but no longer allowed: the admin row was deactivated or removed while
  // the panel was open. Every screen would 403, so this takes the page the way an expired
  // credential does instead of showing "Something went wrong" with a retry that cannot
  // ever succeed. Not for a per-action refusal — those carry their own message and are
  // answered by the server with a 400 or a 403 that names a rule.
  if (res.status === 403 && path === '/bootstrap') {
    showBlocker('revoked');
    throw new Error('unauthenticated');
  }

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || 'Something went wrong.');
    err.code = data.error;
    err.data = data;
    throw err;
  }
  return data;
}

/** Successful writes so far.
 *
 *  Read by revalidate(): a background re-check that was issued before a write cannot be
 *  allowed to land after it, because the body it is holding predates the change and
 *  would put the old value back — with the toast still on screen saying it saved. */
let writes = 0;

async function post(path, body) {
  const data = await api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  writes++;
  return data;
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
// Requests already on their way. Two screens asking for /units at the same moment used
// to mean two round trips to Samsara for one answer; the Fleet screen starting it in the
// background and the group detail wanting it a second later is exactly that case.
const inflight = new Map();

async function load(path) {
  if (cache.has(path)) return cache.get(path);
  if (inflight.has(path)) return inflight.get(path);
  const pending = api(path)
    .then((data) => { cache.set(path, data); return data; })
    .finally(() => inflight.delete(path));
  inflight.set(path, pending);
  return pending;
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
  const writesBefore = writes;
  busy(true);
  try {
    const fresh = await Promise.all(paths.map((p) => api(p)));
    // A write landed while this was in flight, so what came back is older than what is
    // on screen. Drop it: the mutation already updated the cache and re-rendered, and
    // this body would quietly undo it. The Groups tab made this near-deterministic —
    // /units waits on the provider's API, so the window is seconds wide.
    if (writes !== writesBefore) return;
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
  fleet: 'Fleet', report: 'Report', admins: 'Admins', settings: 'Settings',
};

/** What a detail screen is called in the eyebrow; its own name is the title under it. */
const DETAIL_KINDS = { group: 'Group', admin: 'Admin', addadmin: 'Admins' };

/** The bar, the back button and the page's bottom edge for whatever is on screen. A detail
 *  screen is a page you walked into: it has its own way out, and no tab bar under it. */
/* Not called `chrome`, which it was for a day: a top-level function declaration becomes a
 * property of `window`, so that name replaced window.chrome -- and Telegram Desktop's bridge
 * on Windows talks to the host through window.chrome.webview. The panel opened as a blank
 * screen there and fine on a phone. Nothing at this level may be named after something the
 * browser already owns. */
function syncChrome() {
  const d = state.detail;
  $('#screen-title').textContent = d ? DETAIL_KINDS[d.type] : TITLES[state.tab];
  $('#company-name').textContent = d ? d.title
    : (state.boot ? state.boot.company.name : '');
  const back = $('#back');
  if (back) back.hidden = !d;
  document.body.classList.toggle('in-detail', !!d);
  document.body.classList.remove('has-savebar');
}

function showBlocker(kind) {
  $('#app').hidden = true;
  const blocker = $('#blocker');
  blocker.hidden = false;
  if (kind === 'revoked') {
    $('#blocker-emoji').textContent = '🚫';
    $('#blocker-title').textContent = 'No longer an admin';
    $('#blocker-text').textContent =
      'This account cannot open the panel any more. Ask a super admin if that is wrong.';
    const action = $('#blocker-action');
    action.hidden = false;
    action.textContent = 'Close';
    action.onclick = () => tg && tg.close && tg.close();
  }
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
  fleet: () => `<div><div class="sk" style="height:52px;border-radius:12px;
      margin-bottom:12px"></div>${skRows(5)}</div>`,
  report: () => SKELETONS.dashboard(),
  settings: () => `<div>${skRows(4)}</div>`,
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

/** Whether the navigation a render belongs to has since been superseded.
 *
 *  Every screen is an async function that awaits the network before it builds any DOM,
 *  and the user can tap a tab during that await. Without this check the slow screen
 *  still painted when its data arrived — and painted over whatever the user had moved
 *  to, leaving the Groups list sitting under the title "Dashboard". /units goes out to
 *  the provider's API, so on the Groups tab that await is seconds long and the race is
 *  not theoretical.
 *
 *  Compared against state.epoch, which every navigation bumps. An opts without an epoch
 *  is never stale: in-place redraws pass their own options straight through. */
function stale(opts) {
  return !!opts && opts.epoch !== undefined && opts.epoch !== state.epoch;
}

/** Put a screen on the page.
 *
 *  `keep` is what makes a toggle feel like a toggle: the node is rebuilt from new data
 *  but the page stays exactly where it was. `restore` is the other direction — coming
 *  back to a tab or out of a detail view, to where that tab was left. */
function render(node, opts) {
  opts = opts || {};
  if (stale(opts)) return;
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

/** Push a detail view over whatever is showing.
 *
 *  history.pushState is here for Android's hardware back button: without a history entry
 *  to pop, it closes the whole Mini App from a detail screen, which reads as a crash.
 *  One entry per screen, and every way out consumes exactly one — see popDetail. */
function pushDetail(detail) {
  rememberScroll();
  state.details.push(detail);
  state.detail = detail;
  state.epoch++;
  history.pushState({ depth: state.details.length }, '');
  if (tg && tg.BackButton) tg.BackButton.show();
  renderCurrent();
}

/** Leave the top detail screen — always through history, never by clearing the state.
 *
 *  Clearing it directly is what left a dead entry on the stack behind every exit that
 *  was not a Back press: the first press afterwards did nothing at all, and the second
 *  closed the Mini App. Going through history keeps the stack and the screen in step,
 *  and popstate does the actual work for both routes. */
function popDetail() {
  if (state.details.length) { history.back(); return; }
  closeDetail();
}

/** The state change itself. Driven by popstate, so it runs once per entry however the
 *  user left — the hardware button, the Telegram BackButton, or a screen that finished
 *  its job and sent itself away. */
function closeDetail() {
  state.details.pop();
  state.detail = state.details[state.details.length - 1] || null;
  state.epoch++;
  if (!state.detail && tg && tg.BackButton) tg.BackButton.hide();
  hideMainButton();
  renderCurrent({ restore: state.detail ? 0 : (state.scroll[state.tab] || 0) });
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
  state.epoch++;
  // The tab bar is visible on detail screens, so leaving one this way is ordinary. The
  // entries those screens pushed are given back here; state.detail is already null by
  // the time popstate fires for them, so the listener correctly does nothing.
  if (state.details.length) {
    const depth = state.details.length;
    state.details = [];
    history.go(-depth);
  }
  state.detail = null;
  if (tg && tg.BackButton) tg.BackButton.hide();
  hideMainButton();
  document.querySelectorAll('.tab').forEach((btn) => {
    const on = btn.dataset.tab === tab;
    btn.classList.toggle('is-active', on);
    btn.setAttribute('aria-current', on ? 'page' : 'false');
  });
  renderCurrent({ restore: state.scroll[tab] || 0 });
}

const DETAIL_VIEWS = {
  group: groupDetail, admin: adminDetail, addadmin: addAdminScreen,
};

/** What each screen reads, so the skeleton is honest: a screen whose data is not in the
 *  cache yet must show one, and one whose data is must not flash one. */
const DETAIL_PATHS = {
  admin: () => ['/admins'],
  // Not /units. The Fleet screen and the group detail both paint without the roster and
  // fill it in when it arrives — see fleetScreen.
  group: () => ['/groups'],
};

const TAB_VIEWS = {
  fleet: fleetScreen, report: reportScreen,
  admins: adminsScreen, settings: settingsScreen,
};

function renderCurrent(opts) {
  opts = opts || {};
  syncChrome();

  const view = state.detail ? DETAIL_VIEWS[state.detail.type] : TAB_VIEWS[state.tab];
  if (!view) { popDetail(); return; }

  // A skeleton belongs to a screen that has nothing to show yet. Re-rendering after a
  // toggle, or restoring a cached tab, must not flash one.
  if (!opts.keep && !hasCachedScreen()) setSkeleton(state.detail ? 'detail' : state.tab);

  // Stamped with the navigation this render belongs to, so everything downstream of the
  // await — the screen itself, and the error screen if it throws — can tell whether the
  // user is still on it. See stale().
  const current = Object.assign({}, opts, { epoch: state.epoch });
  view(current).catch((e) => {
    if (e.message === 'unauthenticated') return;
    render(errorScreen(e.message), current);
  });
}

/** Whether the current screen can paint without waiting for the network. */
function hasCachedScreen() {
  if (state.detail) {
    const paths = DETAIL_PATHS[state.detail.type];
    // An unknown detail type has nothing cached by definition. The old fallback asked
    // whether EITHER /groups or /admins was cached, which meant opening a group right
    // after visiting Admins skipped the skeleton and sat blank through the longest wait
    // in the app.
    return paths ? paths().every((path) => cache.has(path)) : false;
  }
  return screenPaths().every((p) => cache.has(p));
}

function screenPaths() {
  if (state.tab === 'fleet') return ['/groups'];
  if (state.tab === 'report') return [statsPath()];
  if (state.tab === 'admins') return ['/admins'];
  if (state.tab === 'settings') return ['/settings'];
  return [];
}

function errorScreen(message) {
  const wrap = el(`<div class="empty"><span class="empty-emoji">⚠️</span>
      <p>${esc(message)}</p></div>`);
  const retry = el('<button class="btn btn-secondary">Try again</button>');
  retry.onclick = () => { haptic(); refreshAll(); };
  wrap.appendChild(retry);
  return wrap;
}

/** The refresh control: drop everything and rebuild the current screen from the wire. */
function refreshAll() {
  haptic();
  cache.clear();
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

// ── screen: report ────────────────────────────────────────────────────────

/* Five windows, rolling and calendar in one row.
 *
 * These used to be two screens: a dashboard on rolling windows and a report on calendar
 * ones. Both were right on their own and the split was invisible while they were apart —
 * but one scroll took you past "top units, last 7 days" and then offered you a file for
 * "last week", and the two lists disagreed. One screen, one control, and the label says
 * which window you are looking at.
 *
 * utils/violation_report.py owns the calendar set; the server marks which of the five it
 * can turn into a file (period.downloadable) rather than the client knowing a second
 * time. */
const PERIODS = [
  ['today', 'Today'], ['yesterday', 'Yesterday'],
  ['last_week', 'Last week'], ['last_month', 'Last month'],
];

/* How many type rows the screen shows before "More". The catalog is sixteen; a landing
 * card that is sixteen bar rows tall is a picker nobody asked for. */
const PRIMARY_TYPES = 6;

function statsPath() {
  return `/stats?period=${encodeURIComponent(state.period)}`;
}

async function reportScreen(opts) {
  const [data] = await screenData([statsPath()], opts);
  const wrap = el('<div></div>');

  // One row, four chips. Each is a window somebody is actually asked about; the rolling
  // 7- and 30-day ones are still answered by the server but are not offered, because a
  // dispatcher who reads "last 7 days" and then downloads "last week" gets two answers to
  // one question.
  const periods = el('<div class="period-bar"></div>');
  PERIODS.forEach(([key, label]) => {
    const on = key === state.period;
    const chip = el(`<button class="chip${on ? ' is-on' : ''}"
        aria-pressed="${on}">${label}</button>`);
    chip.onclick = () => {
      hapticSelect();
      state.period = key;
      state.epoch++;
      renderCurrent();
    };
    periods.appendChild(chip);
  });
  wrap.appendChild(periods);

  // The number and the days behind it share a card. Nothing here opens anything: the alerts
  // themselves are in Telegram, and a figure that looks tappable and goes nowhere is worse
  // than one that does not move.
  const panel = el('<section class="panel"></section>');
  panel.appendChild(el(`<div class="panel-hero panel-hero-static">
      <span class="panel-hero-main">
        <span class="panel-hero-num">${num(data.totals.total)}</span>
        <span class="panel-hero-label">alerts · ${esc(data.period.range)}</span>
      </span></div>`));
  if (data.by_day && data.by_day.length > 1) panel.appendChild(trendBlock(data.by_day));
  wrap.appendChild(panel);

  markAttention(data.groups.unpaired + data.groups.muted);

  const tiles = el('<div class="tiles"></div>');
  // Not "Units". That tile used to show COUNT(DISTINCT vehicle_number) of the violations in
  // the window -- trucks that did something, sitting next to a tile called Speeding and
  // reading as fleet size. This is the number that was missing: how much of the fleet is
  // wired up at all. It is also the only tile that goes anywhere.
  const paired = el(`<button class="tile">
      <span class="tile-value">${num(data.groups.with_unit)}<small>/${num(data.groups.total)}</small></span>
      <span class="tile-label">Paired</span></button>`);
  paired.onclick = () => { haptic(); selectTab('fleet'); };
  tiles.appendChild(paired);
  tiles.appendChild(el(`<div class="tile is-static">
      <span class="tile-value">${num(data.totals.speeding)}</span>
      <span class="tile-label">Speeding</span></div>`));
  tiles.appendChild(el(`<div class="tile is-static${data.totals.crashes > 0 ? ' tile-alarm' : ''}">
      <span class="tile-value">${num(data.totals.crashes)}</span>
      <span class="tile-label">Crashes</span></div>`));
  wrap.appendChild(tiles);

  if (data.top_units.length) {
    const peak = data.top_units[0].total || 1;
    const card = el(`<div class="card"><div class="card-head">
        <div class="card-title">Worst units</div></div></div>`);
    // The name takes its own line with the bar underneath. A unit number fits the column
    // beside a bar; a roster that spells them out -- Elegant Express sends "TRUCK #002
    // JAVOKHIR AZIZOV" -- does not, and every row truncated at the same place. Stacked is
    // right for both, so it is no longer chosen per roster.
    data.top_units.forEach((u, i) => {
      card.appendChild(el(`<div class="bar-row is-stacked">
          <span class="bar-rank">${i + 1}</span>
          <span class="bar-name">${esc(u.vehicle_number)}</span>
          <span class="bar-track"><span class="bar-fill"
                style="width:${Math.round((u.total / peak) * 100)}%"></span></span>
          <span class="bar-count">${num(u.total)}</span></div>`));
    });
    wrap.appendChild(card);
  }

  if (data.by_type.length) {
    const peak = data.by_type[0].total || 1;
    const shown = state.typesOpen ? data.by_type : data.by_type.slice(0, PRIMARY_TYPES);
    const card = el(`<div class="card"><div class="card-head">
        <div class="card-title">By event type</div></div></div>`);
    shown.forEach((t) => {
      const tone = eventStyle(t.token || t.event_type)[1];
      card.appendChild(el(`<div class="type-row" style="--c:var(${tone})">
          <span class="type-dot"></span>
          <span class="type-name">${esc(t.label)}</span>
          <span class="type-track"><span class="type-fill"
                style="width:${Math.round((t.total / peak) * 100)}%"></span></span>
          <span class="type-count">${num(t.total)}</span></div>`));
    });
    if (data.by_type.length > PRIMARY_TYPES) {
      const more = el(`<button class="link-row">${state.typesOpen ? 'Show fewer'
        : `Show ${data.by_type.length - PRIMARY_TYPES} more types`}</button>`);
      more.onclick = () => {
        hapticSelect();
        state.typesOpen = !state.typesOpen;
        renderCurrent({ keep: true });
      };
      card.appendChild(more);
    }
    wrap.appendChild(card);
  }

  if (!data.totals.total) {
    wrap.appendChild(el(`<div class="empty"><span class="empty-emoji">✅</span>
      Nothing recorded in this period.</div>`));
  }

  const send = el(`<button class="btn btn-block section-gap"
      >📥 Send full report to my chat</button>`);
  if (!data.period.downloadable) {
    // Said by the server, not guessed here: only Today, Last week and Last month have a
    // file behind them, and which those are is the backend's to say.
    disable(send, 'A file can be built for Today, Last week or Last month — pick '
                + 'one of those periods.');
  } else if (!data.totals.total) {
    disable(send, 'There is nothing to put in a report for this period.');
  } else {
    send.onclick = async () => {
      busy(true);
      send.textContent = 'Sending…';
      try {
        await post('/report/send', { period: state.period, type: '' });
        hapticResult(true);
        toast('Report sent to your chat');
      } catch (e) {
        hapticResult(false);
        if (e.message !== 'unauthenticated') alertMessage(e.message);
      } finally {
        send.textContent = '📥 Send full report to my chat';
        busy(false);
      }
    };
  }
  wrap.appendChild(control(send));

  // The file is not on this screen on purpose, so say where it goes.
  wrap.appendChild(el(`<div class="note">The full report lists every event under every
    unit, so it arrives as a file in your chat with the bot rather than on this
    screen.</div>`));

  render(wrap, opts);
}

/** The days behind the headline number, inside the same card. The busiest day is the one
 *  filled; tapping or hovering a column reads its day and count out underneath, which is
 *  the only route to the figure on a phone, where there is no hover. */
function trendBlock(byDay) {
  const peak = Math.max(...byDay.map((d) => d.total), 1);
  const block = el('<div class="panel-trend"></div>');
  const trend = el(`<div class="trend${byDay.length <= 10 ? ' is-sparse' : ''}"></div>`);
  const readout = el('<div class="trend-read" aria-live="polite"></div>');
  let pinned = () => { };

  // A week's days are named (Mon, Tue); anything longer would not fit seven-plus labels
  // across a phone, so a month is labelled with the date at about six places.
  const every = Math.max(1, Math.round(byDay.length / 6));
  const busiest = byDay.reduce((a, b) => (b.total > a.total ? b : a), byDay[0]);

  byDay.forEach((d, i) => {
    const when = new Date(d.day + 'T12:00:00');
    const weekday = when.toLocaleDateString('en-US',
      { weekday: byDay.length <= 7 ? 'short' : 'narrow' });
    const short = when.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    const label = byDay.length <= 10 ? weekday
      : (i % every === 0 || i === byDay.length - 1 ? String(when.getDate()) : '');

    const col = el(`<button class="trend-col${d === busiest ? ' is-peak' : ''}"
        aria-label="${esc(short)}: ${d.total}">
        <span class="trend-tip">${esc(short)} · ${num(d.total)}</span>
        <div class="trend-bar" style="height:${Math.round((d.total / peak) * 100)}%"></div>
        <div class="trend-day">${esc(label)}</div></button>`);
    const say = () => {
      readout.textContent = `${short} · ${num(d.total)} alert${d.total === 1 ? '' : 's'}`;
    };
    col.onclick = () => {
      hapticSelect();
      trend.querySelectorAll('.trend-col').forEach((c) => c.classList.remove('is-on'));
      col.classList.add('is-on');
      pinned = say;
      say();
    };
    // Hover previews and restores; a tap pins. The tooltip itself is CSS.
    col.onmouseenter = say;
    col.onmouseleave = () => pinned();
    trend.appendChild(col);
  });

  pinned = () => {
    readout.textContent = `Busiest: ${new Date(busiest.day + 'T12:00:00')
      .toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} · ${num(busiest.total)}`;
  };
  pinned();

  block.appendChild(trend);
  block.appendChild(readout);
  return block;
}

/** The admin's own DM switches -- the bot's settings menu, in the panel -- and, under
 *  them, what this deployment is.
 *
 *  Deliberately only this. "Settings" in a fleet tool sounds like it should hold the
 *  group filters and the company's configuration; the group filters are per group and live
 *  on that group's page, and the configuration is environment variables on the deployment,
 *  which no panel should be able to edit. The second card is a read-out of that
 *  configuration -- it was the More screen's, and there is no More any more. */
async function settingsScreen(opts) {
  const [data] = await screenData(['/settings'], opts);
  const boot = state.boot;
  const wrap = el('<div></div>');

  const card = el(`<div class="card notif-card"><div class="notif-head">
      <div class="notif-title">My notifications</div>
      <div class="notif-sub">Which alerts reach your own chat</div></div></div>`);
  data.notifications.forEach((n) => {
    const [icon, tone] = eventStyle(n.type);
    const row = el(`<div class="notif-row">
        <span class="ev-icon is-lg${n.type === 'crash' ? ' is-solid' : ''}"
              style="--c:var(${tone})">${ICON[icon]}</span>
        <div class="notif-text"><div class="notif-label">${esc(n.label)}</div>
        ${n.note ? `<div class="notif-note">${esc(n.note)}</div>` : ''}</div></div>`);
    const sw = el(`<button class="switch${n.on ? ' is-on' : ''}" role="switch"
        aria-checked="${!!n.on}" aria-label="${esc(n.label)}"></button>`);
    sw.onclick = async () => {
      const next = !sw.classList.contains('is-on');
      // Flipped first and put back on failure. A switch that waits for a round trip before
      // moving feels broken on a phone, and this one is idempotent server-side: it sends
      // the state it is now in, not "toggle".
      sw.classList.toggle('is-on', next);
      sw.setAttribute('aria-checked', String(next));
      sw.disabled = true;
      busy(true);
      try {
        await post('/settings/notifications', { type: n.type, on: next });
        invalidate('/settings');
        hapticResult(true);
      } catch (e) {
        sw.classList.toggle('is-on', !next);
        sw.setAttribute('aria-checked', String(!next));
        hapticResult(false);
        if (e.message !== 'unauthenticated') alertMessage(e.message);
      } finally {
        sw.disabled = false;
        busy(false);
      }
    };
    row.appendChild(sw);
    card.appendChild(row);
  });
  wrap.appendChild(card);
  wrap.appendChild(el(`<div class="note">These are your personal alerts and nobody
    else's. What a driver group receives is set on that group's page, under
    <b>Fleet</b>.</div>`));

  const role = boot.me.is_owner ? 'Deployment owner'
    : boot.me.is_super ? 'Super admin' : 'Admin';
  const deploy = el(`<div class="card deploy-card section-gap">
      <div class="card-title">This deployment</div></div>`);
  [
    ['Company', boot.company.name, ''],
    ['Bot', boot.bot_username ? `@${boot.bot_username}` : '—', ''],
    ['You', role, ''],
    // Both of these are the difference between an alert having somewhere to go and having
    // nowhere, and neither was visible anywhere in the panel before.
    ['Fleet-wide chat', boot.main_group_id ? 'Connected' : 'Not set up',
     boot.main_group_id ? '' : ' is-warn'],
    ['Crash chat', boot.crash_group_id ? 'Connected' : 'Not set up',
     boot.crash_group_id ? '' : ' is-warn'],
  ].forEach(([label, value, tone]) => {
    deploy.appendChild(el(`<div class="field">
        <span class="field-label">${esc(label)}</span>
        <span class="field-value${tone}">${esc(value)}</span></div>`));
  });
  wrap.appendChild(deploy);

  if (!boot.crash_group_id) {
    wrap.appendChild(el(`<div class="note-card">With no crash chat, a crash detection
      reaches only the admins who have crash DMs switched on. That switch is
      <b>Crash</b>, under My notifications.</div>`));
  }

  render(wrap, opts);
}

// ── screen 2: groups ────────────────────────────────────────────────────────────

/* The three questions this screen answers.
 *
 * There are two entirely different ways a truck can be uncovered, and the screen used to
 * show them as the same kind of thing at two ends of one scroll: a group carrying a "no
 * unit" badge somewhere in the list, and a card of leftover roster units underneath it.
 * Neither was countable at a glance, which is the only form the question "is my fleet
 * covered" is ever actually asked in.
 *
 * The sections are a partition, not a set of views — every registered group is in
 * exactly one of the first two, and the third holds what has no group at all. An "All"
 * tab on top of a partition is the same rows a second time, so there isn't one, and
 * nothing may fall between the sections: a group that belongs to none of them is a
 * group nobody can reach. */
const GROUP_FILTERS = [
  ['active', 'Active'],
  ['unpaired', 'Unpaired'],
  ['unconnected', 'No group'],
];

/** The catalog row a stored event type belongs to.
 *
 *  Two rows in the catalog carry several provider spellings — "Camera Obstructed" is
 *  three types and "Drowsiness" is two — and what is stored on a violation or in a
 *  group's filter is the real one. Everything the user taps is a row, so anything
 *  counting or linking from stored types has to come back through here first.
 *
 *  Built once from /bootstrap, which ships each row's full membership. */
let _rowOf = null;
function chipTokenFor(type) {
  if (!_rowOf) {
    _rowOf = new Map();
    ((state.boot && state.boot.event_types) || []).forEach((row) => {
      (row.types || [row.type]).forEach((real) => _rowOf.set(real, row.type));
    });
  }
  return _rowOf.get(type) || type;
}

/** Registered because the bot was added to the chat, but nobody has said which truck it
 *  belongs to — so it receives nothing until someone picks its unit. */
function isUnpaired(g) { return !g.is_main && !g.vehicle_number; }

/** Set up: pointed at a truck, or the all-fleet group.
 *
 *  Mute is not the dividing line. It is a switch on the row, flipped and unflipped from
 *  the list itself, and excluding a muted group here would leave it in no section at
 *  all now that "All" is gone — invisible in the one screen that can unmute it. The
 *  count of muted ones is called out above the list instead. */
function isActive(g) { return !isUnpaired(g); }

/** The part of a unit you would say aloud, for the 44px tile.
 *
 *  "TRUCK #002 JAVOKHIR AZIZOV" is 002, "unit571" is 571, "G8PZ-7X5-FF2" -- which has no
 *  number in it -- is its first block. Short units are shown whole. */
function tileText(unit) {
  const v = String(unit || '').trim();
  if (!v) return '—';
  if (v.length <= 5) return v.toUpperCase();
  const tokens = v.split(/[^A-Za-z0-9]+/).filter(Boolean);
  const whole = tokens.find((t) => /^\d{2,}$/.test(t));
  if (whole) return whole.slice(-4);
  const trailing = tokens.map((t) => (t.match(/(\d{2,})$/) || [])[1]).find(Boolean);
  return (trailing || tokens[0] || v).slice(0, 4).toUpperCase();
}


/** A count on the Fleet tab while something there needs doing.
 *
 *  Fleet is the only screen that shows an unpaired or muted group, and somebody can sit on
 *  Report all day. A number rather than a dot: "4" says how much, a dot only says that
 *  something is somewhere. Set from whichever screen last had the numbers -- Fleet from the
 *  group list, Report from the coverage block -- rather than from a poll of its own. */
function markAttention(count) {
  const icon = document.querySelector('.tab[data-tab="fleet"] .tab-icon');
  if (!icon) return;
  let badge = icon.querySelector('.tab-badge');
  if (!count) { if (badge) badge.remove(); return; }
  if (!badge) {
    badge = el('<span class="tab-badge" aria-hidden="true"></span>');
    icon.appendChild(badge);
  }
  badge.textContent = count > 99 ? '99+' : String(count);
}

/** The link that hands the user Telegram's own chat picker with this bot preloaded.
 *
 *  Adding the bot to a group is the one setup step the panel cannot perform — Telegram
 *  has no API for a bot to put itself in a chat — and the old copy said so and stopped
 *  there. It is impossible for the *bot*, not for the *user*: this is one tap.
 *
 *  Returns false when the client is too old for openTelegramLink or the bot's username
 *  could not be read at boot, so callers can fall back to telling people what to do. */
function addToGroup() {
  const name = state.boot && state.boot.bot_username;
  if (!name || !tg || !tg.openTelegramLink) return false;
  try {
    tg.openTelegramLink(`https://t.me/${encodeURIComponent(name)}?startgroup=true`);
    return true;
  } catch (_) {
    return false;
  }
}

/** What a deployment sees before anything is connected.
 *
 *  Not an empty list with three zeroes over it: a correctly installed bot that nobody
 *  has added to a group yet is not a fleet with no alerts, it is a setup that has not
 *  been done, and the old screen answered it with a green check mark saying everything
 *  was fine. The steps are the real ones, in order, and the first is a button. */
function firstRunScreen(opts, roster) {
  markAttention(0);
  const wrap = el('<div></div>');
  const bot = state.boot.bot_username ? `@${state.boot.bot_username}` : 'this bot';

  // Not a panel of zeroes over an empty list: a correctly installed bot that nobody has
  // added to a chat yet is not a quiet week, it is a setup that has not been done, and
  // the old screen answered it with a green check mark saying everything was fine.
  const empty = el(`<div class="empty">
      <span class="empty-emoji" aria-hidden="true">🚛</span>
      <b>No truck groups yet</b>
      <p>Add ${esc(bot)} to a truck's group chat. It shows up here straight away, and
        you give it a unit from its own page.</p>
    </div>`);
  const go = el('<button class="btn">Add the bot to a group</button>');
  go.onclick = () => {
    haptic();
    // Telegram hands over its own chat picker; adding the bot is the one step this
    // panel cannot do itself. On a client too old for the deep link, say how.
    if (!addToGroup()) {
      alertMessage('Open the truck\u2019s group chat in Telegram, tap its name, choose '
                 + `Add members, and search for ${bot}.`);
    }
  };
  empty.appendChild(go);
  wrap.appendChild(empty);

  // How many groups they are about to need. Genuinely useful here, and it is the only
  // screen on which the roster is the whole story.
  if (roster && roster.available && roster.units.length) {
    const card = el(`<div class="card">
        <div class="card-title">Waiting for a group</div></div>`);
    const chips = el('<div class="chips"></div>');
    roster.units.slice(0, 40).forEach((u) => {
      chips.appendChild(el(`<span class="chip is-static">${esc(u.name)}</span>`));
    });
    card.appendChild(chips);
    card.appendChild(el(`<div class="note">${num(roster.units.length)}
      truck${roster.units.length === 1 ? '' : 's'} in your roster. Each one needs its own
      group chat before its alerts have anywhere to go.</div>`));
    wrap.appendChild(card);
  } else if (roster && roster.reason) {
    wrap.appendChild(el(`<div class="note note-warn">${esc(roster.reason)}</div>`));
  }

  render(wrap, opts);
}

async function fleetScreen(opts) {
  const [groups] = await screenData(['/groups'], opts);
  if (stale(opts)) return;

  // The roster is read from the cache rather than awaited. /units goes out to Samsara
  // and Motive, and only one of the three sections below needs it — so the list used to
  // sit behind seconds of somebody else's API before it could draw rows it already had.
  const roster = cache.has('/units') ? cache.get('/units') : null;
  if (!roster) {
    load('/units').then(() => {
      // Not while they are typing in the search box: nobody asked for this refresh and
      // taking the focus out of the field mid-word is not a trade worth making.
      if (!stale(opts) && !isTyping()) renderCurrent({ keep: true });
    }).catch(() => { /* the section that needs it says so itself */ });
  }

  if (!groups.length) { firstRunScreen(opts, roster); return; }

  const wrap = el('<div></div>');

  const strayUnits = roster && roster.available
    ? roster.units.filter((u) => !u.linked) : [];
  const unpairedGroups = groups.filter(isUnpaired);
  const mutedGroups = groups.filter((g) => !g.enabled);
  const counts = {
    active: groups.filter(isActive).length,
    unpaired: unpairedGroups.length,
    unconnected: strayUnits.length,
  };

  // The coverage card comes first because "is my fleet covered" is the question this screen
  // exists to answer, and a list only answers it by being counted. Groups, not trucks: a
  // truck with no group is below, under No group, and the roster may not have answered.
  const pct = groups.length ? Math.round((counts.active / groups.length) * 100) : 0;
  const week = groups.reduce((n, g) => n + (g.alerts_7d || 0), 0);
  // The paired-but-quiet ones are the visible symptom of a unit the provider spells
  // differently -- the failure utils/units.py exists to prevent.
  const silent = groups.filter((g) => !g.is_main && g.vehicle_number && g.enabled
                                      && !g.alerts_7d).length;
  wrap.appendChild(el(`<section class="hero" aria-label="Coverage">
      <div class="ring" style="--pct:${pct}"><div class="ring-in">
        <span class="ring-num">${num(counts.active)}</span>
        <span class="ring-of">of ${num(groups.length)}</span></div></div>
      <div class="hero-main">
        <div class="hero-kicker">Groups with a unit</div>
        <div class="hero-line">${pct === 100 ? 'Every group is set up'
          : `${pct}% of groups are set up`}</div>
        <div class="hero-stats">
          <span class="hero-stat"><b>${num(week)}</b> alerts · 7d</span>
          ${silent ? `<span class="hero-stat is-warn"><b>${num(silent)}</b> silent</span>` : ''}
        </div></div></section>`));

  // One card for both states the counts underneath cannot show: a group that receives
  // nothing contributes nothing to them. Two amber cards stacked would be twice as loud for
  // a problem that is not twice as bad, so both share one line, and the card goes
  // wherever the bigger of them lives.
  markAttention(unpairedGroups.length + mutedGroups.length);
  if (unpairedGroups.length || mutedGroups.length) {
    const parts = [];
    if (unpairedGroups.length) parts.push(`${unpairedGroups.length} unpaired`);
    if (mutedGroups.length) parts.push(`${mutedGroups.length} muted`);
    const worst = unpairedGroups.length ? unpairedGroups : mutedGroups;
    const names = worst.slice(0, 4)
      .map((g) => g.title || String(g.telegram_group_id)).join(', ');

    const card = el(`<button class="warn-card">
        <span class="warn-card-icon" aria-hidden="true">${ICON.warn}</span>
        <span class="warn-card-main">
          <b>${esc(parts.join(' · '))}</b>
          <small>${esc(names)}</small>
        </span>
        <span class="chev" aria-hidden="true">${ICON.chevron}</span></button>`);
    card.onclick = () => {
      haptic();
      state.groupsQuery = '';
      // Unpaired groups are in their own section; a muted one has a unit, so it sits in
      // Active with its switch off.
      state.groupsFilter = unpairedGroups.length ? 'unpaired' : 'active';
      state.epoch++;
      renderCurrent();
    };
    wrap.appendChild(card);
  }

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
  // A search box over three rows is 56px of the first screen spent filtering a list you
  // can already see all of. It appears when the biggest section is long enough to need
  // it — and once it is on screen it stays, whatever the filter is narrowed to.
  //
  // It sits above the sections because it searches past them: the sections are three
  // ways of looking through one fleet, not three places a group can be.
  if (Math.max(counts.active, counts.unpaired, counts.unconnected) > 8
      || state.groupsQuery) {
    wrap.appendChild(searchWrap);
  }

  const seg = el('<div class="segmented segmented-stack" role="tablist"></div>');
  GROUP_FILTERS.forEach(([key, label]) => {
    const on = key === state.groupsFilter;
    const b = el(`<button${on ? ' class="is-active"' : ''} role="tab"
        aria-selected="${on}">
        <span class="seg-count${counts[key] ? '' : ' is-zero'}${
          counts[key] && key !== 'active' ? ' is-todo' : ''}">${
          key === 'unconnected' && !roster ? '·' : num(counts[key])}</span>
        <span class="seg-label">${label}</span></button>`);
    b.onclick = () => {
      if (state.groupsFilter === key) return;
      hapticSelect();
      state.groupsFilter = key;
      // Repainting the buttons rather than the screen keeps the search text and the
      // scroll position, which a re-render would both throw away.
      [...seg.children].forEach((child, i) => {
        const active = GROUP_FILTERS[i][0] === key;
        child.classList.toggle('is-active', active);
        child.setAttribute('aria-selected', String(active));
      });
      renderList(search.value);
    };
    seg.appendChild(b);
  });
  wrap.appendChild(seg);

  const head = el('<div class="list-head"></div>');
  wrap.appendChild(head);

  const listWrap = el('<div></div>');
  wrap.appendChild(listWrap);

  function rowsOf(list) {
    const rows = el('<div class="rows"></div>');
    list.forEach((g) => rows.appendChild(groupRow(g)));
    return rows;
  }

  /** The empty state says what is empty. "No groups match" is wrong for a section that is
   *  empty because everything is fine, and "nothing here" is no use in either case. */
  function emptyFor(tab, query) {
    if (query) {
      return el(`<div class="empty"><span class="empty-emoji">🔍</span>
        <p>Nothing matches "${esc(query)}" — no group title, no unit.</p></div>`);
    }
    if (tab === 'unpaired') {
      return el(`<div class="empty"><span class="empty-emoji">✅</span>
        Every group has a unit.</div>`);
    }
    return el(`<div class="empty"><span class="empty-emoji">👥</span>
      No group is set up for a truck yet.<br>Add the bot to a truck's group chat, then
      give it a unit here.</div>`);
  }

  /** Roster units with no Telegram group at all — the coverage gap no per-group list can
   *  show, because a missing group has nowhere in one to appear. */
  function renderUnconnected() {
    if (!roster) {
      head.innerHTML = '';
      listWrap.appendChild(el(`<div class="empty"><span class="spinner"></span>
        <div style="margin-top:12px">Checking the roster…</div></div>`));
      return;
    }
    if (!roster.available) {
      head.innerHTML = '';
      listWrap.appendChild(el(`<div class="empty"><span class="empty-emoji">⚠️</span>
        ${esc(roster.reason || 'The unit roster is unavailable.')}</div>`));
      return;
    }
    head.innerHTML = `<span>${num(strayUnits.length)} unit${
      strayUnits.length === 1 ? '' : 's'}</span>`;
    if (!strayUnits.length) {
      listWrap.appendChild(el(`<div class="empty"><span class="empty-emoji">✅</span>
        Every unit has a group.</div>`));
      return;
    }
    listWrap.appendChild(unitChipsCard(strayUnits));
  }

  /** The chips for roster units with no group. Its own function because a search finds
   *  these too: "Search groups or units" has to mean it, and a unit with no group yet is
   *  exactly the one somebody types the number of. */
  function unitChipsCard(units) {
    const card = el('<div class="card"></div>');
    const chips = el('<div class="chips"></div>');
    const linkable = !!(state.boot.bot_username && tg && tg.openTelegramLink);
    units.forEach((u) => {
      const chip = el(linkable
        ? `<button class="chip chip-add">${esc(u.name)} +</button>`
        : `<span class="chip is-static">${esc(u.name)}</span>`);
      if (linkable) chip.onclick = () => { haptic(); addToGroup(); };
      chips.appendChild(chip);
    });
    card.appendChild(chips);
    // Telegram has no API for a bot to add itself to a chat, which is why this screen
    // used to end in an instruction. It does have a deep link that hands the user their
    // own chat picker, so the instruction became a button.
    card.appendChild(el(linkable
      ? `<div class="note">These trucks have no Telegram group receiving their alerts.
         Tap one to pick the chat to add the bot to — the group appears here straight
         away, and you set its unit from its page.</div>`
      : `<div class="note">These trucks have no Telegram group receiving their alerts.
         Add the bot to that truck's group chat — it appears here straight away, and
         you can set its unit from its page.</div>`));
    return card;
  }

  /** Every group whose title or unit matches, wherever it sits, and the roster units
   *  with no group at all.
   *
   *  The search used to run inside the section the user happened to be standing in, so
   *  typing a unit number on Active answered "No groups match" while the group sat one
   *  segment over under Unpaired — and a group somebody has to search for is exactly
   *  the one that is not where they expected it. The sections are suspended while a
   *  query is on screen rather than silently narrowing the answer. */
  function renderSearch(q, raw) {
    const hits = groups.filter((g) => (g.title || '').toLowerCase().includes(q)
      || (g.vehicle_number || '').toLowerCase().includes(q));
    const units = strayUnits.filter((u) => u.name.toLowerCase().includes(q));
    const total = hits.length + units.length;

    // "whole fleet" rather than a count of what was searched: the one thing somebody
    // needs to be told here is that the answer is not limited to the section they were
    // standing in when they started typing.
    head.innerHTML = `<span>${num(total)} result${total === 1 ? '' : 's'}</span>`
      + '<span>whole fleet</span>';

    if (!total) {
      listWrap.appendChild(emptyFor(null, raw));
      return;
    }
    if (hits.length) listWrap.appendChild(rowsOf(hits));
    if (units.length) {
      listWrap.appendChild(el(`<div class="list-head section-gap"><span>${
        num(units.length)} unit${units.length === 1 ? '' : 's'} with no group</span></div>`));
      listWrap.appendChild(unitChipsCard(units));
    }
  }

  function renderList(query) {
    listWrap.innerHTML = '';
    clear.hidden = !query;
    const q = query.trim().toLowerCase();

    // The sections are hidden rather than left lit while a query is on screen: "Active"
    // highlighted above a list holding an unpaired group is a lie about what is being
    // shown. They sit below the field, so nothing the user is typing into moves.
    seg.hidden = !!q;
    if (q) { renderSearch(q, query); return; }

    const tab = state.groupsFilter;
    if (tab === 'unconnected') {
      renderUnconnected();
      return;
    }

    const inTab = groups.filter((g) => (tab === 'active' ? isActive(g) : isUnpaired(g)));
    const muted = inTab.filter((g) => !g.enabled).length;

    head.innerHTML = `<span>${num(inTab.length)} group${inTab.length === 1 ? '' : 's'}</span>`
      + (muted ? `<span class="count-warn">${num(muted)} muted</span>` : '');

    if (!inTab.length) {
      listWrap.appendChild(emptyFor(tab, ''));
    } else {
      listWrap.appendChild(rowsOf(inTab));
    }

    // On the section that lists what receives alerts, because that is where someone
    // counts the chats and wonders which one gets a crash.
    if (tab === 'active' && state.boot.crash_group_id) {
      listWrap.appendChild(el(`<div class="note">💥 Crash alerts go to a dedicated chat
        configured in the deployment settings, not listed here.</div>`));
    }
  }

  /** One row: the name on its own line so a long one is readable, the badges under it,
   *  and the mute switch on the row itself — muting used to cost two taps and a screen
   *  change, which is a lot for the control people reach for most. */
  function groupRow(g) {
    // Rows in the catalog, not stored event types: "Camera Obstructed" is three of the
    // latter, so counting those reported numbers no chip on any screen agrees with.
    const rowsOn = g.filter_mode === 'all' ? 0
      : new Set(g.event_types.map(chipTokenFor)).size;
    const filter = g.filter_mode === 'all' ? 'All events'
      : `${rowsOn} event type${rowsOn === 1 ? '' : 's'}`;

    // A paired, unmuted group that has received nothing all week is the visible symptom of
    // a unit the provider spells differently -- the failure utils/units.py exists to
    // prevent, and one that otherwise shows up as a quiet 0 in a subtitle.
    const silent = !g.is_main && g.vehicle_number && g.enabled && !g.alerts_7d;
    const week = silent
      ? '<span class="row-flag">No alerts in 7 days</span>'
      : `${num(g.alerts_7d)} this week`;

    // The tile is the unit at a glance. A roster that spells its units out ("TRUCK #002
    // JAVOKHIR AZIZOV") is shortened to the part you would say aloud; the whole name is on
    // the group's own page, and the line under the title stays for what the list is about --
    // what the group receives and whether it is hearing anything.
    const unit = g.is_main ? '' : (g.vehicle_number || '');
    const tile = g.is_main
      ? '<span class="unit-tile">ALL</span>'
      : unit
        ? `<span class="unit-tile${g.enabled ? '' : ' is-off'}">${esc(tileText(unit))}</span>`
        : `<span class="unit-tile is-open">${ICON.link}</span>`;
    const lead = g.enabled ? '' : 'Muted · ';

    const row = el(`<div class="row has-tile${g.enabled ? '' : ' is-muted'}"></div>`);
    const main = el(`<button class="row-main">${tile}<span class="row-text">
        <span class="row-title">${esc(g.title || 'Untitled group')}</span>
        <span class="row-sub">${lead}${filter} · ${week}</span></span></button>`);
    main.onclick = () => {
      haptic();
      pushDetail({ type: 'group', id: g.telegram_group_id, title: g.title || 'Group' });
    };
    row.appendChild(main);

    const sw = el(`<button class="switch${g.enabled ? ' is-on' : ''}" role="switch"
        aria-checked="${!!g.enabled}" aria-label="Alerts for this group"></button>`);
    // Redrawing the list keeps the "N muted" count above it honest. The search field is
    // outside listWrap, so whatever was typed -- and the focus -- survives.
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
  const [groups] = await screenData(['/groups'], opts);
  const roster = cache.has('/units') ? cache.get('/units') : null;
  if (!roster) {
    load('/units').then(() => {
      if (!stale(opts) && !isTyping()) renderCurrent({ keep: true });
    }).catch(() => { /* the picker says so itself */ });
  }
  // Checked before the row is looked up, not just before the paint: this screen answers a
  // missing row by navigating, and a stale instance doing that would throw the user out of
  // whatever they had opened in the meantime.
  if (stale(opts)) return;
  const g = groups.find((x) => x.telegram_group_id === id);
  if (!g) { popDetail(); return; }

  const wrap = el('<div></div>');

  // — who this is —
  const tile = g.is_main ? '<span class="unit-tile is-lg">ALL</span>'
    : g.vehicle_number
      ? `<span class="unit-tile is-lg${g.enabled ? '' : ' is-off'}">${esc(tileText(g.vehicle_number))}</span>`
      : `<span class="unit-tile is-lg is-open">${ICON.link}</span>`;
  const status = el(`<div class="identity-status ${g.enabled ? 'is-on' : 'is-off'}"
      >${g.enabled ? 'Receiving alerts' : 'Muted'}</div>`);
  const who = el(`<div class="card identity">${tile}<div class="identity-main">
      <div class="identity-name">${esc(g.is_main ? 'All units'
        : g.vehicle_number || 'No unit yet')}</div>
      <div class="identity-sub">Chat ID ${esc(String(id))}</div></div></div>`);
  who.querySelector('.identity-name').after(status);
  wrap.appendChild(who);

  // — alerts on/off —
  const card = el('<div class="card"></div>');
  const field = el(`<div class="field"><span class="field-label">Alerts</span></div>`);
  const sw = el(`<button class="switch${g.enabled ? ' is-on' : ''}" role="switch"
      aria-checked="${!!g.enabled}" aria-label="Alerts"></button>`);
  sw.onclick = async () => {
    await toggleGroupEnabled(g, sw);
    status.textContent = g.enabled ? 'Receiving alerts' : 'Muted';
    status.className = `identity-status ${g.enabled ? 'is-on' : 'is-off'}`;
  };
  field.appendChild(sw);
  card.appendChild(field);
  card.appendChild(el(`<div class="field"><span class="field-label">Alerts this week</span>
      <span class="field-value">${num(g.alerts_7d)}</span></div>`));
  wrap.appendChild(card);

  // — unit —
  //
  // Directly under the on/off switch, because it is the only field on this screen that can
  // be wrong and it is the reason the screen exists: an admin opens a group from a DM that
  // says its unit is not in the roster.
  if (!g.is_main) {
    const unitCard = el('<div class="card"><div class="card-title">Unit</div></div>');
    if (!roster) {
      unitCard.appendChild(el(`<div class="field">
          <span class="field-label">${esc(g.vehicle_number || 'Not set')}</span>
          <span class="field-value">checking the roster…</span></div>`));
    } else if (roster.available) {
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
  //
  // The tiles are a local selection and one button saves it. Every tap used to be its own
  // round trip, so going from "everything" to "speeding only" was fifteen taps and fifteen
  // requests on a truck-stop connection. What is sent is the rows that are lit, never a
  // computed allowlist: the collapse rule -- an allowlist covering every type becomes the
  // empty "all types" state -- stays in data/event_catalog.py, where one implementation of
  // it can't drift from another.
  const rows = state.boot.event_types;
  const stored = g.filter_mode === 'all'
    ? new Set(rows.map((t) => t.type))
    : new Set(g.event_types.map(chipTokenFor));
  // The selection lives in state, not in this closure. A background load finishing -- the
  // roster above, or a revalidation -- re-runs this function, and a half-made selection
  // that vanished when it did would be worse than no batching at all.
  const draft = state.filterDraft && state.filterDraft.id === id
    ? new Set(state.filterDraft.picked) : null;
  const picked = draft || new Set(stored);

  const evCard = el(`<div class="card"><div class="events-head">
      <div><div class="card-title">Event types</div>
      <div class="events-count"></div></div>
      <div class="events-quick"></div></div></div>`);
  const count = evCard.querySelector('.events-count');
  const quick = evCard.querySelector('.events-quick');
  const grid = el('<div class="event-grid"></div>');
  const note = el('<div class="note"></div>');
  evCard.appendChild(grid);
  evCard.appendChild(note);
  evCard.appendChild(el(`<div class="note">Crash alerts bypass this filter and always go to
    the crash chat and to admins who have crash DMs on.</div>`));

  // Pinned to the bottom of the screen while a selection is unsaved, and nowhere else.
  const bar = el(`<div class="savebar" hidden>
      <div class="savebar-note"></div>
      <button class="btn btn-block">Save event types</button></div>`);
  const barNote = bar.querySelector('.savebar-note');
  const save = bar.querySelector('button');

  const sameAsStored = () => picked.size === stored.size
    && [...picked].every((t) => stored.has(t));

  function drawChips() {
    grid.innerHTML = '';
    quick.innerHTML = '';
    const all = picked.size === rows.length;

    const allBtn = el(`<button class="chip" aria-pressed="${all}">All</button>`);
    allBtn.onclick = () => {
      hapticSelect();
      rows.forEach((t) => picked.add(t.type));
      drawChips();
    };
    const noneBtn = el(`<button class="chip" aria-pressed="${!picked.size}">None</button>`);
    noneBtn.onclick = () => {
      hapticSelect();
      picked.clear();
      drawChips();
    };
    quick.appendChild(allBtn);
    quick.appendChild(noneBtn);

    rows.forEach((t) => {
      const on = picked.has(t.type);
      const [icon, tone] = eventStyle(t.type);
      const tileEl = el(`<button class="event-tile${on ? ' is-on' : ''}"
          aria-pressed="${on}">
          <span class="ev-icon" style="--c:var(${tone})">${ICON[icon]}</span>
          <span class="event-tile-label">${esc(t.label)}</span>
          <span class="event-tile-check">${ICON.check}</span></button>`);
      tileEl.onclick = () => {
        hapticSelect();
        if (on) picked.delete(t.type); else picked.add(t.type);
        drawChips();
      };
      grid.appendChild(tileEl);
    });

    count.textContent = `${picked.size} of ${rows.length} on`;
    note.textContent = all
      ? 'Receiving every event type, including any added later.'
      : picked.size
        ? 'Only the highlighted types are delivered to this group.'
        : 'Nothing is selected. Pick at least one type, or mute the group above.';
    const dirty = !sameAsStored();
    state.filterDraft = dirty ? { id, picked: [...picked] } : null;
    bar.hidden = !dirty;
    document.body.classList.toggle('has-savebar', dirty);
    barNote.textContent = `Unsaved changes · ${picked.size} of ${rows.length} on`;
    save.disabled = !picked.size;
  }

  save.onclick = async () => {
    // Every type lit is "all", not an allowlist that happens to list everything: the
    // server collapses the two, and sending the honest intent keeps that rule in one place.
    const body = picked.size === rows.length
      ? { action: 'all' }
      : { action: 'set', event_types: [...picked] };
    save.disabled = true;
    save.textContent = 'Saving…';
    await toggleEvent(g, body, () => {
      stored.clear();
      (g.filter_mode === 'all' ? rows.map((t) => t.type)
                               : g.event_types.map(chipTokenFor)).forEach((t) => stored.add(t));
      picked.clear();
      stored.forEach((t) => picked.add(t));
      drawChips();
    });
    save.textContent = 'Save event types';
    save.disabled = false;
  };

  drawChips();
  wrap.appendChild(evCard);
  wrap.appendChild(bar);

  // Super admins only, and never the all-fleet group: it is recreated on every boot.
  if (state.boot.me.is_super && !g.is_main) {
    const zone = el('<div class="card danger-zone"></div>');
    const btn = el(`<button class="btn btn-danger btn-block">${ICON.trash} Remove group</button>`);
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

// ── admins ──────────────────────────────────────────────────────────────────────

const AVATAR_COLORS = ['#2f5bea', '#7c4ddb', '#0e7490', '#b45309', '#a21caf', '#15803d'];

/** Two letters for the avatar: first and last word of the name. */
function initials(name) {
  const parts = String(name || '').trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

function avatarHtml(a, large) {
  const color = AVATAR_COLORS[Math.abs(Number(a.telegram_id) || 0) % AVATAR_COLORS.length];
  return `<span class="avatar${large ? ' is-lg' : ''}${a.is_active ? '' : ' is-off'}"
      style="--av:${color}" aria-hidden="true">${esc(initials(a.full_name))}</span>`;
}

function rolePills(a, withActive) {
  return (a.is_owner ? '<span class="pill pill-owner">OWNER</span>' : '')
    + (a.is_super && !a.is_owner ? '<span class="pill pill-super">SUPER</span>' : '')
    + (a.is_active ? (withActive ? '<span class="pill pill-on">ACTIVE</span>' : '')
                   : '<span class="pill">INACTIVE</span>');
}

async function adminsScreen(opts) {
  const [admins] = await screenData(['/admins'], opts);
  const wrap = el('<div></div>');

  const active = admins.filter((a) => a.is_active).length;
  wrap.appendChild(el(`<div class="list-head">
      <span>${num(admins.length)} admin${admins.length === 1 ? '' : 's'}</span>
      <span>${num(active)} active</span></div>`));

  const rows = el('<div class="rows"></div>');
  admins.forEach((a) => {
    const handle = a.username ? `@${esc(a.username)}` : `ID ${esc(String(a.telegram_id))}`;
    const row = el(`<button class="row has-avatar${a.is_active ? '' : ' is-inactive'}">
        ${avatarHtml(a)}
        <div class="row-main" style="pointer-events:none">
          <div class="row-title"><span>${esc(a.full_name || 'Unknown')}</span>${rolePills(a)}</div>
          <div class="row-sub">${handle}</div>
        </div>
        <span class="row-chevron" aria-hidden="true"></span>
      </button>`);
    row.onclick = () => {
      haptic();
      pushDetail({ type: 'admin', id: a.id, title: a.full_name || 'Admin' });
    };
    rows.appendChild(row);
  });
  wrap.appendChild(rows);

  if (state.boot.me.is_super) {
    const add = el(`<button class="btn btn-block btn-add">${ICON.plus} Add admin</button>`);
    add.onclick = () => { haptic(); pushDetail({ type: 'addadmin', title: 'Add admin' }); };
    wrap.appendChild(add);
  } else {
    wrap.appendChild(el('<div class="note">Only a super admin can change this list.</div>'));
  }
  render(wrap, opts);
}

async function adminDetail(opts) {
  const [admins] = await screenData(['/admins'], opts);
  if (stale(opts)) return;
  const a = admins.find((x) => x.id === state.detail.id);
  if (!a) { popDetail(); return; }

  const isSuper = state.boot.me.is_super;
  const isSelf = a.telegram_id === state.boot.me.telegram_id;
  /* Two tiers, not one. Any super admin can hand the role out; only the deployment's own
   * account -- the one named in the deployment config -- can take it back, deactivate a
   * super admin or remove one. Peers stripping each other is a race nobody wins, and the
   * loser has no way back into the panel that isn't a hand-written SQL statement.
   *
   * The rules are the server's (update_admin in utils/webapp/api.py). These are the same
   * rules stated in buttons, so the panel refuses before the request rather than after
   * it. */
  const owner = !!state.boot.me.is_owner;
  const ownerRow = "This account is named in the deployment's own config — change it "
    + 'there, not here.';
  const ownerOnly = (verb) => `Only the deployment's own account can ${verb} a super admin.`;
  const wrap = el('<div></div>');

  const handle = a.username ? `@${esc(a.username)}` : `ID ${esc(String(a.telegram_id))}`;
  wrap.appendChild(el(`<div class="card identity-card">
      ${avatarHtml(a, true)}
      <div class="identity-name">${esc(a.full_name || 'Unknown')}</div>
      <div class="identity-handle">${handle}</div>
      <div class="pills">${rolePills(a, true).replace(/class="pill/g, 'class="pill pill-lg')}</div>
    </div>`));

  wrap.appendChild(el(`<div class="card">
      <div class="field"><span class="field-label">Telegram ID</span>
        <span class="field-value">${esc(String(a.telegram_id))}</span></div>
      <div class="field"><span class="field-label">Role</span>
        <span class="field-value">${a.is_owner ? 'Deployment owner'
          : a.is_super ? 'Super admin' : 'Admin'}</span></div>
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
  // exist -- hiding the control just makes the panel look broken.
  const actions = el('<div class="card stack-actions"></div>');

  const activeBtn = el(`<button class="btn btn-secondary btn-block"
      >${ICON.pause} ${a.is_active ? 'Deactivate' : 'Activate'}</button>`);
  if (isSelf && a.is_active) disable(activeBtn, "You can't deactivate yourself.");
  else if (a.is_owner) disable(activeBtn, ownerRow);
  else if (a.is_super && !owner) disable(activeBtn, ownerOnly('deactivate'));
  else activeBtn.onclick = () => mutateAdmin(a.id, { is_active: !a.is_active },
                                             a.is_active ? 'Deactivated' : 'Activated');
  actions.appendChild(control(activeBtn));

  if (!a.is_super) {
    const promote = el(`<button class="btn btn-secondary btn-block"
        >${ICON.up} Make super admin</button>`);
    if (!a.is_active) disable(promote, 'Activate this admin before promoting them.');
    else promote.onclick = async () => {
      if (await confirmAction(`Make ${a.full_name || 'this admin'} a super admin?`)) {
        mutateAdmin(a.id, { is_super: true }, 'Promoted');
      }
    };
    actions.appendChild(control(promote));
  } else {
    const demote = el(`<button class="btn btn-secondary btn-block"
        >${ICON.down} Remove super admin</button>`);
    if (isSelf) disable(demote, "You can't take away your own super-admin role.");
    else if (a.is_owner) disable(demote, ownerRow);
    else if (!owner) disable(demote, ownerOnly('demote'));
    else demote.onclick = async () => {
      // Named as what it leaves behind, not as what it takes: the usual reason to press this
      // is that somebody was promoted by mistake, and the question in their head is whether
      // it also throws the person out of the panel. It does not.
      if (await confirmAction(`${a.full_name || 'This admin'} stays an admin but loses `
          + 'super-admin rights — no managing admins, groups or units. Continue?')) {
        mutateAdmin(a.id, { is_super: false }, 'Super admin removed');
      }
    };
    actions.appendChild(control(demote));
    actions.appendChild(el(`<div class="note" style="margin:0">They stay an admin but lose
      super-admin rights: no managing admins, groups or units.</div>`));
  }
  wrap.appendChild(actions);

  const remove = el(`<button class="btn btn-danger btn-block"
      >${ICON.trash} Remove admin</button>`);
  if (isSelf) disable(remove, "You can't remove yourself.");
  else if (a.is_owner) disable(remove, ownerRow);
  else if (a.is_super && !owner) disable(remove, ownerOnly('remove'));
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
  const zone = el('<div class="card danger-zone"></div>');
  zone.appendChild(control(remove));
  wrap.appendChild(zone);

  render(wrap, opts);
}

let guardId = 0;

/** Refuse a control, and say why underneath it.
 *
 *  The reason used to live in title=, which needs a mouse to show — on the device this
 *  panel is actually used on it was unreachable, so the button was simply dead. It is
 *  printed instead, and the tap still opens the alert: the caption may have scrolled off
 *  by the time somebody presses the thing it explains.
 *
 *  Returns the wrapper, which is what callers must append. */
function disable(button, reason) {
  button.classList.add('is-guarded');
  button.setAttribute('aria-disabled', 'true');
  button.onclick = () => alertMessage(reason);

  const id = `guard-${++guardId}`;
  button.setAttribute('aria-describedby', id);
  const wrap = el('<div class="guard"></div>');
  wrap.appendChild(button);
  wrap.appendChild(el(`<p class="guard-reason" id="${id}">${esc(reason)}</p>`));
  return wrap;
}

/** The node to put on screen for `button`: the guard wrapper when it was refused, and
 *  the button itself otherwise. Lets a caller build a control, maybe refuse it, and
 *  append the result without each site knowing which happened. */
function control(button) {
  return button.parentNode || button;
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
  window.addEventListener('popstate', () => { if (state.detail) closeDetail(); });
  window.addEventListener('resize', measureTopbar);

  document.querySelectorAll('.tab').forEach((btn) => {
    btn.onclick = () => { hapticSelect(); selectTab(btn.dataset.tab); };
  });
  $('#refresh').onclick = refreshAll;
  const back = $('#back');
  if (back) back.onclick = () => { haptic(); popDetail(); };

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
