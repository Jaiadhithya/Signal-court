(() => {
  'use strict';
  const tooltip = document.getElementById('record-popover');
  const message = document.getElementById('status-message');
  let messageTimer;
  function status(text) {
    message.textContent = text;
    message.style.display = 'block';
    clearTimeout(messageTimer);
    messageTimer = setTimeout(() => { message.style.display = 'none'; }, 2200);
  }
  function reveal(target) {
    if (!target) return;
    let parent = target.parentElement;
    while (parent) { if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement; }
    target.hidden = false;
  }
  function revealHash() {
    try { reveal(document.getElementById(decodeURIComponent(location.hash.slice(1)))); } catch (_) { /* Invalid fragment. */ }
  }
  document.addEventListener('click', event => {
    const a = event.target.closest('a[href^="#"]');
    if (a) reveal(document.getElementById(a.getAttribute('href').slice(1)));
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.action === 'print') window.print();
    if (button.dataset.action === 'expand') {
      const expand = button.textContent !== 'Collapse all';
      document.querySelectorAll('details').forEach(d => { d.open = expand; });
      button.textContent = expand ? 'Collapse all' : 'Expand all';
    }
    if (button.dataset.copy) {
      const text = document.getElementById(button.dataset.copy).textContent;
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => status('Hash copied')).catch(() => status('Select the hash to copy it.'));
      else status('Select the hash to copy it.');
    }
  });
  function showTip(event) {
    const a = event.target.closest('[data-record][title]');
    if (!a) return;
    tooltip.textContent = a.getAttribute('title');
    tooltip.hidden = false;
    const rect = a.getBoundingClientRect();
    tooltip.style.left = Math.max(12, Math.min(rect.left, innerWidth - tooltip.offsetWidth - 12)) + 'px';
    tooltip.style.top = Math.max(12, Math.min(rect.bottom + 10, innerHeight - tooltip.offsetHeight - 12)) + 'px';
  }
  document.addEventListener('mouseover', showTip);
  document.addEventListener('focusin', showTip);
  document.addEventListener('mouseout', event => { if (event.target.closest('[data-record]')) tooltip.hidden = true; });
  document.addEventListener('focusout', () => { tooltip.hidden = true; });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') tooltip.hidden = true; });
  window.addEventListener('hashchange', revealHash);
  revealHash();
  const search = document.getElementById('ledger-search');
  search.addEventListener('input', () => {
    let visible = 0;
    document.querySelectorAll('.ledger-table tbody tr').forEach(row => {
      row.hidden = !row.textContent.toLowerCase().includes(search.value.toLowerCase());
      if (!row.hidden) visible++;
    });
    document.getElementById('ledger-empty').hidden = visible > 0;
  });
  let printState = [];
  window.addEventListener('beforeprint', () => {
    printState = Array.from(document.querySelectorAll('details'), d => [d, d.open]);
    printState.forEach(([d]) => { d.open = true; });
    document.querySelectorAll('.ledger-table tr[hidden]').forEach(row => { row.dataset.filtered = 'true'; row.hidden = false; });
  });
  window.addEventListener('afterprint', () => {
    printState.forEach(([d, open]) => { d.open = open; });
    document.querySelectorAll('[data-filtered]').forEach(row => { row.hidden = true; delete row.dataset.filtered; });
  });
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
      entries.filter(e => e.isIntersecting).forEach(e => {
        document.querySelectorAll('.section-index nav a').forEach(a => a.classList.toggle('active', a.hash === '#' + e.target.id));
      });
    }, { rootMargin: '-100px 0px -65% 0px' });
    document.querySelectorAll('main>section,main>details').forEach(el => observer.observe(el));
  }
})();
