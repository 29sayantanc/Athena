'use strict';
// Shared message rendering for the popup (current question) and the app's Conversation page (everything).
(function () {
  const A = window.athena;
  const ICON_SPEAKER = '<svg viewBox="0 0 16 16"><path d="M2.75 6.25v3.5h2.5L8.5 12.5v-9L5.25 6.25z"/><path d="M11 6a2.75 2.75 0 0 1 0 4"/></svg>';
  const ICON_COPY = '<svg viewBox="0 0 16 16"><rect x="5.25" y="5.25" width="8" height="8" rx="1.75"/><path d="M10.75 5.25v-1A1.5 1.5 0 0 0 9.25 2.75h-5a1.5 1.5 0 0 0-1.5 1.5v5a1.5 1.5 0 0 0 1.5 1.5h1"/></svg>';
  const confirms = new Map();

  function md(text) {
    const html = window.marked ? window.marked.parse(text || '', { breaks: true, gfm: true }) : (text || '');
    return window.DOMPurify ? window.DOMPurify.sanitize(html) : String(text).replace(/</g, '&lt;');
  }
  function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const time = (ts) => new Date(ts * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

  function setConfirm(id, status) {
    for (const node of confirms.get(id) || []) {
      node.querySelectorAll('.row, .done').forEach((n) => n.remove());
      node.classList.remove('approved', 'denied');
      if (status === 'pending') {
        const row = el('div', 'row');
        const no = el('button', 'btn', 'Cancel');
        const yes = el('button', 'btn primary', 'Approve');
        no.onclick = () => A.emit('chat.confirm', { id, approve: false });
        yes.onclick = () => A.emit('chat.confirm', { id, approve: true });
        row.append(el('span', 'hint', 'Or say yes or no'), no, yes); node.append(row);
      } else {
        node.classList.add(status);
        node.append(el('div', 'done', status === 'approved' ? 'Approved' : 'Cancelled'));
      }
    }
  }

  /** opts.mode: 'popup' (question shown as a heading) or 'log' (chat bubbles). */
  function entryNode(entry, opts = {}) {
    let node;
    if (entry.role === 'user') {
      if (opts.mode === 'popup') {
        node = el('div', 'ask'); node.textContent = entry.text;
      } else {
        node = el('div', 'msg user');
        const t = el('div', 'text'); t.textContent = entry.text; node.append(t);
        if (entry.shot) node.append(el('div', 'meta', 'New screenshot'));
      }
    } else if (entry.role === 'athena') {
      node = el('div', 'msg athena');
      if (entry.md) {
        if (entry.text && !norm(entry.md).startsWith(norm(entry.text).slice(0, 40))) {
          const said = el('div', 'said', ICON_SPEAKER); const s = el('span'); s.textContent = entry.text; said.append(s); node.append(said);
        }
        node.append(el('div', 'md', md(entry.md)));
      } else node.append(el('div', 'md', md(entry.text)));
      const copy = el('button', 'icon copy', ICON_COPY);
      copy.title = 'Copy'; copy.setAttribute('aria-label', 'Copy');
      copy.onclick = () => { A.copy(entry.md || entry.text); copy.classList.add('done'); setTimeout(() => copy.classList.remove('done'), 900); };
      node.append(copy);
    } else if (entry.role === 'note') {
      node = el('div', 'note'); node.textContent = entry.text;
    } else if (entry.role === 'confirm') {
      node = el('div', 'confirm');
      node.append(el('div', 'head', 'Needs your approval'));
      const act = el('div', 'act'); act.textContent = entry.text; node.append(act);
      if (entry.md) node.append(el('div', 'md', md(entry.md)));
      confirms.set(entry.id, [...(confirms.get(entry.id) || []), node]);
      setConfirm(entry.id, entry.status || 'pending');
    } else if (entry.role === 'divider') {
      node = el('div', 'divider'); node.textContent = time(entry.ts);
    } else return null;
    return node;
  }

  function clearConfirms() { confirms.clear(); }

  // Mic button with three unmistakable states:
  //   idle      mic icon             click = start listening
  //   listening olive pill, live bars "Send"   click = stop and send
  //   speaking  "Stop" pill          click = silence Athena (doesn't record)
  const MIC_HTML =
    '<span class="m-idle"><svg viewBox="0 0 16 16"><rect x="5.75" y="1.75" width="4.5" height="7.5" rx="2.25"/><path d="M3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2.25"/></svg></span>' +
    '<span class="m-live"><span class="bars"><i></i><i></i><i></i><i></i></span>Send</span>' +
    '<span class="m-stop"><svg viewBox="0 0 16 16"><rect x="4.5" y="4.5" width="7" height="7" rx="1.25"/></svg>Stop</span>';
  const MIC_LABEL = { idle: 'Speak', listening: 'Stop listening and send', speaking: 'Stop Athena talking' };
  function micControl(btn, composer, input) {
    btn.classList.add('mic');
    btn.innerHTML = MIC_HTML;
    const bars = [...btn.querySelectorAll('.bars i')];
    const placeholder = input.getAttribute('placeholder');
    let mode = 'idle';
    const ctl = {
      set(state) {
        mode = state === 'listening' ? 'listening' : state === 'speaking' ? 'speaking' : 'idle';
        btn.dataset.state = mode;
        btn.title = MIC_LABEL[mode]; btn.setAttribute('aria-label', MIC_LABEL[mode]);
        composer.classList.toggle('listening', mode === 'listening');
        input.setAttribute('placeholder', mode === 'listening' ? 'Listening…' : placeholder);
        if (mode !== 'listening') bars.forEach((b) => { b.style.height = ''; });
      },
      level(v) {
        if (mode !== 'listening') return;
        const t = performance.now() / 110;
        bars.forEach((b, i) => { b.style.height = `${3 + Math.min(1, v) * 11 * (0.55 + 0.45 * Math.abs(Math.sin(t + i * 1.7)))}px`; });
      },
      action() { return mode === 'listening' ? 'stop' : mode === 'speaking' ? 'stop_speaking' : 'start'; },
    };
    ctl.set('idle');
    return ctl;
  }

  window.ChatView = { md, entryNode, setConfirm, clearConfirms, time, micControl };
})();
