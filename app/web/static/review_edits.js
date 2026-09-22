/* Draft writes are serialized. Only an explicit activation of Validar publishes a sheet. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  function initialize() {
    if (!$('validate-form') || window.reviewEdits) return;
    const states = new Map(), pending = new Set(), failures = new Map();
    let revision = Number($('validate-form').elements.revision.value);
    let running = null, activeJob = null, validating = false, navigating = false, mutation = null, mutationActive = false;
    let reviewToken = $('validate-form').elements.review_token.value;
    const headers = () => [...document.querySelectorAll('#header-form input[name^="header_"]')];
    const forms = () => [...document.querySelectorAll('.cell-edit-form')];
    const group = key => key.startsWith('header.') ? '@header' : key;
    const keyFor = input => input.matches('.header-input')
      ? 'header.' + input.name.replace(/^header_/, '')
      : input.closest('.cell-edit-form')?.dataset.fieldPath;
    const inputFor = key => key.startsWith('header.')
      ? headers().find(el => el.name === 'header_' + key.slice(7))
      : forms().find(el => el.dataset.fieldPath === key)?.elements.value;
    const members = key => [...states].filter(([path]) => group(path) === key);
    const dirty = state => state && state.version > state.committed;
    function register() {
      document.querySelectorAll('.cell-edit-form input[name="value"], .header-input').forEach(input => {
        const key = keyFor(input);
        if (!states.has(key)) states.set(key, {version: 0, committed: 0});
      });
    }
    register();
    function status(key, message, failed = false) {
      const target = key === '@header' ? $('header-save-status')
        : inputFor(key)?.form.querySelector('.edit-status');
      if (!target) return;
      target.replaceChildren(document.createTextNode(message));
      target.classList.toggle('save-error', failed);
      if (failed) {
        const retry = document.createElement('button');
        retry.type = 'button'; retry.className = 'btn ghost';
        retry.dataset.saveRetry = key; retry.textContent = 'Tentar novamente';
        target.append(' ', retry);
      }
    }
    function setRevision(value) {
      if (!Number.isInteger(value)) return;
      revision = value;
      document.querySelectorAll('input[name="revision"]').forEach(input => { input.value = revision; });
      document.querySelectorAll('[data-revision]').forEach(el => { el.dataset.revision = revision; });
    }
    const detailKey = el => el.dataset.detailKey || (el.closest('[id^="row-"]')?.id || 'sheet') + ':' + el.className;
    function applyHTML(html, nextRevision) {
      const next = new DOMParser().parseFromString(html, 'text/html');
      const renderedRevision = next.querySelector('#validate-form input[name="revision"]')?.value;
      const nextToken = next.querySelector('#validate-form input[name="review_token"]')?.value;
      const focused = document.activeElement;
      const selection = focused?.tagName === 'INPUT' ? [focused.selectionStart, focused.selectionEnd] : null;
      const position = [window.scrollX, window.scrollY];
      const details = new Map([...document.querySelectorAll('.review-heading details, .review-content details')]
        .map(el => [detailKey(el), el.open]));
      const scroll = [...document.querySelectorAll('[data-review-scroll]')].map(el => [el.dataset.reviewScroll, el.scrollLeft, el.scrollTop]);
      const history = $('validate-form')?.elements.history_back.value;
      const back = $('validate-form')?.elements.back.value;
      const existing = new Map(forms().map(form => [form.dataset.fieldPath, form]));
      next.querySelectorAll('.cell-edit-form').forEach(fresh => {
        const key = fresh.dataset.fieldPath, old = existing.get(key);
        if (!old) return;
        const state = states.get(key), input = old.elements.value, incoming = fresh.elements.value;
        if (!state || !dirty(state)) input.value = incoming.value;
        // Keep the live input node and its draft while updating cross metadata around it.
        for (const attr of ['placeholder', 'title', 'class']) {
          if (incoming.hasAttribute(attr)) input.setAttribute(attr, incoming.getAttribute(attr));
          else input.removeAttribute(attr);
        }
        fresh.replaceWith(old);
      });
      const oldHeader = $('header-form'), newHeader = next.getElementById('header-form');
      if (oldHeader && newHeader) {
        headers().forEach(input => {
          const incoming = newHeader.elements[input.name];
          if (!incoming) return;
          if (!dirty(states.get(keyFor(input)))) input.value = incoming.value;
          input.closest('label').className = incoming.closest('label').className;
        });
        const oldOfs = oldHeader.querySelector('.sheet-ofs'), newOfs = newHeader.querySelector('.sheet-ofs');
        if (oldOfs) oldOfs.remove();
        if (newOfs) oldHeader.append(newOfs);
        newHeader.replaceWith(oldHeader);
      }
      for (const selector of ['.review-heading', '.review-content']) {
        const current = document.querySelector(selector), fresh = next.querySelector(selector);
        if (current && fresh) current.replaceWith(fresh);
      }
      register();
      reviewToken = nextToken || reviewToken;
      setRevision(renderedRevision === undefined ? nextRevision : Number(renderedRevision));
      if ($('validate-form')) {
        if (history !== undefined) $('validate-form').elements.history_back.value = history;
        if (back !== undefined) $('validate-form').elements.back.value = back;
      }
      document.querySelectorAll('.review-heading details, .review-content details').forEach(el => {
        if (details.has(detailKey(el))) el.open = details.get(detailKey(el));
      });
      scroll.forEach(([key, left, top]) => {
        const el = [...document.querySelectorAll('[data-review-scroll]')].find(el => el.dataset.reviewScroll === key);
        if (el) { el.scrollLeft = left; el.scrollTop = top; }
      });
      if (focused?.isConnected) {
        focused.focus({preventScroll: true});
        if (selection && selection[0] !== null) focused.setSelectionRange(...selection);
      }
      window.scrollTo(...position);
      failures.forEach((message, key) => status(key, message, true));
      if ($('validate-button')) $('validate-button').disabled = validating;
      document.dispatchEvent(new CustomEvent('review:updated', {detail: {revision, inlineEdit: true}}));
      return revision;
    }
    function snapshot(key) {
      const versions = new Map(), body = new URLSearchParams();
      body.set('revision', revision);
      body.set('review_token', reviewToken);
      body.set('back', $('validate-form').elements.back.value);
      body.set('actor', 'operador');
      if (key === '@header') {
        headers().forEach(input => {
          const path = keyFor(input);
          body.set(path.slice(7), input.value);
          versions.set(path, states.get(path).version);
        });
      } else {
        body.set('field_path', key); body.set('value', inputFor(key).value);
        versions.set(key, states.get(key).version);
      }
      return {key, versions, body, url: key === '@header' ? $('header-form').action : inputFor(key).form.action};
    }
    function pump() {
      if (running) return running;
      running = Promise.resolve().then(async () => {
        while (pending.size && !failures.size) {
          if (mutationActive) await mutation;
          const key = pending.values().next().value;
          pending.delete(key);
          if (!members(key).some(([, state]) => dirty(state))) continue;
          const job = activeJob = snapshot(key);
          status(key, 'A guardar…');
          try {
            // No timeout/abort for writes: a disconnected response can already be committed.
            const response = await fetch(job.url, {method: 'POST', headers: {Accept: 'application/json'}, body: job.body});
            const data = await response.json();
            if (response.status !== 409) {
              setRevision(data.revision);
              reviewToken = data.review_token || reviewToken;
            }
            if (!response.ok || !data.ok) throw new Error(data.detail || 'Não foi possível guardar.');
            job.versions.forEach((version, path) => { states.get(path).committed = version; });
            applyHTML(data.html, data.revision);
            status(key, members(key).some(([, state]) => dirty(state)) ? 'Alterações por guardar' : 'Guardado');
          } catch (error) {
            const message = error.message === 'Failed to fetch'
              ? 'Não foi possível confirmar a gravação. Tenta novamente.' : error.message;
            failures.set(key, message);
            status(key, message, true);
          } finally { activeJob = null; }
        }
      }).finally(() => { running = null; });
      return running;
    }
    function save(key) {
      if (!key || navigating) return Promise.resolve();
      const id = group(key);
      failures.delete(id);
      if (activeJob?.key === id && members(id).every(([path, state]) => activeJob.versions.get(path) === state.version)) return running;
      pending.add(id);
      return pump();
    }
    async function flush(skipMutation = false) {
      while (true) {
        if (mutation && !skipMutation) await mutation;
        if (failures.size) return false;
        states.forEach((state, path) => { if (dirty(state)) pending.add(group(path)); });
        await pump();
        if (failures.size) return false;
        if (![...states.values()].some(dirty) && !pending.size) return true;
      }
    }
    document.addEventListener('input', event => {
      const key = keyFor(event.target);
      if (!key) return;
      states.get(key).version++;
      if (!failures.has(group(key))) status(group(key), 'Alterações por guardar');
    });
    document.addEventListener('focusout', event => {
      const key = keyFor(event.target);
      if (key && dirty(states.get(key)) && !failures.has(group(key))) save(key);
    });
    document.addEventListener('keydown', event => {
      const key = keyFor(event.target);
      if (key && event.key === 'Enter' && !event.isComposing) {
        event.preventDefault(); event.stopPropagation(); save(key);
      }
    });
    document.addEventListener('submit', event => {
      if (event.target.matches('.cell-edit-form, #header-form, #validate-form')) {
        event.preventDefault();
        if (event.target.id !== 'validate-form') save(event.target.id === 'header-form' ? '@header' : event.target.dataset.fieldPath);
      }
    });
    document.addEventListener('click', async event => {
      const retry = event.target.closest('[data-save-retry]');
      if (retry) save(retry.dataset.saveRetry);
      if (!event.target.closest('#validate-button') || validating || navigating) return;
      event.preventDefault();
      if (!$('header-form').reportValidity()) return;
      validating = true; $('validate-button').disabled = true;
      if (!await flush()) {
        validating = false; $('validate-button').disabled = false;
        $('validation-status').textContent = 'Resolve a gravação pendente antes de validar.';
        return;
      }
      if (!$('header-form').reportValidity()) {
        validating = false; $('validate-button').disabled = false; return;
      }
      // Use persisted fields and the last revision; implicit form submissions never reach this path.
      navigating = true;
      $('validation-status').textContent = 'A validar…';
      HTMLFormElement.prototype.submit.call($('validate-form'));
    });
    window.guardarCabecalho = () => save('@header');
    function mutate(action) {
      if (mutation || validating || navigating) return Promise.resolve(false);
      // Publish the queued mutation immediately: Validar must await an action
      // already requested, including the draft saves which precede that action.
      mutation = Promise.resolve().then(async () => {
        if (!await flush(true)) return false;
        mutationActive = true;
        await action();
        return true;
      }).finally(() => { mutation = null; mutationActive = false; });
      return mutation;
    }
    const hasDrafts = () => [...states.values()].some(dirty);
    window.reviewEdits = {flush, applyHTML, setRevision, mutate, hasDrafts,
      token: () => reviewToken, revision: () => revision,
      busy: () => Boolean(running || mutation || validating || navigating)};
    document.addEventListener('review:before-update', event => {
      if (!event.detail?.rowMutation && (running || mutation || validating || navigating || hasDrafts())) event.preventDefault();
    });
    window.addEventListener('beforeunload', event => {
      if (!navigating && (running || [...states.values()].some(dirty))) {
        event.preventDefault(); event.returnValue = '';
      }
    });
  }
  initialize();
  document.addEventListener('review:updated', initialize);
})();
