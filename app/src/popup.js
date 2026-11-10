'use strict';
// Athena popup: the details for the question you're asking right now, plus a follow-up box.
// The whole conversation lives in the app (Conversation page).
const A = window.athena;
const CV = window.ChatView;
const $ = (id) => document.getElementById(id);
const thread = $('thread');
const turnEl = $('turn');
const input = $('input');
let state = 'idle';
let pinned = true;
let currentTurn = null;
const mic = CV.micControl($('mic'), document.querySelector('.composer'), input);

function startTurn(turn) {
  currentTurn = turn;
  turnEl.replaceChildren();
  CV.clearConfirms();
}

function render(entry) {
  if (entry.role === 'divider') { startTurn(entry.turn); $('empty').hidden = false; return; }
  if (entry.turn !== currentTurn) startTurn(entry.turn);
  const node = CV.entryNode(entry, { mode: 'popup' });
  if (!node) return;
  $('empty').hidden = true;
  turnEl.append(node);
  // Long answers read from the top; everything else keeps the newest line in view.
  if (entry.role === 'athena' && entry.md && node.offsetHeight > thread.clientHeight * 0.6) thread.scrollTop = node.offsetTop - 12;
  else thread.scrollTop = thread.scrollHeight;
}

const STATUS = { listening: 'Listening', transcribing: 'Transcribing', thinking: 'Thinking', acting: 'Athena has control',
  speaking: 'Speaking', confirm: 'Needs approval', waiting: 'Your turn', error: 'Something went wrong', paused: 'Paused' };
const MARK = { listening: 'var(--olive)', thinking: 'var(--silver)', transcribing: 'var(--silver)', speaking: 'var(--text)',
  acting: 'var(--bronze)', confirm: 'var(--brass)', error: 'var(--danger)', paused: 'var(--text-3)' };
function setState(s) {
  state = s;
  $('status').textContent = STATUS[s] || '';
  $('mark').style.setProperty('--mark', MARK[s] || 'var(--olive)');
  const busy = ['transcribing', 'thinking', 'acting'].includes(s);
  $('typing').hidden = !busy;
  $('typing').classList.toggle('acting', s === 'acting');
  $('typingText').textContent = s === 'acting' ? 'Working' : s === 'transcribing' ? 'Transcribing' : 'Thinking';
  if (busy) thread.scrollTop = thread.scrollHeight;
  mic.set(s);
}

function send() {
  const text = input.value.trim();
  if (!text) return;
  input.value = ''; grow();
  A.rpc('agent.ask', { text }).catch((e) => render({ role: 'note', text: e.message, turn: currentTurn }));
}
function grow() {
  input.style.height = 'auto'; input.style.height = Math.min(140, input.scrollHeight) + 'px';
  $('send').classList.toggle('ready', !!input.value.trim());
}

$('send').onclick = send;
input.addEventListener('input', grow);
input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
$('mic').onclick = () => A.rpc('chat.mic', { action: mic.action() })
  .catch((e) => render({ role: 'note', text: e.message, turn: currentTurn }));
$('close').onclick = () => A.chatHide();
$('history').onclick = () => A.openSettings('conversation');
$('pin').onclick = () => { pinned = !pinned; $('pin').classList.toggle('on', pinned); $('pin').setAttribute('aria-pressed', String(pinned)); A.chatPin(pinned); };
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') A.chatHide(); });
thread.addEventListener('click', (e) => { const a = e.target.closest('a'); if (a) { e.preventDefault(); A.openUrl(a.href); } });

A.onEvent(({ event, data }) => {
  switch (event) {
    case 'chat.message': render(data.entry); break;
    case 'chat.update': CV.setConfirm(data.id, data.status); break;
    case 'chat.cleared': startTurn(null); $('empty').hidden = false; break;
    case 'state': setState(data.state); break;
    case 'level': if (data.mic != null) mic.level(data.mic); break;
    case 'settings.changed':
      if (data.general) $('hk').textContent = (data.general.hotkey || 'f8').split('+').map((k) => k.length > 1 && !/^f\d/.test(k) ? k[0].toUpperCase() + k.slice(1) : k.toUpperCase()).join(' + ');
      break;
  }
});

(async () => {
  grow();
  try { (await A.rpc('chat.history', { current: true }) || []).forEach(render); } catch {}
})();
