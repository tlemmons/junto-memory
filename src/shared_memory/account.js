// Junto account widget, shared by /admin, /backlog and /dashboard (served at /ui/account.js).
// Put <span id="juntoAccount"></span> in the page header and load this script before
// the page's own. It renders the login form (username + password, or an API key) and,
// once logged in, the account menu: change password and API keys (create, revoke).
//
// Page API:
//   JuntoUI.ready             Promise of the first `me`
//   JuntoUI.me                {name, role, can_write, via: "session"|"key"} or null
//   JuntoUI.onChange(fn)      fn(me) after every login / logout
//   JuntoUI.headers(extra)    headers for fetch: X-Junto-UI (CSRF) + Bearer when a key is in use
//   JuntoUI.api(path, opts)   fetch → parsed JSON; non-2xx gets {error}
// The login cookie is HttpOnly; this script never sees it. A pasted API key lives only in
// this tab's sessionStorage (slot junto_admin_key, as before).
(function () {
  const KEY_SLOT = 'junto_admin_key';
  let KEY = null; try { KEY = sessionStorage.getItem(KEY_SLOT); } catch (e) {}
  const listeners = [];
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function headers(extra) {
    const h = Object.assign({ 'X-Junto-UI': '1' }, extra || {});
    if (KEY) h['Authorization'] = 'Bearer ' + KEY;
    return h;
  }
  async function api(path, opts = {}) {
    const h = headers(opts.headers);
    if (opts.body) h['Content-Type'] = 'application/json';
    let r; try { r = await fetch(path, { ...opts, headers: h, credentials: 'same-origin' }); }
    catch (e) { return { error: 'network error: ' + e.message }; }
    let data; try { data = await r.json(); } catch (e) { data = { error: 'HTTP ' + r.status }; }
    if (!r.ok && !data.error) data.error = 'HTTP ' + r.status;
    return data;
  }
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });

  const UI = { me: null, headers, api, onChange: fn => listeners.push(fn) };
  window.JuntoUI = UI;

  async function refreshMe(notify) {
    const me = await api('/admin/api/whoami');
    if (me.error && KEY) {           // a stale or revoked key: drop it, fall back to the cookie
      KEY = null; try { sessionStorage.removeItem(KEY_SLOT); } catch (e) {}
      return refreshMe(notify);
    }
    UI.me = me.error ? null : me;
    render();
    if (notify) listeners.forEach(fn => { try { fn(UI.me); } catch (e) { console.error(e); } });
    return UI.me;
  }

  // ── styles (self-contained; picks up the page's CSS variables when present) ──
  const css = document.createElement('style');
  css.textContent = `
  #juntoAccount { position: relative; display: inline-block; font-size: 12px; }
  #juntoAccount .ja-btn { background: #21262d; color: var(--text, #c9d1d9); border: 1px solid var(--border, #30363d);
    border-radius: 4px; padding: 5px 11px; font-size: 12px; cursor: pointer; font-family: inherit; }
  #juntoAccount .ja-btn:hover { border-color: var(--accent, #58a6ff); }
  #juntoAccount .ja-btn.pri { background: #1f6feb; border-color: #1f6feb; color: #fff; }
  #juntoAccount .ja-btn.bad { color: var(--red, #f85149); }
  #juntoAccount .ja-panel { position: absolute; right: 0; top: calc(100% + 6px); z-index: 50; width: 360px; max-width: 94vw;
    background: var(--panel, #161b22); color: var(--text, #c9d1d9); border: 1px solid var(--border, #30363d);
    border-radius: 8px; padding: 12px; box-shadow: 0 8px 24px rgba(0,0,0,.5); text-align: left; }
  #juntoAccount .ja-panel.wide { width: 560px; }
  #juntoAccount input, #juntoAccount select { background: var(--bg, #0d1117); color: var(--text, #c9d1d9);
    border: 1px solid var(--border, #30363d); border-radius: 4px; padding: 5px 7px; font-size: 12.5px; font-family: inherit; width: 100%; margin-bottom: 6px; }
  #juntoAccount .ja-h { font-size: 11px; font-weight: 600; color: var(--accent, #58a6ff); text-transform: uppercase;
    letter-spacing: .5px; margin: 12px 0 6px; }
  #juntoAccount .ja-muted { color: var(--muted, #8b949e); }
  #juntoAccount .ja-err { color: var(--red, #f85149); margin: 4px 0; min-height: 1em; }
  #juntoAccount .ja-row { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  #juntoAccount table { width: 100%; border-collapse: collapse; font-size: 12px; }
  #juntoAccount td, #juntoAccount th { padding: 4px 4px; border-bottom: 1px solid var(--border, #30363d); text-align: left; vertical-align: top; }
  #juntoAccount th { color: var(--muted, #8b949e); font-weight: 500; font-size: 10.5px; text-transform: uppercase; }
  #juntoAccount .ja-key { font-family: ui-monospace, monospace; word-break: break-all; background: var(--bg, #0d1117);
    border: 1px solid var(--green, #3fb950); border-radius: 4px; padding: 6px; margin: 6px 0; }
  #juntoAccount a { color: var(--accent, #58a6ff); cursor: pointer; }
  #juntoAccount .ja-scroll { max-height: 240px; overflow: auto; }`;
  document.head.appendChild(css);

  let OPEN = false, VIEW = 'menu', KEYS = null, NEWKEY = null, MSG = '';
  function root() { return document.getElementById('juntoAccount'); }

  function render() {
    const el = root(); if (!el) return;
    const me = UI.me;
    const label = me ? `${esc(me.name)} ▾` : 'Log in';
    let panel = '';
    if (OPEN) panel = `<div class="ja-panel${me && VIEW === 'keys' ? ' wide' : ''}">${me ? menuHtml(me) : loginHtml()}</div>`;
    el.innerHTML = `<button class="ja-btn${me ? '' : ' pri'}" id="jaToggle" title="${me ? esc(me.role + ' · ' + (me.via === 'key' ? 'API key' : 'logged in')) : 'Log in to edit'}">${label}</button>${panel}`;
    wire();
  }

  function loginHtml() {
    if (VIEW === 'key') return `
      <div class="ja-h" style="margin-top:0">Use an API key</div>
      <input id="jaKey" type="password" placeholder="smk_…" autocomplete="off">
      <div class="ja-err" id="jaErr">${esc(MSG)}</div>
      <div class="ja-row"><button class="ja-btn pri" id="jaUseKey">Use key</button>
        <a id="jaToPw" class="ja-muted">← username and password</a></div>
      <div class="ja-muted" style="margin-top:8px">The key is kept in this tab only.</div>`;
    return `
      <div class="ja-h" style="margin-top:0">Log in</div>
      <form id="jaLogin" autocomplete="on">
        <input id="jaUser" name="username" placeholder="username" autocomplete="username">
        <input id="jaPw" name="password" type="password" placeholder="password" autocomplete="current-password">
        <div class="ja-err" id="jaErr">${esc(MSG)}</div>
        <div class="ja-row"><button class="ja-btn pri" type="submit">Log in</button>
          <a id="jaToKey" class="ja-muted">use an API key instead</a></div>
      </form>
      <div class="ja-muted" style="margin-top:8px">Stays logged in on this browser for 30 days.
        No account? On the server host: <code>docker exec -it mcp-rag-arch python -m shared_memory.ui_user add &lt;name&gt;</code></div>`;
  }

  function menuHtml(me) {
    const head = `<div><b>${esc(me.name)}</b> <span class="ja-muted">· ${esc(me.role)} · ${me.via === 'key' ? 'API key' : 'logged in'}</span></div>
      ${me.can_write ? '' : '<div class="ja-muted">Read-only: editing needs the owner role.</div>'}
      <div class="ja-row" style="margin-top:8px">
        ${me.via === 'key' ? '<button class="ja-btn" id="jaForget">Forget key</button>'
          : '<button class="ja-btn" id="jaLogout">Log out</button><button class="ja-btn" id="jaLogoutAll" title="End this account\'s sessions in every browser">Log out everywhere</button>'}
      </div>
      <div class="ja-row" style="margin-top:8px">
        ${me.via === 'session' ? `<a id="jaToPwChange">${VIEW === 'pw' ? '▾' : '▸'} Change password</a>` : ''}
        ${me.can_write ? `<a id="jaToKeys">${VIEW === 'keys' ? '▾' : '▸'} API keys</a>` : ''}
      </div>
      <div class="ja-err" id="jaErr">${esc(MSG)}</div>`;
    if (VIEW === 'pw') return head + `
      <div class="ja-h">Change password</div>
      <input id="jaCur" type="password" placeholder="current password" autocomplete="current-password">
      <input id="jaNew" type="password" placeholder="new password (10+ characters)" autocomplete="new-password">
      <input id="jaNew2" type="password" placeholder="new password again" autocomplete="new-password">
      <button class="ja-btn pri" id="jaPwSave">Change password</button>
      <div class="ja-muted" style="margin-top:6px">Other browsers are logged out; this one stays in.</div>`;
    if (VIEW === 'keys') return head + keysHtml();
    return head;
  }

  function keysHtml() {
    if (!KEYS) return '<div class="ja-h">API keys</div><div class="ja-muted">Loading…</div>';
    if (KEYS.error) return `<div class="ja-h">API keys</div><div class="ja-err">${esc(KEYS.error)}</div>`;
    const roles = (KEYS.roles || ['agent']).map(r => `<option${r === 'agent' ? ' selected' : ''}>${esc(r)}</option>`).join('');
    const rows = (KEYS.keys || []).map(k => `<tr><td>${esc(k.name)}<div class="ja-muted">${esc(k.key_prefix || '')}…</div></td>
        <td>${esc(k.role)}</td><td>${esc((k.projects || []).join(', ') || 'all')}</td>
        <td class="ja-muted" title="${esc(k.last_used || '')}">${k.last_used ? esc(String(k.last_used).slice(0, 10)) : 'never'}</td>
        <td><button class="ja-btn bad" data-revoke="${esc(k.name)}">Revoke</button></td></tr>`).join('');
    return `
      ${NEWKEY ? `<div class="ja-h">New key: ${esc(NEWKEY.name)}</div>
        <div class="ja-key" id="jaNewKey">${esc(NEWKEY.key)}</div>
        <div class="ja-row"><button class="ja-btn pri" id="jaCopy">Copy</button>
          <span class="ja-muted">Shown once. Only its hash is stored, so copy it now.</span></div>` : ''}
      <div class="ja-h">Create a key</div>
      <div class="ja-row" style="flex-wrap:nowrap">
        <input id="jaKName" placeholder="name, e.g. laptop-claude" style="flex:2">
        <select id="jaKRole" style="flex:1">${roles}</select>
      </div>
      <input id="jaKProj" placeholder="projects, comma-separated (empty = all projects)">
      <button class="ja-btn pri" id="jaKCreate">Create key</button>
      <div class="ja-h">Active keys (${(KEYS.keys || []).length})</div>
      <div class="ja-muted" style="margin-bottom:4px">Existing keys can't be shown again; replace a lost one.</div>
      <div class="ja-scroll"><table><thead><tr><th>Name</th><th>Role</th><th>Projects</th><th>Last used</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="ja-muted">none</td></tr>'}</tbody></table></div>`;
  }

  const $ = id => document.getElementById(id);
  function setMsg(m) { MSG = m || ''; const e = $('jaErr'); if (e) e.textContent = MSG; }
  async function loadKeys() { KEYS = await api('/ui/api/keys'); render(); }

  function wire() {
    $('jaToggle').onclick = e => { e.stopPropagation(); OPEN = !OPEN; MSG = ''; if (!OPEN) NEWKEY = null; render(); if (OPEN && $('jaUser')) $('jaUser').focus(); };
    const panel = root().querySelector('.ja-panel'); if (panel) panel.onclick = e => e.stopPropagation();
    if ($('jaLogin')) $('jaLogin').onsubmit = async e => {
      e.preventDefault(); setMsg('');
      const r = await post('/ui/api/login', { username: $('jaUser').value, password: $('jaPw').value });
      if (r.error) { setMsg(r.error); $('jaPw').value = ''; $('jaPw').focus(); return; }
      OPEN = false; await refreshMe(true);
    };
    if ($('jaToKey')) $('jaToKey').onclick = () => { VIEW = 'key'; MSG = ''; render(); $('jaKey').focus(); };
    if ($('jaToPw')) $('jaToPw').onclick = () => { VIEW = 'menu'; MSG = ''; render(); };
    if ($('jaKey')) $('jaKey').onkeydown = e => { if (e.key === 'Enter') $('jaUseKey').click(); };
    if ($('jaUseKey')) $('jaUseKey').onclick = async () => {
      const k = $('jaKey').value.trim(); if (!k) return;
      const prev = KEY; KEY = k;
      const me = await api('/admin/api/whoami');
      if (me.error) { KEY = prev; setMsg('Key rejected: ' + me.error); return; }
      try { sessionStorage.setItem(KEY_SLOT, k); } catch (e) {}
      OPEN = false; VIEW = 'menu'; await refreshMe(true);
    };
    if ($('jaForget')) $('jaForget').onclick = async () => { KEY = null; try { sessionStorage.removeItem(KEY_SLOT); } catch (e) {} OPEN = false; await refreshMe(true); };
    if ($('jaLogout')) $('jaLogout').onclick = async () => { await post('/ui/api/logout'); OPEN = false; VIEW = 'menu'; await refreshMe(true); };
    if ($('jaLogoutAll')) $('jaLogoutAll').onclick = async () => {
      if (!confirm('Log this account out of every browser, including this one?')) return;
      const r = await post('/ui/api/logout_all'); if (r.error) { setMsg(r.error); return; }
      OPEN = false; VIEW = 'menu'; await refreshMe(true);
    };
    if ($('jaToPwChange')) $('jaToPwChange').onclick = () => { VIEW = VIEW === 'pw' ? 'menu' : 'pw'; MSG = ''; render(); if ($('jaCur')) $('jaCur').focus(); };
    if ($('jaToKeys')) $('jaToKeys').onclick = () => { VIEW = VIEW === 'keys' ? 'menu' : 'keys'; MSG = ''; NEWKEY = null; KEYS = null; render(); if (VIEW === 'keys') loadKeys(); };
    if ($('jaPwSave')) $('jaPwSave').onclick = async () => {
      if ($('jaNew').value !== $('jaNew2').value) { setMsg("New passwords don't match."); return; }
      const r = await post('/ui/api/password', { current: $('jaCur').value, new: $('jaNew').value });
      if (r.error) { setMsg(r.error); return; }
      VIEW = 'menu'; MSG = 'Password changed.'; render();
    };
    if ($('jaKCreate')) $('jaKCreate').onclick = async () => {
      const projects = $('jaKProj').value.split(',').map(s => s.trim()).filter(Boolean);
      const r = await post('/ui/api/keys/create', { name: $('jaKName').value.trim(), role: $('jaKRole').value, projects });
      if (r.error) { setMsg(r.error); return; }
      NEWKEY = r; MSG = ''; await loadKeys();
    };
    if ($('jaCopy')) $('jaCopy').onclick = async () => {
      try { await navigator.clipboard.writeText(NEWKEY.key); setMsg('Copied.'); }
      catch (e) { const r = document.createRange(); r.selectNodeContents($('jaNewKey')); const s = getSelection(); s.removeAllRanges(); s.addRange(r); setMsg('Selected: press Ctrl+C.'); }
    };
    root().querySelectorAll('[data-revoke]').forEach(b => b.onclick = async () => {
      const name = b.dataset.revoke;
      if (!confirm(`Revoke key "${name}"? Anything using it stops working at once.`)) return;
      const r = await post('/ui/api/keys/revoke', { name }); if (r.error) { setMsg(r.error); return; }
      if (NEWKEY && NEWKEY.name === name) NEWKEY = null;
      await loadKeys();
    });
  }
  document.addEventListener('click', () => { if (OPEN) { OPEN = false; NEWKEY = null; render(); } });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && OPEN) { e.stopImmediatePropagation(); OPEN = false; NEWKEY = null; render(); } }, true);

  UI.ready = new Promise(res => {
    const go = () => refreshMe(false).then(res);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go); else go();
  });
})();
