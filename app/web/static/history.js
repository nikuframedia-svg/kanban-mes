(() => {
  'use strict';
  const key = document.body.dataset.historyKey;
  if (!key) return;
  const fields = ['status', 'operador', 'setor', 'data', 'data_captura', 'of', 'page'];
  const safe = value => {
    if (!value || !value.startsWith('/') || value.startsWith('//') || /[\\\x00-\x1f]/.test(value)) return null;
    const url = new URL(value, location.origin);
    if (url.origin !== location.origin || url.pathname !== '/') return null;
    const params = new URLSearchParams();
    fields.forEach(name => { if (url.searchParams.has(name)) params.set(name, url.searchParams.get(name)); });
    return '/' + (params.size ? '?' + params : '');
  };
  const save = value => { try { sessionStorage.setItem(key, value); } catch (_) { /* explicit back still works */ } };
  let stored = null;
  try { stored = safe(sessionStorage.getItem(key)); } catch (_) { /* disabled storage */ }
  if (location.pathname === '/') {
    const params = new URLSearchParams(location.search);
    if (!location.search && stored && stored !== '/') { location.replace(stored); return; }
    save(safe(location.pathname + location.search) || '/');
  }
  // Delegation survives replacement of the history table by HTMX polling.
  document.addEventListener('click', event => {
    const clear = event.target.closest('[data-clear-history]');
    if (clear) save(safe(clear.getAttribute('href')) || '/');
  });
  const params = new URLSearchParams(location.search);
  const explicit = safe(params.get('back'));
  const destination = explicit || stored || '/';
  document.querySelectorAll('input[name="history_back"]').forEach(input => { input.value = destination; });
  if (location.pathname.startsWith('/sheet/') && !params.has('back')) {
    document.querySelectorAll('input[name="back"]').forEach(input => { input.value = destination; });
    document.querySelectorAll('[data-back]').forEach(button => { button.dataset.back = destination; });
    document.querySelectorAll('a[href]').forEach(link => {
      if (link.classList.contains('sheet-back')) { link.href = destination; return; }
      const url = new URL(link.href);
      if (url.origin === location.origin && url.pathname === location.pathname) {
        url.searchParams.set('back', destination);
        link.href = url.pathname + url.search;
      }
    });
  }
})();
