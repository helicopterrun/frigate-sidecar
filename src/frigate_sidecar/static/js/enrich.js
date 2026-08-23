// Identity clusters: name (promote to known person), merge, delete.

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    SC.toast('request failed: ' + detail, true);
    return false;
  }
  return true;
}

document.querySelectorAll('.name-cluster').forEach(btn => {
  btn.addEventListener('click', async () => {
    const id = btn.dataset.id;
    const input = document.querySelector('.cluster-name[data-id="' + id + '"]');
    const name = (input && input.value || '').trim();
    if (!name) { SC.toast('enter a name first', true); return; }
    if (await post('/enrich/clusters/' + id + '/name', { name })) {
      SC.toast('named "' + name + '" — future matches will sub-label events');
      setTimeout(() => window.location.reload(), 700);
    }
  });
});

document.querySelectorAll('.merge-cluster').forEach(btn => {
  btn.addEventListener('click', async () => {
    const into = window.prompt('Merge cluster ' + btn.dataset.id + ' into cluster id:');
    if (!into) return;
    if (await post('/enrich/clusters/' + btn.dataset.id + '/merge', { into: Number(into) })) {
      window.location.reload();
    }
  });
});

document.querySelectorAll('.delete-cluster').forEach(btn => {
  btn.addEventListener('click', async () => {
    const section = btn.closest('.capture-visit');
    if (await post('/enrich/clusters/' + btn.dataset.id + '/delete')) section.remove();
  });
});
