/* One job per opening; refresh review regions without navigating or losing drafts. */
(() => {
  if (window.kanbanAutomaticReview) return;
  window.kanbanAutomaticReview = true;
  const initial = document.querySelector('[data-automatic-review], [data-ocr-pending]');
  if (!initial) return;
  const endpoint = initial.dataset.automaticReview || initial.dataset.ocrPending;
  let revision = Number(initial.dataset.revision), dirty = false, submitting = false;
  let waitingOCR = Boolean(initial.dataset.ocrPending);
  function edited(event) {
    if (event.target.matches('.plan-filter, #of-query') || event.target.closest('#new-row-dialog')) return;
    if (!window.reviewEdits && event.target.closest('[data-review-region], .review-dialog')) dirty = true;
  }
  document.addEventListener('input', edited, true);
  document.addEventListener('change', edited, true);
  document.addEventListener('submit', event => {
    if (event.target.method === 'post' && !event.defaultPrevented) submitting = true;
  });
  document.addEventListener('review:updated', event => { if (event.detail?.revision != null) revision = event.detail.revision; });
  function message(value) {
    let panel = document.querySelector('[data-automatic-review], [data-ocr-pending]');
    if (!panel) {
      panel = document.createElement('div');
      panel.dataset.automaticReview = endpoint;
      const details = document.createElement('details'); details.className = 'header-audit'; details.dataset.detailKey = 'reading';
      const summary = document.createElement('summary'); summary.textContent = 'Detalhes da leitura automática';
      details.append(summary, panel);
      document.querySelector('[data-review-region="header"]').append(details);
    }
    panel.className = 'muted';
    panel.textContent = value;

  }
  async function refresh() {
    if (dirty || submitting || window.reviewEdits?.hasDrafts() || window.reviewEdits?.busy()) return false;
    const response = await fetch(location.href, {cache: 'no-store', headers: {'X-Review-Refresh': '1'}});
    if (!response.ok) throw new Error('refresh');
    const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
    if (dirty || submitting || window.reviewEdits?.hasDrafts() || window.reviewEdits?.busy()) return false;
    const permission = new CustomEvent('review:before-update', {cancelable: true});
    if (!document.dispatchEvent(permission)) return false;
    if (window.reviewEdits) {
      revision = window.reviewEdits.applyHTML(doc.documentElement.outerHTML, Number(doc.querySelector('[data-review-region="header"]').dataset.revision));
      return true;
    }
    const replacements = [...document.querySelectorAll('[data-review-region]')].map(old =>
      [old, doc.querySelector(`[data-review-region="${old.dataset.reviewRegion}"]`)]);
    if (replacements.some(([, next]) => !next)) throw new Error('regions');
    const position = {x: window.scrollX, y: window.scrollY};
    const scrolls = [...document.querySelectorAll('[data-review-region] .table-scroll, [data-review-region] .tbl-scroll, [data-review-region] .scroll-x')].map(el => el.scrollLeft);
    const expanded = [...document.querySelectorAll('[data-review-region] details')].map(el => el.open);
    replacements.forEach(([old, next]) => {
      next.querySelectorAll('script').forEach(script => script.remove());
      old.replaceWith(next);
      if (window.htmx) window.htmx.process(next);
    });
    document.querySelectorAll('[data-review-region] details').forEach((el, i) => { if (expanded[i]) el.open = true; });
    document.querySelectorAll('[data-review-region] .table-scroll, [data-review-region] .tbl-scroll, [data-review-region] .scroll-x').forEach((el, i) => { el.scrollLeft = scrolls[i] || 0; });
    const heading = document.querySelector('[data-review-region="header"]');
    revision = Number(heading.dataset.revision);
    window.scrollTo(position.x, position.y);
    document.dispatchEvent(new CustomEvent('review:updated', {detail: {revision}}));
    return true;
  }
  async function finished(job) {
    if (job.status === 'idle') { message('Não há uma verificação em execução. Os dados guardados foram preservados.'); return; }
    if (job.status === 'conflict') {
      message(job.error || 'A folha mudou. As tuas alterações foram preservadas.');
      return;
    }
    const updated = await refresh();
    if (!updated) {
      message('A verificação terminou. As tuas alterações por guardar foram mantidas; os novos resultados serão apresentados depois de guardares.');
    } else if (job.status === 'error') {
      message(job.error || 'Parte da verificação não terminou. Os resultados guardados foram preservados.');
    }
  }
  async function poll() {
    try {
      const response = await fetch(endpoint, {cache: 'no-store'});
      if (!response.ok) throw new Error('status');
      const job = await response.json();
      if (waitingOCR) {
        if (job.sheet_status === 'pending') { setTimeout(poll, 4000); return; }
        waitingOCR = false;
        const updated = await refresh();
        if (!updated) { message('A leitura terminou. As tuas alterações por guardar foram mantidas.'); return; }
        const panel = document.querySelector('[data-automatic-review][data-start="true"]');
        if (panel) { start(); return; }
        return;
      }
      if (['queued', 'running'].includes(job.status)) { setTimeout(poll, 1200); return; }
      await finished(job);
    } catch (_) { message('Não foi possível acompanhar a verificação. Os dados guardados foram preservados.'); }
  }
  async function start(retry = false) {
    if (submitting || dirty || window.reviewEdits?.hasDrafts() || window.reviewEdits?.busy()) { message('Verificação adiada para preservar as alterações por guardar.'); return; }
    try {
      const response = await fetch(endpoint, {method: 'POST', body: new URLSearchParams({revision: String(revision), retry: String(retry)})});
      if (response.status === 409) { message('A folha mudou. As tuas alterações foram preservadas.'); return; }
      if (!response.ok) throw new Error('start');
      const job = await response.json();
      if (['queued', 'running'].includes(job.status)) {
        const panel = document.querySelector('[data-automatic-review]');
        if (panel) { panel.className = 'muted'; panel.textContent = 'A verificar automaticamente a folha…'; }
        poll();
      }
      else await finished(job);
    } catch (_) { message('A verificação está indisponível. Os dados guardados foram preservados.'); }
  }
  if (waitingOCR) poll();
  else if (initial.dataset.start === 'true') start();
  else if (initial.dataset.status === 'error') message(initial.textContent);
})();
