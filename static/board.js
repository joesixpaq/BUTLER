// BUTLER board — statuses are vertical lanes (top to bottom); letter cards flow
// horizontally inside a lane (wrap, never h-scroll).
// Interaction (delegated on document, so listeners survive live refresh swaps):
//   CLICK a card -> the letter opens in a new tab (the board tab stays put).
//   DRAG a card  -> the whole letter moves to another status lane.
// The board (lanes + header note) live-refreshes every 5 s by re-fetching "/"
// and swapping the DOM — skipped while dragging and while the tab is hidden.
let justDragged = false;
let dragging = false;

document.addEventListener('dragstart', e => {
  const card = e.target.closest('.card');
  if (!card) return;
  e.dataTransfer.setData('text/plain', card.dataset.batch);
  card.classList.add('dragging');
  dragging = true;
  justDragged = false;
});
document.addEventListener('dragend', e => {
  const card = e.target.closest('.card');
  if (card) card.classList.remove('dragging');
  dragging = false;
  justDragged = true;                      // suppress the click that follows a drag
  setTimeout(() => { justDragged = false; }, 250);
});
document.addEventListener('click', e => {
  const card = e.target.closest('.card');
  if (!card) return;
  if (justDragged) { e.preventDefault(); return; }
  const url = card.dataset.url;
  if (url) window.open(url, '_blank', 'noopener');
});
document.addEventListener('dragover', e => {
  const lane = e.target.closest('.lane');
  if (!lane) return;
  e.preventDefault();
  lane.classList.add('over');
});
document.addEventListener('dragleave', e => {
  const lane = e.target.closest('.lane');
  if (!lane) return;
  if (e.relatedTarget && lane.contains(e.relatedTarget)) return;  // still inside
  lane.classList.remove('over');
});
document.addEventListener('drop', async e => {
  const lane = e.target.closest('.lane');
  if (!lane) return;
  e.preventDefault();
  lane.classList.remove('over');
  const id = e.dataTransfer.getData('text/plain');
  const status = lane.dataset.status;
  if (!id || !status) return;
  try {
    const r = await fetch(`/api/batch/${id}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status })
    });
    if (r.ok) refreshBoard(true); else alert('move failed: ' + r.status);
  } catch (err) { alert('move failed: ' + err); }
});

// ---- live refresh: lanes + header note (the scanner panel polls separately) --
async function refreshBoard(force) {
  if (!force && (dragging || document.hidden)) return;
  try {
    const r = await fetch('/', { cache: 'no-store' });
    if (!r.ok) return;
    const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
    const nm = doc.querySelector('main.board');
    const cm = document.querySelector('main.board');
    if (nm && cm && nm.innerHTML !== cm.innerHTML) cm.innerHTML = nm.innerHTML;
    const nh = doc.querySelector('header .meta');
    const ch = document.querySelector('header .meta');
    if (nh && ch && nh.textContent !== ch.textContent) ch.textContent = nh.textContent;
  } catch (err) { /* transient — the next tick retries */ }
}
setInterval(refreshBoard, 5000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshBoard(); });

// ---- scanner panel — manual session flow (one-by-one feeding) --------------
const sc = { pending: false, clickedAt: 0 };

function scRefresh() {
  fetch('/api/scanner').then(r => r.json()).then(d => {
    const dot = document.getElementById('sc-dot');
    const mode = document.getElementById('sc-mode');
    const sess = document.getElementById('sc-session');
    const last = document.getElementById('sc-last');
    if (!dot) return;
    dot.className = 'dot ' + (d.online ? 'on' : (d.mode === 'suspended' ? 'warn' : 'off'));
    mode.textContent = d.online ? 'scanner online'
      : (d.mode === 'suspended' ? 'scanner suspended — power-cycle it' : 'scanner offline');
    if (d.session && d.session.open) {
      sess.textContent = 'session open — ' + d.session.sheets + ' sheet(s), '
        + (d.session.runs || []).length + ' run(s) — fed one by one, press Finish when done';
    } else if (d.last_session) {
      sess.textContent = 'last session — ' + d.last_session.sheets + ' sheet(s), '
        + (d.last_session.closed_reason || 'closed');
    } else {
      sess.textContent = '';
    }
    const lr = d.last_request;
    if (lr) {
      const age = Math.max(0, Math.round(Date.now() / 1000 - lr.ts));
      last.textContent = 'last: ' + (lr.note || ('rc=' + lr.rc)) + ' · ' + age + 's ago';
    }
    // release the pending lock once our request has been answered
    if (sc.pending && lr && lr.ts >= sc.clickedAt - 1) sc.pending = false;
    const scanBtn = document.getElementById('sc-scan');
    const finBtn = document.getElementById('sc-finish');
    scanBtn.disabled = sc.pending || !d.online;
    finBtn.disabled = sc.pending || !(d.session && d.session.open);
    scanBtn.textContent = sc.pending ? 'scanning…' : 'Scan sheet';
  }).catch(() => {});
}

async function scSend(action) {
  sc.pending = true;
  sc.clickedAt = Date.now() / 1000;
  scRefresh();
  try {
    const r = await fetch('/api/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action })
    });
    const d = await r.json();
    if (!d.ok) {
      alert(action + ' failed: ' + (d.error || r.status));
      sc.pending = false;
      scRefresh();
      return;
    }
    setTimeout(scRefresh, 1500);
    // safety release (e.g. request lost, or an unexpectedly long attempt)
    setTimeout(() => { sc.pending = false; scRefresh(); }, 25000);
  } catch (err) {
    alert('scan request failed: ' + err);
    sc.pending = false;
    scRefresh();
  }
}

const scanBtn = document.getElementById('sc-scan');
const finBtn = document.getElementById('sc-finish');
if (scanBtn) scanBtn.addEventListener('click', () => scSend('scan'));
if (finBtn) finBtn.addEventListener('click', () => scSend('finish'));
scRefresh();
setInterval(scRefresh, 3000);
