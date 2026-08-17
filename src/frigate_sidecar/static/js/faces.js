// Faces curation: rescan train/, promote/discard pending crops.

async function decide(card, action) {
  const filename = card.dataset.filename;
  const body = { filename, action };
  if (action === 'promote') {
    const name = card.querySelector('.name').value.trim();
    if (!name) { SC.toast('Enter a name to promote into.', true); return; }
    body.name = name;
  }
  const res = await fetch('/faces/decide', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    SC.toast(action + ' failed: ' + detail, true);
    return;
  }
  card.remove();
}

document.querySelectorAll('.face-card').forEach(card => {
  card.querySelector('.promote').addEventListener('click', () => decide(card, 'promote'));
  card.querySelector('.discard').addEventListener('click', () => decide(card, 'discard'));
});

const rescan = document.getElementById('rescan');
if (rescan) {
  rescan.addEventListener('click', async () => {
    rescan.disabled = true;
    rescan.textContent = 'Scanning…';
    try {
      const res = await fetch('/faces/scan', { method: 'POST' });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
        SC.toast('Scan failed: ' + detail, true);
        return;
      }
      window.location.reload();
    } finally {
      rescan.disabled = false;
      rescan.textContent = 'Rescan train/';
    }
  });
}
