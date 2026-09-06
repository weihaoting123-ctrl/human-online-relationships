/* Backup controls call only this loopback service. No paths or file contents are rendered. */
(() => {
  'use strict';
  let active = false, ready = false, loading = false, requesting = false, running = false;
  let snapshots = 0, timer = null, sequence = 0;
  const count = (value) => Number.isFinite(Number(value)) ? Math.max(0, Math.floor(Number(value))) : 0;
  const fmt = (value) => count(value).toLocaleString('zh-CN');
  const bytes = (value) => {
    const size = count(value);
    if (size < 1024) return `${fmt(size)} B`;
    const index = Math.min(4, Math.floor(Math.log(size) / Math.log(1024)));
    return `${(size / (1024 ** index)).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} ${['B', 'KiB', 'MiB', 'GiB', 'TiB'][index]}`;
  };
  function feedback(message = '', error = false) {
    const node = $('#backup-feedback');
    node.textContent = message; node.hidden = !message;
    node.classList.toggle('is-error', error);
  }
  function controls() {
    $('#backup-run').disabled = !ArchiveShell.isEnabled('backup') || !ready || loading || requesting || running;
    $('#backup-verify').disabled = !ready || loading || requesting || running || !snapshots;
    $('#backup-refresh').disabled = loading || requesting;
    $('#backup-panel').setAttribute('aria-busy', String(loading || requesting || running));
  }
  function render(backup) {
    const counts = backup.counts || {}, categories = counts.categories || {};
    const wasRunning = running;
    running = backup.state === 'running';
    snapshots = count(backup.snapshot_count);
    const verifying = backup.mode === 'verify';
    const fullyVerified = backup.verification_complete === true;
    const verifyLabel = fullyVerified ? '校验完成' : '局部校验完成';
    const labels = { idle: '尚未备份', running: verifying ? '正在校验' : '正在备份', busy: '其他操作占用', completed: verifying ? verifyLabel : '备份完成', failed: verifying ? '校验未完成' : '备份未完成' };
    $('#backup-status-badge').textContent = labels[backup.state] || '状态待确认';
    $('#backup-status-badge').classList.toggle('is-ready', backup.state === 'completed' && (!verifying || fullyVerified));
    $('#backup-status-badge').classList.toggle('is-running', running);
    $('#backup-status-badge').classList.toggle('is-warning', ['failed', 'busy'].includes(backup.state) || (verifying && backup.state === 'completed' && !fullyVerified));
    $('#backup-summary').textContent = running
      ? (verifying ? '正在检查快照与独立文件副本，请等待校验结束。' : '正在保存本机副本；完成后才会将新快照加入历史。')
      : backup.state === 'busy'
        ? '其他本机操作占用，请稍后重试。'
        : backup.state === 'failed'
        ? '本次操作未完成。已有历史快照保留；可刷新状态后重新执行。'
        : snapshots ? `已保留 ${fmt(snapshots)} 份历史快照；最近一次记录包含 ${fmt(counts.files)} 个文件。` : '还没有备份快照。点击“立即备份”，保存当前已归档的本机内容。';
    [['text', '#backup-text-count'], ['voice', '#backup-voice-count'], ['images', '#backup-image-count'], ['other', '#backup-other-count']].forEach(([key, selector]) => {
      $(selector).textContent = ready && (snapshots || counts.files !== undefined) ? `${fmt(categories[key])} 个` : '—';
    });
    $('#backup-last-success').textContent = backup.last_success_at ? formatLocalTime(backup.last_success_at) : '尚无';
    $('#backup-snapshot-count').textContent = `${fmt(snapshots)} 份`;
    $('#backup-bytes').textContent = counts.bytes === undefined ? '—' : bytes(counts.bytes);
    $('#backup-changes').textContent = counts.files === undefined ? '' : `最近一次：新增或变更副本 ${fmt(counts.objects_copied)} 个（${bytes(counts.bytes_copied)}），未变化 ${fmt(counts.unchanged)} 个，跳过 ${fmt(counts.skipped)} 个。数量按文件统计，不代表消息条数。`;
    $('#backup-verification').hidden = !verifying;
    $('#backup-verification').textContent = `本次已校验 ${fmt(counts.verified)} 个独立内容副本。${backup.state === 'completed' ? fullyVerified ? '整个快照校验完成。' : '尚未完成整个快照的校验，可点击“校验备份”完整检查。' : ''}`;
    if (backup.state === 'busy') feedback('本次备份或校验没有开始，也不会自动重新提交。稍后点击按钮可手动再试。');
    else if (backup.state === 'failed') feedback('备份或校验未完成。请检查可用磁盘空间与本机文件权限后再执行；已有备份不会被本次失败覆盖。', true);
    else if (backup.state === 'completed' && wasRunning) feedback(verifying ? fullyVerified ? '校验完成，整个快照的备份内容已检查。' : '局部校验完成，整个快照仍需完整校验。' : '备份完成，本机快照历史已更新。');
  }
  function renderHistory(items) {
    const target = $('#backup-history-list'); target.replaceChildren();
    const rows = Array.isArray(items) ? items : [];
    if (!rows.length) { target.append(el('p', 'field-note', '尚无历史快照。首次备份成功后会显示在这里。')); return; }
    const list = el('ol', 'backup-history-list');
    rows.forEach((snapshot) => {
      const row = el('li', 'backup-history-row');
      const date = el('time', '', snapshot.created_at ? formatLocalTime(snapshot.created_at) : '时间未知');
      const categories = snapshot.categories || {};
      row.append(date, el('span', '', `${fmt(snapshot.files)} 个文件`), el('span', '', bytes(snapshot.bytes)),
        el('small', '', `文字 / 消息 ${fmt(categories.text)} · 语音 ${fmt(categories.voice)} · 图片 ${fmt(categories.images)} · 其他 ${fmt(categories.other)}`));
      list.append(row);
    }); target.append(list);
  }
  async function refresh() {
    if (loading || requesting) return;
    clearTimeout(timer); loading = true; controls();
    const requested = ++sequence;
    const [status, history] = await Promise.allSettled([api('/api/backup/status'), api('/api/backup/history')]);
    if (requested !== sequence) return;
    loading = false;
    if (status.status === 'fulfilled' && status.value.backup && typeof status.value.backup === 'object') {
      ready = true; render(status.value.backup);
    } else {
      ready = false;
      $('#backup-status-badge').textContent = '状态未能读取';
      $('#backup-status-badge').classList.remove('is-ready');
      feedback('暂时无法读取备份状态。点击“刷新状态”重新检查。', true);
    }
    if (history.status === 'fulfilled') renderHistory(history.value.snapshots);
    else $('#backup-history-list').replaceChildren(el('p', 'field-note', '暂时无法读取历史快照，刷新状态后重试。'));
    controls();
    if (active && ready) timer = setTimeout(refresh, running ? 1500 : 15000);
  }
  async function start(mode) {
    if (mode === 'run' && !ArchiveShell.isEnabled('backup')) return;
    if (!ready || loading || requesting || running || (mode === 'verify' && !snapshots)) return;
    clearTimeout(timer); requesting = true; controls(); feedback();
    try {
      await api(`/api/backup/${mode}`, { method: 'POST', body: JSON.stringify({}) });
      running = true;
      feedback(mode === 'verify' ? '已开始校验备份，正在读取进度。' : '已开始备份当前本机归档，正在读取进度。');
    } catch (_) {
      feedback('请求状态尚未确认。正在读取本机状态；没有自动重新提交。', true);
    } finally { requesting = false; await refresh(); }
  }
  $('#backup-run').addEventListener('click', () => start('run'));
  $('#backup-verify').addEventListener('click', () => start('verify'));
  $('#backup-refresh').addEventListener('click', () => { feedback(); refresh(); });
  window.BackupWorkspace = {
    setActive(value) { active = Boolean(value); clearTimeout(timer); if (active) refresh(); },
  };
  ArchiveShell.register('maintenance', window.BackupWorkspace);
  document.addEventListener('archive:modules', controls);
})();
