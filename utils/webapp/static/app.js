/* Admin panel — four screens, no framework, no build step.
 *
 * Every Telegram API call below is optional-chained. Telegram Desktop and older Android
 * builds lag the Bot API by several versions, and a panel that throws on a missing
 * HapticFeedback is worse than one that simply doesn't buzz.
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
  alertFilter: { type: '', unit: '' },
  alertItems: [],
  alertNext: null,
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

function haptic(style) {
  try { tg.HapticFeedback.impactOccurred(style || 'light'); } catch (_) { /* older client */ }
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

function alertMessage(message) {
  if (tg && tg.showAlert) tg.showAlert(message);
  else window.alert(message);
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

function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

/** Day bucket label for the feed's sticky headers, in the panel's own local reading of
 *  the timestamp — the server already sent it converted to Eastern. */
function dayLabel(iso) {
  const d = new Date(iso);
  const today = new Date();
  const yday = new Date(today.getTime() - 86400000);
  const same = (a, b) => a.toDateString() === b.toDateString();
  if (same(d, today)) return 'Today';
  if (same(d, yday)) return 'Yesterday';
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
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

function setLoading() {
  $('#main').innerHTML = '<div class="loading"><span class="spinner"></span></div>';
}

function render(node) {
  const main = $('#main');
  main.innerHTML = '';
  main.appendChild(node);
  main.scrollTop = 0;
  window.scrollTo(0, 0);
}

/** Push a detail view over the current tab.
 *
 *  history.pushState is here for Android's hardware back button: without a history entry
 *  to pop, it closes the whole Mini App from a detail screen, which reads as a crash. */
function pushDetail(detail) {
  state.detail = detail;
  history.pushState({ detail: true }, '');
  if (tg && tg.BackButton) tg.BackButton.show();
  renderCurrent();
}

function popDetail() {
  state.detail = null;
  if (tg && tg.BackButton) tg.BackButton.hide();
  if (tg && tg.MainButton) tg.MainButton.hide();
  renderCurrent();
}

function selectTab(tab) {
  state.tab = tab;
  state.detail = null;
  if (tg && tg.BackButton) tg.BackButton.hide();
  if (tg && tg.MainButton) tg.MainButton.hide();
  document.querySelectorAll('.tab').forEach((btn) => {
    btn.classList.toggle('is-active', btn.dataset.tab === tab);
  });
  renderCurrent();
}

function renderCurrent() {
  $('#screen-title').textContent = state.detail ? state.detail.title : TITLES[state.tab];
  setLoading();
  const view = state.detail
    ? { group: groupDetail, admin: adminDetail }[state.detail.type]
    : { dashboard: dashboardScreen, groups: groupsScreen,
        alerts: alertsScreen, admins: adminsScreen }[state.tab];
  view().catch((e) => {
    if (e.message === 'unauthenticated') return;
    render(el(`<div class="empty">${esc(e.message)}</div>`));
  });
}

// ── screen 1: dashboard ─────────────────────────────────────────────────────────

const PERIODS = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days']];

async function dashboardScreen() {
  const data = await api(`/stats?period=${encodeURIComponent(state.period)}`);
  const wrap = el('<div></div>');

  const seg = el('<div class="segmented"></div>');
  PERIODS.forEach(([key, label]) => {
    const b = el(`<button${key === state.period ? ' class="is-active"' : ''}>${label}</button>`);
    b.onclick = () => { haptic(); state.period = key; renderCurrent(); };
    seg.appendChild(b);
  });
  wrap.appendChild(seg);

  // The muted warning comes before the numbers on purpose: a silent group is a problem
  // the numbers themselves cannot show, because its alerts are missing from them.
  if (data.groups.muted > 0) {
    const n = data.groups.muted;
    const card = el(`<div class="warn-card">
        <span style="font-size:22px">🔕</span>
        <div><b>${n} group${n > 1 ? 's are' : ' is'} muted</b>
          <small>${esc(data.groups.muted_titles.join(', '))}</small></div>
      </div>`);
    card.onclick = () => { haptic(); selectTab('groups'); };
    wrap.appendChild(card);
  }

  wrap.appendChild(el(`<div class="card headline">
      <div class="headline-number">${data.totals.total}</div>
      <div class="headline-label">alerts · ${esc(data.period.label)}</div>
    </div>`));

  wrap.appendChild(el(`<div class="tiles">
      <div class="tile"><div class="tile-value">${data.totals.units}</div>
        <div class="tile-label">Units</div></div>
      <div class="tile"><div class="tile-value">${data.totals.speeding}</div>
        <div class="tile-label">Speeding</div></div>
      <div class="tile"><div class="tile-value">${data.totals.crashes}</div>
        <div class="tile-label">Crashes</div></div>
    </div>`));

  if (data.by_day && data.by_day.length > 1) {
    const peak = Math.max(...data.by_day.map((d) => d.total), 1);
    const card = el('<div class="card"><div class="card-title">Per day</div></div>');
    const trend = el('<div class="trend"></div>');
    data.by_day.forEach((d) => {
      const pct = Math.round((d.total / peak) * 100);
      const label = new Date(d.day + 'T12:00:00').toLocaleDateString('en-US', { weekday: 'narrow' });
      trend.appendChild(el(`<div class="trend-col" title="${d.total}">
          <div class="trend-bar" style="height:${pct}%"></div>
          <div class="trend-day">${label}</div></div>`));
    });
    card.appendChild(trend);
    wrap.appendChild(card);
  }

  if (data.top_units.length) {
    const peak = data.top_units[0].total || 1;
    const card = el('<div class="card"><div class="card-title">Top units</div></div>');
    data.top_units.forEach((u) => {
      const row = el(`<div class="bar-row">
          <span class="bar-name">🚛 ${esc(u.vehicle_number)}</span>
          <span class="bar-track"><span class="bar-fill"
                style="width:${Math.round((u.total / peak) * 100)}%"></span></span>
          <span class="bar-count">${u.total}</span></div>`);
      row.style.cursor = 'pointer';
      row.onclick = () => {
        haptic();
        state.alertFilter = { type: '', unit: u.vehicle_number };
        selectTab('alerts');
      };
      card.appendChild(row);
    });
    wrap.appendChild(card);
  }

  if (data.by_type.length) {
    const peak = data.by_type[0].total || 1;
    const card = el('<div class="card"><div class="card-title">By type</div></div>');
    data.by_type.forEach((t) => {
      card.appendChild(el(`<div class="bar-row">
          <span class="bar-name">${esc(t.emoji)} ${esc(t.label)}</span>
          <span class="bar-track"><span class="bar-fill"
                style="width:${Math.round((t.total / peak) * 100)}%"></span></span>
          <span class="bar-count">${t.total}</span></div>`));
    });
    wrap.appendChild(card);
  }

  if (!data.totals.total) {
    wrap.appendChild(el('<div class="empty">✅ No alerts in this period.</div>'));
  }
  render(wrap);
}

// ── screen 2: groups ────────────────────────────────────────────────────────────

async function groupsScreen() {
  const groups = await api('/groups');
  const wrap = el('<div></div>');

  if (!groups.length) {
    render(el('<div class="empty">No groups registered yet.<br>'
      + 'Add the bot to a truck\'s group to get started.</div>'));
    return;
  }

  const rows = el('<div class="rows"></div>');
  groups.forEach((g) => {
    const unitBadge = g.is_main
      ? '<span class="badge badge-main">ALL UNITS</span>'
      : `<span class="badge badge-unit">${esc(g.vehicle_number || '—')}</span>`;
    const muted = g.enabled ? '' : ' <span class="badge badge-muted">MUTED</span>';
    const filter = g.filter_mode === 'all' ? 'All events' : `${g.event_types.length} event types`;

    const row = el(`<button class="row">
        <div class="row-main">
          <div class="row-title">${unitBadge}${muted}
            ${esc(g.title || 'Untitled group')}</div>
          <div class="row-sub">${filter} · ${g.alerts_7d} alerts this week</div>
        </div>
        <span class="row-chevron">›</span>
      </button>`);
    row.onclick = () => {
      haptic();
      pushDetail({ type: 'group', id: g.telegram_group_id, title: g.title || 'Group' });
    };
    rows.appendChild(row);
  });
  wrap.appendChild(rows);

  if (state.boot.crash_group_id) {
    wrap.appendChild(el(`<div class="note">💥 Crash alerts go to a dedicated chat
      configured in the deployment settings, not listed here.</div>`));
  }
  render(wrap);
}

async function groupDetail() {
  const id = state.detail.id;
  const [groups, roster] = await Promise.all([api('/groups'), api('/units')]);
  const g = groups.find((x) => x.telegram_group_id === id);
  if (!g) { popDetail(); return; }

  const wrap = el('<div></div>');

  // — alerts on/off —
  const card = el('<div class="card"></div>');
  const field = el(`<div class="field"><span class="field-label">Alerts</span></div>`);
  const sw = el(`<button class="switch${g.enabled ? ' is-on' : ''}"></button>`);
  sw.onclick = async () => {
    const next = !sw.classList.contains('is-on');
    sw.classList.toggle('is-on', next);      // optimistic; reconciled by the reload below
    haptic();
    try {
      await post(`/groups/${id}/enabled`, { enabled: next });
      hapticResult(true);
    } catch (e) {
      sw.classList.toggle('is-on', !next);
      hapticResult(false);
      alertMessage(e.message);
    }
  };
  field.appendChild(sw);
  card.appendChild(field);
  card.appendChild(el(`<div class="field"><span class="field-label">Chat ID</span>
      <span class="field-value">${esc(String(id))}</span></div>`));
  wrap.appendChild(card);

  // — unit —
  if (!g.is_main) {
    const unitCard = el('<div class="card"><div class="card-title">Unit</div></div>');
    if (roster.available) {
      // A picker, not a text box: the roster's spelling is the only one alert routing
      // matches, so handing over the exact strings removes the whole class of typo.
      const select = el('<select class="input"></select>');
      select.appendChild(el(`<option value="">— choose a unit —</option>`));
      roster.units.forEach((u) => {
        const taken = u.linked && u.name !== g.vehicle_number ? ' (already linked)' : '';
        const sel = u.name === g.vehicle_number ? ' selected' : '';
        select.appendChild(el(
          `<option value="${esc(u.name)}"${sel}>${esc(u.name)}${taken}</option>`));
      });
      select.onchange = () => saveUnit(id, select.value);
      const wrap = el('<div class="select-wrap"></div>');
      wrap.appendChild(select);
      unitCard.appendChild(wrap);
    } else {
      const input = el(`<input class="input" value="${esc(g.vehicle_number || '')}"
                         placeholder="e.g. 1234">`);
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
  const selected = new Set(g.event_types);
  const chips = el('<div class="chips"></div>');

  const allChip = el(`<button class="chip${g.filter_mode === 'all' ? ' is-on' : ''}">All types</button>`);
  allChip.onclick = () => toggleEvent(id, { action: 'all' });
  chips.appendChild(allChip);

  state.boot.event_types.forEach((t) => {
    const on = g.filter_mode === 'all' || selected.has(t.type);
    const chip = el(`<button class="chip${on ? ' is-on' : ''}">${esc(t.emoji)} ${esc(t.label)}</button>`);
    chip.onclick = () => toggleEvent(id, { action: 'toggle', event_type: t.type });
    chips.appendChild(chip);
  });
  evCard.appendChild(chips);
  evCard.appendChild(el(`<div class="note">${g.filter_mode === 'all'
    ? 'Receiving every event type, including any added later.'
    : 'Only the highlighted types are delivered to this group.'}</div>`));
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
        hapticResult(true);
        popDetail();
      } catch (e) { hapticResult(false); alertMessage(e.message); }
    };
    zone.appendChild(btn);
    wrap.appendChild(zone);
  }

  render(wrap);
}

async function saveUnit(id, unit) {
  if (!unit) return;
  try {
    const res = await post(`/groups/${id}/unit`, { unit });
    hapticResult(true);
    if (res.note) alertMessage(res.note);
    renderCurrent();
  } catch (e) {
    hapticResult(false);
    // The server sends "did you mean" candidates with a 409; showing them is the whole
    // point of the endpoint returning them rather than a bare rejection.
    const hint = (e.data && e.data.suggestions && e.data.suggestions.length)
      ? `\n\nDid you mean: ${e.data.suggestions.join(', ')}`
      : '';
    alertMessage(e.message + hint);
    renderCurrent();
  }
}

async function toggleEvent(id, body) {
  haptic();
  try {
    // The response carries the authoritative list — the collapse rule that turns a full
    // allowlist back into "all types" lives in next_event_filter on the server, and is
    // deliberately not reimplemented here where it would drift.
    await post(`/groups/${id}/events`, body);
    hapticResult(true);
    renderCurrent();
  } catch (e) { hapticResult(false); alertMessage(e.message); }
}

// ── screen 3: alerts ────────────────────────────────────────────────────────────

async function alertsScreen(append) {
  const params = new URLSearchParams({ limit: '50' });
  if (state.alertFilter.type) params.set('type', state.alertFilter.type);
  if (state.alertFilter.unit) params.set('unit', state.alertFilter.unit);
  if (append && state.alertNext) {
    params.set('before_ts', state.alertNext.before_ts);
    params.set('before_id', state.alertNext.before_id);
  }

  const data = await api(`/alerts?${params.toString()}`);
  state.alertItems = append ? state.alertItems.concat(data.items) : data.items;
  state.alertNext = data.next;

  const wrap = el('<div></div>');

  const bar = el('<div class="filter-bar"></div>');
  const filters = [['', 'All'], ['speeding', '🚨 Speeding'], ['crash', '💥 Crash'],
                   ['hard_brake', '🛑 Hard brake'], ['cell_phone', '📵 Phone']];
  filters.forEach(([type, label]) => {
    const chip = el(`<button class="chip${state.alertFilter.type === type ? ' is-on' : ''}">${label}</button>`);
    chip.onclick = () => {
      haptic();
      state.alertFilter.type = type;
      state.alertNext = null;
      renderCurrent();
    };
    bar.appendChild(chip);
  });
  if (state.alertFilter.unit) {
    const chip = el(`<button class="chip is-on">🚛 ${esc(state.alertFilter.unit)} ✕</button>`);
    chip.onclick = () => { haptic(); state.alertFilter.unit = ''; renderCurrent(); };
    bar.appendChild(chip);
  }
  wrap.appendChild(bar);

  if (!state.alertItems.length) {
    wrap.appendChild(el('<div class="empty">No alerts match this filter.</div>'));
    render(wrap);
    return;
  }

  let currentDay = null;
  let rows = null;
  state.alertItems.forEach((item) => {
    const day = dayLabel(item.occurred_at);
    if (day !== currentDay) {
      currentDay = day;
      wrap.appendChild(el(`<div class="day-header">${esc(day)}</div>`));
      rows = el('<div class="rows"></div>');
      wrap.appendChild(rows);
    }
    const sev = item.severity ? ` · ${esc(item.severity)}` : '';
    rows.appendChild(el(`<div class="row" style="cursor:default">
        <div class="row-main">
          <div class="row-title">${esc(item.emoji)} ${esc(item.label)}</div>
          <div class="row-sub">🚛 ${esc(item.vehicle_number)}${sev}</div>
        </div>
        <span class="row-side">${esc(fmtTime(item.occurred_at))}</span>
      </div>`));
  });

  if (state.alertNext) {
    const more = el('<button class="btn btn-secondary btn-block" style="margin-top:14px">Load more</button>');
    more.onclick = () => {
      more.textContent = 'Loading…';
      alertsScreen(true).catch(() => { more.textContent = 'Load more'; });
    };
    wrap.appendChild(more);
  }
  render(wrap);
}

// ── screen 4: admins ────────────────────────────────────────────────────────────

async function adminsScreen() {
  const admins = await api('/admins');
  const wrap = el('<div></div>');

  const rows = el('<div class="rows"></div>');
  admins.forEach((a) => {
    const badges = (a.is_super ? '<span class="badge badge-super">SUPER</span> ' : '')
      + (a.is_active ? '' : '<span class="badge badge-muted">INACTIVE</span> ');
    const handle = a.username ? `@${esc(a.username)}` : `ID ${esc(String(a.telegram_id))}`;
    const row = el(`<button class="row">
        <div class="row-main">
          <div class="row-title">${badges}${esc(a.full_name || 'Unknown')}</div>
          <div class="row-sub">${handle}</div>
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
    add.onclick = addAdmin;
    wrap.appendChild(add);
  } else {
    wrap.appendChild(el('<div class="note">Only a super admin can change this list.</div>'));
  }
  render(wrap);
}

async function adminDetail() {
  const admins = await api('/admins');
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
        <span class="field-value">${a.is_active ? 'Active' : 'Inactive'}</span></div>
    </div>`));

  if (!isSuper) {
    wrap.appendChild(el('<div class="note">Only a super admin can change this.</div>'));
    render(wrap);
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
  else activeBtn.onclick = () => mutateAdmin(a.id, { is_active: !a.is_active });
  actions.appendChild(activeBtn);

  if (!a.is_super) {
    const promote = el('<button class="btn btn-secondary btn-block" style="margin-bottom:10px">⭐ Make super admin</button>');
    if (!a.is_active) disable(promote, 'Activate this admin before promoting them.');
    else promote.onclick = async () => {
      if (await confirmAction(`Make ${a.full_name || 'this admin'} a super admin?`)) {
        mutateAdmin(a.id, { is_super: true });
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
      hapticResult(true);
      popDetail();
    } catch (e) { hapticResult(false); alertMessage(e.message); }
  };
  const zone = el('<div class="danger-zone"></div>');
  zone.appendChild(remove);
  wrap.appendChild(zone);

  render(wrap);
}

function disable(button, reason) {
  button.disabled = true;
  button.style.opacity = '.45';
  button.title = reason;
  button.onclick = () => alertMessage(reason);
  button.disabled = false;   // keep it tappable so the reason can be shown
}

async function mutateAdmin(id, body) {
  try {
    await post(`/admins/${id}/update`, body);
    hapticResult(true);
    renderCurrent();
  } catch (e) { hapticResult(false); alertMessage(e.message); }
}

function addAdmin() {
  const wrap = el(`<div>
      <div class="card">
        <div class="card-title">Telegram ID</div>
        <input class="input" id="new-admin-id" inputmode="numeric" placeholder="e.g. 123456789">
      </div>
    </div>`);
  const save = el('<button class="btn btn-block">Add admin</button>');
  save.onclick = async () => {
    const value = wrap.querySelector('#new-admin-id').value.trim();
    if (!value) return;
    try {
      await post('/admins', { telegram_id: Number(value) });
      hapticResult(true);
      selectTab('admins');
    } catch (e) { hapticResult(false); alertMessage(e.message); }
  };
  wrap.appendChild(save);
  state.detail = { type: 'addadmin', title: 'Add admin' };
  history.pushState({ detail: true }, '');
  if (tg && tg.BackButton) tg.BackButton.show();
  $('#screen-title').textContent = 'Add admin';
  render(wrap);
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
  }
  window.addEventListener('popstate', () => { if (state.detail) popDetail(); });

  document.querySelectorAll('.tab').forEach((btn) => {
    btn.onclick = () => { haptic(); selectTab(btn.dataset.tab); };
  });

  try {
    state.boot = await api('/bootstrap');
  } catch (e) {
    if (e.message === 'unauthenticated') return;
    $('#app').hidden = false;
    render(el(`<div class="empty">${esc(e.message)}</div>`));
    return;
  }

  $('#app').hidden = false;
  $('#company-name').textContent = state.boot.company.name;
  renderCurrent();
}

boot();
