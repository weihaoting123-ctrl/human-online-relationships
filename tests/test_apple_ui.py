"""Opt-in visual accessibility checks against synthetic static UI fixtures only.

No production server, contacts, browser profile, keys, or cloud request is used.
The tests reuse the fixture's ephemeral static server and API responses without
inheriting its test cases. UI behavior and computed styles remain the real app.
"""
from __future__ import annotations

import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import test_library_ui as fixtures


STYLE = '''node => {
  const style = getComputedStyle(node), bounds = node.getBoundingClientRect();
  const backgrounds = [];
  for (let parent = node; parent; parent = parent.parentElement) backgrounds.push(getComputedStyle(parent).backgroundColor);
  return {color: style.color, background: style.backgroundColor, backgrounds,
    font: parseFloat(style.fontSize), height: bounds.height, width: bounds.width,
    border: style.borderColor, borderWidth: parseFloat(style.borderWidth),
    outline: style.outlineStyle, outlineWidth: parseFloat(style.outlineWidth), outlineColor: style.outlineColor,
    animation: style.animationName, duration: style.animationDuration, transition: style.transitionDuration};
}'''


def rgba(value):
    numbers = [float(part) for part in re.findall(r'[\d.]+', value)]
    return tuple(numbers[:3]) + (numbers[3] if len(numbers) > 3 else 1.0,)


def luminance(color):
    channels = [value / 255 for value in color[:3]]
    channels = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
    return sum(value * weight for value, weight in zip(channels, (.2126, .7152, .0722)))


def contrast(first, second):
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + .05) / (dark + .05)


def background(style):
    result = (255, 255, 255)
    for value in reversed(style['backgrounds']):
        red, green, blue, alpha = rgba(value)
        result = tuple(channel * alpha + old * (1 - alpha) for channel, old in zip((red, green, blue), result))
    return result


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1', 'Opt-in synthetic browser tests')
class AppleUIBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.LibraryBrowserTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.LibraryBrowserTests.tearDownClass()

    def setUp(self):
        self.fixture = fixtures.LibraryBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.page = self.fixture.page
        self.expect = self.fixture.expect
        self.fixture.no_ai = False
        descriptions = {
            'analysis': '选定一个会话和日期范围，核对内容后逐次授权分析。已有报告保留。',
            'media': '归档本机图片与附件，按月份浏览。微信中的原始文件保持保留。',
            'voice': '保存并回听语音，在本机转写为可检索的文字。',
            'sync': '读取新增聊天与附件，也可导入已有聊天文件。',
            'backup': '保存独立的本机快照，查看备份历史并校验文件。',
        }
        for module in self.fixture.modules:
            module['description'] = descriptions[module['id']]
        for conversation in self.fixture.conversations:
            conversation['source']['message_count'] = 8
        self.page.route('**/api/ai/config', self.connection_response)
        self.page.route('**/api/ai/preview', self.preview_response)
        self.page.emulate_media(color_scheme='light', reduced_motion='reduce')
        self.page.reload(wait_until='networkidle')

    def tearDown(self):
        self.fixture.tearDown()
        self.assertFalse(any(path == '/api/ai/run' for path, _ in self.fixture.mutations))

    def preview_response(self, route):
        scope = route.request.post_data_json
        self.fixture.mutations.append(('/api/ai/preview', scope))
        self.fixture.reply(route, {
            'status': 'ok', 'preview_id': 'd' * 48, 'scope': scope,
            'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            'recipient': {'configured': True, 'provider': 'openai', 'model': 'synthetic-model',
                          'endpoint': 'https://api.synthetic.invalid/v1/chat/completions'},
            'eligible_messages': 8, 'sample_messages': 8, 'sample_chars': 200,
            'plan': {'mode': 'sample', 'segments': 1, 'total_calls': 1, 'cached_calls': 0,
                     'new_calls': 1, 'merge_calls': 0, 'characters': 200},
            'stats': {'scope_messages': 8, 'excluded_nontext': 0, 'voice_messages': 0,
                      'transcribed_voice_messages': 0, 'sampled_voice_messages': 0},
            'metrics': {
                'version': 1, 'basis': 'selected_scope_all_messages',
                'totals': {'messages': 8, 'me': 4, 'other': 4, 'text': 8, 'voice': 0,
                           'transcribed_voice': 0, 'characters': 200, 'active_days': 4, 'sessions': 3},
                'activity': [{'date': day, 'count': 2, 'me': 1, 'other': 1}
                             for day in ('2026-08-28', '2026-08-29', '2026-08-30', '2026-09-01')],
                'activity_granularity': 'day',
                'hours': [{'hour': hour, 'count': 4 if hour in (18, 21) else 0} for hour in range(24)],
                'weekdays': [{'weekday': day, 'count': 2 if day == 0 else 1} for day in range(7)],
                'types': [{'type': 'text', 'count': 8}],
                'response_times': {'me': {'samples': 3, 'median_seconds': 35, 'p90_seconds': 60},
                                   'other': {'samples': 2, 'median_seconds': 90, 'p90_seconds': 120}},
                'methodology': ['合成统计只用于界面验证，不对应任何真实聊天。'],
            },
        })

    def connection_response(self, route):
        if route.request.method != 'GET':
            self.fixture.route_request(route)
            return
        self.fixture.reply(route, {
            'status': 'ok', 'provider': 'openai', 'model': 'synthetic-model',
            'configured': True, 'has_key': True,
            'endpoint': 'https://api.synthetic.invalid/v1/chat/completions',
        })

    def open_view(self, view):
        self.page.locator(f'[data-view="{view}"]').click()
        self.expect(self.page.locator(f'#{view}')).to_be_visible()

    def style(self, selector):
        # Resolve and read styles in one browser execution. Separate element
        # handle lookup/evaluation can observe a detached module after refresh.
        snapshot = self.page.wait_for_function('''selector => {
            const node = document.querySelector(selector);
            if (!node?.isConnected) return false;
            const value = (''' + STYLE + ''')(node);
            return value.duration && value.transition ? value : false;
        }''', arg=selector)
        try:
            return snapshot.json_value()
        finally:
            snapshot.dispose()

    def assert_text_contrast(self, selector, minimum=4.5):
        style = self.style(selector)
        self.assertGreaterEqual(contrast(rgba(style['color']), background(style)), minimum, (selector, style))

    def assert_no_overflow(self):
        size = self.page.evaluate('({viewport: innerWidth, width: document.documentElement.scrollWidth})')
        self.assertLessEqual(size['width'], size['viewport'] + 1, size)

    def use_large_catalog(self):
        # Real catalog rendering computes the totals and pagination from these
        # summaries. All names/counts are invented; there are no chat contents.
        template = self.fixture.conversations[0]
        for index in range(2, 1249):
            source = {**template['source'], 'id': f'large-{index:04d}', 'contact': f'合成档案{index:04d}'}
            self.fixture.conversations.append({
                'bundle_id': source['id'], 'alias': '', 'note': '', 'pinned': False,
                'hidden_at': None, 'version': 1, 'tags': [], 'source': source, 'source_missing': False,
            })
        for index, conversation in enumerate(self.fixture.conversations):
            conversation['source']['message_count'] = 307 if index < 446 else 306
        self.page.reload(wait_until='networkidle')
        self.expect(self.page.locator('#total-conversations')).to_have_text('1,249')
        self.expect(self.page.locator('#total-messages')).to_have_text('382,640')
        self.expect(self.page.locator('#nav-count')).to_have_text('1,249')

    def capture(self, name, full_page=True):
        if os.environ.get('SHE_LOVE_ME_UI_SCREENSHOTS') != '1':
            return
        directory = fixtures.ROOT / 'scripts' / 'tmp'
        directory.mkdir(parents=True, exist_ok=True)
        if full_page:
            # Full-page screenshots otherwise pin sticky UI at the previous
            # scroll position and can visually resemble a layout defect.
            self.page.evaluate('() => { document.activeElement?.blur(); window.scrollTo(0, 0); }')
            self.page.wait_for_function('window.scrollY === 0')
        self.page.screenshot(path=str(directory / name), full_page=full_page)

    def test_light_and_dark_surfaces_follow_system_preference(self):
        light = self.style('body')
        self.assertTrue(all(abs(channel - desired) <= 5 for channel, desired in zip(background(light), (245, 245, 247))), light)
        self.assert_text_contrast('body')
        self.capture('apple-catalog-desktop-light.png')
        self.open_view('settings')
        self.capture('apple-settings-desktop-light.png')
        self.page.emulate_media(color_scheme='dark')
        dark = self.style('body')
        self.assertTrue(all(abs(channel - desired) <= 10 for channel, desired in zip(background(dark), (28, 28, 30))), dark)
        for selector in ('body', '#settings-title', '#library-health-detail', '#tag-name'):
            self.assert_text_contrast(selector)
        self.capture('apple-settings-desktop-dark.png')
        self.open_view('catalog')
        self.capture('apple-catalog-desktop-dark.png')
        self.page.emulate_media(color_scheme='light')
        self.assertEqual(self.style('body')['background'], light['background'])

    def test_text_inputs_and_regular_labels_are_readable(self):
        for width, minimum in ((1440, 14), (390, 16)):
            self.page.set_viewport_size({'width': width, 'height': 900})
            for view, inputs, labels in (
                ('catalog', ('#search-input', '#filter-kind', '#date-from'), ('.filter-bar > label > span', '#search-help')),
                ('settings', ('#tag-name', '#tag-color'), ('#tag-form label > span', '.module-description strong')),
                ('analysis-workspace', ('#ai-contact-search', '#ai-bundle', '#ai-date-from'), ('#ai-scope-fields label > span',)),
            ):
                self.open_view(view)
                if view == 'catalog':
                    search = self.page.locator('.query-field').evaluate('''field => {
                        const input = field.querySelector('input'), icon = field.querySelector('.search-icon');
                        return {padding: parseFloat(getComputedStyle(input).paddingLeft),
                            iconRight: icon.getBoundingClientRect().right - input.getBoundingClientRect().left};
                    }''')
                    self.assertGreaterEqual(search['padding'], search['iconRight'] + 8, (width, search))
                for selector in inputs:
                    self.assertGreaterEqual(self.style(selector)['font'], minimum, (width, selector, self.style(selector)))
                for selector in labels:
                    sizes = self.page.locator(selector).evaluate_all('nodes => nodes.filter(n => n.getClientRects().length).map(n => parseFloat(getComputedStyle(n).fontSize))')
                    self.assertTrue(sizes)
                    self.assertGreaterEqual(min(sizes), 13, (width, selector, sizes))
            self.open_view('catalog')
            self.fixture.open_metadata()
            for selector in ('#conversation-alias', '#conversation-note'):
                self.assertGreaterEqual(self.style(selector)['font'], minimum, (width, selector, self.style(selector)))
            self.page.locator('#conversation-close').click()

    def test_mobile_controls_have_44_pixel_hit_regions(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        for view in ('catalog', 'settings', 'analysis-workspace', 'maintenance', 'trash'):
            self.open_view(view)
            controls = self.page.locator('button:visible, select:visible, .nav-item:visible, input:visible:not([type="checkbox"]):not([type="radio"]):not([type="file"]), textarea:visible').evaluate_all('nodes => nodes.map(n => ({id:n.id || n.textContent.trim(),width:n.getBoundingClientRect().width,height:n.getBoundingClientRect().height}))')
            for control in controls:
                self.assertGreaterEqual(control['height'], 44, (view, control))
                self.assertGreaterEqual(control['width'], 44, (view, control))
        self.open_view('catalog')
        self.fixture.open_metadata()
        for selector in ('#conversation-close', '#conversation-save', '#conversation-hide', '.pin-choice', '.conversation-tag-choice'):
            self.assertGreaterEqual(self.style(selector)['height'], 44, (selector, self.style(selector)))

    def test_primary_secondary_and_destructive_actions_remain_distinct(self):
        self.fixture.open_metadata()
        for scheme in ('light', 'dark'):
            self.page.emulate_media(color_scheme=scheme)
            primary, secondary, destructive = [self.style(selector) for selector in ('#conversation-save', '#conversation-close', '#conversation-hide')]
            primary_color = background(primary)
            self.assertGreater(primary_color[2], primary_color[0] + 50, (scheme, primary))
            self.assertGreater(primary_color[2], primary_color[1] + 20, (scheme, primary))
            secondary_color = background(secondary)
            self.assertLessEqual(max(secondary_color) - min(secondary_color), 25, (scheme, secondary))
            red = rgba(destructive['color'])
            self.assertGreater(red[0], red[1] * 1.3, (scheme, destructive))
            self.assertGreater(red[0], red[2] * 1.2, (scheme, destructive))
            for selector in ('#conversation-save', '#conversation-close', '#conversation-hide'):
                self.assert_text_contrast(selector)

    def test_keyboard_focus_is_visible_and_skip_link_keeps_workspace(self):
        self.open_view('settings')
        self.page.locator('.skip-link').focus()
        self.page.keyboard.press('Enter')
        self.expect(self.page.locator('#settings')).to_be_visible()
        self.expect(self.page.locator('#main')).to_be_focused()
        self.page.keyboard.press('Tab')
        target = self.page.locator(':focus')
        style = target.evaluate(STYLE)
        self.assertNotEqual(style['outline'], 'none', style)
        self.assertGreaterEqual(style['outlineWidth'], 2, style)

    def test_increased_contrast_strengthens_control_boundaries(self):
        self.open_view('settings')
        regular = self.style('#tag-name')
        self.page.emulate_media(contrast='more')
        higher = self.style('#tag-name')
        self.assertNotEqual((regular['border'], regular['borderWidth']), (higher['border'], higher['borderWidth']))
        self.assertGreaterEqual(contrast(rgba(higher['border']), background(higher)), 3, higher)
        self.page.emulate_media(forced_colors='active')
        self.page.locator('#tag-name').focus()
        self.page.keyboard.press('Tab')
        focus = self.page.locator(':focus').evaluate(STYLE)
        self.assertGreaterEqual(focus['outlineWidth'], 2, focus)

    def test_reduced_motion_keeps_controls_static(self):
        self.open_view('settings')
        self.page.emulate_media(reduced_motion='reduce')
        for selector in ('.module-toggle', '.workspace-panel', '.nav-item.is-active'):
            style = self.style(selector)
            self.assertTrue(style['animation'] == 'none' or all(float(value.rstrip('s')) <= .001 for value in style['duration'].split(', ')), style)
            self.assertTrue(all(float(value.rstrip('s')) <= .001 for value in style['transition'].split(', ')), style)

    def test_style_snapshot_uses_live_dom_after_control_replacement(self):
        self.open_view('settings')
        old = self.page.locator('.module-toggle').first.element_handle()
        old.evaluate('node => node.replaceWith(node.cloneNode(true))')
        self.assertFalse(old.evaluate('node => node.isConnected'))
        # Model the lookup/evaluate race: a module refresh can detach the node
        # after a locator resolves. Keep the actual browser CSS engine and DOM;
        # intercept only that stale locator handle at the cross-process boundary.
        with patch.object(self.page, 'locator', return_value=SimpleNamespace(first=old)):
            style = self.style('.module-toggle')
        self.assertNotEqual(style['duration'], '', 'Snapshot must belong to the live replacement')
        self.assertEqual(style['animation'], 'none')
        self.assertTrue(all(float(value.rstrip('s')) <= .001 for value in style['transition'].split(', ')), style)

    def test_module_switch_geometry_and_hit_region_survive_both_themes(self):
        self.open_view('settings')
        for width in (1440, 390, 320):
            self.page.set_viewport_size({'width': width, 'height': 900})
            for scheme in ('light', 'dark'):
                self.page.emulate_media(color_scheme=scheme)
                values = self.page.locator('.module-row').evaluate_all('rows => rows.map(n=>({row:n.getBoundingClientRect().toJSON(),control:n.querySelector("input").getBoundingClientRect().toJSON(),description:n.querySelector(".module-description").getBoundingClientRect().toJSON()}))')
                self.assertEqual(len(values), 5)
                self.assertLess(max(item['control']['width'] for item in values) - min(item['control']['width'] for item in values), 1)
                for item in values:
                    self.assertGreaterEqual(item['row']['height'], 44, item)
                    self.assertGreaterEqual(item['control']['x'], item['description']['right'], item)
                    self.assertTrue(40 <= item['control']['width'] <= 56, item)

    def test_small_windows_and_larger_text_have_no_document_overflow(self):
        self.use_large_catalog()
        for width in (390, 320, 768, 1024):
            self.page.set_viewport_size({'width': width, 'height': 844})
            for view in ('catalog', 'settings', 'analysis-workspace', 'maintenance', 'trash'):
                self.open_view(view)
                self.assert_no_overflow()
                if width == 390 and view in ('catalog', 'settings'):
                    self.capture(f'apple-{view}-mobile-light.png')
        self.page.set_viewport_size({'width': 1440, 'height': 1000})
        self.open_view('settings')
        # Synthetic text-only zoom: capture every computed size before changing
        # any element, so nested descendants grow by exactly 200%, not 400%+.
        self.page.evaluate('''() => {
          const sizes = Array.from(document.querySelectorAll('body,body *'), n => [n, parseFloat(getComputedStyle(n).fontSize)]);
          sizes.forEach(([node,size]) => { node.style.fontSize = `${size * 2}px`; });
        }''')
        self.assert_no_overflow()

    def test_drawer_fits_phone_viewport_and_restores_keyboard_focus(self):
        for width in (390, 320):
            self.page.set_viewport_size({'width': width, 'height': 844})
            self.fixture.open_metadata()
            self.expect(self.page.locator('#conversation-alias')).to_be_enabled()
            bounds = self.page.locator('#conversation-dialog').bounding_box()
            self.assertGreaterEqual(bounds['x'], 0, bounds)
            self.assertGreaterEqual(bounds['y'], 0, bounds)
            self.assertLessEqual(bounds['x'] + bounds['width'], width + 1, bounds)
            self.assertLessEqual(bounds['height'], 845, bounds)
            self.expect(self.page.locator('#conversation-close')).to_be_in_viewport()
            if width == 390:
                self.capture('apple-drawer-mobile-light.png', full_page=False)
                self.page.emulate_media(color_scheme='dark')
                self.capture('apple-drawer-mobile-dark.png', full_page=False)
            self.page.keyboard.press('Escape')
            self.expect(self.page.locator('#manage-conversation')).to_be_focused()

    def test_analysis_consent_is_readable_visible_and_never_prechecked(self):
        self.open_view('analysis-workspace')
        self.page.locator('#ai-bundle').select_option('fixture-0')
        self.page.locator('#ai-preview-button').click()
        self.expect(self.page.locator('#ai-consent')).to_be_visible()
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.expect(self.page.locator('#ai-run-button')).to_be_disabled()
        self.assertGreaterEqual(self.style('#ai-consent-text')['font'], 13)
        for scheme in ('light', 'dark'):
            self.page.emulate_media(color_scheme=scheme)
            self.assert_text_contrast('#ai-consent-text')
            self.assert_no_overflow()
            self.capture(f'apple-analysis-desktop-{scheme}.png')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.assert_no_overflow()


if __name__ == '__main__':
    unittest.main(verbosity=2)
