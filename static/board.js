// BUTLER board — statuses are vertical lanes (top to bottom); letter cards flow
// horizontally inside a lane (wrap, never h-scroll).
// Interaction: CLICK a card -> the letter opens in a new tab (the board tab
// stays put). DRAG a card -> the whole letter moves to another status lane.
let justDragged = false;

document.querySelectorAll('.card').forEach(card => {
  card.addEventListener('dragstart', e => {
    e.dataTransfer.setData('text/plain', card.dataset.batch);
    card.classList.add('dragging');
    justDragged = false;
  });
  card.addEventListener('dragend', () => {
    card.classList.remove('dragging');
    justDragged = true;                      // suppress the click that follows a drag
    setTimeout(() => { justDragged = false; }, 250);
  });
  card.addEventListener('click', e => {
    if (justDragged) { e.preventDefault(); return; }
    const url = card.dataset.url;
    if (url) window.open(url, '_blank', 'noopener');
  });
});

document.querySelectorAll('.lane').forEach(lane => {
  lane.addEventListener('dragover', e => { e.preventDefault(); lane.classList.add('over'); });
  lane.addEventListener('dragleave', () => lane.classList.remove('over'));
  lane.addEventListener('drop', async e => {
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
      if (r.ok) location.reload(); else alert('move failed: ' + r.status);
    } catch (err) { alert('move failed: ' + err); }
  });
});

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
