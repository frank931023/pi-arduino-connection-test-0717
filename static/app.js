'use strict';

/*
 * Local page for one Pi. Same look and behaviour as the PC Motion Control Center,
 * but it talks to this Pi's own API (/api/status, /api/ping, /api/command).
 *
 * The buttons come from GET /api/commands (commands.py), the same catalog the PC uses.
 * While any command runs on this Pi (from this page or from the PC), /api/status
 * answers "busy" without touching the serial port, and new commands are refused.
 */

// ===== Config =====

const STATUS_POLL_MS = 10000;
const BUSY_POLL_MS = 2000;      // poll faster while another client's command runs
const HISTORY_MAX = 30;
const STATUS_TEXT = { online: 'Online', busy: 'Busy', 'no-arduino': 'No Arduino', offline: 'Offline', checking: 'Checking' };

// ===== State =====

const state = {
  running: null,       // command started from this page
  remote: null,        // { command, seconds } running from another client (e.g. the PC)
  catalog: null,       // from /api/commands
  history: [],
  checking: false,
};

// ===== Utilities =====

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clock = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

/** fetch wrapper: never throws, always times out. Resolves to { ok, status, data, error }. */
async function api(method, path, body, timeoutMs = 10000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); } catch {
      return { ok: false, status: resp.status, data: {}, error: `Pi returned HTTP ${resp.status} (not JSON)` };
    }
    const ok = resp.ok && data.status === 'success';
    return { ok, status: resp.status, data, error: ok ? null : (data.error || `HTTP ${resp.status}`) };
  } catch (e) {
    return { ok: false, status: 0, data: {},
             error: e.name === 'AbortError' ? `No answer after ${Math.round(timeoutMs / 1000)} s` : 'Cannot reach this Pi' };
  } finally {
    clearTimeout(timer);
  }
}

function toast(msg, type = 'info') {
  const t = document.createElement('div');
  t.className = 'toast' + (type === 'error' ? ' error' : '');
  t.textContent = msg;
  $('#toasts').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, type === 'error' ? 5000 : 3000);
}

// ===== Status =====

function showStatus(kind, fields) {
  $('#badge').className = `badge ${kind}`;
  $('#badge').textContent = STATUS_TEXT[kind];
  $('#nav-dot').className = 'st ' + (kind === 'online' || kind === 'busy' ? 'ok' : kind === 'checking' ? '' : 'dead');
  const text = { online: 'Connected', 'no-arduino': 'Not responding',
                 busy: state.remote ? `Running ${state.remote.command} (${state.remote.seconds}s)` : 'Busy' }[kind];
  $('#s-status').textContent = text || STATUS_TEXT[kind];
  if ('rig' in fields) $('#s-rig').textContent = fields.rig || '–';
  if ('err' in fields) $('#s-err').textContent = fields.err || '–';
  if ('hb' in fields) {
    const hb = fields.hb;
    // Heartbeats are only read while a command runs, so this can be old or missing.
    $('#s-hb').textContent = hb ? `${hb.arduino_state} · ${hb.seconds_ago}s ago` : '–';
  }
}

async function checkStatus() {
  if (state.running || state.checking) return;
  state.checking = true;
  const res = await api('GET', '/api/status', null, 15000);
  state.checking = false;
  if (!res.ok) { setRemote(null); showStatus('offline', { err: res.error }); return; }
  const d = res.data;
  setRemote(d.arduino_status === 'busy' ? d.running : null);
  if (d.arduino_status === 'busy') showStatus('busy', { hb: d.heartbeat });
  else showStatus(d.arduino_status === 'connected' ? 'online' : 'no-arduino',
                  { rig: d.rig_name, err: d.arduino_error, hb: d.heartbeat });
}

/** A command started elsewhere is running: lock the buttons until it ends. */
function setRemote(job) {
  const changed = !!job !== !!state.remote;
  state.remote = job || null;
  if (changed) renderBusy();
}

async function ping() {
  const btn = $('#btn-ping');
  btn.disabled = true;
  btn.textContent = 'Pinging…';
  const res = await api('GET', '/api/ping', null, 15000);
  btn.disabled = false;
  btn.textContent = 'Ping Arduino';
  if (res.ok) {
    showStatus('online', { rig: res.data.rig_name, err: null });
    toast(`Arduino answered: ${res.data.rig_name || 'OK'}`);
  } else if (res.status === 409) {
    toast(`Busy running ${res.data.running ? res.data.running.command : 'a command'}`, 'error');
    checkStatus();
  } else {
    showStatus(res.status === 503 ? 'no-arduino' : 'offline', { err: res.error });
    toast(`Ping failed: ${res.error}`, 'error');
  }
}

// ===== Controls =====

async function loadCatalog() {
  const res = await api('GET', '/api/commands');
  if (!res.ok) {
    $('#groups').innerHTML = `<div class="empty">Cannot load the command list: ${esc(res.error)}</div>`;
    return;
  }
  state.catalog = res.data;
  renderGroups();
  renderBusy();
}

function renderGroups() {
  $('#groups').innerHTML = state.catalog.groups.map((g) => {
    const buttons = g.actions.map((a) => `
      <button class="cmd" data-action="cmd" data-cmd="${esc(a.cmd)}" data-timeout="${a.timeout}" title="${esc(a.cmd)}">${esc(a.label)}</button>`).join('');
    const move = g.move ? `
      <div class="mv">
        <input type="number" step="any" id="mv-${esc(g.move.prefix)}" placeholder="${esc(g.move.unit)}" data-enter="move-${esc(g.move.prefix)}">
        <button class="cmd primary" id="move-${esc(g.move.prefix)}" data-action="move"
          data-prefix="${esc(g.move.prefix)}" data-timeout="${g.move.timeout}">${esc(g.move.label)}</button>
      </div>` : '';
    return `
      <div class="grp">
        <div class="gh"><b>${esc(g.title)}</b><span>${esc(g.range)}</span></div>
        <div class="btns">${buttons}</div>
        ${move}
      </div>`;
  }).join('');
}

function renderHistory() {
  const el = $('#hist');
  if (!state.history.length) { el.innerHTML = '<div class="empty">No commands sent yet</div>'; return; }
  el.innerHTML = state.history.map((h, i) => {
    const result = h.pending
      ? `<span class="spin"></span><span class="res idle" data-elapsed="${h.at.getTime()}">Running…</span>`
      : `<span class="res ${h.cls}">${esc(h.text)}</span>`;
    return `
      <div class="it">
        <div class="lh"><code>${esc(h.cmd)}</code><span class="sp"></span>${result}<time>${clock(h.at)}</time></div>
        ${h.lines && h.lines.length && i < 5 ? `<pre>${esc(h.lines.join('\n'))}</pre>` : ''}
      </div>`;
  }).join('');
  tickElapsed();
}

function renderBusy() {
  const cmd = state.running || (state.remote && state.remote.command);
  const prefix = cmd ? cmd.split(',')[0] : null;
  $$('.cmd').forEach((b) => {
    b.disabled = !!cmd;
    b.classList.toggle('running', !!cmd && (b.dataset.cmd === cmd || b.dataset.prefix === prefix));
  });
  $('#btn-ping').disabled = !!cmd;
  $('#btn-refresh').disabled = !!state.running;
}

function tickElapsed() {
  $$('[data-elapsed]').forEach((el) => {
    el.textContent = `Running… ${Math.floor((Date.now() - Number(el.dataset.elapsed)) / 1000)}s`;
  });
}

async function runCommand(cmd, timeout) {
  if (state.running || state.remote) return;
  if (/[\r\n]/.test(cmd)) { toast('Command must be one line', 'error'); return; }
  state.running = cmd;
  const entry = { cmd, at: new Date(), pending: true };
  state.history.unshift(entry);
  if (state.history.length > HISTORY_MAX) state.history.length = HISTORY_MAX;
  renderHistory();
  renderBusy();

  // The Pi waits up to `timeout` for DONE/ERR; give the HTTP request a little longer.
  const res = await api('POST', '/api/command', { command: cmd, timeout }, (timeout + 10) * 1000);

  state.running = null;
  if (res.ok) {
    const raw = res.data.arduino_response;
    const lines = Array.isArray(raw) ? raw : (raw ? [String(raw)] : []);
    // ⚠ Firmware ends with DONE even when a move exceeds the travel limit.
    const exceeded = lines.some((l) => /exceeded max travel/i.test(l));
    if (res.data.arduino_result === 'DONE') {
      Object.assign(entry, exceeded ? { text: 'Out of range', cls: 'warn' } : { text: 'Done', cls: 'ok' });
    } else {
      Object.assign(entry, { text: 'Unknown command', cls: 'fail' });
    }
    entry.lines = lines;
    showStatus('online', {});
  } else {
    const busy = res.status === 409 && res.data.running;
    Object.assign(entry, { text: busy ? `Busy: ${busy.command} is running` : res.error, cls: 'fail', lines: [] });
    if (res.status === 503) showStatus('no-arduino', { err: res.error });
    else if (res.status === 0) showStatus('offline', { err: res.error });
  }
  entry.pending = false;
  renderHistory();
  renderBusy();
  if (res.status === 409) checkStatus();
}

// ===== Events =====

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el || el.disabled) return;
  switch (el.dataset.action) {
    case 'cmd':
      runCommand(el.dataset.cmd, Number(el.dataset.timeout));
      break;
    case 'move': {
      const input = $(`#mv-${el.dataset.prefix}`);
      const v = input.value.trim();
      if (v === '' || !Number.isFinite(Number(v))) { toast('Enter a number first', 'error'); input.focus(); return; }
      runCommand(`${el.dataset.prefix},${v}`, Number(el.dataset.timeout));
      break;
    }
    case 'custom': {
      const input = $('#custom');
      const cmd = input.value.trim();
      if (!cmd) { toast('Enter a command first', 'error'); input.focus(); return; }
      if (!state.catalog) { toast('Command list not loaded yet', 'error'); return; }
      runCommand(cmd, state.catalog.default_timeout);
      break;
    }
    case 'refresh': checkStatus(); break;
    case 'ping': ping(); break;
  }
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.dataset && e.target.dataset.enter) {
    e.preventDefault();
    const btn = document.getElementById(e.target.dataset.enter);
    if (btn && !btn.disabled) btn.click();
  }
});

document.addEventListener('visibilitychange', () => { if (!document.hidden) checkStatus(); });

// ===== Start =====

/** Poll every 10 s, or every 2 s while another client's command is running. */
function schedulePoll() {
  setTimeout(async () => {
    if (!document.hidden) await checkStatus();
    schedulePoll();
  }, state.remote ? BUSY_POLL_MS : STATUS_POLL_MS);
}

renderHistory();
loadCatalog();
checkStatus();
schedulePoll();
setInterval(tickElapsed, 1000);
