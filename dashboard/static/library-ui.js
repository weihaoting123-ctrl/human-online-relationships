/* Local metadata feature: API versions prevent overwriting another window.
 * Original bundle summaries are display-only; mutations contain only the
 * explicit metadata fields. No raw chat, key, or filesystem path is loaded.
 */
(() => {
  'use strict';
  let library = { conversations: [], tags: [], health: {} };
  let loaded = false, librarySequence = 0, moduleSequence = 0;
  let editing = null, editingTag = null, saving = false, tagSaving = false, moduleSaving = false;
  const dialog = $('#conversation-dialog');
  const send = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });
  const activeTags = () => library.tags.filter((tag) => !tag.deleted);
  const metadata = (id) => library.conversations.find((item) => item.bundle_id === id);
  const tagNames = (ids = []) => activeTags().filter((tag) => ids.includes(tag.id)).map((tag) => tag.name);
  const originalName = (item) => item.source?.contact || item.contact || '未命名会话';
  const name = (item) => item.alias || originalName(item);
  function feedback(id, message = '', error = false) {
    const node = $(id);
    node.textContent = message; node.hidden = !message;
    node.classList.toggle('is-error', error);
    node.setAttribute('role', error ? 'alert' : 'status');
  }
  function colorChip(tag) {
    const chip = el('span', 'library-tag', tag.name);
    // Colors are validated again at the DOM boundary; no arbitrary CSS values.
    if (/^#[0-9a-f]{6}$/i.test(tag.color || '')) chip.style.setProperty('--tag-color', tag.color);
    return chip;
  }
  function setBundles(value) {
    value.forEach((bundle) => {
      if (bundle.library) bundle.library.tag_names = tagNames(bundle.library.tags);
    });
  }
  function applyCatalogMetadata() {
    state.bundles = state.bundles.filter((bundle) => {
      const item = metadata(bundle.id);
      if (!item) return true;
      if (item.hidden_at || item.source_missing) return false;
      bundle.library = { alias: item.alias, note: item.note, pinned: item.pinned,
        hidden_at: item.hidden_at, version: item.version, tags: item.tags, tag_names: tagNames(item.tags) };
      return true;
    });
    if (state.activeBundle) {
      const current = metadata(state.activeBundle.id);
      if (current?.hidden_at || current?.source_missing) {
        state.loadSequence += 1; state.activeBundle = null;
        $('#dashboard').hidden = true;
      } else {
        const summary = state.bundles.find((bundle) => bundle.id === state.activeBundle.id);
        if (summary) state.activeBundle.library = summary.library;
        $('#case-title').textContent = displayName(state.activeBundle);
      }
    }
    renderBundles();
    window.AiWorkspace?.setBundles(state.bundles);
  }
  function renderHealth(error = false) {
    const health = library.health || {};
    const failed = error || ['error', 'unavailable', 'corrupt'].includes(health.status) || health.ok === false;
    const badge = $('#library-health-badge');
    badge.textContent = failed ? '资料库需要检查' : '资料库可用';
    badge.classList.toggle('is-ready', !failed);
    badge.classList.toggle('is-warning', failed);
    const missing = library.conversations.filter((item) => item.source_missing).length;
    $('#library-health-detail').textContent = failed
      ? '暂时无法读取会话资料。可以刷新设置重新检查；原始档案保持保留。'
      : `${number(library.conversations.length)} 个会话资料 · ${number(activeTags().length)} 个可用标签${missing ? ` · ${number(missing)} 个会话的原始档案暂不可用` : ''}。别名、备注和设置保存在本机资料库。`;
  }
  function renderTagFilter() {
    const select = $('#filter-tag');
    const selected = state.filters.tag || 'all';
    const all = el('option', '', '全部标签'); all.value = 'all';
    select.replaceChildren(all);
    activeTags().forEach((tag) => {
      const option = el('option', '', tag.name); option.value = tag.id; select.append(option);
    });
    state.filters.tag = activeTags().some((tag) => tag.id === selected) ? selected : 'all';
    select.value = state.filters.tag;
  }
  function renderTags() {
    const list = $('#tag-list'); list.replaceChildren();
    if (!activeTags().length) list.append(el('p', 'field-note', '还没有标签。创建一个，整理你常用的会话。'));
    activeTags().forEach((tag) => {
      const row = el('div', 'tag-row');
      const actions = el('div', 'tag-row-actions');
      const edit = el('button', 'text-button tag-edit', '编辑'); edit.type = 'button';
      edit.setAttribute('aria-label', `编辑标签 ${tag.name}`);
      edit.addEventListener('click', () => {
        editingTag = { ...tag };
        $('#tag-name').value = tag.name;
        $('#tag-color').value = tag.color;
        if (!$('#tag-color').value) $('#tag-color').value = '#365ecb';
        $('#tag-save').textContent = '保存标签'; $('#tag-cancel').hidden = false;
        feedback('#tag-feedback'); $('#tag-name').focus();
      });
      const remove = el('button', 'text-button tag-delete', '删除'); remove.type = 'button';
      remove.setAttribute('aria-label', `删除标签 ${tag.name}`);
      remove.addEventListener('click', async () => {
        if (tagSaving) return;
        tagSaving = true; remove.disabled = true;
        try {
          const payload = await send('/api/library/tags', { id: tag.id, expected_version: tag.version, deleted: true });
          replaceTag(payload.tag); if (editingTag?.id === tag.id) resetTagForm();
          feedback('#tag-feedback', '已删除标签，聊天原件保留。');
        } catch (error) { feedback('#tag-feedback', `${error.message} 可点击“刷新设置”读取最新标签。`, true); }
        finally { tagSaving = false; remove.disabled = false; }
      });
      actions.append(edit, remove); row.append(colorChip(tag), actions); list.append(row);
    });
    const deleted = library.tags.filter((tag) => tag.deleted);
    const deletedList = $('#tag-deleted-list'); deletedList.replaceChildren();
    $('#tag-deleted').hidden = !deleted.length;
    $('#tag-deleted-count').textContent = `${number(deleted.length)} 个`;
    deleted.forEach((tag) => {
      const row = el('div', 'tag-row');
      const restore = el('button', 'text-button tag-restore', '恢复标签'); restore.type = 'button';
      restore.setAttribute('aria-label', `恢复标签 ${tag.name}`);
      restore.addEventListener('click', async () => {
        if (tagSaving) return;
        tagSaving = true; restore.disabled = true;
        try {
          const payload = await send('/api/library/tags', { id: tag.id, expected_version: tag.version, deleted: false });
          replaceTag(payload.tag); feedback('#tag-feedback', '标签已恢复，可继续整理会话。');
        } catch (error) { feedback('#tag-feedback', `${error.message} 可刷新设置后重试。`, true); }
        finally { tagSaving = false; restore.disabled = false; }
      });
      row.append(colorChip(tag), restore); deletedList.append(row);
    });
    renderTagFilter();
  }
  function renderTrash() {
    const list = $('#trash-list'); list.replaceChildren();
    const hidden = library.conversations.filter((item) => item.hidden_at);
    $('#trash-count').textContent = number(hidden.length);
    const terms = Catalog.tokens($('#trash-search').value);
    const items = hidden.filter((item) => terms.every((term) => Catalog.normalize(
      [name(item), originalName(item), ...tagNames(item.tags)].join(' ')).includes(term)));
    if (!items.length) list.append(el('p', 'library-empty', '没有符合条件的已隐藏会话。'));
    items.forEach((item) => {
      const row = el('article', 'trash-row');
      const info = el('div', 'trash-row-info');
      info.append(el('strong', '', name(item)));
      if (item.alias) info.append(el('small', '', `原名：${originalName(item)}`));
      info.append(el('small', '', `收起于 ${formatLocalTime(item.hidden_at)}`));
      if (item.source_missing) info.append(el('span', 'source-missing', '原始档案暂不可用；恢复资料不会重建原件。'));
      const restore = el('button', 'quiet-button trash-restore', '恢复会话'); restore.type = 'button';
      restore.setAttribute('aria-label', `恢复会话 ${name(item)}`);
      restore.addEventListener('click', async () => {
        setBusy(restore, true, '恢复中…');
        try {
          const payload = await send('/api/library/conversations', { bundle_id: item.bundle_id, expected_version: item.version, hidden: false });
          replaceConversation(payload.conversation);
          await loadState(false); applyCatalogMetadata(); renderTrash();
          feedback('#trash-feedback', item.source_missing ? '资料已恢复；原始档案仍需重新导入。' : '会话已恢复到档案检索。');
        } catch (error) { feedback('#trash-feedback', `${error.message} 可刷新回收站后重试。`, true); }
        finally { setBusy(restore, false); }
      });
      row.append(info, restore); list.append(row);
    });
  }
  async function refreshLibrary() {
    const sequence = ++librarySequence;
    try {
      const payload = await api('/api/library');
      if (sequence !== librarySequence) return;
      library = { conversations: payload.conversations || [], tags: payload.tags || [], health: payload.health || {} };
      loaded = true; renderTags(); renderTrash(); renderHealth(); applyCatalogMetadata();
    } catch (error) { renderHealth(true); throw error; }
  }
  function invalidateCatalogRead() {
    state.stateSequence += 1;
    // Invalidating a pending read must also settle its loading state. A known
    // snapshot remains usable; an unfinished first read needs a fresh GET.
    if (state.catalogPhase === 'loading') state.catalogPhase = state.catalogLoaded ? 'ready' : 'error';
  }
  function replaceConversation(value) {
    librarySequence += 1;
    // A catalog/detail response started before this write must not reinstall
    // old metadata or bring a newly hidden conversation back into the view.
    invalidateCatalogRead(); state.loadSequence += 1;
    $('#dashboard').setAttribute('aria-busy', 'false');
    const index = library.conversations.findIndex((item) => item.bundle_id === value.bundle_id);
    if (index < 0) library.conversations.push(value);
    else library.conversations[index] = { ...library.conversations[index], ...value };
    applyCatalogMetadata(); renderTrash(); renderHealth();
  }
  function replaceTag(value) {
    librarySequence += 1;
    invalidateCatalogRead();
    const index = library.tags.findIndex((item) => item.id === value.id);
    if (index < 0) library.tags.push(value); else library.tags[index] = { ...library.tags[index], ...value };
    renderTags(); applyCatalogMetadata(); renderTrash(); renderHealth();
  }
  function populateConversation(item) {
    if (!item) throw new Error('找不到这个会话的资料。请刷新档案后重试。');
    editing = { ...item };
    $('#conversation-original').textContent = `原始会话：${originalName(item)} · 只修改本机显示资料`;
    $('#conversation-alias').value = item.alias || '';
    $('#conversation-note').value = item.note || '';
    $('#conversation-pinned').checked = Boolean(item.pinned);
    const tags = $('#conversation-tags'); tags.replaceChildren();
    if (!activeTags().length) tags.append(el('p', 'field-note', '暂无标签。可到设置中创建标签，再分配给会话。'));
    activeTags().forEach((tag) => {
      const label = el('label', 'conversation-tag-choice');
      const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.value = tag.id;
      checkbox.checked = (item.tags || []).includes(tag.id);
      label.append(checkbox, colorChip(tag)); tags.append(label);
    });
    $('#conversation-reload').hidden = true;
    $('#conversation-fields').disabled = false; $('#conversation-hide').disabled = false;
  }
  async function reloadConversation() {
    if (saving) return;
    const id = editing?.bundle_id || state.activeBundle?.id;
    $('#conversation-fields').disabled = true; $('#conversation-hide').disabled = true;
    try { await refreshLibrary(); populateConversation(metadata(id)); feedback('#conversation-feedback'); }
    catch (error) { feedback('#conversation-feedback', error.message, true); $('#conversation-reload').hidden = false; }
  }
  $('#manage-conversation').addEventListener('click', () => {
    if (!state.activeBundle) return;
    editing = null; feedback('#conversation-feedback');
    dialog.showModal(); reloadConversation();
  });
  $('#conversation-close').addEventListener('click', () => { if (!saving) dialog.close(); });
  dialog.addEventListener('cancel', (event) => { if (saving) event.preventDefault(); });
  dialog.addEventListener('close', () => { editing = null; $('#manage-conversation').focus(); });
  $('#conversation-reload').addEventListener('click', reloadConversation);
  async function saveConversation(hidden = false) {
    if (saving || !editing) return;
    saving = true;
    $('#conversation-fields').disabled = true; $('#conversation-hide').disabled = true;
    $('#conversation-close').disabled = true; $('#conversation-reload').disabled = true;
    const body = { bundle_id: editing.bundle_id, expected_version: editing.version };
    if (hidden) body.hidden = true;
    else {
      Object.assign(body, { alias: $('#conversation-alias').value.trim(), note: $('#conversation-note').value,
        pinned: $('#conversation-pinned').checked });
      const selected = Array.from(document.querySelectorAll('#conversation-tags input:checked'), (node) => node.value);
      const previous = activeTags().filter((tag) => (editing.tags || []).includes(tag.id)).map((tag) => tag.id);
      // Omit an unchanged selection: deleted tags retain their associations so
      // restoring a tag still restores its context after unrelated note edits.
      if (selected.length !== previous.length || selected.some((id) => !previous.includes(id))) body.tag_ids = selected;
    }
    try {
      const payload = await send('/api/library/conversations', body);
      replaceConversation(payload.conversation);
      if (hidden) { dialog.close(); showNotice('会话已移入回收站，原始文件保留。'); }
      else { populateConversation(metadata(body.bundle_id)); feedback('#conversation-feedback', '会话资料已保存。'); }
    } catch (error) {
      feedback('#conversation-feedback', error.message, true);
      $('#conversation-reload').hidden = error.status !== 409;
    } finally {
      saving = false; $('#conversation-fields').disabled = false;
      $('#conversation-hide').disabled = false; $('#conversation-close').disabled = false;
      $('#conversation-reload').disabled = false;
    }
  }
  $('#conversation-form').addEventListener('submit', (event) => { event.preventDefault(); saveConversation(); });
  $('#conversation-hide').addEventListener('click', () => saveConversation(true));
  $('#trash-search').addEventListener('input', renderTrash);
  $('#trash-refresh').addEventListener('click', () => {
    refreshLibrary().then(() => feedback('#trash-feedback', '回收站已刷新。')).catch((error) => feedback('#trash-feedback', error.message, true));
  });
  function resetTagForm() {
    editingTag = null; $('#tag-form').reset(); $('#tag-save').textContent = '创建标签'; $('#tag-cancel').hidden = true;
  }
  $('#tag-cancel').addEventListener('click', resetTagForm);
  $('#tag-form').addEventListener('submit', async (event) => {
    event.preventDefault(); if (tagSaving) return;
    const body = { name: $('#tag-name').value.trim(), color: $('#tag-color').value };
    if (!body.name) return;
    if (editingTag) Object.assign(body, { id: editingTag.id, expected_version: editingTag.version });
    tagSaving = true; setBusy($('#tag-save'), true, '保存中…');
    try {
      const payload = await send('/api/library/tags', body); replaceTag(payload.tag);
      setBusy($('#tag-save'), false); resetTagForm(); feedback('#tag-feedback', '标签已保存。');
    } catch (error) { setBusy($('#tag-save'), false); feedback('#tag-feedback', `${error.message} 可点击“刷新设置”读取最新标签。`, true); }
    finally { tagSaving = false; }
  });
  function renderModules() {
    const list = $('#module-list'); list.replaceChildren();
    ArchiveShell.modules.forEach((module) => {
      const row = el('label', 'module-row');
      const info = el('span', 'module-description');
      info.append(el('strong', '', module.label), el('small', '', module.description || '可选功能'));
      if (module.requires?.length) {
        const names = module.requires.map((id) => ArchiveShell.modules.find((item) => item.id === id)?.label || id);
        info.append(el('small', 'module-dependency', `需要先启用：${names.join('、')}`));
      }
      const toggle = el('input', 'module-toggle'); toggle.type = 'checkbox'; toggle.checked = module.enabled;
      toggle.disabled = moduleSaving;
      toggle.dataset.moduleToggle = module.id; toggle.setAttribute('role', 'switch');
      toggle.setAttribute('aria-label', `${module.label}功能`);
      toggle.addEventListener('change', async () => {
        if (moduleSaving) { toggle.checked = module.enabled; return; }
        const enabled = toggle.checked;
        moduleSaving = true;
        list.querySelectorAll('input').forEach((node) => { node.disabled = true; });
        feedback('#module-feedback');
        moduleSequence += 1;
        try {
          const payload = await send('/api/modules', { id: module.id, enabled, expected_version: module.version });
          // A refresh may have begun while the write was in flight. Its old
          // snapshot cannot supersede the successful mutation response.
          moduleSequence += 1; moduleSaving = false;
          ArchiveShell.setModules(payload.modules); renderModules();
          feedback('#module-feedback', `${module.label}已${enabled ? '启用' : '停用'}，已有档案与历史保留。`);
        } catch (error) {
          moduleSaving = false;
          toggle.checked = module.enabled;
          feedback('#module-feedback', error.message, true);
          list.querySelectorAll('input').forEach((node) => { node.disabled = false; });
        }
      });
      row.append(info, toggle); list.append(row);
    });
  }
  async function refreshModules() {
    const sequence = ++moduleSequence;
    const payload = await api('/api/modules');
    if (sequence !== moduleSequence) return;
    ArchiveShell.setModules(payload.modules); renderModules();
  }
  async function refreshSettings() {
    const results = await Promise.allSettled([refreshLibrary(), refreshModules()]);
    if (results[0].status === 'rejected') feedback('#tag-feedback', results[0].reason.message, true);
    if (results[1].status === 'rejected') feedback('#module-feedback', results[1].reason.message, true);
  }
  $('#settings-refresh').addEventListener('click', () => { resetTagForm(); refreshSettings(); });
  $('#open-model-settings').addEventListener('click', () => {
    if (!window.AiWorkspace) { feedback('#module-feedback', '分析界面暂未加载。刷新页面后可重新查看模型连接。', true); return; }
    ArchiveShell.navigate('analysis-workspace'); $('#ai-settings').open = true;
    $('#ai-settings').scrollIntoView({ behavior: 'smooth', block: 'start' }); $('#ai-provider').focus();
  });
  $('#filter-tag').addEventListener('change', () => {
    state.filters.tag = $('#filter-tag').value; state.filters.page = 1; renderBundles();
  });
  window.LibraryWorkspace = { setBundles, refresh: refreshLibrary };
  ArchiveShell.register('settings', { activate() { if (loaded) refreshSettings(); } });
  ArchiveShell.register('trash', { activate() { refreshLibrary().catch((error) => feedback('#trash-feedback', error.message, true)); } });
  refreshSettings();
})();
