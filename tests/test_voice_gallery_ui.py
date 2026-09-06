"""Real browser checks of the voice gallery, using only invented local data."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock
import wave

from dashboard import app

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from voice_gallery import write_gallery

ARTIFACTS = ROOT / 'scripts/tmp'
UI_ENABLED = os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1'
XSS = '</script><img src=x onerror=alert(1)>'


@unittest.skipUnless(UI_ENABLED, 'Opt-in: set SHE_LOVE_ME_UI_TESTS=1 for synthetic browser tests')
class VoiceGalleryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='voice-gallery-ui-', dir=ARTIFACTS)
        cls.repo = Path(cls.temp.name)
        cls.data = cls.repo / 'data'
        for identifier, name in (('synthetic-a', '合成青禾'), ('synthetic-b', '合成云舟')):
            path = cls.data / 'contacts' / identifier / 'messages.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'contact_display': name, 'messages': []}), 'utf-8')
        catalog, results = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
        catalog.row_factory = results.row_factory = sqlite3.Row
        catalog.executescript('CREATE TABLE voice_messages(id TEXT,bundle_id TEXT,timestamp REAL,sha TEXT,state TEXT); CREATE TABLE assets(sha TEXT);')
        results.execute('CREATE TABLE transcripts(sha TEXT,state TEXT,text TEXT,duration REAL)')
        cls.rows = []
        for index in range(45):
            sha = hashlib.sha256(f'synthetic-{index}'.encode()).hexdigest()
            day = datetime(2026, 8, 1, 12) + timedelta(days=index)
            content = f'合成转写 {index:02d}' + (' 排期' if index in (6, 7) else '') + (XSS if index == 44 else '')
            state = 'pending' if index == 3 else 'done'
            duration = (index * 17) % 45 + 1
            cls.rows.append({'index': index, 'sha': sha, 'date': day, 'duration': duration})
            catalog.execute('INSERT INTO voice_messages VALUES(?,?,?,?,?)',
                            (str(index), 'synthetic-a' if index % 2 else 'synthetic-b', day.timestamp(), sha, 'archived'))
            catalog.execute('INSERT INTO assets VALUES(?)', (sha,))
            if state == 'done':
                results.execute('INSERT INTO transcripts VALUES(?,?,?,?)', (sha, state, content, duration))
        cls.audio_sha = cls.rows[-1]['sha']
        audio = cls.data / 'exports/wechat-voice/audio' / (cls.audio_sha + '.wav')
        audio.parent.mkdir(parents=True)
        with wave.open(str(audio), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b'\x00\x00' * 16000 * 3)
        write_gallery(cls.repo, catalog, results, {'transcribed_audio': 44, 'playable_audio': 1})
        catalog.close()
        results.close()
        cls.patches = [mock.patch.object(app, 'DATA_DIR', cls.data),
                       mock.patch.object(app, 'CONTACTS_DIR', cls.data / 'contacts'),
                       mock.patch.object(app, 'SYNC_STATUS_PATH', cls.data / 'sync-status.json'),
                       mock.patch.object(app, 'exporter_status', return_value={'ready': True})]
        for patch in cls.patches:
            patch.start()
        cls.server = app.create_server(port=0, token='synthetic-gallery-local-token')
        assert cls.server.server_port != 8765
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
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
        for patch in reversed(cls.patches):
            patch.stop()
        cls.temp.cleanup()

    def setUp(self):
        from playwright.sync_api import expect

        self.expect = expect
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1024}, reduced_motion='reduce')
        self.addCleanup(self.context.close)
        self.context.request.get(self.url + '/')  # Issue this synthetic server's session cookie.
        self.page = self.context.new_page()
        self.errors, self.dialogs, self.requests = [], [], []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('console', lambda message: self.errors.append(message.text) if message.type == 'error' else None)
        self.page.on('dialog', lambda dialog: (self.dialogs.append(dialog.message), dialog.dismiss()))
        self.page.on('request', lambda request: self.requests.append((request.url, request.headers)))
        self.page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.url + '/') else route.abort())
        self.page.goto(self.url + '/exports/wechat-voice/index.html', wait_until='networkidle')
        self.expect(self.page.locator('.voice-card')).to_have_count(40)

    def tearDown(self):
        self.assertEqual(self.errors, [], 'No script, JSON or media CSP failures')
        self.assertEqual(self.dialogs, [], 'Untrusted transcript text must not execute')
        self.assertTrue(all(url.startswith(self.url + '/') for url, _ in self.requests))

    def test_filters_sorting_and_pagination(self):
        self.expect(self.page.locator('#voice-count')).to_have_text('共 45 条匹配记录')
        self.expect(self.page.locator('#voice-page')).to_have_text('1 / 2')
        self.expect(self.page.locator('.transcript').first).to_contain_text('合成转写 44')
        self.page.locator('#voice-next').click()
        self.expect(self.page.locator('.voice-card')).to_have_count(5)
        self.expect(self.page.locator('#voice-page')).to_have_text('2 / 2')
        self.page.locator('#voice-query').fill('排期')
        self.expect(self.page.locator('.voice-card')).to_have_count(2)
        self.expect(self.page.locator('#voice-page')).to_have_text('1 / 1')
        self.expect(self.page.locator('.transcript').first).to_have_text('合成转写 07 排期')
        self.page.locator('#voice-sort').select_option('oldest')
        self.expect(self.page.locator('.transcript').first).to_have_text('合成转写 06 排期')
        self.page.locator('#voice-query').fill('')
        self.page.locator('#voice-from').fill('2026-08-07')
        self.page.locator('#voice-to').fill('2026-08-08')
        self.expect(self.page.locator('.voice-card')).to_have_count(2)
        self.page.locator('#voice-from').fill('')
        self.page.locator('#voice-to').fill('')
        self.page.locator('#voice-state').select_option('pending')
        self.expect(self.page.locator('.voice-card')).to_have_count(1)
        self.expect(self.page.locator('.badge')).to_have_text('待转写')
        self.page.locator('#voice-state').select_option('')
        self.page.locator('#voice-sort').select_option('longest')
        longest = max(self.rows, key=lambda item: item['duration'])
        self.expect(self.page.locator('.transcript').first).to_contain_text(f"合成转写 {longest['index']:02d}")
        self.page.locator('#voice-sort').select_option('contact')
        names = self.page.locator('.card-heading h2').all_text_contents()
        self.assertEqual(set(names), {'合成青禾', '合成云舟'})
        transitions = sum(names[index] != names[index - 1] for index in range(1, len(names)))
        self.assertEqual(transitions, 1)
        self.page.locator('#voice-query').fill('不存在的合成关键词')
        self.expect(self.page.locator('.voice-card')).to_have_count(0)
        self.expect(self.page.locator('#voice-list .empty')).to_be_visible()

    def test_voice_page_uses_current_project_identity(self):
        self.expect(self.page).to_have_title('语音档案 · 人类线上关系可视化')
        # Existing private HTML is not regenerated just for branding. The shared
        # script also updates the title when a legacy export is opened.
        self.page.evaluate("document.title = '语音档案 · 她不一样'")
        self.page.add_script_tag(url=self.url + '/voice-gallery.js')
        self.expect(self.page).to_have_title('语音档案 · 人类线上关系可视化')

    def test_csp_xss_and_audio_are_local_and_lazy_loaded(self):
        self.expect(self.page.locator('.transcript').first).to_contain_text(XSS)
        self.expect(self.page.locator('#voice-list img, #voice-list script')).to_have_count(0)
        self.expect(self.page.locator('audio')).to_have_attribute('preload', 'none')
        self.assertFalse(any(url.endswith('.wav') for url, _ in self.requests))
        self.page.locator('audio').evaluate('audio => audio.play()')
        self.page.wait_for_function('() => document.querySelector("audio").currentTime > 0')
        self.assertTrue(any(url.endswith('.wav') and headers.get('range') for url, headers in self.requests))
        self.page.locator('audio').evaluate('audio => { audio.pause(); audio.currentTime = 1; }')
        self.page.wait_for_function('() => Math.abs(document.querySelector("audio").currentTime - 1) < 0.1')
        self.page.screenshot(path=str(ARTIFACTS / 'voice-gallery-desktop.png'), full_page=False)

    def test_mobile_has_no_document_overflow(self):
        self.page.set_viewport_size({'width': 375, 'height': 812})
        self.page.locator('#voice-query').fill('合成转写 44')
        self.expect(self.page.locator('.voice-card')).to_have_count(1)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 375)
        self.assertLessEqual(self.page.locator('audio').bounding_box()['width'], 305)
        self.page.screenshot(path=str(ARTIFACTS / 'voice-gallery-mobile.png'), full_page=True)

    def test_voice_controls_and_secondary_copy_have_comfortable_minimum_sizes(self):
        controls = self.page.locator('nav a, .filters input, .filters select, .pager button, audio').evaluate_all('''nodes => nodes.map(node => {
            const box = node.getBoundingClientRect();
            return {name: node.id || node.tagName, width: box.width, height: box.height};
        })''')
        self.assertTrue(controls)
        for control in controls:
            with self.subTest(control=control['name']):
                self.assertGreaterEqual(control['height'], 44)
                self.assertGreaterEqual(control['width'], 44)
        captions = self.page.locator('.eyebrow, .metrics span, label span, .list-heading p, .meta, .badge, .muted, .pager, footer').evaluate_all(
            'nodes => nodes.map(node => ({name: node.className || node.tagName, size: parseFloat(getComputedStyle(node).fontSize)}))')
        for caption in captions:
            with self.subTest(caption=caption['name']):
                self.assertGreaterEqual(caption['size'], 13)

    def test_voice_uses_shared_readable_light_and_dark_surfaces(self):
        themes = {}
        for theme in ('light', 'dark'):
            self.page.emulate_media(color_scheme=theme)
            style = self.page.evaluate('''() => {
                const rgb = value => value.match(/[\\d.]+/g).slice(0, 3).map(Number);
                const luminance = value => rgb(value).map(n => {
                    n /= 255; return n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4;
                }).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
                const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
                const root = getComputedStyle(document.documentElement);
                const body = getComputedStyle(document.body);
                const card = getComputedStyle(document.querySelector('.voice-card'));
                const meta = getComputedStyle(document.querySelector('.meta'));
                return {pageToken: root.getPropertyValue('--surface-page').trim(), colorScheme: root.colorScheme,
                    page: body.backgroundColor, card: card.backgroundColor, foreground: body.color,
                    bodyContrast: contrast(body.color, body.backgroundColor), metaContrast: contrast(meta.color, card.backgroundColor)};
            }''')
            with self.subTest(theme=theme):
                self.assertTrue(style['pageToken'], 'Voice page must reuse the shared theme tokens')
                self.assertIn('light', style['colorScheme'])
                self.assertIn('dark', style['colorScheme'])
                self.assertGreaterEqual(style['bodyContrast'], 4.5)
                self.assertGreaterEqual(style['metaContrast'], 4.5)
            themes[theme] = style
        self.assertNotEqual(themes['light']['page'], themes['dark']['page'])
        self.assertNotEqual(themes['light']['card'], themes['dark']['card'])
        self.page.screenshot(path=str(ARTIFACTS / 'voice-gallery-apple-desktop-dark.png'), full_page=False)

    def test_voice_phone_inputs_fit_320_pixels_in_both_themes(self):
        self.page.set_viewport_size({'width': 320, 'height': 812})
        self.page.locator('#voice-query').fill('合成转写 44')
        self.expect(self.page.locator('.voice-card')).to_have_count(1)
        for theme in ('light', 'dark'):
            self.page.emulate_media(color_scheme=theme)
            with self.subTest(theme=theme):
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 320)
                fields = self.page.locator('.filters input, .filters select').evaluate_all('''nodes => nodes.map(node => {
                    const box = node.getBoundingClientRect();
                    return {name: node.id, size: parseFloat(getComputedStyle(node).fontSize), left: box.left, right: box.right};
                })''')
                for field in fields:
                    self.assertGreaterEqual(field['size'], 16, field)
                    self.assertGreaterEqual(field['left'], 0, field)
                    self.assertLessEqual(field['right'], 320, field)
                self.page.screenshot(path=str(ARTIFACTS / f'voice-gallery-apple-mobile-{theme}.png'), full_page=True)


if __name__ == '__main__':
    unittest.main()
