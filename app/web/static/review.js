/* Shared review dialogs. Requests belong to a dialog opening, never to a row index alone. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let active = null, origin = null, generation = 0, request = null, lastPlan = '';
  let picker = null, busy = false;
  const text = (tag, value, className) => {
    const el = document.createElement(tag);
    el.textContent = value == null || value === '' ? '—' : String(value);
    if (className) el.className = className;
    return el;
  };
  function cancelRequest() {
    generation++;
    if (request) request.abort();
    request = null;
  }
  function close() {
    // A submitted write must finish before navigating away from its result.
    if (busy) return;
    cancelRequest();
    if (active) active.style.display = 'none';
    active = null;
    document.body.classList.remove('dialog-open');
    if (origin && origin.isConnected) origin.focus();
  }
  function open(id, button) {
    close();
    origin = button || document.activeElement;
    active = $(id);
    active.style.display = 'flex';
    document.body.classList.add('dialog-open');
    active.focus();
  }
  async function read(url, options = {}) {
    cancelRequest();
    const version = generation, controller = new AbortController();
    request = controller;
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(url, {...options, signal: controller.signal});
      const body = options.method ? await response.json() : await response.text();
      if (version !== generation) return null;
      return {response, body};
    } catch (error) {
      if (version !== generation) return null;
      throw error;
    } finally { clearTimeout(timer); }
  }
  window.abrirPlano = async url => {
    if (busy) return;
    const button = active ? origin : document.activeElement;
    lastPlan = url;
    open('plano-modal', button);
    $('plano-modal-body').replaceChildren(text('p', 'A carregar referências…', 'muted'));
    try {
      const result = await read(url);
      if (!result) return;
      if (!result.response.ok) throw new Error('query');
      $('plano-modal-body').innerHTML = result.body;
    } catch (_) {
      const message = text('div', 'Não foi possível carregar as referências.', 'alert warn');
      const retry = text('button', 'Tentar novamente', 'btn');
      retry.type = 'button';
      retry.onclick = () => window.abrirPlano(lastPlan);
      $('plano-modal-body').replaceChildren(message, retry);
    }
  };
  window.fecharPlano = close;
  window.filtrarPlano = query => {
    const normalized = query.toLocaleLowerCase('pt');
    const rows = [...document.querySelectorAll('#tabela-plano tbody tr')];
    rows.forEach(row => { row.hidden = !row.textContent.toLocaleLowerCase('pt').includes(normalized); });
    const count = document.querySelector('[data-plan-count]');
    if (count) count.textContent = `${rows.filter(row => !row.hidden).length} de ${rows.length} referência(s).`;
  };
  function showError(message) {
    $('of-error').textContent = message;
    $('of-error').hidden = !message;
  }
  function renderEntries(data) {
    picker.data = data;
    picker.selected = null;
    $('of-apply').disabled = true;
    $('of-prev').disabled = data.offset === 0;
    $('of-next').disabled = !data.has_more;
    $('of-status').textContent = data.entries.length
      ? `${data.offset + 1}–${data.offset + data.entries.length} · ${data.selection_kind === 'profile' ? 'Escolhe o perfil completo' : 'Escolhe uma referência'}`
      : 'Sem resultados. Confirma a pesquisa ou inclui as peças já fechadas.';
    const cards = data.entries.map(entry => {
      const card = text('button', '', 'of-entry');
      card.replaceChildren();
      card.type = 'button';
      card.dataset.planKey = entry.plan_key;
      card.setAttribute('aria-pressed', 'false');
      card.append(text('strong', entry.component_ref, 'mono'),
        text('span', `OF ${entry.production_order_no || '—'} · OV ${entry.sales_order_no || '—'}`, 'mono tiny'),
        text('span', entry.customer_name),
        text('span', `Perfil ${data.mtg2 ? (entry.profile_excel_o || '—') : (entry.profile_type || '—')}${data.mtg2 ? ' · Tubo ' + (entry.profile_type || '—') : ''} · ${entry.length_mm == null ? '—' : entry.length_mm} mm`, 'tiny'),
        text('span', `Por fazer: ${entry.remaining_valid ? (entry.remaining_quantity ?? '—') : 'desconhecido'}${entry.closed_x ? ' · Fechada' : ''}`, 'tiny'));
      card.onclick = () => {
        if (busy) return;
        document.querySelectorAll('.of-entry').forEach(c => c.setAttribute('aria-pressed', String(c === card)));
        picker.selected = entry.plan_key;
        $('of-apply').disabled = false;
      };
      return card;
    });
    $('of-entries').replaceChildren(...cards);
  }
  async function search(offset = 0) {
    if (busy || !picker) return;
    picker.selected = null;
    showError('');
    $('of-apply').disabled = true;
    $('of-prev').disabled = $('of-next').disabled = true;
    $('of-status').textContent = 'A pesquisar…';
    $('of-entries').replaceChildren();
    const params = new URLSearchParams({q: $('of-query').value, row_index: picker.row,
      include_done: String($('of-include-done').checked), offset: String(offset)});
    try {
      const result = await read(`/sheet/${encodeURIComponent(picker.uid)}/of-lookup?${params}`);
      if (!result) return;
      const data = JSON.parse(result.body);
      if (!result.response.ok) throw new Error(data.detail || 'Não foi possível pesquisar.');
      renderEntries(data);
    } catch (error) {
      $('of-status').textContent = '';
      showError(error.name === 'AbortError' ? 'A pesquisa demorou demasiado. Carrega em Procurar para tentar novamente.' : error.message);
    }
  }
  window.abrirEditorOF = button => {
    if (busy) return;
    picker = {...button.dataset, selected: null, data: null};
    open('of-modal', button);
    $('of-row-label').textContent = `Linha ${Number(picker.row) + 1}`;
    $('of-query').value = picker.of;
    $('of-include-done').checked = false;
    $('of-query').focus();
    search();
  };
  $('of-search-form')?.addEventListener('submit', event => { event.preventDefault(); search(); });
  $('of-include-done')?.addEventListener('change', () => search());
  $('of-prev')?.addEventListener('click', () => search(Math.max(0, picker.data.offset - 50)));
  $('of-next')?.addEventListener('click', () => search(picker.data.offset + 50));
  $('of-cancel')?.addEventListener('click', close);
  $('of-apply')?.addEventListener('click', async () => {
    if (busy || !picker?.selected) return;
    busy = true;
    showError('');
    $('of-apply').disabled = $('of-cancel').disabled = true;
    $('of-status').textContent = 'A guardar e verificar a linha…';
    try {
      // Do not abort a write on a timer: the server may already have committed it.
      cancelRequest();
      const response = await fetch(`/sheet/${encodeURIComponent(picker.uid)}/rows/${picker.row}/plan-selection`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({revision: picker.data.revision, snapshot_id: picker.data.snapshot_id,
          selection_kind: picker.data.selection_kind, plan_key: picker.selected, back: picker.back})
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.detail || 'Não foi possível guardar.');
      location.assign(data.redirect_url);
    } catch (error) {
      showError(error.message || 'Não foi possível confirmar a gravação. Reabre a pesquisa antes de aplicar novamente.');
      $('of-status').textContent = '';
      picker.selected = null;
      busy = false;
      $('of-cancel').disabled = false;
    }
  });
  document.querySelectorAll('.review-dialog').forEach(dialog => dialog.addEventListener('click', event => {
    if (event.target === dialog) close();
  }));
  document.addEventListener('keydown', event => {
    if (!active) return;
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key !== 'Tab') return;
    const elements = [...active.querySelectorAll('button, input, a[href], select, [tabindex="0"]')]
      .filter(el => !el.disabled && el.getClientRects().length);
    const first = elements[0], last = elements[elements.length - 1];
    if (!first) { event.preventDefault(); active.focus(); }
    else if (event.shiftKey && (document.activeElement === first || document.activeElement === active)) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === active)) {
      event.preventDefault(); first.focus();
    }
  });
  // Header edits survive the page reload after an individual cell/OF edit.
  const sheetId = location.pathname.split('/')[2], draftKey = `${document.body.dataset.historyKey}:header:${sheetId}`;
  const headerInputs = () => [...document.querySelectorAll('#header-form input[name^="header_"]')];
  window.guardarCabecalho = () => {
    const headers = headerInputs();
    if (busy || submitting || !headers.length) return;
    const validation = $('validate-form');
    const form = document.createElement('form');
    form.method = 'post';
    form.action = `/sheet/${encodeURIComponent(sheetId)}/header`;
    const values = Object.fromEntries(headers.map(input => [input.name.replace(/^header_/, ''), input.value]));
    values.revision = validation.querySelector('[name="revision"]').value;
    values.back = validation.querySelector('[name="back"]').value;
    values.actor = 'operador';
    for (const [name, value] of Object.entries(values)) {
      const input = document.createElement('input');
      input.type = 'hidden'; input.name = name; input.value = value;
      form.append(input);
    }
    document.body.append(form);
    form.requestSubmit();
  };
  function restoreDraft() {
    try {
      const draft = JSON.parse(sessionStorage.getItem(draftKey) || '{}');
      headerInputs().forEach(input => {
        if (Object.hasOwn(draft, input.name)) input.value = draft[input.name];
      });
    } catch (_) { /* Explicit form values remain available without storage. */ }
  }
  restoreDraft();
  document.addEventListener('input', event => {
    if (!event.target.matches('#header-form input[name^="header_"]')) return;
    try {
      const draft = JSON.parse(sessionStorage.getItem(draftKey) || '{}');
      draft[event.target.name] = event.target.value;
      sessionStorage.setItem(draftKey, JSON.stringify(draft));
    } catch (_) { /* Browser policy. */ }
  });
  document.addEventListener('review:before-update', event => { if (busy || submitting) event.preventDefault(); });
  document.addEventListener('review:updated', async () => {
    restoreDraft();
    if (!active || active.id !== 'plano-modal' || !lastPlan || busy) return;
    try {
      const result = await read(lastPlan);
      if (!result || !result.response.ok || !active || active.id !== 'plano-modal') return;
      const filter = active.querySelector('.plan-filter');
      const query = filter ? filter.value : '';
      const focused = document.activeElement === filter;
      const wrap = active.querySelector('.plan-table-wrap');
      const position = wrap ? {top: wrap.scrollTop, left: wrap.scrollLeft} : {top: 0, left: 0};
      $('plano-modal-body').innerHTML = result.body;
      const nextFilter = active.querySelector('.plan-filter');
      if (nextFilter) { nextFilter.value = query; window.filtrarPlano(query); if (focused) nextFilter.focus({preventScroll: true}); }
      const nextWrap = active.querySelector('.plan-table-wrap');
      if (nextWrap) { nextWrap.scrollTop = position.top; nextWrap.scrollLeft = position.left; }
    } catch (_) { /* Keep the currently visible references if the refresh fails. */ }
  });
  let submitting = false;
  document.addEventListener('submit', event => {
    if (event.target.method !== 'post' || event.defaultPrevented || event.target.hasAttribute('data-row-action')) return;
    if (submitting || busy) { event.preventDefault(); return; }
    submitting = true;
    window.setTimeout(() => { document.querySelectorAll('button[type="submit"]').forEach(b => { b.disabled = true; }); }, 0);
  });
})();
