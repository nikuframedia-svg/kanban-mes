(() => {
  const panel = document.querySelector('[data-automatic-review]');
  if (!panel) return;
  let dirty = false, submitting = false;
  try {
    const key = `${document.body.dataset.historyKey}:header:${location.pathname.split('/')[2]}`;
    dirty = Object.keys(JSON.parse(sessionStorage.getItem(key) || '{}')).length > 0;
  } catch (_) { /* Track input events if session storage is unavailable. */ }
  document.addEventListener('input', () => { dirty = true; }, true);
  document.addEventListener('change', () => { dirty = true; }, true);
  document.addEventListener('submit', () => { submitting = true; }, true);
  const endpoint = panel.dataset.automaticReview;
  const revision = Number(panel.dataset.revision);
  function finished() {
    if (submitting) return;
    if (!dirty) { location.reload(); return; }
    panel.textContent = 'A verificação automática terminou. As tuas alterações por guardar foram mantidas.';
    // Never replace an input while a person is typing. Existing revision guards
    // preserve their draft on save if the automatic job changed this sheet.
  }
  async function poll() {
    try {
      const response = await fetch(endpoint, {cache: 'no-store'});
      if (!response.ok) throw new Error('status');
      const job = await response.json();
      if (['queued', 'running'].includes(job.status)) { setTimeout(poll, 1200); return; }
      finished();
    } catch (_) {
      panel.textContent = 'Não foi possível acompanhar a verificação. Os dados guardados foram preservados.';
    }
  }
  fetch(endpoint, {method:'POST', body:new URLSearchParams({revision:String(revision)})})
    .then(response => { if (response.status === 409) return {status:'running'}; if (!response.ok) throw new Error('start'); return response.json(); })
    .then(job => { if (['queued','running'].includes(job.status)) poll(); else finished(); })
    .catch(() => { panel.textContent = 'A folha mudou ou a verificação está indisponível. As tuas alterações foram mantidas.'; });
})();
