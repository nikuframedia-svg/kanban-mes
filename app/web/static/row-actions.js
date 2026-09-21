/* Row writes are atomic; refresh only the review body, keeping local drafts. */
(() => {
  'use strict';
  if (window.kanbanRowActions) return;
  const state = window.kanbanRowActions = {busy: false};
  const $ = id => document.getElementById(id);
  const dialog = $('new-row-dialog'), form = $('new-row-form');
  if (!dialog) return;
  const uid = dialog.dataset.uid, back = dialog.dataset.back;
  const draftValues = new Map();
  let selected = null, searchVersion = 0, pageOffset = 0, requestId = null, draftRevision = null, locked = false;
  const revision = () => Number(document.querySelector('[data-review-region="header"]').dataset.revision);
  function feedback(message, undo) {
    const node = $('row-feedback'); node.hidden = false; node.replaceChildren(document.createTextNode(message));
    if (undo !== undefined) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'btn ghost'; button.textContent = 'Desfazer';
      button.onclick = () => mutate(`/sheet/${uid}/rows/${undo}/restore`, new URLSearchParams({revision: revision(), back}), 'Linha restaurada.');
      node.append(button);
    }
  }
  function key(input) {
    const owner = input.form;
    return JSON.stringify([owner?.getAttribute('action'), owner?.id, owner?.querySelector('[name="field_path"]')?.value, input.name]);
  }
  function edited(input) {
    return input.type === 'checkbox' || input.type === 'radio' ? input.checked !== input.defaultChecked : input.value !== input.defaultValue;
  }
  async function refresh(expected) {
    const response = await fetch(location.href, {cache: 'no-store', headers: {'X-Review-Refresh': '1'}});
    if (!response.ok) throw new Error('A alteração foi guardada, mas não foi possível atualizar a tabela.');
    const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
    const next = doc.querySelector('[data-review-region="body"]');
    if (!next || Number(doc.querySelector('[data-review-region="header"]')?.dataset.revision) !== expected) {
      locked = true;
      throw new Error('A alteração foi guardada, mas a folha mudou noutra aba. Atualiza a folha antes de continuar; os campos em edição foram mantidos.');
    }
    const old = document.querySelector('[data-review-region="body"]');
    const position = {x: scrollX, y: scrollY};
    const scrolls = [...old.querySelectorAll('.scroll-x')].map(el => el.scrollLeft);
    const expanded = new Set([...old.querySelectorAll('details[open][data-detail-key]')].map(el => el.dataset.detailKey));
    old.querySelectorAll('input:not([type=hidden]), textarea, select').forEach(input => {
      if (input.name && edited(input)) draftValues.set(key(input), {value: input.value, checked: input.checked});
    });
    const focusedKey = old.contains(document.activeElement) ? key(document.activeElement) : null;
    next.querySelectorAll('script').forEach(script => script.remove());
    next.querySelectorAll('input:not([type=hidden]), textarea, select').forEach(input => {
      const draft = draftValues.get(key(input));
      if (draft) { input.value = draft.value; input.checked = draft.checked; }
    });
    old.replaceWith(next);
    if (window.htmx) window.htmx.process(next);
    next.querySelectorAll('details[data-detail-key]').forEach(el => { el.open = expanded.has(el.dataset.detailKey); });
    next.querySelectorAll('.scroll-x').forEach((el, i) => { el.scrollLeft = scrolls[i] || 0; });
    // Header inputs remain in place. Only advance our own successful revision.
    document.querySelector('[data-review-region="header"]').dataset.revision = String(expected);
    document.querySelectorAll('input[name="revision"], [data-revision]').forEach(el => {
      if (el.name === 'revision') el.value = String(expected);
      else el.dataset.revision = String(expected);
    });
    if (focusedKey) [...next.querySelectorAll('input, textarea, select')].find(input => key(input) === focusedKey)?.focus({preventScroll: true});
    window.scrollTo(position.x, position.y);
    document.dispatchEvent(new CustomEvent('review:updated', {detail: {revision: expected, rowMutation: true}}));
  }
  async function mutate(url, body, message, undo, creation = false) {
    if (state.busy || locked) { if (locked) feedback('A folha mudou. Atualiza-a antes de continuar; os campos em edição foram mantidos.'); return; }
    const allowed = document.dispatchEvent(new CustomEvent('review:before-update', {cancelable: true, detail: {rowMutation: true}}));
    if (!allowed) return;
    state.busy = true;
    document.querySelectorAll('[data-row-action] button, [data-new-row], [data-new-row-close]').forEach(b => { b.disabled = true; });
    let saved = false;
    try {
      const response = await fetch(url, {method: 'POST', headers: {Accept: 'application/json', ...(creation ? {'Content-Type': 'application/json'} : {})}, body});
      const result = await response.json();
      if (!response.ok) {
        if (response.status === 409) locked = true;
        throw new Error(typeof result.detail === 'string' ? result.detail : 'Não foi possível guardar a linha. Confirma os campos.');
      }
      saved = true;
      if (creation) { dialog.close(); form.reset(); selected = null; requestId = null; }
      if (result.conflict) { locked = true; throw new Error('A alteração ficou guardada, mas a folha mudou entretanto. Atualiza a folha antes de continuar.'); }
      await refresh(result.revision);
      feedback(result.warning || message, undo);
    } catch (error) {
      if (saved) locked = true; // A failed refresh is never a second write.
      const message = error instanceof TypeError ? 'Não foi possível obter a resposta. Os valores foram mantidos; podes tentar novamente.' : error.message;
      if (creation && !saved) { $('new-row-error').textContent = message; $('new-row-error').hidden = false; }
      else feedback(message);
    } finally {
      state.busy = false;
      document.querySelectorAll('[data-row-action] button, [data-new-row], [data-new-row-close]').forEach(b => { b.disabled = locked; });
    }
  }
  // A row button can blur a cell whose legacy onchange submits the form.
  // Keep that draft in place instead of navigating before the row action.
  let keepingCellDraft = false;
  document.addEventListener('pointerdown', event => {
    keepingCellDraft = Boolean(event.target.closest('[data-new-row], [data-row-action], #row-feedback button'));
  }, true);
  document.addEventListener('submit', event => {
    if (keepingCellDraft && event.target.getAttribute('action')?.endsWith('/edit')) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener('pointerup', () => setTimeout(() => { keepingCellDraft = false; }, 0), true);
  document.addEventListener('submit', event => {
    const target = event.target;
    if (!target.matches('form[data-row-action][action]')) return;
    event.preventDefault();
    const remove = target.action.endsWith('/exclude');
    mutate(target.action, new URLSearchParams(new FormData(target)), remove ? 'Linha retirada — ' : 'Linha restaurada.', remove ? Number(target.dataset.row) : undefined);
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-new-row]');
    if (!button || state.busy || locked) return;
    const select = $('new-row-position'); select.replaceChildren();
    const option = (value, label) => select.add(new Option(label, value));
    option('start', 'No início');
    document.querySelectorAll('[data-production-row]').forEach((row, i) => {
      option(`before:${row.dataset.productionRow}`, `Antes da linha ${i + 1}`);
      option(`after:${row.dataset.productionRow}`, `Depois da linha ${i + 1}`);
    });
    option('end', 'No fim'); select.value = 'end';
    requestId = requestId || crypto.randomUUID();
    draftRevision = revision();
    $('new-row-error').hidden = true;
    dialog.showModal();
  });
  document.querySelector('[data-new-row-close]')?.addEventListener('click', () => { if (!state.busy) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (state.busy) event.preventDefault(); });
  form.addEventListener('input', event => {
    if (['of', 'ov', 'cliente', 'perfil', 'modelo'].includes(event.target.name)) selected = null;
    if (event.target.name === 'perf_comp' && selected) fillSelection(selected.entry);
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    const [position, anchor] = $('new-row-position').value.split(':');
    const payload = {revision: draftRevision, request_id: requestId, values, position, anchor: anchor === undefined ? null : Number(anchor), back};
    if (selected) { payload.snapshot_id = selected.snapshot; payload.plan_key = selected.entry.plan_key; }
    mutate(`/sheet/${uid}/add-row`, JSON.stringify(payload), 'Linha adicionada.', undefined, true);
  });
  function fillSelection(entry) {
    const full = form.elements.perf_comp?.checked;
    const values = {of: entry.production_order_no, ov: entry.sales_order_no, cliente: entry.customer_name,
      perfil: entry.profile_type, modelo: full ? '' : entry.component_ref};
    for (const [field, value] of Object.entries(values)) if (form.elements[field]) form.elements[field].value = value || '';
    // The plan's remaining quantity is not a production observation.
  }
  async function search(offset = 0) {
    if (state.busy) return;
    const version = ++searchVersion; pageOffset = offset;
    const results = $('new-row-results'); results.textContent = 'A pesquisar…';
    $('new-row-prev').disabled = $('new-row-next').disabled = true;
    try {
      const query = new URLSearchParams({q: $('new-row-search').elements.q.value, include_done: String($('new-row-include-done').checked), offset});
      const response = await fetch(`/sheet/${uid}/of-lookup?${query}`);
      const data = await response.json();
      if (version !== searchVersion) return;
      if (!response.ok) throw new Error(data.detail || 'Pesquisa indisponível.');
      if (data.revision !== draftRevision) throw new Error('A folha mudou. Os dados preenchidos foram mantidos.');
      results.replaceChildren();
      if (!data.entries.length) results.textContent = 'Sem resultados. Podes preencher a linha manualmente.';
      for (const entry of data.entries) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'of-entry';
        button.textContent = `${entry.component_ref || '—'} · OF ${entry.production_order_no || '—'} · ${entry.profile_type || '—'} · ${entry.customer_name || '—'}`;
        button.onclick = () => {
          selected = {entry, snapshot: data.snapshot_id}; fillSelection(entry);
          results.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === button)));
        };
        results.append(button);
      }
      $('new-row-prev').disabled = !offset; $('new-row-next').disabled = !data.has_more;
    } catch (error) { if (version === searchVersion) results.textContent = error.message; }
  }
  $('new-row-search')?.addEventListener('submit', event => { event.preventDefault(); search(); });
  $('new-row-include-done')?.addEventListener('change', () => search());
  $('new-row-prev')?.addEventListener('click', () => search(Math.max(0, pageOffset - 50)));
  $('new-row-next')?.addEventListener('click', () => search(pageOffset + 50));
  document.addEventListener('review:before-update', event => { if (state.busy || (dialog.open && !event.detail?.rowMutation)) event.preventDefault(); });
})();
