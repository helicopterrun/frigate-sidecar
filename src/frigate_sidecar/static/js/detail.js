// Triage detail: label/clear keyboard shortcuts, prev/next nav, zone overlay toggle.
const sessionInput = document.getElementById('session');
if (sessionInput) {
  sessionInput.value = localStorage.getItem('triage_session') || '';
  sessionInput.addEventListener('change', () => {
    localStorage.setItem('triage_session', sessionInput.value);
  });
}

const eventId = document.body.dataset.eventId;
const prevUrl = document.body.dataset.prevUrl;
const nextUrl = document.body.dataset.nextUrl;

async function applyLabel(label) {
  const note = document.getElementById('note').value;
  const session = sessionInput ? sessionInput.value : '';
  const res = await fetch('/label', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({event_id: eventId, label, note, session})
  });
  if (!res.ok) { alert('Save failed: ' + await res.text()); return; }
  if (nextUrl) { window.location = nextUrl; }
  else { window.location.reload(); }
}

async function clearLabel() {
  const res = await fetch('/clear-label', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({event_id: eventId})
  });
  if (!res.ok) { alert('Clear failed: ' + await res.text()); return; }
  window.location.reload();
}

document.querySelectorAll('[data-label]').forEach(b => {
  b.addEventListener('click', () => applyLabel(b.dataset.label));
});
document.querySelectorAll('[data-clear]').forEach(b => {
  b.addEventListener('click', clearLabel);
});

const zonesToggle = document.getElementById('zones-on');
const detailMedia = document.querySelector('.detail-media');
function applyZoneToggle() {
  if (!zonesToggle || !detailMedia) return;
  detailMedia.classList.toggle('zones-hide', !zonesToggle.checked);
  localStorage.setItem('triage_zones_on', zonesToggle.checked ? '1' : '0');
}
if (zonesToggle && detailMedia) {
  const saved = localStorage.getItem('triage_zones_on');
  if (saved === '0') zonesToggle.checked = false;
  applyZoneToggle();
  zonesToggle.addEventListener('change', applyZoneToggle);
}

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === 't') applyLabel('tp');
  else if (e.key === 'f') applyLabel('fp');
  else if (e.key === 's') applyLabel('skip');
  else if (e.key === 'c') clearLabel();
  else if (e.key === 'j' || e.key === 'ArrowRight') { if (nextUrl) window.location = nextUrl; }
  else if (e.key === 'k' || e.key === 'ArrowLeft') { if (prevUrl) window.location = prevUrl; }
  else if (e.key === 'z' && zonesToggle) { zonesToggle.checked = !zonesToggle.checked; applyZoneToggle(); }
});

document.querySelectorAll('.detail-tabs button[data-tab]').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.detail-tabs button[data-tab]').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(x => x.style.display = 'none');
    b.classList.add('active');
    document.getElementById('tab-' + b.dataset.tab).style.display = 'block';
  });
});
