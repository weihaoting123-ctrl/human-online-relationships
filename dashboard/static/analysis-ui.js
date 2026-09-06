/* Selection and consent live only in this page; no automatic model invocation. */
(() => {
  'use strict';
  const providers = { openai: 'OpenAI', deepseek: 'DeepSeek' };
  const defaultModels = { openai: 'gpt-5.6-terra', deepseek: 'deepseek-v4-pro' };
  const endpoints = { openai: 'https://api.openai.com/v1/chat/completions', deepseek: 'https://api.deepseek.com/chat/completions' };
  const focuses = { overview: '对话摘要与行动项', communication: '沟通方式与互动变化', business: '业务推进与待办风险', relationship: '关系观察与边界建议' };
  let bundles = [], config = {}, preview = null, revision = 0, busy = false, loaded = false;
  let configDirty = false, expiryTimer = null, displayedReport = null;
  let configSequence = 0, reportSequence = 0;
  let activeJob = null, pollTimer = null, pollSequence = 0, cancelRequested = false;
  const selectedMode = () => document.querySelector('input[name="ai-analysis-mode"]:checked').value;
  const modeName = (mode) => mode === 'full' ? '全量文字' : '抽样文字';
  const send = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });
  const byId = (id) => bundles.find((bundle) => bundle.id === id);
  const contact = (id) => byId(id) ? displayName(byId(id)) : '所选会话';
  function feedback(message = '', error = false) {
    const node = $('#ai-feedback');
    node.textContent = message; node.hidden = !message;
    node.classList.toggle('is-error', error);
  }
  function step(name) {
    ['scope', 'confirm', 'result'].forEach((key) => {
      const node = $(`#ai-step-${key}`);
      node.classList.toggle('is-current', key === name);
      if (key === name) node.setAttribute('aria-current', 'step');
      else node.removeAttribute('aria-current');
    });
  }
  function canRun() {
    $('#ai-run-button').disabled = !ArchiveShell.isEnabled('analysis') || busy || !preview?.recipient?.configured || configDirty || !$('#ai-consent').checked
      || (Number(preview?.plan?.blocked_calls) > 0 && !$('#ai-retry-uncertain').checked);
  }
  function invalidate() {
    revision += 1; preview = null;
    reportSequence += 1;
    window.RelationshipTimeline?.clear($('#ai-relationship-timeline'));
    clearTimeout(expiryTimer);
    $('#ai-consent').checked = false;
    $('#ai-retry-uncertain').checked = false;
    $('#ai-preview').hidden = true;
    $('#ai-metrics').hidden = true;
    $('#ai-placeholder').hidden = !$('#ai-result').hidden;
    if (!busy) step('scope');
    canRun();
  }
  function setBusyState(value) {
    busy = value;
    $('#ai-scope-fields').disabled = value || !ArchiveShell.isEnabled('analysis');
    $('#ai-config-fields').disabled = value;
    $('#ai-recheck-partial').disabled = value || !ArchiveShell.isEnabled('analysis');
    $('#ai-run-button').textContent = value ? '正在分析，请勿重复提交…' : '确认并开始云端分析';
    canRun();
  }
  function updateMode() {
    const full = selectedMode() === 'full';
    $('#ai-sample-limit').hidden = full;
    $('#ai-max-messages').disabled = full;
    $('#ai-mode-note').textContent = full
      ? '全量文字不设条数上限，所选范围按时间分段分析并汇总；可能产生多次计费调用。只包含当前会话。'
      : '抽样按时间分层选取。全部日期只改变时间范围，不会自动切换到全量文字。';
    invalidate();
  }
  function populateBundles() {
    const selected = $('#ai-bundle').value;
    const terms = Catalog.tokens($('#ai-contact-search').value);
    const filtered = bundles.filter((bundle) => terms.every((term) => Catalog.normalize([bundle.contact, bundle.library?.alias, ...(bundle.library?.tag_names || [])].join(' ')).includes(term)) || bundle.id === selected);
    const options = [el('option', '', filtered.length ? '请选择一个会话' : '没有匹配的会话')];
    options[0].value = '';
    filtered.sort((a, b) => String(a.contact).localeCompare(String(b.contact), 'zh-CN')).forEach((bundle) => {
      const option = el('option', '', `${displayName(bundle)}${bundle.library?.alias ? `（${bundle.contact}）` : ''}${bundle.conversation_kind === 'group' ? ' · 群聊' : ''}`);
      option.value = bundle.id; options.push(option);
    });
    $('#ai-bundle').replaceChildren(...options);
    $('#ai-bundle').value = selected;
  }
  function preset(value) {
    const bundle = byId($('#ai-bundle').value);
    if (!bundle) return;
    const last = Catalog.lastDate(bundle), first = Catalog.firstDate(bundle);
    let from = first;
    if (value !== 'all' && last) {
      const day = new Date(`${last}T00:00:00Z`);
      day.setUTCDate(day.getUTCDate() - Number(value) + 1);
      from = day.toISOString().slice(0, 10);
      if (first && from < first) from = first;
    }
    $('#ai-date-from').value = from; $('#ai-date-to').value = last;
    document.querySelectorAll('[data-ai-range]').forEach((button) => {
      button.setAttribute('aria-pressed', String(button.dataset.aiRange === String(value)));
    });
    invalidate();
  }
  function selectBundle(id) {
    if (busy) { feedback('请等待当前分析结束后再切换对象。'); return; }
    $('#ai-contact-search').value = '';
    populateBundles(); $('#ai-bundle').value = id;
    const bundle = byId(id);
    $('#ai-contact-meta').textContent = bundle
      ? `${number(bundle.message_count)} 条记录 · ${Catalog.firstDate(bundle) || '日期未知'} — ${Catalog.lastDate(bundle) || '日期未知'}。快捷区间截至该会话最近记录。`
      : '选择联系人对应的私聊，或一个群聊。不跨会话合并人员身份。';
    if (bundle) preset('30'); else invalidate();
    $('#ai-result').hidden = true; $('#ai-placeholder').hidden = false;
    feedback();
  }
  // Prefill only. The existing preview and single-run consent remain separate actions.
  function selectRange(id, dateFrom, dateTo) {
    if (busy || !ArchiveShell.isEnabled('analysis') || !byId(id)) return false;
    selectBundle(id);
    $('#ai-date-from').value = dateFrom; $('#ai-date-to').value = dateTo;
    document.querySelectorAll('[data-ai-range]').forEach((node) => node.setAttribute('aria-pressed', 'false'));
    invalidate(); return true;
  }
  function renderConfig() {
    $('#ai-provider').value = config.provider || 'openai';
    $('#ai-model').value = config.model || defaultModels[$('#ai-provider').value] || '';
    $('#ai-api-key').value = '';
    $('#ai-config-badge').textContent = config.configured ? `${providers[config.provider]} · 已配置` : '尚未配置模型';
    $('#ai-config-badge').classList.toggle('is-ready', Boolean(config.configured));
    $('#ai-endpoint').textContent = `目标：${endpoints[$('#ai-provider').value]}`;
    $('#ai-key-note').textContent = config.has_key ? '已在本机加密保存；留空可保留当前服务商的密钥。不会回显密钥。' : '尚未保存密钥。保存设置不会发送聊天或试用模型。';
    $('#ai-clear-config').disabled = !config.has_key;
    configDirty = false; canRun();
  }
  async function loadConfig() {
    const sequence = ++configSequence;
    const response = await api('/api/ai/config');
    if (sequence !== configSequence) return;
    config = response;
    renderConfig();
  }
  function renderResult(report) {
    displayedReport = report;
    const result = report.result || {};
    const mode = report.mode || report.scope?.analysis_mode || 'sample';
    const coverage = report.coverage;
    const partial = coverage?.complete === false;
    $('#ai-placeholder').hidden = true; $('#ai-result').hidden = false;
    if (!busy) $('#ai-job-progress').hidden = true;
    $('#ai-result-title').textContent = `${contact(report.scope?.bundle_id)} · ${focuses[report.scope?.focus] || '分析报告'}`;
    $('#ai-result-meta').textContent = `${report.scope?.date_from || ''} — ${report.scope?.date_to || ''} · ${modeName(mode)} · ${number(coverage?.analyzed_messages ?? report.sample_messages)} 条已分析${report.scope?.include_voice_transcripts ? ' · 已选择加入语音转写' : ''} · ${providers[report.provider] || report.provider || ''} / ${report.model || ''} · ${formatLocalTime(report.created_at)}`;
    $('#ai-result-status').hidden = !partial;
    const remainingMessages = Math.max(0, Number(coverage?.eligible_messages || 0) - Number(coverage?.analyzed_messages || 0));
    const remainingSegments = Math.max(0, Number(coverage?.total_segments || 0) - Number(coverage?.completed_segments || 0));
    $('#ai-result-status').textContent = partial ? `${mode === 'full' ? '全量计划 · 部分完成。' : ''}部分结果：已完成 ${number(coverage.completed_segments)} / ${number(coverage.total_segments)} 段；还有 ${number(remainingMessages)} 条文字、${number(remainingSegments)} 段未完成。${remainingSegments === 0 ? '分段已完成，汇总尚未完成。' : '后续分段或汇总未完成。'}不能视为完整范围的结论。` : '';
    $('#ai-result-recovery').hidden = !partial;
    $('#ai-result-stop-reason').textContent = partial ? `停止原因：${typeof report.stop_reason === 'string' && report.stop_reason ? report.stop_reason : '这份历史报告未记录具体原因，已完成分段仍保留在本机。'}${Number.isInteger(report.failed_segment) ? `（第 ${number(report.failed_segment)} 段）` : ''}` : '';
    AnalysisCharts.renderCoverage($('#ai-result-coverage'), { eligible: coverage?.eligible_messages, analyzed: coverage?.analyzed_messages ?? report.sample_messages, segments: coverage?.total_segments ?? report.plan?.segments, completed: coverage?.completed_segments, mode });
    if (partial && mode === 'full') $('#ai-result-coverage .ai-coverage-label strong').textContent = '全量计划 · 部分完成';
    AnalysisCharts.renderSegments($('#ai-result-segments'), report.segments);
    AnalysisCharts.renderMetrics($('#ai-metrics'), report.metrics, `${contact(report.scope?.bundle_id)} · ${report.scope?.date_from || ''} — ${report.scope?.date_to || ''}`);
    window.RelationshipTimeline?.render($('#ai-relationship-timeline'), {
      local: report.metrics?.timeline, semantic: result.timeline, mode: 'report', partial,
      label: `${contact(report.scope?.bundle_id)} · 报告中的记录与解释`,
    });
    $('#ai-result-summary').textContent = result.summary || '没有可用摘要。';
    const sections = $('#ai-result-sections'); sections.replaceChildren();
    [['observations', '从已分析文字中观察到'], ['actions', '可以做的下一步'], ['caveats', '需要保留的不确定性']].forEach(([key, title]) => {
      const section = el('section', `result-section result-${key}`);
      const list = el('ul');
      (Array.isArray(result[key]) ? result[key] : []).forEach((line) => list.append(el('li', '', String(line))));
      section.append(el('h3', '', title), list); sections.append(section);
    });
    step('result');
  }
  async function loadReport(id) {
    // A history selection supersedes a pending preview, including its timeline.
    invalidate();
    const sequence = ++reportSequence;
    try {
      const report = await api(`/api/ai/reports/${encodeURIComponent(id)}`);
      if (sequence === reportSequence) { $('#ai-preview').hidden = true; renderResult(report); }
    } catch (error) { if (sequence === reportSequence) feedback(error.message, true); }
  }
  $('#ai-recheck-partial').addEventListener('click', () => {
    if (busy || !displayedReport?.scope) return;
    const scope = displayedReport.scope;
    if (!byId(scope.bundle_id)) { feedback('这份报告对应的会话目前不在本机档案中，请先刷新或同步档案。', true); return; }
    selectBundle(scope.bundle_id);
    $('#ai-date-from').value = scope.date_from || '';
    $('#ai-date-to').value = scope.date_to || '';
    $('#ai-focus').value = focuses[scope.focus] ? scope.focus : 'overview';
    $('#ai-include-voice').checked = Boolean(scope.include_voice_transcripts);
    const mode = displayedReport.mode || scope.analysis_mode || 'sample';
    document.querySelectorAll('input[name="ai-analysis-mode"]').forEach((input) => { input.checked = input.value === (mode === 'full' ? 'full' : 'sample'); });
    if (scope.max_messages && [...$('#ai-max-messages').options].some((option) => option.value === String(scope.max_messages))) $('#ai-max-messages').value = String(scope.max_messages);
    document.querySelectorAll('[data-ai-range]').forEach((button) => button.setAttribute('aria-pressed', 'false'));
    updateMode();
    $('#ai-scope-form').requestSubmit();
  });
  async function loadHistory() {
    const payload = await api('/api/ai/history');
    const list = $('#ai-history-list'); list.replaceChildren();
    if (!payload.reports?.length) { list.append(el('p', 'field-note', '尚无分析记录。报告和已保存的部分结果会保存在这里。')); return; }
    payload.reports.forEach((report) => {
      const button = el('button', 'history-item'); button.type = 'button';
      const label = el('span');
      label.append(el('strong', '', contact(report.scope?.bundle_id)), el('small', '', `${report.scope?.date_from || ''} — ${report.scope?.date_to || ''} · ${modeName(report.mode || report.scope?.analysis_mode)} · ${focuses[report.scope?.focus] || '分析报告'}${report.scope?.include_voice_transcripts ? ' · 已选择加入语音转写' : ''}`));
      if (report.coverage?.complete === false) label.append(el('small', 'ai-history-partial', `部分结果 · ${number(report.coverage.completed_segments)} / ${number(report.coverage.total_segments)} 段`));
      button.append(label, el('span', 'history-provider', `${providers[report.provider] || report.provider} / ${report.model}`), el('span', 'history-date', formatLocalTime(report.created_at)), el('span', '', '查看 →'));
      button.addEventListener('click', () => { if (!busy) loadReport(report.id); });
      list.append(button);
    });
  }
  async function activate() {
    if (loaded) return;
    loaded = true;
    try { await Promise.all([loadConfig(), loadHistory(), recoverJobs()]); }
    catch (error) { loaded = false; feedback(error.message, true); }
  }
  $('#ai-contact-search').addEventListener('input', populateBundles);
  $('#ai-bundle').addEventListener('change', () => selectBundle($('#ai-bundle').value));
  ['#ai-date-from', '#ai-date-to', '#ai-focus', '#ai-max-messages', '#ai-include-voice'].forEach((id) => {
    $(id).addEventListener('input', invalidate); $(id).addEventListener('change', invalidate);
  });
  document.querySelectorAll('input[name="ai-analysis-mode"]').forEach((input) => input.addEventListener('change', updateMode));
  ['#ai-date-from', '#ai-date-to'].forEach((id) => $(id).addEventListener('input', () => {
    document.querySelectorAll('[data-ai-range]').forEach((button) => button.setAttribute('aria-pressed', 'false'));
  }));
  document.querySelectorAll('[data-ai-range]').forEach((button) => button.addEventListener('click', () => preset(button.dataset.aiRange)));
  $('#ai-consent').addEventListener('change', canRun);
  $('#ai-retry-uncertain').addEventListener('change', canRun);
  $('#ai-config-form').addEventListener('input', () => { configSequence += 1; configDirty = true; invalidate(); });
  $('#ai-provider').addEventListener('change', () => {
    configSequence += 1; configDirty = true; invalidate(); $('#ai-api-key').value = '';
    const currentModel = $('#ai-model').value.trim();
    if (!currentModel || Object.values(defaultModels).includes(currentModel)) {
      $('#ai-model').value = defaultModels[$('#ai-provider').value] || '';
    }
    $('#ai-endpoint').textContent = `目标：${endpoints[$('#ai-provider').value]}`;
    $('#ai-key-note').textContent = $('#ai-provider').value === config.provider && config.has_key ? '留空可保留当前服务商的已存密钥。' : '切换服务商后需要输入该服务商的新密钥。';
  });
  $('#ai-config-form').addEventListener('submit', async (event) => {
    event.preventDefault(); if (busy) return;
    configSequence += 1;
    const settings = { provider: $('#ai-provider').value, model: $('#ai-model').value.trim(), api_key: $('#ai-api-key').value.trim() };
    $('#ai-api-key').value = ''; invalidate();
    $('#ai-config-fields').disabled = true;
    try {
      config = await send('/api/ai/config', settings); renderConfig();
      feedback('连接已加密保存。尚未调用模型；请重新核对分析范围。');
    } catch (error) { feedback(error.message, true); }
    finally { settings.api_key = ''; $('#ai-api-key').value = ''; $('#ai-config-fields').disabled = false; }
  });
  $('#ai-clear-config').addEventListener('click', async () => {
    if (busy) return;
    configSequence += 1;
    invalidate(); $('#ai-config-fields').disabled = true;
    try { config = await send('/api/ai/config/clear', {}); renderConfig(); feedback('已移除本机模型连接。已有分析报告保留。'); }
    catch (error) { feedback(error.message, true); }
    finally { $('#ai-config-fields').disabled = false; }
  });
  $('#ai-scope-form').addEventListener('submit', async (event) => {
    event.preventDefault(); if (busy) return;
    if (!ArchiveShell.isEnabled('analysis')) { feedback('AI 分析已停用。可查看历史，或到设置中启用新的分析。'); return; }
    invalidate(); const requested = revision;
    const scope = { bundle_id: $('#ai-bundle').value, date_from: $('#ai-date-from').value, date_to: $('#ai-date-to').value, focus: $('#ai-focus').value, analysis_mode: selectedMode(), include_voice_transcripts: $('#ai-include-voice').checked };
    if (scope.analysis_mode === 'sample') scope.max_messages = Number($('#ai-max-messages').value);
    if (!scope.bundle_id || !scope.date_from || !scope.date_to || scope.date_from > scope.date_to) { feedback('请选择一个会话，并填写有效的起止日期。', true); return; }
    if (configDirty) { feedback('模型设置有未保存的修改，请先保存或重新载入页面。', true); return; }
    $('#ai-preview-button').disabled = true; $('#ai-recheck-partial').disabled = true; feedback('正在本机核对文字范围、统计与调用计划，不调用云端…');
    try {
      const response = await send('/api/ai/preview', scope);
      if (requested !== revision) return;
      preview = response;
      const recipient = response.recipient || {};
      const plan = response.plan || { mode: scope.analysis_mode, segments: 1, total_calls: 1, cached_calls: 0, new_calls: 1, merge_calls: 0, characters: response.sample_chars };
      const mode = plan.mode || scope.analysis_mode;
      $('#ai-placeholder').hidden = true; $('#ai-result').hidden = true; $('#ai-preview').hidden = false;
      $('#ai-job-progress').hidden = true;
      $('#ai-preview-title').textContent = contact(scope.bundle_id);
      $('#ai-preview-range').textContent = `${scope.date_from} — ${scope.date_to} · ${modeName(mode)} · ${focuses[scope.focus]}`;
      AnalysisCharts.renderCoverage($('#ai-preview-coverage'), { eligible: response.eligible_messages, analyzed: response.sample_messages, segments: plan.segments, mode, planned: true });
      AnalysisCharts.renderMetrics($('#ai-metrics'), response.metrics, `${contact(scope.bundle_id)} · ${scope.date_from} — ${scope.date_to}`);
      window.RelationshipTimeline?.render($('#ai-relationship-timeline'), {
        local: response.metrics?.timeline, mode: 'preview', label: `${contact(scope.bundle_id)} · 本机范围核对`,
      });
      const counts = $('#ai-preview-counts'); counts.replaceChildren();
      [['区间内记录', response.stats?.scope_messages], ['可用文字', response.eligible_messages], ['计划分析文字', response.sample_messages], ['脱敏后字数', plan.characters ?? response.sample_chars]].forEach(([label, value]) => {
        const item = el('div'); item.append(el('dt', '', label), el('dd', '', number(value))); counts.append(item);
      });
      const callPlan = $('#ai-call-plan'); callPlan.replaceChildren();
      callPlan.append(el('strong', '', `${number(plan.segments)} 段 · ${number(plan.new_calls)} 次新调用`));
      callPlan.append(el('p', 'field-note', `计划共 ${number(plan.total_calls)} 次调用，其中 ${number(plan.cached_calls)} 次可复用已完成结果，${number(plan.merge_calls)} 次用于汇总。${plan.estimated_input_tokens === undefined ? '' : `输入约 ${number(plan.estimated_input_tokens)} tokens；每次输出上限 ${number(plan.output_token_limit)} tokens。`}`));
      callPlan.append(el('p', 'field-note', '多次调用可能分别计费。token 数是近似值，实际费用以服务商计费为准。已完成分段只在重新核对并确认后复用，不会自动补跑。'));
      (Array.isArray(plan.warnings) ? plan.warnings : []).forEach((warning) => callPlan.append(el('p', 'field-note', String(warning))));
      $('#ai-retry-line').hidden = !(Number(plan.blocked_calls) > 0);
      $('#ai-retry-uncertain').checked = false;
      if (Number(plan.blocked_calls) > 0) callPlan.append(el('p', 'field-note', `${number(plan.blocked_calls)} 次此前失败或结果未知，需要下方单独确认后才能重试。`));
      $('#ai-preview-recipient').textContent = recipient.configured ? `接收方：${providers[recipient.provider]} / ${recipient.model} · ${recipient.endpoint}` : '尚未配置接收方。请在模型连接设置中保存服务商、模型与 API Key，然后重新核对。';
      $('#ai-preview-limit').textContent = `${mode === 'full' ? '分析所选单一会话与日期内全部可用文字' : '按时间抽样分析所选范围文字'}；排除 ${number(response.stats?.excluded_nontext)} 条非文本记录。${mode === 'full' ? `${number(plan.split_messages)} 条长文字被拆分以完整覆盖。` : `${number(response.truncated_messages)} 条长文字被截断。`}图片、原始音频和附件不上传。自动脱敏不能保证自由文本完全匿名。`;
      $('#ai-preview-voice').textContent = `区间内 ${number(response.stats?.voice_messages)} 条语音，${number(response.stats?.transcribed_voice_messages)} 条已有转写；本次计划文字中含 ${number(response.stats?.sampled_voice_messages)} 条语音转写。${scope.include_voice_transcripts ? '已选择加入转写。' : '未选择加入转写。'}转写可能有识别错误，不能据此判断语气；原始音频不上传。`;
      $('#ai-consent-text').textContent = `我确认按上述${modeName(mode)}范围，将脱敏文字及已选转写发送给显示的服务商和模型，并授权本次全部 ${number(plan.segments)} 个分段及 ${number(plan.merge_calls)} 次汇总，合计 ${number(plan.new_calls)} 次新调用。本次不包含其他会话、图片、原始音频或定时分析。`;
      feedback(recipient.configured ? '本机核对完成。确认范围、接收方与全部调用计划后，勾选单次授权开始分析。' : '范围已核对，尚未发送。需要配置模型连接才能继续。');
      step('confirm'); canRun();
      const remaining = new Date(response.expires_at).getTime() - Date.now();
      expiryTimer = setTimeout(() => { if (!busy && preview) { invalidate(); feedback('核对结果已过期，请重新核对范围。'); } }, Number.isFinite(remaining) ? Math.max(0, remaining) : 600000);
    } catch (error) { if (requested === revision) feedback(error.message, true); }
    finally { $('#ai-preview-button').disabled = !ArchiveShell.isEnabled('analysis'); $('#ai-recheck-partial').disabled = busy || !ArchiveShell.isEnabled('analysis'); }
  });
  function progress(job) {
    const value = job.progress || {};
    const plan = job.plan || activeJob?.plan || {};
    const scope = job.scope || activeJob?.scope || {};
    const stateNames = { pending: '等待中', running: '进行中', completed: '已完成', error: '未完成', cancelled: '已停止' };
    const stageNames = { segments: '按时间顺序分析分段', merge: '分段完成，正在汇总', completed: '分析结束' };
    $('#ai-job-progress').hidden = false; $('#ai-placeholder').hidden = true;
    $('#ai-progress-title').textContent = stageNames[value.stage] || '等待分析开始';
    $('#ai-progress-state').textContent = cancelRequested ? '正在停止' : stateNames[job.state] || '检查中';
    $('#ai-progress-scope').textContent = `${contact(scope.bundle_id)} · ${scope.date_from || ''} — ${scope.date_to || ''} · ${modeName(plan.mode || scope.analysis_mode)}`;
    const total = Number(value.total_segments ?? plan.segments) || 1;
    $('#ai-progress-bar').max = total;
    $('#ai-progress-bar').value = Number(value.completed_segments) || 0;
    $('#ai-progress-detail').textContent = `分段 ${number(value.completed_segments)} / ${number(total)} · 调用 ${number(value.completed_calls)} / ${number(value.total_calls ?? plan.total_calls)} · 已复用 ${number(value.cached_calls ?? plan.cached_calls)} 次。${value.stage === 'merge' ? '正在整合分段结果。' : ''}`;
    $('#ai-progress-note').textContent = cancelRequested
      ? '已请求停止。当前请求可能仍在完成，此后不再发起新调用；已发出的请求仍可能产生费用。'
      : '每段完成后保存在本机。停止会等待当前请求结束，再取消后续调用；不会自动重试失败请求。';
    $('#ai-cancel-job').disabled = cancelRequested || !['pending', 'running'].includes(job.state);
  }
  function watch(job, recovered = false) {
    clearTimeout(pollTimer); pollSequence += 1;
    const plan = job.plan || {}, scope = job.scope || {};
    activeJob = { ...job, plan, scope, started: Date.now(), budget: Math.max(180000, (Number(plan.new_calls ?? plan.total_calls ?? job.progress?.total_calls) || 1) * 120000 + 120000) };
    cancelRequested = Boolean(job.cancel_requested);
    const metricsWereVisible = !$('#ai-metrics').hidden;
    invalidate(); setBusyState(true);
    $('#ai-metrics').hidden = !metricsWereVisible;
    $('#ai-result').hidden = true;
    $('#ai-resume-poll').hidden = true;
    progress(job);
    if (recovered) feedback('已找到进行中的分析，正在恢复查看进度；没有重新发送分析请求。');
    poll(activeJob.job_id || activeJob.id, pollSequence);
  }
  async function recoverJobs() {
    const payload = await api('/api/ai/jobs');
    if (activeJob) return;
    const job = (Array.isArray(payload.jobs) ? payload.jobs : []).find((item) => ['pending', 'running'].includes(item.state));
    if (job) watch(job, true);
  }
  async function poll(jobId, sequence) {
    try {
      const job = await api(`/api/ai/jobs/${encodeURIComponent(jobId)}`);
      if (sequence !== pollSequence || !activeJob) return;
      Object.assign(activeJob, job);
      cancelRequested = cancelRequested || Boolean(job.cancel_requested);
      progress(job);
      if (['completed', 'error', 'cancelled'].includes(job.state)) {
        activeJob = null; setBusyState(false); updateMode();
        $('#ai-resume-poll').hidden = true;
        const message = job.state === 'completed' ? '分析完成，报告已保存在本机。'
          : job.state === 'cancelled' ? '已停止后续调用。已完成分段保留在本机；没有自动重试。'
            : `${job.error || '分析未完成。'} 已完成分段保留在本机；不会自动重试。`;
        feedback(message, job.state === 'error');
        if (job.report_id) await loadReport(job.report_id);
        else if (job.state === 'completed') renderResult(job);
        await loadHistory(); return;
      }
      if (Date.now() - activeJob.started > activeJob.budget) throw new Error('已超过按计划调用数预留的等待时间，后台状态尚未确认。');
      pollTimer = setTimeout(() => poll(jobId, sequence), 1500);
    } catch (error) {
      if (sequence !== pollSequence) return;
      $('#ai-resume-poll').hidden = false;
      feedback(`${error.message} 可重新查看进度；此操作不会重新提交分析。`, true);
    }
  }
  $('#ai-run-button').addEventListener('click', async () => {
    if ($('#ai-run-button').disabled) return;
    const id = preview.preview_id;
    const plan = preview.plan || {}, scope = preview.scope || {};
    const retry = Number(plan.blocked_calls) > 0 && $('#ai-retry-uncertain').checked;
    setBusyState(true); clearTimeout(expiryTimer); preview = null;
    $('#ai-consent').checked = false; $('#ai-preview').hidden = true;
    $('#ai-retry-uncertain').checked = false;
    feedback('已按本次授权提交文字范围和调用计划。正在等待模型；不会自动重试。');
    try {
      const body = { preview_id: id, consent: true };
      if (retry) body.retry_uncertain = true;
      const job = await send('/api/ai/run', body);
      watch({ ...job, plan: job.plan || plan, scope: job.scope || scope });
    } catch (error) {
      setBusyState(false); feedback(`${error.message} 未自动重试；正在检查本地任务。`, true);
      try { await recoverJobs(); await loadHistory(); } catch (_) { feedback(`${error.message} 请刷新查看任务状态，避免重复提交。`, true); }
    }
  });
  $('#ai-cancel-job').addEventListener('click', async () => {
    if (!activeJob || cancelRequested) return;
    cancelRequested = true; progress(activeJob);
    try {
      await send(`/api/ai/jobs/${encodeURIComponent(activeJob.job_id || activeJob.id)}/cancel`, {});
      feedback('已请求停止后续调用。当前请求可能仍会完成，已完成结果会保留。');
    } catch (error) { cancelRequested = false; if (activeJob) progress(activeJob); feedback(error.message, true); }
  });
  $('#ai-resume-poll').addEventListener('click', () => { if (activeJob) watch(activeJob, true); });
  $('#ai-refresh-history').addEventListener('click', () => Promise.all([loadHistory(), recoverJobs()]).catch((error) => feedback(error.message, true)));
  window.AiWorkspace = { activate, selectBundle, selectRange, setBundles(value) { bundles = value; populateBundles(); } };
  document.addEventListener('archive:modules', () => {
    if (!ArchiveShell.isEnabled('analysis')) invalidate();
    setBusyState(busy);
    $('#ai-preview-button').disabled = !ArchiveShell.isEnabled('analysis');
  });
  setBusyState(busy);
  ArchiveShell.register('analysis-workspace', { activate });
  $('#scope-ai-button').addEventListener('click', () => {
    if (!state.activeBundle) return;
    selectBundle(state.activeBundle.id); window.navigateWorkspace('analysis-workspace'); window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  AiWorkspace.setBundles(state.bundles);
})();
