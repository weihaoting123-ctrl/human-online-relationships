/* Explicit, fixed-source authorization. No source polling or cloud work on load. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 5000;
  const reasons = {
    CALL_FAILED_POSSIBLY_CHARGED: '模型请求未完成，可能已计费。本次已停止，不会自动重试。',
    SESSION_EXPIRED: '本次 15 分钟授权已到期，请重新核对后开启。',
    USER_STOPPED: '已停止。已发出的请求不能收回，可能已计费。',
    READ_OR_SCOPE_UNAVAILABLE: '读取或授权范围暂不可用，已停止；如有在途调用，可能已计费，不会自动重试。',
    BINDING_CHANGED: '窗口候选已变化，已停止，请重新核对固定会话。',
    SOURCE_IDENTITY_CHANGED: '固定来源身份发生变化，已停止，请重新核对。',
    SOURCE_GAP: '近期记录无法与上次衔接，已停止；请同步归档后重新核对。',
    SOURCE_TIME_REWIND: '发现迟到的历史文字，无法当作授权期间新消息，已停止；请重新核对范围。',
    SOURCE_TIME_OUT_OF_RANGE: '消息时间超出本次授权范围，已停止，不发送这批文字；请核对来源时间。',
    SOURCE_CHANGED: '已读取的消息发生变化，已停止，不重复发送。',
    SOURCE_OVERFLOW: '本次消息数量超出安全范围，已停止，请重新核对。',
    BATCH_OVERFLOW: '这批新增文字超出 20 条或 4,000 字符，已停止，不自动扩大范围。',
    SCOPE_UNAVAILABLE: '会话已隐藏或模块暂不可用，已停止，请核对本机设置。',
    CONFIG_UNAVAILABLE: '模型配置暂不可用，已停止，请在主控制台检查。',
    CONFIG_CHANGED: '模型配置已变化，已停止，请重新核对接收方。',
    SETTINGS_CHANGED: '本机设置已变更，本次授权已失效；恢复设置后也需要重新核对。',
    PROMPT_CHANGED: '建议规则已更新，已停止，需要重新核对授权。',
    ARCHIVE_CHANGED: '历史归档已变化，已停止，请重新核对范围。',
    KEY_UNAVAILABLE: '模型凭据暂不可用，已停止，请在主控制台检查连接设置。',
    LIVE_STOPPED: '已停止。重新开启需要再次核对授权。',
    LIVE_EXPIRED: '本次 15 分钟授权已到期，请重新核对后开启。',
    LIVE_BUDGET_EXHAUSTED: '调用上限已用完，保留最后建议；不会继续调用模型。',
    LIVE_BUSY: '本机同步或读取正在进行，本次辅助已停止；完成后请重新开启，不堆积任务。',
    LIVE_ACCOUNT_UNAVAILABLE: '无法确认唯一来源账号，已停止，请核对本机账号。',
    LIVE_KEY_UNAVAILABLE: '已有读取缓存不可用，已停止；不会重新扫描密钥。',
    LIVE_IDENTITY_UNAVAILABLE: '此归档没有可核验的原生私聊来源，暂不支持自动读取。',
    LIVE_IDENTITY_CHANGED: '固定来源身份发生变化，已停止，请重新核对。',
    LIVE_SOURCE_BUSY: '消息源仍在变化，无法建立稳定副本，已停止。',
    LIVE_SOURCE_UNAVAILABLE: '消息源布局或顺序无法安全读取，已停止，请先核对本机同步状态。',
    LIVE_READ_FAILED: '本机读取未完成，已停止；不会自动重试，请核对同步状态。',
    LIVE_OVERFLOW: '新增文字超出安全范围或无法衔接，已停止；请重新核对。',
    LIVE_SENDER_UNKNOWN: '无法确认新增文字的发送方，已停止。',
    LIVE_DECODE_FAILED: '本机文字解码未完成，已停止，不发送不确定内容。',
  };
  window.CopilotLive = Object.freeze({ create(host) {
    let preview = null, session = null, generation = 0, timer = null;
    let preparing = false, starting = false, ticking = false, resultId = '';
    let statusText = '默认关闭。先识别标题、核对固定会话，再开启限时辅助。';
    const blocked = () => Boolean(preview || session || preparing || starting || ticking);
    const eligible = () => host.mode() === 'auto' && host.ready() && !host.busy();
    function render() {
      $('live-prepare').disabled = !eligible() || blocked();
      $('live-prepare').textContent = preparing ? '正在核对限时范围…' : '核对并开启自动建议';
      $('live-start').disabled = !preview || !eligible() || starting || !$('live-confirmed').checked;
      $('live-start').textContent = starting ? '正在建立本机基线…' : '授权这个会话，开启 15 分钟';
      $('live-stop').hidden = !session && !starting;
      $('live-compact-stop').hidden = !session && !starting;
      $('live-status').textContent = statusText;
      $('live-compact-status').textContent = session || starting ? statusText : '';
      $('live-compact-bar').hidden = !session && !starting;
    }
    function changed() { render(); host.controls(); }
    function clear() { resultId = ''; host.clearResult(); }
    function stopRemote(id) {
      if (id) host.request('/api/copilot/live/stop', { session_id: id }).catch(() => {
        // Never retry an uncertain stop or restart the session. No further ticks.
      });
    }
    function invalidate(message) {
      const wasOpen = blocked(), id = session?.session_id;
      generation += 1;
      window.clearTimeout(timer); timer = null;
      preview = null; session = null; preparing = false; starting = false;
      $('live-consent').hidden = true;
      $('live-confirmed').checked = false;
      if (wasOpen) {
        statusText = message || '上下文或窗口状态已变化，本次自动建议已停止；需重新授权。';
        clear();
      }
      stopRemote(id);
      render();
    }
    function fail() {
      invalidate('本次自动建议已停止：连接或响应未完成，可能已计费；不会自动重试。');
      changed();
    }
    function validPreview(value, scope) {
      return /^[a-f0-9]{48}$/.test(value?.preview_id || '')
        && value.binding_revision === scope.binding_revision && value.account_verified === false
        && value.binding_required === true
        && value.scope && ['bundle_id', 'date_from', 'date_to', 'max_messages', 'direction']
          .every((key) => value.scope[key] === scope[key])
        && value.recipient?.configured === true
        && ['provider', 'model', 'endpoint'].every((key) => typeof value.recipient[key] === 'string')
        && value.limits?.duration_seconds === 900 && value.limits.max_calls === 6
        && value.limits.min_call_interval_seconds === 20
        && Number.isInteger(value.counts?.sample_messages) && value.counts.sample_messages >= 0
        && Number.isInteger(value.counts?.sample_chars) && value.counts.sample_chars >= 0;
    }
    function showPreview(value) {
      const facts = $('live-facts'); facts.replaceChildren();
      const add = (label, text) => {
        const dt = document.createElement('dt'), dd = document.createElement('dd');
        dt.textContent = label; dd.textContent = text; facts.append(dt, dd);
      };
      add('固定会话', host.name());
      add('历史上下文', `${value.scope.date_from} 至 ${value.scope.date_to} · ${value.counts.sample_messages} 条 / ${value.counts.sample_chars} 字符`);
      add('新文字范围', '本次基线之后、授权到期之前，这一个来源的新文字；可跨午夜，不延长授权。');
      add('接收方', `${value.recipient.provider.slice(0, 80)} · ${value.recipient.model.slice(0, 120)}`);
      add('接收地址', value.recipient.endpoint.slice(0, 300));
      add('上限', '15 分钟 · 最多 6 次尝试 · 两次调用至少间隔 20 秒');
      add('文字预算', '每批最多 20 条 / 4,000 字符；含历史每次不超过 200 条 / 20,000 字符。');
      $('live-confirmed').checked = false;
      $('live-consent').hidden = false;
    }
    async function prepare() {
      if (!eligible() || blocked()) return;
      host.reset();
      const epoch = generation, revision = host.revision(), token = host.scope().binding_token;
      preparing = true; statusText = '正在本机核对授权范围，尚未调用模型。'; changed();
      try {
        if (!await host.fresh(token) || revision !== host.revision() || epoch !== generation) return;
        const scope = { ...host.scope(), latest_draft: '' };
        const value = await host.request('/api/copilot/live/preview', scope);
        if (epoch !== generation || revision !== host.revision()) return;
        if (!validPreview(value, scope)) throw new Error('invalid-preview');
        preview = value; showPreview(value);
        statusText = '请核对固定来源与持续授权；此时尚未读取新消息或调用模型。';
      } catch (_) {
        if (epoch === generation) {
          invalidate('无法准备限时范围，请检查本机设置后重新核对；尚未调用模型。'); changed();
        }
      }
      finally { if (epoch === generation) preparing = false; changed(); }
    }
    function accept(value) {
      if (!value || !/^[a-f0-9]{48}$/.test(value.session_id || '')
        || !['active', 'busy', 'exhausted', 'stopped'].includes(value.state)
        || value.binding_revision !== host.revision()
        || !Number.isInteger(value.calls_used) || value.calls_used < 0 || value.calls_used > 6
        || !Number.isInteger(value.calls_remaining) || value.calls_remaining < 0 || value.calls_remaining > 6
        || !Number.isFinite(value.remaining_seconds) || value.remaining_seconds < 0 || value.remaining_seconds > 900
        || (session && value.session_id !== session.session_id)) throw new Error('invalid-session');
      if (value.state === 'stopped') {
        invalidate(reasons[value.reason] || '安全检查未通过，本次自动建议已停止；不会自动重试，请重新核对。');
        changed(); return;
      }
      session = value;
      const minutes = Math.ceil(value.remaining_seconds / 60);
      const phase = value.state === 'exhausted' ? '调用上限已用完，最后建议可能过时'
        : value.state === 'busy' ? '本机读取忙，等待下一轮' : value.result ? '建议已更新' : '等待新消息';
      statusText = `${phase} · 剩余约 ${minutes} 分钟 · 已尝试 ${value.calls_used} / 6 次`;
      if (value.result === null) clear();
      else {
        if (typeof value.result?.result_id !== 'string' || !host.validResult(value.result)) throw new Error('invalid-result');
        if (value.result.result_id !== resultId) {
          clear(); host.renderResult(value.result); resultId = value.result.result_id;
        }
      }
      changed();
      timer = window.setTimeout(tick, POLL_MS);
    }
    async function start() {
      if (!preview || !eligible() || starting || !$('live-confirmed').checked) return;
      const captured = preview, epoch = generation, token = host.scope().binding_token;
      starting = true; clear(); statusText = '正在只读建立基线，已有消息不会自动触发模型。'; changed();
      try {
        if (!await host.fresh(token) || epoch !== generation || !preview || !$('live-confirmed').checked) return;
        preview = null; $('live-consent').hidden = true;
        const value = await host.request('/api/copilot/live/start', {
          preview_id: captured.preview_id, consent: true, binding_confirmed: true,
          binding_revision: captured.binding_revision,
        });
        if (epoch !== generation) { stopRemote(value?.session_id); return; }
        accept(value);
      } catch (_) { if (epoch === generation) fail(); }
      finally { if (epoch === generation) starting = false; changed(); }
    }
    async function tick() {
      if (!session || ticking) return;
      const epoch = generation, id = session.session_id, token = host.scope().binding_token;
      ticking = true;
      try {
        if (!eligible() || !await host.fresh(token) || epoch !== generation) {
          if (epoch === generation) { invalidate(); changed(); }
          return;
        }
        statusText = '正在检查新文字；读取与生成期间不会重复启动。'; changed();
        const value = await host.request('/api/copilot/live/tick', {
          session_id: id, binding_token: token, binding_revision: host.revision(),
        });
        if (epoch !== generation) return;
        accept(value);
      } catch (_) { if (epoch === generation) fail(); }
      finally { ticking = false; changed(); }
    }
    $('live-prepare').addEventListener('click', prepare);
    $('live-confirmed').addEventListener('change', render);
    $('live-start').addEventListener('click', start);
    for (const id of ['live-stop', 'live-compact-stop', 'live-cancel']) {
      $(id).addEventListener('click', () => { invalidate('已停止本次自动建议。已发出的请求无法收回，可能已计费。'); changed(); });
    }
    window.addEventListener('beforeunload', () => invalidate());
    render();
    return Object.freeze({ render, invalidate, blocked });
  }});
})();
