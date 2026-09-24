"""Opt-in copilot browser tests: invented metadata, local mocked cloud transport.

No real archive, credential, WeChat window, or provider is opened. The fixture
server serves public static files on an ephemeral port; artifacts stay in tmp.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
XSS = '<img src=x onerror=alert(1)>'


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self'; script-src 'self'; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        super().end_headers()


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1', 'Opt-in synthetic browser tests')
class CopilotBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        artifacts = ROOT / 'scripts' / 'tmp'
        artifacts.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='copilot-ui-', dir=artifacts)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(
            QuietHandler, directory=str(ROOT / 'dashboard' / 'static')))
        assert cls.server.server_port != 8765
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/'
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.browser = cls.playwright.chromium.launch(headless=True, channel='msedge')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def setUp(self):
        from playwright.sync_api import expect
        self.expect = expect
        self.context = self.browser.new_context(viewport={'width': 380, 'height': 760}, reduced_motion='reduce')
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(3000)
        self.errors, self.dialogs, self.requests, self.writes = [], [], [], []
        self.hold_preview = self.hold_run = False
        self.pending_preview = self.pending_run = None
        self.fail_run = False
        self.hold_live_start = self.hold_live_tick = self.fail_live_tick = False
        self.pending_live_start = self.pending_live_tick = None
        self.live_tick_result = None
        self.live_tick_reason = None
        self.enabled = True
        self.analysis_enabled = True
        self.modules_override = None
        self.status_conversations = None
        self.hold_binding = False
        self.pending_binding = None
        self.binding_identity = None
        self.binding_counter = 0
        self.binding_token = None
        self.binding_session = None
        self.binding_session_counter = 0
        self.desktop = {'supported': True, 'installed': True, 'state': 'stopped', 'code': 'DESKTOP_STOPPED'}
        self.hold_desktop = False
        self.pending_desktop = None
        self.fail_desktop = False
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('dialog', lambda dialog: (self.dialogs.append(dialog.message), dialog.dismiss()))
        self.page.on('request', lambda request: self.requests.append(request.url))
        self.page.route('**/*', self.route_request)

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.dialogs, [])
        self.assertTrue(all(url.startswith(self.url) for url in self.requests))
        self.assertFalse(any('/api/bundles/' in url or '/api/ai/run' in url for url in self.requests))

    def reply(self, route, value, status=200):
        route.fulfill(status=status, content_type='application/json', body=json.dumps(value, ensure_ascii=False))

    def route_request(self, route):
        request = route.request
        if not request.url.startswith(self.url):
            route.abort()
            return
        path = urlparse(request.url).path
        if not path.startswith('/api/'):
            route.continue_()
            return
        body = request.post_data_json if request.method == 'POST' else None
        if body is not None:
            self.writes.append((path, body))
        if path == '/api/copilot/desktop':
            self.reply(route, {'status': 'ok', 'desktop': self.desktop})
        elif path == '/api/copilot/desktop/launch':
            response = {'status': 'ok', 'desktop': {**self.desktop,
                        'state': 'launch_requested', 'code': 'DESKTOP_LAUNCH_REQUESTED'}}
            if self.hold_desktop:
                self.pending_desktop = (route, response)
            elif self.fail_desktop:
                self.reply(route, {'status': 'error', 'error': 'private-launch-detail'}, 500)
            else:
                self.reply(route, response, 202)
        elif path == '/api/modules':
            if body:
                self.enabled = body.get('enabled') is True
            self.reply(route, {'status': 'ok', 'modules': self.modules_override or [
                {'id': 'analysis', 'enabled': self.analysis_enabled, 'version': 1},
                {'id': 'copilot', 'enabled': self.enabled, 'version': 2}]})
        elif path == '/api/library':
            self.reply(route, {'status': 'ok', 'conversations': [
                {'bundle_id': f'fixture-{i}', 'alias': name, 'hidden_at': None,
                 'source_missing': False, 'source': {'contact': name, 'conversation_kind': 'direct',
                 'date_range': ['2026-03-02', '2026-09-20'], 'message_count': 123}}
                for i, name in enumerate(['合成青禾', '合成云舟', XSS])]})
        elif path == '/api/copilot/status':
            self.reply(route, {'status': 'ok', 'enabled': self.enabled, 'configured': True,
                'provider': 'openai', 'model': 'synthetic-model',
                'endpoint': 'https://api.openai.com/v1/chat/completions',
                'capabilities': {'archive_context': True, 'live_capture': False, 'auto_send': False,
                                 'max_messages': 200, 'max_chars': 20000, 'max_calls': 1},
                **({'conversations': self.status_conversations} if self.status_conversations is not None else {})})
        elif path == '/api/copilot/binding/start':
            self.binding_identity = None
            self.binding_session_counter += 1
            self.binding_session = format(self.binding_session_counter, '048x')
            self.reply(route, {'status': 'ok', 'session_id': self.binding_session})
        elif path == '/api/copilot/binding':
            if body.get('session_id') != self.binding_session:
                self.reply(route, {'state': 'unavailable', 'reason': '识别会话已失效，请重新开启自动识别',
                                   'observation_seq': 0, 'account_verified': False})
                return
            identity = (body.get('target'), body.get('title')) if body.get('state') == 'observed' else None
            if identity != self.binding_identity:
                self.binding_counter += 1
                self.binding_token = format(self.binding_counter, '048x')
                self.binding_identity = identity
            bundle = {'合成青禾': 'fixture-0', '合成云舟': 'fixture-1'}.get(body.get('title'))
            response = {'state': 'unavailable', 'reason': '当前会话暂不可识别',
                        'observation_seq': body['seq'], 'account_verified': False}
            if identity and bundle:
                response.update(state='suggested', reason='仅名称精确匹配，请核对当前微信账号和联系人',
                                bundle_id=bundle, binding_token=self.binding_token)
            elif identity and body.get('title') == '同名合成':
                response.update(state='ambiguous', reason='存在同名归档，请手动选择')
            if self.hold_binding:
                self.pending_binding = (route, response)
            else:
                self.reply(route, response)
        elif path.startswith('/api/copilot/live/'):
            action = path.rsplit('/', 1)[1]
            response = {'status': 'ok', 'session_id': 'f' * 48, 'state': 'active',
                        'reason': 'LIVE_WAITING', 'binding_revision': body.get('binding_revision', 0),
                        'remaining_seconds': 900, 'calls_used': 0, 'calls_remaining': 6, 'result': None}
            if action == 'preview':
                response = {'status': 'ok', 'preview_id': 'e' * 48,
                    'binding_revision': body['binding_revision'], 'scope': body,
                    'binding_required': True, 'account_verified': False,
                    'recipient': {'configured': True, 'provider': 'openai', 'model': 'synthetic-model',
                                  'endpoint': 'https://api.openai.com/v1/chat/completions'},
                    'counts': {'sample_messages': 12, 'sample_chars': 1234},
                    'limits': {'duration_seconds': 900, 'max_calls': 6, 'min_call_interval_seconds': 20,
                               'debounce_seconds': 3, 'max_context_messages': 200, 'max_context_chars': 20000,
                               'max_batch_messages': 20, 'max_batch_chars': 4000}}
            elif action == 'stop':
                response.update(state='stopped', reason='LIVE_STOPPED')
            elif action == 'start' and self.hold_live_start:
                self.pending_live_start = (route, response)
                return
            elif action == 'tick':
                response['result'] = self.live_tick_result
                if self.live_tick_reason:
                    response.update(state='stopped', reason=self.live_tick_reason, result=None)
                if self.hold_live_tick:
                    self.pending_live_tick = (route, response)
                    return
                if self.fail_live_tick:
                    self.reply(route, {'status': 'error', 'error': 'private-reader-detail'}, 502)
                    return
            self.reply(route, response)
        elif path == '/api/copilot/preview':
            response = {'status': 'ok', 'preview_id': 'a' * 48, 'binding_revision': body['binding_revision'],
                'binding_required': 'binding_token' in body, 'account_verified': False,
                'scope': body, 'recipient': {'configured': True, 'provider': 'openai', 'model': 'synthetic-model',
                'endpoint': 'https://api.openai.com/v1/chat/completions'},
                'counts': {'scope_messages': 123, 'eligible_messages': 123, 'sample_messages': 12,
                           'sample_chars': 1234, 'excluded_nontext': 0, 'truncated_messages': 1,
                           'sample_date_from': '2026-08-01', 'sample_date_to': '2026-09-20',
                           'omitted_messages': 111,
                           'draft_chars': len(body.get('latest_draft', ''))},
                'privacy_notices': ['自动脱敏无法保证自由文本完全匿名'], 'max_calls': 1}
            if self.hold_preview:
                self.pending_preview = (route, response)
            else:
                self.reply(route, response)
        elif path == '/api/copilot/run':
            response = {'status': 'ok', 'binding_revision': body['binding_revision'],
                'replies': [{'style': style, 'text': '合成候选 ' + XSS, 'reason': '合成说明'}
                            for style in ('natural', 'warm', 'invite')],
                'topics': [{'title': '合成话题', 'text': '合成话题内容'}], 'caveats': ['仅供你选择']}
            if self.hold_run:
                self.pending_run = (route, response)
            elif self.fail_run:
                self.reply(route, {'status': 'error', 'error': 'private-path ' + XSS}, 502)
            else:
                self.reply(route, response)
        else:
            self.errors.append('Unexpected endpoint: ' + path)
            self.reply(route, {'status': 'error'}, 404)

    def open(self, native=False):
        if native:
            self.page.add_init_script("""
                window.fixtureState = {state:'bound',target:'window-one',paused:false,mode:'dock',size:'expanded',effectiveSize:'expanded'};
                window.bridgeCalls=[]; window.bridgeListener=null;
                window.emitNative=(value)=>{window.fixtureState=value;window.bridgeListener?.(value);};
                window.copilotDesktop={state:async()=>window.fixtureState,
                  onState:(callback)=>{window.bridgeListener=callback;return ()=>{window.bridgeListener=null;}},
                  setMode:(value)=>window.bridgeCalls.push(['mode',value]),
                  setSize:(value)=>window.bridgeCalls.push(['size',value]),
                  pause:(value)=>window.bridgeCalls.push(['pause',value]),
                  copy:async(value)=>{window.bridgeCalls.push(['copy',value]);if(window.nativeCopyFails)throw new Error('private-copy-error');},
                  close:()=>window.bridgeCalls.push(['close']),edit:(value)=>window.bridgeCalls.push(['edit',value])};
            """)
            if native == 'auto':
                self.page.add_init_script("""
                    const attachRecognition=()=>{
                      if (!window.copilotDesktop) {queueMicrotask(attachRecognition); return;}
                      window.fixtureConversation={state:'unavailable',title:'',target:'window-one',source:'local_ocr',seq:1,reason:'TITLE_CALIBRATION_REQUIRED'};
                      window.conversationListener=null;
                      window.emitConversation=(value)=>{window.fixtureConversation={source:'local_ocr',target:'window-one',...value};window.conversationListener?.(window.fixtureConversation);};
                      Object.assign(window.copilotDesktop,{
                        conversationState:async()=>window.fixtureConversation,
                        onConversation:(callback)=>{window.conversationListener=callback; if(window.earlyConversation)window.emitConversation(window.earlyConversation); return ()=>{window.conversationListener=null;}},
                        setRecognition:(value)=>window.bridgeCalls.push(['recognition',value]),
                        calibrateTitle:()=>window.bridgeCalls.push(['calibrate']),
                        autoCalibrateTitle:async()=>{window.bridgeCalls.push(['autoCalibrate']);return {state:'unavailable',reason:window.calibrationReason||'TITLE_CALIBRATION_SAVED'};},
                        refreshConversation:async()=>{window.bridgeCalls.push(['refreshConversation']);const value={...window.fixtureConversation,...window.nextRefresh,seq:window.fixtureConversation.seq+1};window.nextRefresh=null;window.emitConversation(value);return value;}
                      });
                    };attachRecognition();
                """)
        self.page.goto(self.url + 'copilot/index.html', wait_until='networkidle')
        self.expect(self.page.locator('#contact-select option')).to_have_count(4)

    def select(self, contact='fixture-0'):
        self.page.locator('#context-settings').evaluate('(node)=>node.open=true')
        self.page.locator('#contact-select').select_option(contact)

    def preview(self):
        self.select()
        self.page.locator('#prepare-preview').click()
        self.expect(self.page.locator('#consent-panel')).to_be_visible()

    def generate(self):
        self.preview()
        self.page.locator('#confirm-run').click()
        self.expect(self.page.locator('.reply-card')).to_have_count(3)

    def test_initial_state_and_preview_never_call_cloud(self):
        self.open()
        self.expect(self.page.locator('#native-status')).to_contain_text('浏览器模式')
        self.assertEqual(self.writes, [])
        self.preview()
        self.assertEqual([path for path, _ in self.writes], ['/api/copilot/preview'])
        self.expect(self.page.locator('#consent-panel')).to_contain_text('synthetic-model')
        self.expect(self.page.locator('#consent-panel')).to_contain_text('12')
        self.expect(self.page.locator('#date-from')).to_have_value('2026-03-02')
        self.expect(self.page.locator('#date-to')).to_have_value('2026-09-20')
        self.assertEqual(self.page.locator('input[type=checkbox]:checked').count(), 0)

    def test_desktop_launch_visible_without_modules_and_keyboard_accessible(self):
        self.enabled = self.analysis_enabled = False
        self.page.set_viewport_size({'width': 320, 'height': 700})
        self.open()
        self.expect(self.page.locator('#desktop-launch')).to_have_text('启动悬浮助手')
        self.assertEqual(self.writes, [])
        self.assertLess(self.page.locator('#desktop-launch').bounding_box()['y'], 360)
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
        self.page.locator('#desktop-launch').focus()
        self.page.keyboard.press('Enter')
        self.expect(self.page.locator('#desktop-launch-status')).to_contain_text('已请求打开')
        self.assertEqual(self.writes, [('/api/copilot/desktop/launch', {})])

    def test_desktop_running_opens_existing_and_pending_disables_repeats(self):
        self.desktop.update(state='running', code='DESKTOP_RUNNING')
        self.hold_desktop = True
        self.open()
        self.expect(self.page.locator('#desktop-launch')).to_have_text('打开悬浮助手')
        self.page.locator('#desktop-launch').click()
        self.expect(self.page.locator('#desktop-launch')).to_be_disabled()
        self.page.locator('#desktop-launch').evaluate('(button)=>button.click()')
        self.assertEqual(self.writes, [('/api/copilot/desktop/launch', {})])
        self.reply(*self.pending_desktop, 202)
        self.expect(self.page.locator('#desktop-launch-status')).to_contain_text('跟随微信')

    def test_desktop_failure_is_safe_and_never_automatically_retried(self):
        self.fail_desktop = True
        self.open()
        self.page.locator('#desktop-launch').click()
        self.expect(self.page.locator('#desktop-launch-status')).to_contain_text('不会自动重试')
        self.expect(self.page.locator('body')).not_to_contain_text('private-launch-detail')
        self.assertEqual(self.writes, [('/api/copilot/desktop/launch', {})])

    def test_desktop_missing_runtime_disables_launch_without_installing(self):
        self.desktop.update(installed=False, state='unavailable', code='DESKTOP_NOT_INSTALLED')
        self.open()
        self.expect(self.page.locator('#desktop-launch')).to_be_disabled()
        self.expect(self.page.locator('#desktop-launch-status')).to_contain_text('未安装')
        self.assertEqual(self.writes, [])

    def test_desktop_unsupported_or_unknown_state_is_not_claimed_running(self):
        self.desktop.update(supported=False, installed=False, state='unavailable', code='DESKTOP_UNSUPPORTED')
        self.open()
        self.expect(self.page.locator('#desktop-launch-status')).to_contain_text('Windows')
        self.expect(self.page.locator('#desktop-launch')).to_be_disabled()
        self.desktop.update(supported=True, installed=True, state='unknown', code='DESKTOP_PROBE_TIMEOUT')
        self.page.reload(wait_until='networkidle')
        self.expect(self.page.locator('#desktop-launch-status')).to_contain_text('无法确认')
        self.expect(self.page.locator('#desktop-launch')).not_to_have_text('打开悬浮助手')
        self.assertEqual(self.writes, [])

    def test_native_hides_launch_card_and_explicit_presentation_can_resume_follow(self):
        self.open(native=True)
        self.expect(self.page.locator('#desktop-launch-card')).to_be_hidden()
        self.assertFalse(any('/api/copilot/desktop' in url for url in self.requests))
        self.page.evaluate("emitNative({state:'waiting',target:null,paused:true,presentation:true,mode:'dock',size:'expanded'})")
        self.expect(self.page.locator('#native-status')).to_have_text('设置窗口 · 跟随已暂停')
        self.expect(self.page.locator('#pause-follow')).to_have_text('跟随微信')
        self.expect(self.page.locator('#prepare-preview')).to_be_disabled()
        self.page.locator('#pause-follow').click()
        self.assertIn(['pause', False], self.page.evaluate('window.bridgeCalls'))
        self.assertEqual(self.writes, [])

    def live_preview(self):
        self.open(native='auto')
        self.page.evaluate("emitConversation({state:'observed',title:'合成青禾',seq:2})")
        self.expect(self.page.locator('#live-prepare')).to_be_enabled()
        self.page.locator('#live-prepare').click()
        self.expect(self.page.locator('#live-consent')).to_be_visible()

    def live_start(self):
        self.live_preview()
        self.page.locator('#live-confirmed').check()
        self.page.locator('#live-start').click()
        self.expect(self.page.locator('#live-status')).to_contain_text('等待新消息')

    def test_live_off_by_default_and_browser_cannot_start(self):
        self.open()
        self.select()
        self.expect(self.page.locator('#live-prepare')).to_be_disabled()
        self.assertFalse(any('/live/' in path for path, _ in self.writes))

    def test_live_preview_is_explicit_pinned_scope_with_unchecked_consent(self):
        self.live_preview()
        self.expect(self.page.locator('#live-start')).to_be_disabled()
        self.expect(self.page.locator('#live-consent')).to_contain_text('15 分钟')
        self.expect(self.page.locator('#live-consent')).to_contain_text('6 次')
        self.expect(self.page.locator('#live-consent')).to_contain_text('synthetic-model')
        self.expect(self.page.locator('#live-consent')).to_contain_text('不会自动换人')
        self.assertEqual([path for path, _ in self.writes if '/live/' in path], ['/api/copilot/live/preview'])

    def test_live_native_switch_stops_and_never_restarts(self):
        self.live_start()
        self.page.evaluate("emitConversation({state:'observed',title:'合成云舟',seq:50})")
        self.expect(self.page.locator('#live-start')).to_be_disabled()
        self.expect(self.page.locator('#live-stop')).to_be_hidden()
        self.page.wait_for_timeout(150)
        self.assertEqual(len([path for path, _ in self.writes if path.endswith('/live/stop')]), 1)
        self.assertEqual(len([path for path, _ in self.writes if path.endswith('/live/start')]), 1)

    def test_live_late_start_is_revoked_after_scope_change(self):
        self.hold_live_start = True
        self.live_preview()
        self.page.locator('#live-confirmed').check()
        self.page.locator('#live-start').click()
        self.page.wait_for_timeout(100)
        self.page.evaluate("emitConversation({state:'unavailable',title:'',seq:50,reason:'TITLE_PAUSED'})")
        route, response = self.pending_live_start
        self.reply(route, response)
        self.page.wait_for_timeout(100)
        self.assertTrue(any(path.endswith('/live/stop') for path, _ in self.writes))
        self.expect(self.page.locator('#live-stop')).to_be_hidden()

    def test_live_tick_result_uses_existing_cards_and_compact_stop(self):
        self.live_tick_result = {'result_id': 'synthetic-live-result',
            'replies': [{'style': style, 'text': '合成自动建议 ' + XSS, 'reason': '合成说明'}
                        for style in ('natural', 'warm', 'invite')], 'topics': [], 'caveats': []}
        self.live_start()
        self.expect(self.page.locator('.reply-card')).to_have_count(3, timeout=9000)
        self.assertEqual(self.page.locator('.reply-card img').count(), 0)
        self.page.evaluate("emitNative({state:'bound',target:'window-one',paused:false,mode:'dock',size:'compact',effectiveSize:'compact'})")
        self.expect(self.page.locator('#live-compact-stop')).to_be_visible()
        self.page.locator('#live-compact-stop').click()
        self.expect(self.page.locator('#compact-suggestion')).to_be_hidden()
        self.assertEqual(self.page.evaluate('localStorage.length'), 0)

    def test_live_tick_failure_does_not_retry_or_echo_reader_error(self):
        self.fail_live_tick = True
        self.live_start()
        self.expect(self.page.locator('#live-status')).to_contain_text('不会自动重试', timeout=9000)
        self.assertNotIn('private-reader-detail', self.page.locator('body').inner_text())
        self.page.wait_for_timeout(5300)
        self.assertEqual(len([path for path, _ in self.writes if path.endswith('/live/tick')]), 1)

    def test_live_late_tick_result_does_not_survive_stop(self):
        self.hold_live_tick = True
        self.live_tick_result = {'result_id': 'late', 'replies': [
            {'style': style, 'text': '不应出现', 'reason': ''} for style in ('natural', 'warm', 'invite')],
            'topics': [], 'caveats': []}
        self.live_start()
        self.page.wait_for_timeout(5400)
        self.page.locator('#live-stop').click()
        route, response = self.pending_live_tick
        self.reply(route, response)
        self.page.wait_for_timeout(100)
        self.expect(self.page.locator('.reply-card')).to_have_count(0)

    def test_live_stopped_provider_failure_discloses_possible_charge(self):
        self.live_tick_reason = 'CALL_FAILED_POSSIBLY_CHARGED'
        self.live_start()
        self.expect(self.page.locator('#live-status')).to_contain_text('可能已计费', timeout=9000)
        self.expect(self.page.locator('#live-status')).to_contain_text('不会自动重试')
        self.expect(self.page.locator('#live-stop')).to_be_hidden()

    def test_live_stopped_expiry_is_not_mislabelled_as_safety_failure(self):
        self.live_tick_reason = 'SESSION_EXPIRED'
        self.live_start()
        self.expect(self.page.locator('#live-status')).to_contain_text('授权已到期', timeout=9000)
        self.assertNotIn('安全检查未通过', self.page.locator('#live-status').inner_text())

    def test_live_future_message_time_has_specific_scope_error(self):
        self.live_tick_reason = 'SOURCE_TIME_OUT_OF_RANGE'
        self.live_start()
        self.expect(self.page.locator('#live-status')).to_contain_text('消息时间超出本次授权范围', timeout=9000)
        self.expect(self.page.locator('#live-stop')).to_be_hidden()

    def test_run_requires_click_then_shows_literal_result_and_topics(self):
        self.open()
        self.generate()
        self.assertEqual(len([path for path, _ in self.writes if path.endswith('/run')]), 1)
        run = self.writes[-1][1]
        self.assertIs(run['consent'], True)
        self.expect(self.page.locator('.reply-card').first).to_contain_text(XSS)
        self.assertEqual(self.page.locator('.reply-card img').count(), 0)
        self.page.get_by_role('tab', name='话题', exact=True).click()
        self.expect(self.page.locator('#panel-topics')).to_contain_text('合成话题内容')
        self.page.get_by_role('tab', name='同城', exact=True).click()
        self.expect(self.page.locator('#panel-city')).to_contain_text('尚未连接活动来源')
        self.assertEqual(self.page.evaluate('localStorage.length'), 0)

    def test_person_change_clears_preview_draft_and_results(self):
        self.open()
        self.generate()
        self.page.locator('#latest-draft').fill('合成手动输入')
        self.select('fixture-1')
        self.expect(self.page.locator('.reply-card')).to_have_count(0)
        self.expect(self.page.locator('#consent-panel')).to_be_hidden()
        self.expect(self.page.locator('#latest-draft')).to_have_value('')
        self.expect(self.page.locator('#confirm-run')).to_be_disabled()

    def test_range_direction_and_draft_changes_invalidate_consent(self):
        self.open()
        self.preview()
        revision = self.writes[-1][1]['binding_revision']
        for selector, value in [('#date-from', '2026-04-01'), ('#direction', 'invite'), ('#latest-draft', '合成输入')]:
            if selector == '#direction':
                self.page.locator(selector).select_option(value)
            else:
                self.page.locator(selector).fill(value)
            self.expect(self.page.locator('#consent-panel')).to_be_hidden()
            self.page.locator('#prepare-preview').click()
            self.expect(self.page.locator('#consent-panel')).to_be_visible()
            self.assertGreater(self.writes[-1][1]['binding_revision'], revision)
            revision = self.writes[-1][1]['binding_revision']
        self.assertFalse(any(path.endswith('/run') for path, _ in self.writes))

    def test_native_identity_change_ignores_late_preview(self):
        self.open(native=True)
        self.hold_preview = True
        self.select()
        self.page.locator('#prepare-preview').click()
        self.page.wait_for_function("document.querySelector('#prepare-preview').disabled")
        self.page.evaluate("emitNative({state:'bound',target:'window-two',paused:false,mode:'dock',size:'expanded',effectiveSize:'expanded'})")
        self.expect(self.page.locator('#contact-select')).to_have_value('')
        route, response = self.pending_preview
        self.reply(route, response)
        self.expect(self.page.locator('#consent-panel')).to_be_hidden()
        self.expect(self.page.locator('#confirm-run')).to_be_disabled()

    def test_late_run_is_hidden_after_new_context_and_singleflight(self):
        self.open()
        self.preview()
        self.hold_run = True
        self.page.locator('#confirm-run').click()
        self.expect(self.page.locator('#confirm-run')).to_be_disabled()
        self.select('fixture-1')
        self.expect(self.page.locator('#prepare-preview')).to_be_disabled()
        route, response = self.pending_run
        self.reply(route, response)
        self.expect(self.page.locator('#prepare-preview')).to_be_enabled()
        self.expect(self.page.locator('.reply-card')).to_have_count(0)
        self.assertEqual(len([path for path, _ in self.writes if path.endswith('/run')]), 1)

    def test_failure_clears_consent_and_does_not_retry_or_expose_error(self):
        self.open()
        self.fail_run = True
        self.preview()
        self.page.locator('#confirm-run').click()
        self.expect(self.page.locator('#feedback')).to_contain_text('不会自动重试')
        self.expect(self.page.locator('#consent-panel')).to_be_hidden()
        self.assertNotIn('private-path', self.page.locator('body').inner_text())
        self.assertEqual(len([path for path, _ in self.writes if path.endswith('/run')]), 1)

    def test_module_must_be_boolean_true_and_explicit_enable(self):
        self.enabled = False
        self.open()
        self.expect(self.page.locator('#enable-copilot')).to_be_visible()
        self.select()
        self.expect(self.page.locator('#prepare-preview')).to_be_disabled()
        self.assertEqual(self.writes, [])
        self.page.locator('#enable-copilot').click()
        self.expect(self.page.locator('#prepare-preview')).to_be_enabled()
        self.assertEqual(self.writes[0], ('/api/modules', {'id': 'copilot', 'enabled': True, 'expected_version': 2}))
        self.assertFalse(any(path.endswith('/run') for path, _ in self.writes))

    def test_unknown_module_or_disabled_analysis_fails_closed(self):
        self.modules_override = [{'id': 'analysis', 'enabled': True, 'version': 0},
                                 {'id': 'copilot', 'enabled': 'true', 'version': 0}]
        self.open()
        self.select()
        self.expect(self.page.locator('#prepare-preview')).to_be_disabled()
        self.modules_override = [{'id': 'analysis', 'enabled': False, 'version': 0},
                                 {'id': 'copilot', 'enabled': True, 'version': 0}]
        self.page.reload(wait_until='networkidle')
        self.select()
        self.expect(self.page.locator('#prepare-preview')).to_be_disabled()
        self.expect(self.page.locator('#module-status')).to_contain_text('AI 分析')

    def test_narrow_layout_keyboard_tabs_and_native_controls(self):
        self.open(native=True)
        self.page.set_viewport_size({'width': 320, 'height': 560})
        self.generate()
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 320)
        self.page.get_by_role('tab', name='回复', exact=True).focus()
        self.page.keyboard.press('ArrowRight')
        self.expect(self.page.get_by_role('tab', name='话题', exact=True)).to_be_focused()
        self.page.locator('#compact-window').click()
        self.assertIn(['size', 'compact'], self.page.evaluate('bridgeCalls'))
        self.page.locator('#pause-follow').click()
        self.assertIn(['pause', True], self.page.evaluate('bridgeCalls'))
        self.page.screenshot(path=str(Path(self.temp.name) / 'narrow.png'), full_page=True)
        if os.environ.get('COPILOT_UI_SCREENSHOTS') == '1':
            self.page.screenshot(path=str(ROOT / 'scripts' / 'tmp' / 'copilot-narrow.png'), full_page=True)
        self.assertEqual(self.page.locator('script[src*=shell]').count(), 0)

    def test_copy_is_explicit_and_native_compact_never_autoexpands(self):
        self.page.add_init_script("""
            window.clipboardWrites=[]; window.clipboardReads=0;
            Object.defineProperty(navigator,'clipboard',{value:{
              writeText:async(value)=>window.clipboardWrites.push(value),
              readText:async()=>{window.clipboardReads++;return 'NEVER READ';}}});
        """)
        self.open(native=True)
        self.generate()
        self.assertEqual(self.page.evaluate('clipboardWrites'), [])
        self.page.get_by_role('button', name='复制自然一点的回复', exact=True).click()
        self.assertEqual(self.page.evaluate('clipboardWrites'), [])
        self.assertIn(['copy', '合成候选 ' + XSS], self.page.evaluate('bridgeCalls'))
        self.assertEqual(self.page.evaluate('clipboardReads'), 0)
        self.page.evaluate("emitNative({state:'bound',target:'window-one',paused:false,mode:'dock',size:'compact',effectiveSize:'compact'})")
        self.expect(self.page.locator('.full-content')).to_be_hidden()
        self.expect(self.page.locator('#expand-window')).to_be_visible()
        self.assertNotIn(['size', 'expanded'], self.page.evaluate('bridgeCalls'))
        self.page.set_viewport_size({'width': 360, 'height': 340})
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollHeight'), 340)
        self.page.locator('#expand-window').click()
        self.assertIn(['size', 'expanded'], self.page.evaluate('bridgeCalls'))
        self.page.evaluate("emitNative({state:'bound',target:'window-one',paused:false,mode:'dock',size:'compact',effectiveSize:'bubble'})")
        self.expect(self.page.locator('#bubble-open')).to_be_visible()
        self.expect(self.page.locator('.assistant')).to_be_hidden()

    def test_status_metadata_is_sufficient_and_missing_dates_cannot_send(self):
        self.status_conversations = [
            {'bundle_id': f'fixture-{i}', 'alias': '', 'contact_display': name,
             'date_from': '2026-03-02' if i != 2 else '',
             'date_to': '2026-09-20' if i != 2 else '', 'message_count': 123}
            for i, name in enumerate(['合成青禾', '合成云舟', XSS])]
        self.open()
        self.select('fixture-2')
        self.expect(self.page.locator('#context-heading')).to_have_text(XSS)
        self.expect(self.page.locator('#prepare-preview')).to_be_disabled()
        self.assertFalse(any(urlparse(url).path == '/api/library' for url in self.requests))
        self.select('fixture-0')
        self.expect(self.page.locator('#prepare-preview')).to_be_enabled()

    def test_native_pause_invalidates_consent_and_blocks_new_generation(self):
        self.open(native=True)
        self.preview()
        self.page.evaluate("emitNative({state:'bound',target:'window-one',paused:true,mode:'dock',size:'expanded',effectiveSize:'expanded'})")
        self.expect(self.page.locator('#consent-panel')).to_be_hidden()
        self.expect(self.page.locator('#prepare-preview')).to_be_disabled()
        self.expect(self.page.locator('#confirm-run')).to_be_disabled()
        self.page.evaluate("emitNative({state:'bound',target:'window-one',paused:false,mode:'dock',size:'expanded',effectiveSize:'expanded'})")
        self.expect(self.page.locator('#prepare-preview')).to_be_enabled()
        self.expect(self.page.locator('#confirm-run')).to_be_disabled()
        self.assertFalse(any(path.endswith('/run') for path, _ in self.writes))

    def test_native_header_is_draggable_but_window_buttons_are_clickable(self):
        self.open(native=True)
        region = "(node)=>getComputedStyle(node).getPropertyValue('-webkit-app-region')"
        self.assertEqual(self.page.locator('.topbar').evaluate(region), 'drag')
        self.assertEqual(self.page.locator('#compact-window').evaluate(region), 'no-drag')
        self.assertEqual(self.page.locator('#bubble-open').evaluate(region), 'no-drag')

    def test_compact_keeps_one_copyable_suggestion_and_bubble_fits_native_size(self):
        self.page.add_init_script("""
            window.clipboardWrites=[];
            Object.defineProperty(navigator,'clipboard',{value:{writeText:async(value)=>window.clipboardWrites.push(value)}});
        """)
        self.open(native=True)
        self.generate()
        self.page.evaluate("emitNative({state:'bound',target:'window-one',paused:false,mode:'dock',size:'compact',effectiveSize:'compact'})")
        self.page.set_viewport_size({'width': 360, 'height': 340})
        self.expect(self.page.locator('#compact-reply')).to_have_text('合成候选 ' + XSS)
        self.page.locator('#compact-copy').click()
        self.assertEqual(self.page.evaluate('clipboardWrites'), [])
        self.assertIn(['copy', '合成候选 ' + XSS], self.page.evaluate('bridgeCalls'))
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollHeight'), 340)
        if os.environ.get('COPILOT_UI_SCREENSHOTS') == '1':
            self.page.screenshot(path=str(ROOT / 'scripts' / 'tmp' / 'copilot-compact.png'))
        self.page.evaluate("emitNative({state:'bound',target:'window-two',paused:false,mode:'dock',size:'compact',effectiveSize:'compact'})")
        self.expect(self.page.locator('#compact-suggestion')).to_be_hidden()
        self.page.evaluate("emitNative({state:'bound',target:'window-two',paused:false,mode:'dock',size:'compact',effectiveSize:'bubble'})")
        self.page.set_viewport_size({'width': 156, 'height': 48})
        box = self.page.locator('#bubble-open').bounding_box()
        self.assertLessEqual(box['x'] + box['width'], 156)
        self.assertLessEqual(box['y'] + box['height'], 48)
        self.assertGreaterEqual(box['width'], 140)
        if os.environ.get('COPILOT_UI_SCREENSHOTS') == '1':
            self.page.screenshot(path=str(ROOT / 'scripts' / 'tmp' / 'copilot-bubble.png'))

    def test_native_copy_failure_stays_local_and_visible_in_compact_mode(self):
        self.page.add_init_script("""
            window.nativeCopyFails=true;window.clipboardWrites=[];
            Object.defineProperty(navigator,'clipboard',{value:{writeText:async(value)=>window.clipboardWrites.push(value)}});
        """)
        self.open(native=True)
        self.generate()
        self.assertEqual([call for call in self.page.evaluate('bridgeCalls') if call[0] == 'copy'], [])
        self.page.get_by_role('button', name='复制自然一点的回复', exact=True).click()
        self.expect(self.page.locator('#feedback')).to_contain_text('无法复制')
        self.assertEqual(self.page.evaluate('clipboardWrites'), [])
        self.assertNotIn('private-copy-error', self.page.locator('body').inner_text())
        self.page.evaluate("emitNative({state:'bound',target:'window-one',paused:false,mode:'dock',size:'compact',effectiveSize:'compact'})")
        self.page.locator('#compact-copy').click()
        self.expect(self.page.locator('#compact-copy')).to_have_text('复制失败')
        self.assertEqual(len([call for call in self.page.evaluate('bridgeCalls') if call[0] == 'copy']), 2)

    def test_browser_copy_uses_clipboard_only_after_explicit_click(self):
        self.page.add_init_script("""
            window.clipboardWrites=[];
            Object.defineProperty(navigator,'clipboard',{value:{writeText:async(value)=>window.clipboardWrites.push(value)}});
        """)
        self.open()
        self.generate()
        self.assertEqual(self.page.evaluate('clipboardWrites'), [])
        self.page.get_by_role('button', name='复制自然一点的回复', exact=True).click()
        self.assertEqual(self.page.evaluate('clipboardWrites'), ['合成候选 ' + XSS])

    def test_preview_distinguishes_actual_sample_dates_and_omissions(self):
        self.open()
        self.preview()
        self.expect(self.page.locator('#preview-facts')).to_contain_text('实际取用日期')
        self.expect(self.page.locator('#preview-facts')).to_contain_text('2026-08-01 至 2026-09-20')
        self.expect(self.page.locator('#preview-facts')).to_contain_text('111 条未取用')
        self.expect(self.page.locator('#preview-facts')).to_contain_text('1 条被截断')
        self.assertFalse(any(path.endswith('/run') for path, _ in self.writes))

    def observe(self, seq, title='合成青禾', state='observed'):
        self.page.evaluate('(value)=>emitConversation(value)', {'seq': seq, 'title': title,
            'state': state, 'reason': 'TITLE_UNAVAILABLE' if state == 'unavailable' else ''})

    def test_native_auto_selects_candidate_without_preview_or_cloud(self):
        self.open(native='auto')
        self.expect(self.page.locator('#context-mode')).to_have_value('auto')
        self.expect(self.page.locator('#recognition-status')).to_contain_text('标题区域')
        self.page.locator('#calibrate-title').click()
        self.assertIn(['calibrate'], self.page.evaluate('bridgeCalls'))
        self.observe(2)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')
        self.expect(self.page.locator('#context-heading')).to_have_text('合成青禾')
        self.expect(self.page.locator('#recognition-status')).to_contain_text('待核对')
        self.expect(self.page.locator('#date-from')).to_have_value('2026-03-02')
        self.assertFalse(any(path.endswith(('/preview', '/run')) for path, _ in self.writes))
        self.assertEqual(self.page.evaluate('localStorage.length'), 0)

    def test_one_click_calibration_works_without_enabling_ai_or_sending(self):
        self.enabled = False
        self.analysis_enabled = False
        self.open(native='auto')
        self.expect(self.page.locator('#auto-calibrate-title')).to_be_visible()
        self.page.locator('#auto-calibrate-title').click()
        self.expect(self.page.locator('#auto-calibration-result')).to_contain_text('已保存')
        self.assertIn(['autoCalibrate'], self.page.evaluate('bridgeCalls'))
        self.assertFalse(any(path.endswith(('/preview', '/run')) or path == '/api/modules' for path, _ in self.writes))
        self.expect(self.page.locator('#binding-confirmed')).not_to_be_checked()
        self.page.locator('#context-mode').select_option('manual')
        self.expect(self.page.locator('#auto-calibrate-title')).to_be_disabled()

    def test_capture_and_dpi_failures_are_specific_without_sending_or_retrying(self):
        self.open(native='auto')
        for reason, message in [('TITLE_CAPTURE_UNAVAILABLE', '标题像素'),
                                ('TITLE_DPI_UNAVAILABLE', '缩放坐标')]:
            self.page.evaluate('(reason)=>window.calibrationReason=reason', reason)
            self.page.locator('#auto-calibrate-title').click()
            self.expect(self.page.locator('#auto-calibration-result')).to_contain_text(message)
            self.expect(self.page.locator('#auto-calibration-result')).to_contain_text('不会自动重试')
        self.assertEqual(len([call for call in self.page.evaluate('bridgeCalls') if call[0] == 'autoCalibrate']), 2)
        self.assertFalse(any(path in {'/api/copilot/preview', '/api/copilot/run',
                                     '/api/copilot/live/preview', '/api/copilot/live/start',
                                     '/api/copilot/live/tick'} for path, _ in self.writes))

    def test_auto_calibration_failure_is_specific_and_browser_has_no_button(self):
        self.open(native='auto')
        self.page.evaluate("window.calibrationReason='TITLE_OCCLUDED'")
        self.page.locator('#auto-calibrate-title').click()
        self.expect(self.page.locator('#auto-calibration-result')).to_contain_text('遮挡')
        self.page.evaluate("emitConversation({state:'unavailable',reason:'TITLE_CALIBRATION_REQUIRED',seq:9,title:''})")
        self.expect(self.page.locator('#auto-calibration-result')).to_contain_text('遮挡')
        self.assertEqual(len([c for c in self.page.evaluate('bridgeCalls') if c[0]=='autoCalibrate']), 1)

    def test_automatic_preview_requires_unchecked_identity_checkbox_and_fresh_read(self):
        self.open(native='auto')
        self.observe(2)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')
        self.page.locator('#prepare-preview').click()
        self.expect(self.page.locator('#consent-panel')).to_be_visible()
        self.expect(self.page.locator('#binding-confirmed')).not_to_be_checked()
        self.expect(self.page.locator('#confirm-run')).to_be_disabled()
        self.page.locator('#binding-confirmed').check()
        self.page.locator('#confirm-run').click()
        self.expect(self.page.locator('.reply-card')).to_have_count(3)
        self.assertEqual(len([call for call in self.page.evaluate('bridgeCalls') if call[0] == 'refreshConversation']), 2)
        preview = next(body for path, body in self.writes if path.endswith('/preview'))
        run = next(body for path, body in self.writes if path.endswith('/run'))
        self.assertIn('binding_token', preview)
        self.assertTrue(run['binding_confirmed'])
        self.assertNotIn('title', preview)
        self.assertNotIn('title', run)

    def test_stable_heartbeat_retains_preview_and_change_clears_all_context(self):
        self.open(native='auto')
        self.observe(2)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')
        self.page.locator('#context-settings').evaluate('(node)=>node.open=true')
        self.page.locator('#latest-draft').fill('合成草稿')
        self.page.locator('#prepare-preview').click()
        self.expect(self.page.locator('#consent-panel')).to_be_visible()
        self.page.locator('#binding-confirmed').check()
        self.observe(4)
        self.expect(self.page.locator('#consent-panel')).to_be_visible()
        self.expect(self.page.locator('#binding-confirmed')).to_be_checked()
        self.observe(5, '合成云舟')
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-1')
        self.expect(self.page.locator('#latest-draft')).to_have_value('')
        self.expect(self.page.locator('#consent-panel')).to_be_hidden()
        self.expect(self.page.locator('#binding-confirmed')).not_to_be_checked()
        self.assertFalse(any(path.endswith('/run') for path, _ in self.writes))

    def test_unknown_ambiguous_and_late_binding_responses_never_select(self):
        self.open(native='auto')
        self.hold_binding = True
        self.observe(2)
        self.page.wait_for_timeout(150)
        self.assertIsNotNone(self.pending_binding)
        self.observe(3, '未知合成')
        self.hold_binding = False
        self.reply(*self.pending_binding)
        self.page.wait_for_timeout(150)
        self.expect(self.page.locator('#contact-select')).to_have_value('')
        self.expect(self.page.locator('#prepare-preview')).to_be_disabled()
        self.observe(4, '同名合成')
        self.expect(self.page.locator('#recognition-status')).to_contain_text('同名')
        self.expect(self.page.locator('#contact-select')).to_have_value('')

    def test_fresh_read_change_prevents_send_and_manual_mode_revokes_binding(self):
        self.open(native='auto')
        self.observe(2)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')
        self.page.locator('#prepare-preview').click()
        self.expect(self.page.locator('#consent-panel')).to_be_visible()
        self.page.locator('#binding-confirmed').check()
        self.page.evaluate("window.nextRefresh={state:'observed',title:'合成云舟'}")
        self.page.locator('#confirm-run').click()
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-1')
        self.expect(self.page.locator('#consent-panel')).to_be_hidden()
        self.assertFalse(any(path.endswith('/run') for path, _ in self.writes))
        self.page.locator('#context-mode').select_option('manual')
        self.expect(self.page.locator('#contact-select')).to_have_value('')
        self.assertIn(['recognition', False], self.page.evaluate('bridgeCalls'))
        self.select()
        self.expect(self.page.locator('#prepare-preview')).to_be_enabled()
        self.page.wait_for_timeout(100)
        self.assertEqual([body for path, body in self.writes if path.endswith('/binding')][-1]['state'], 'unavailable')

    def test_observation_before_status_refresh_is_applied_after_metadata_load(self):
        self.page.add_init_script("window.earlyConversation={state:'observed',title:'合成青禾',seq:2};")
        self.open(native='auto')
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')

    def test_late_model_result_after_conversation_change_is_discarded(self):
        self.open(native='auto')
        self.observe(2)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')
        self.page.locator('#prepare-preview').click()
        self.expect(self.page.locator('#consent-panel')).to_be_visible()
        self.page.locator('#binding-confirmed').check()
        self.hold_run = True
        self.page.locator('#confirm-run').click()
        self.page.wait_for_timeout(150)
        self.assertIsNotNone(self.pending_run)
        self.observe(5, '', state='unavailable')
        self.reply(*self.pending_run)
        self.expect(self.page.locator('#contact-select')).to_have_value('')
        self.expect(self.page.locator('.reply-card')).to_have_count(0)

    def test_window_switch_does_not_replay_old_title_when_status_is_refreshed(self):
        self.open(native='auto')
        self.observe(2)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')
        self.page.evaluate("emitNative({state:'bound',target:'window-two',paused:false,mode:'dock',size:'expanded',effectiveSize:'expanded'})")
        self.expect(self.page.locator('#contact-select')).to_have_value('')
        self.page.locator('#refresh-status').click()
        self.page.wait_for_timeout(200)
        self.expect(self.page.locator('#contact-select')).to_have_value('')

    def test_pending_stable_heartbeats_coalesce_to_latest_observation(self):
        self.open(native='auto')
        self.hold_binding = True
        self.observe(2)
        self.page.wait_for_timeout(100)
        self.assertIsNotNone(self.pending_binding)
        for seq in range(3, 8):
            self.observe(seq)
        self.hold_binding = False
        self.reply(*self.pending_binding)
        self.page.wait_for_timeout(200)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')
        self.assertLessEqual(len([body for path, body in self.writes if path.endswith('/binding')]), 3)

    def test_expired_cached_title_cannot_be_reposted_by_status_refresh(self):
        # Advance monotonic time without waiting for or relying on a timer callback.
        self.page.add_init_script("""
            window.syntheticMonotonic=0;
            Object.defineProperty(performance,'now',{value:()=>window.syntheticMonotonic});
        """)
        self.open(native='auto')
        self.observe(2)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')
        observed_before = len([body for path, body in self.writes
                               if path.endswith('/binding') and body['state'] == 'observed'])
        self.page.evaluate('window.syntheticMonotonic=9500')
        self.page.locator('#refresh-status').click()
        self.expect(self.page.locator('#recognition-status')).to_contain_text('过期')
        self.expect(self.page.locator('#contact-select')).to_have_value('')
        self.expect(self.page.locator('#prepare-preview')).to_be_disabled()
        self.assertEqual(len([body for path, body in self.writes
                              if path.endswith('/binding') and body['state'] == 'observed']), observed_before)
        # A genuinely new observation may establish a new candidate after expiry.
        self.observe(3)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')

    def test_binding_response_cannot_extend_an_already_expired_observation(self):
        self.page.add_init_script("""
            window.syntheticMonotonic=0;
            Object.defineProperty(performance,'now',{value:()=>window.syntheticMonotonic});
        """)
        self.open(native='auto')
        self.hold_binding = True
        self.observe(2)
        self.page.wait_for_timeout(100)
        self.assertIsNotNone(self.pending_binding)
        self.page.evaluate('window.syntheticMonotonic=9500')
        self.hold_binding = False
        self.reply(*self.pending_binding)
        self.expect(self.page.locator('#recognition-status')).to_contain_text('过期')
        self.expect(self.page.locator('#contact-select')).to_have_value('')
        self.expect(self.page.locator('#prepare-preview')).to_be_disabled()

    def test_observation_expiring_in_queue_is_sent_only_as_unavailable(self):
        self.page.add_init_script("""
            window.syntheticMonotonic=0;
            Object.defineProperty(performance,'now',{value:()=>window.syntheticMonotonic});
        """)
        self.open(native='auto')
        self.hold_binding = True
        self.observe(2)
        self.page.wait_for_timeout(100)
        self.assertIsNotNone(self.pending_binding)
        self.observe(3, '合成云舟')
        self.page.evaluate('window.syntheticMonotonic=9500')
        self.hold_binding = False
        self.reply(*self.pending_binding)
        self.expect(self.page.locator('#recognition-status')).to_contain_text('过期')
        self.expect(self.page.locator('#contact-select')).to_have_value('')
        self.assertFalse(any(body.get('title') == '合成云舟' for path, body in self.writes
                             if path.endswith('/binding')))

    def test_new_observation_reconnects_after_server_binding_session_is_lost(self):
        self.open(native='auto')
        self.observe(2)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')
        previous_session = self.binding_session
        # A service restart loses all volatile binding sessions.
        self.binding_session = None
        self.observe(3)
        self.expect(self.page.locator('#contact-select')).to_have_value('')
        self.expect(self.page.locator('#recognition-status')).to_contain_text('连接暂不可用')
        self.observe(4)
        self.expect(self.page.locator('#contact-select')).to_have_value('fixture-0')
        self.assertNotEqual(previous_session, self.binding_session)
        self.assertEqual(self.binding_session_counter, 2)
        latest = [body for path, body in self.writes if path.endswith('/binding')][-1]
        self.assertEqual(latest['seq'], 1)
        self.assertFalse(any(path.endswith(('/preview', '/run')) for path, _ in self.writes))
