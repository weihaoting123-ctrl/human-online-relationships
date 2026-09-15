"""Offline, invented aggregate fixtures. No archive or provider calls."""
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'dashboard' / 'static'


def fixture():
    return {'as_of': '2024-03-15T12:00:00+08:00',
            'scope': {'date_from': '2024-01-01', 'date_to': '2024-03-31'},
            'frequency': {'daily': [{'date': '2024-01-10', 'count': 4},
                                    {'date': '2024-02-01', 'count': 1},
                                    {'date': '2024-02-14', 'count': 2},
                                    {'date': '2024-02-29', 'count': 8}],
                          'daily_coverage': {'complete': True, 'date_from': '2024-01-01',
                                             'date_to': '2024-03-15', 'timezone': 'server_local',
                                             'basis': 'selected_scope_non_system_non_future'}}}


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1', 'Synthetic browser opt-in')
class DensityCalendarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True)
        except Exception:
            cls.browser = cls.pw.chromium.launch(headless=True, channel='msedge')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        from playwright.sync_api import expect
        self.expect = expect
        self.context = self.browser.new_context(viewport={'width': 1100, 'height': 900},
                                               reduced_motion='reduce', has_touch=True,
                                               timezone_id='America/Los_Angeles')
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors, self.requests = [], []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('request', lambda request: self.requests.append(request.url))
        self.page.set_content('<!doctype html><html lang="zh-CN"><body style="margin:20px">'
                              '<figure id="calendar"></figure><figure id="second"></figure></body></html>')
        for style in ('relationship-timeline.css', 'density-calendar.css', 'apple-ui.css'):
            if (STATIC / style).exists():
                self.page.add_style_tag(path=str(STATIC / style))
        if (STATIC / 'density-calendar.js').exists():
            self.page.add_script_tag(path=str(STATIC / 'density-calendar.js'))

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.requests, [], 'Calendar must have no network side effects')

    def mount(self, value=None, target='calendar'):
        self.assertTrue(self.page.evaluate('Boolean(window.DensityCalendar)'), 'Calendar renderer missing')
        self.page.evaluate('arg => DensityCalendar.render(document.getElementById(arg.target), arg.value)',
                           {'target': target, 'value': fixture() if value is None else value})

    def test_latest_observed_month_leap_day_and_exact_counts(self):
        self.mount()
        self.expect(self.page.locator('.dc-period-title')).to_have_text('2024 年 2 月')
        self.expect(self.page.locator('.dc-day')).to_have_count(29)
        self.expect(self.page.locator('[data-date="2024-02-29"]')).to_have_attribute('data-level', '4')
        self.expect(self.page.locator('[data-date="2024-02-02"]')).to_have_attribute('data-level', '0')
        self.expect(self.page.locator('.dc-summary')).to_contain_text('11 条')
        self.expect(self.page.locator('.dc-summary')).to_contain_text('3 个活跃日')
        self.assertEqual(self.page.locator('.dc-weekday').all_text_contents(), ['一','二','三','四','五','六','日'])

    def test_year_has_twelve_months_and_month_drilldown_keeps_scale(self):
        self.mount()
        self.page.get_by_role('button', name='年视图', exact=True).click()
        self.expect(self.page.locator('.dc-month')).to_have_count(12)
        self.expect(self.page.locator('.dc-mini-day').first).to_have_text('1')
        self.expect(self.page.locator('.dc-summary')).to_contain_text('15 条')
        self.expect(self.page.locator('.dc-month[data-month="4"]')).to_contain_text('未来日期')
        self.expect(self.page.locator('.dc-month[data-month="4"]')).not_to_contain_text('0 条')
        future = self.page.locator('.dc-mini-day[data-state="future"]').first
        self.assertEqual(future.evaluate('n=>getComputedStyle(n).backgroundImage'), 'none')
        self.assertIn('未来日期', future.get_attribute('title'))
        jan = self.page.locator('.dc-month[data-month="1"]')
        self.expect(jan).to_contain_text('4 条')
        jan.click()
        self.expect(self.page.locator('.dc-period-title')).to_have_text('2024 年 1 月')
        self.expect(self.page.locator('[data-date="2024-01-10"]')).to_have_attribute('data-level', '2')

    def test_hover_focus_touch_and_keyboard_readout(self):
        self.mount()
        day = self.page.locator('[data-date="2024-02-14"]')
        day.hover()
        self.expect(self.page.locator('.dc-readout')).to_contain_text('2024-02-14 · 2 条')
        day.focus()
        day.press('ArrowRight')
        self.expect(self.page.locator('[data-date="2024-02-15"]')).to_be_focused()
        self.expect(self.page.locator('.dc-readout')).to_contain_text('0 条')
        self.page.locator('[data-date="2024-02-29"]').tap()
        self.expect(self.page.locator('.dc-readout')).to_contain_text('8 条')

    def test_coverage_and_future_are_not_zero_and_use_snapshot_day(self):
        self.mount()
        self.page.get_by_role('button', name='下一月').click()
        self.expect(self.page.locator('[data-date="2024-03-15"]')).to_have_attribute('data-state', 'zero')
        self.expect(self.page.locator('[data-date="2024-03-16"]')).to_have_attribute('data-state', 'future')
        value = fixture()
        value['frequency']['daily_coverage']['date_from'] = '2024-02-10'
        value['frequency']['daily'] = value['frequency']['daily'][2:]
        self.mount(value)
        self.expect(self.page.locator('[data-date="2024-02-01"]')).to_have_attribute('data-state', 'outside')
        self.page.locator('[data-date="2024-02-01"]').click()
        self.expect(self.page.locator('.dc-readout')).to_contain_text('统计范围外')

    def test_legacy_incomplete_and_malformed_data_never_fabricate_daily_counts(self):
        values = [{'frequency': {'weekly': [{'week_start': '2024-02-01', 'count': 99}]}}]
        for change in ({'complete': False}, {'date_from': '2024-02-31'}):
            value = fixture()
            value['frequency']['daily_coverage'].update(change)
            values.append(value)
        value = fixture()
        value['frequency']['daily'].append({'date': '<img src=x>', 'count': 10})
        values.append(value)
        value = fixture()
        value['as_of'] = {'slice': 7}
        values.append(value)
        for value in values:
            self.mount(value)
            self.expect(self.page.locator('.dc-day')).to_have_count(0)
            self.expect(self.page.locator('.dc-unavailable')).to_contain_text('逐日统计')
        self.expect(self.page.locator('img')).to_have_count(0)

    def test_empty_scope_rerender_and_multiple_instances_do_not_leak_selection(self):
        self.mount()
        self.page.get_by_role('button', name='年视图').click()
        value = fixture()
        value['frequency']['daily'] = []
        self.mount(value)
        self.expect(self.page.locator('#calendar .dc-period-title')).to_have_text('2024 年 3 月')
        self.expect(self.page.locator('#calendar .dc-summary')).to_contain_text('0 条')
        self.mount(target='second')
        self.expect(self.page.locator('#second .dc-period-title')).to_have_text('2024 年 2 月')
        self.page.locator('#calendar').get_by_role('button', name='年视图').click()
        self.expect(self.page.locator('#second .dc-day')).to_have_count(29)

    def test_year_navigation_and_month_boundaries(self):
        value = fixture()
        value['frequency']['daily_coverage']['date_from'] = '2023-12-01'
        self.mount(value)
        self.page.get_by_label('联系密度年份').select_option('2023')
        self.expect(self.page.locator('.dc-period-title')).to_have_text('2023 年 2 月')
        self.page.get_by_role('button', name='年视图').click()
        self.page.locator('.dc-month[data-month="12"]').click()
        self.page.get_by_role('button', name='下一月').click()
        self.expect(self.page.locator('.dc-period-title')).to_have_text('2024 年 1 月')

    def test_mobile_dark_reduced_motion_and_synthetic_screenshots(self):
        self.mount()
        capture = os.environ.get('SHE_LOVE_ME_UI_SCREENSHOTS')
        if capture == '1':
            capture = ROOT / 'scripts' / 'tmp' / 'density-calendar-shots'
        if capture:
            Path(capture).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(capture) / 'density-month-desktop.png'), full_page=True)
        for width in (320, 390, 768):
            self.page.set_viewport_size({'width': width, 'height': 844})
            self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width + 1)
            self.assertGreaterEqual(self.page.locator('.dc-day').first.bounding_box()['height'], 44)
            self.assertGreaterEqual(self.page.locator('.dc-year').evaluate('n=>parseFloat(getComputedStyle(n).fontSize)'), 16)
        self.page.emulate_media(color_scheme='dark', reduced_motion='reduce')
        self.page.get_by_role('button', name='年视图').click()
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 769)
        self.assertEqual(self.page.locator('.dc-month').first.evaluate('n=>getComputedStyle(n).transitionDuration'), '0s')
        if capture:
            self.page.screenshot(path=str(Path(capture) / 'density-year-dark.png'), full_page=True)
            self.page.set_viewport_size({'width': 390, 'height': 844})
            self.page.get_by_role('button', name='月视图').click()
            self.page.screenshot(path=str(Path(capture) / 'density-month-mobile-dark.png'), full_page=True)


if __name__ == '__main__':
    unittest.main()
