// Identities: name (promotes + retro-labels past events), merge via picker
// dialog, evict single sightings, delete. Confirmation dialogs reuse the map
// editor's .me-overlay/.me-dialog classes; everything else is SC helpers.

function post(path, body) {
  return SC.fetchJson(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

function clusterMeta(id) {
  const section = document.querySelector('.id-cluster[data-cluster="' + id + '"]');
  if (!section) return null;
  return {
    section,
    id: String(id),
    name: section.dataset.name || null,
    title: section.querySelector('.id-title').textContent,
    thumb: section.dataset.thumb || null,
  };
}

// Small dialog helper: title, a content node, and action buttons.
function dialog(title, contentNodes, actions) {
  const buttons = actions.map(a =>
    SC.el('button', { class: 'btn ' + (a.kind || 'btn-neutral'), text: a.label }, [])
  );
  const overlay = SC.el('div', { class: 'me-overlay' }, [
    SC.el('div', { class: 'me-dialog' }, [
      SC.el('h3', { text: title }, []),
      ...contentNodes,
      SC.el('div', { class: 'me-actions' }, buttons),
    ]),
  ]);
  actions.forEach((a, i) => {
    buttons[i].addEventListener('click', async () => {
      if (a.onclick) await a.onclick();
      overlay.remove();
    });
  });
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
  return overlay;
}

function clusterRow(meta, extra) {
  const row = SC.el('div', { class: 'id-pick-row' }, [
    meta.thumb
      ? SC.el('img', { src: meta.thumb, alt: '' }, [])
      : SC.el('span', { class: 'id-pick-noimg', text: '?' }, []),
    SC.el('span', { text: meta.title + (extra ? ' · ' + extra : '') }, []),
  ]);
  return row;
}

async function doMerge(fromId, intoId) {
  try {
    await post('/enrich/clusters/' + fromId + '/merge', { into: Number(intoId) });
  } catch (e) {
    SC.toast('merge failed: ' + e.message, true);
    return;
  }
  SC.toast('merged');
  window.location.reload();
}

// Merge confirm for a "looks like" suggestion: show both clusters.
function confirmMerge(fromId, intoId) {
  const from = clusterMeta(fromId);
  const into = clusterMeta(intoId);
  if (!from || !into) return;
  // The named (or older) cluster survives; merge the unnamed one into it.
  let loser = from, winner = into;
  if (from.name && !into.name) { loser = into; winner = from; }
  dialog('Merge these two identities?', [
    clusterRow(loser, 'goes away'),
    clusterRow(winner, 'keeps its sightings + gains the others'),
  ], [
    { label: 'cancel' },
    { label: 'merge', kind: 'btn-primary', onclick: () => doMerge(loser.id, winner.id) },
  ]);
}

// Manual merge: pick the surviving cluster from a list.
function pickMergeTarget(fromId) {
  const from = clusterMeta(fromId);
  const others = Array.from(document.querySelectorAll('.id-cluster'))
    .map(s => clusterMeta(s.dataset.cluster))
    .filter(m => m && m.id !== String(fromId));
  if (!others.length) { SC.toast('no other cluster to merge into', true); return; }
  const rows = others.map(m => {
    const row = clusterRow(m, null);
    row.classList.add('id-pick-tap');
    row.addEventListener('click', () => { overlay.remove(); doMerge(fromId, m.id); });
    return row;
  });
  const overlay = dialog('Merge "' + from.title + '" into…', rows, [{ label: 'cancel' }]);
}

document.querySelectorAll('.name-cluster').forEach(btn => {
  btn.addEventListener('click', async () => {
    const id = btn.dataset.id;
    const input = document.querySelector('.cluster-name[data-id="' + id + '"]');
    const name = ((input && input.value) || '').trim();
    if (!name) { SC.toast('enter a name first', true); input && input.focus(); return; }
    btn.disabled = true;
    try {
      const res = await post('/enrich/clusters/' + id + '/name', { name });
      const meta = clusterMeta(id);
      if (meta) {
        meta.section.dataset.name = name;
        meta.section.querySelector('.id-title').textContent = name;
        const badge = meta.section.querySelector('.badge');
        badge.classList.remove('unknown');
        badge.classList.add('recognized');
        badge.textContent = 'known';
        btn.textContent = 'rename';
      }
      SC.toast('named "' + name + '" — relabeled ' + (res.relabeled || 0) + ' past events');
    } catch (e) {
      SC.toast('naming failed: ' + e.message, true);
    } finally {
      btn.disabled = false;
    }
  });
});

document.querySelectorAll('.id-similar').forEach(btn => {
  btn.addEventListener('click', () => confirmMerge(btn.dataset.id, btn.dataset.other));
});

document.querySelectorAll('.merge-cluster').forEach(btn => {
  btn.addEventListener('click', () => pickMergeTarget(btn.dataset.id));
});

document.querySelectorAll('.delete-cluster').forEach(btn => {
  btn.addEventListener('click', () => {
    const meta = clusterMeta(btn.dataset.id);
    if (!meta) return;
    dialog('Delete "' + meta.title + '"?', [
      SC.el('p', { text: 'Its sightings stay in history but lose this identity.' }, []),
    ], [
      { label: 'cancel' },
      {
        label: 'delete', kind: 'btn-danger',
        onclick: async () => {
          try {
            await post('/enrich/clusters/' + meta.id + '/delete');
            meta.section.remove();
          } catch (e) {
            SC.toast('delete failed: ' + e.message, true);
          }
        },
      },
    ]);
  });
});

document.querySelectorAll('.id-evict').forEach(btn => {
  btn.addEventListener('click', async () => {
    const card = btn.closest('.id-sighting');
    btn.disabled = true;
    try {
      const res = await post('/enrich/events/' + btn.dataset.event + '/remove');
      if (res.cluster_deleted) {
        const section = btn.closest('.id-cluster');
        section && section.remove();
        SC.toast('last sighting removed — cluster deleted');
      } else {
        card && card.remove();
        SC.toast('sighting removed');
      }
    } catch (e) {
      SC.toast('remove failed: ' + e.message, true);
      btn.disabled = false;
    }
  });
});
