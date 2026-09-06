/* Stable application shell. Workspaces register lifecycle adapters here.
 * Module flags affect entry points/new operations only; history stays local
 * and readable. The server separately enforces every operation boundary.
 */
(() => {
  'use strict';
  const views = ['catalog', 'analysis-workspace', 'maintenance', 'settings', 'trash'];
  const adapters = new Map();
  let modules = [];
  // Existing static galleries are adapters to optional modules, with no data
  // copies in this shell. Their backend paths enforce the same module flags.
  document.querySelectorAll('a[href^="/exports/wechat-media/"]').forEach((node) => { node.dataset.moduleEntry = 'media'; });
  document.querySelectorAll('a[href^="/exports/wechat-voice/"]').forEach((node) => { node.dataset.moduleEntry = 'voice'; });
  $('#import-button').dataset.moduleOperation = 'sync';
  function isEnabled(id) { return modules.find((item) => item.id === id)?.enabled !== false; }
  function navigate(view, updateHash = true) {
    const selected = views.includes(view) ? view : 'catalog';
    state.interactionEpoch += 1;
    state.currentView = selected;
    views.forEach((id) => { const node = $(`#${id}`); if (node) node.hidden = id !== selected; });
    $('#dashboard').hidden = selected !== 'catalog' || !state.activeBundle;
    $('#empty-state').hidden = selected !== 'catalog' || !state.catalogLoaded || state.catalogPhase !== 'ready' || Boolean(state.bundles.length);
    document.querySelectorAll('[data-view]').forEach((item) => {
      const active = item.dataset.view === selected;
      item.classList.toggle('is-active', active);
      if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
    });
    if (!views.includes(view) || !location.hash) history.replaceState(null, '', `#${selected}`);
    else if (updateHash && location.hash !== `#${selected}`) history.pushState(null, '', `#${selected}`);
    adapters.forEach((adapter, id) => {
      adapter.setActive?.(selected === id);
      if (selected === id) adapter.activate?.();
    });
    document.dispatchEvent(new CustomEvent('archive:view', { detail: { view: selected } }));
    if (updateHash) focusView();
  }
  function focusView() {
    const title = $(`#${state.currentView} h1`);
    if (title) { title.tabIndex = -1; title.focus({ preventScroll: true }); }
  }
  function setModules(value) {
    modules = Array.isArray(value) ? value : [];
    document.querySelectorAll('[data-module-entry]').forEach((node) => { node.hidden = !isEnabled(node.dataset.moduleEntry); });
    document.querySelectorAll('[data-module-operation]').forEach((node) => {
      const disabled = !isEnabled(node.dataset.moduleOperation);
      if (disabled && !node.dataset.moduleDisabled) node.dataset.preModuleDisabled = String(node.disabled);
      if (disabled) { node.disabled = true; node.dataset.moduleDisabled = 'true'; }
      else if (node.dataset.moduleDisabled) {
        node.disabled = node.dataset.preModuleDisabled === 'true';
        delete node.dataset.moduleDisabled; delete node.dataset.preModuleDisabled;
      }
    });
    const stopped = $('#analysis-module-stopped');
    if (stopped) stopped.hidden = isEnabled('analysis');
    document.dispatchEvent(new CustomEvent('archive:modules', { detail: { modules } }));
  }
  const shell = {
    navigate, isEnabled, setModules,
    register(id, adapter) {
      adapters.set(id, adapter);
      adapter.setActive?.(state.currentView === id);
      if (state.currentView === id) adapter.activate?.();
    },
    get modules() { return modules; },
  };
  window.ArchiveShell = Object.freeze(shell);
  window.navigateWorkspace = navigate; // Existing workspace links remain compatible.
  document.addEventListener('click', (event) => {
    const link = event.target.closest('a[href^="#"]');
    if (link?.hash === '#main') {
      event.preventDefault();
      state.interactionEpoch += 1;
      $('#main').focus({ preventScroll: true });
      $('#main').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
      return;
    }
    if (!link || !views.includes(link.hash.slice(1))) return;
    event.preventDefault(); navigate(link.hash.slice(1));
    if (link.id === 'first-import-link') {
      $('#import-panel').open = true;
      $('#chat-file').focus({ preventScroll: true });
      $('#import-panel').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
    } else window.scrollTo({ top: 0, behavior: scrollBehavior() });
  });
  function restoreView() {
    const view = location.hash.slice(1);
    // popstate and hashchange may describe the same traversal.
    if (view === state.currentView) return;
    navigate(view, false);
    focusView();
  }
  window.addEventListener('popstate', restoreView);
  window.addEventListener('hashchange', restoreView);
  navigate(location.hash.slice(1), false);
})();
