// BUTLER board — drag a card to a column to change its status.
document.querySelectorAll('.card').forEach(card => {
  card.addEventListener('dragstart', e => {
    e.dataTransfer.setData('text/plain', card.dataset.id);
    card.classList.add('dragging');
  });
  card.addEventListener('dragend', () => card.classList.remove('dragging'));
});

document.querySelectorAll('.col').forEach(col => {
  col.addEventListener('dragover', e => { e.preventDefault(); col.classList.add('over'); });
  col.addEventListener('dragleave', () => col.classList.remove('over'));
  col.addEventListener('drop', async e => {
    e.preventDefault();
    col.classList.remove('over');
    const id = e.dataTransfer.getData('text/plain');
    const status = col.dataset.status;
    try {
      const r = await fetch(`/api/sheet/${id}/status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status })
      });
      if (r.ok) location.reload(); else alert('move failed: ' + r.status);
    } catch (err) { alert('move failed: ' + err); }
  });
});
