"""Chart interactions with invented aggregates on a request-blocked blank page.

No app server, archives, model API, or persistent browser profile is used.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'dashboard' / 'static'
UI_ENABLED = os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1'
XSS = '<img src=x onerror=alert(1)>'


def synthetic_metrics():
    return {
        'totals': {'messages': 1240, 'me': 1203, 'other': 37,
                   'active_days': 2, 'sessions': 4, 'characters': 3210,
                   'voice': 0, 'transcribed_voice': 0},
        'activity': [{'date': '2026-09-01', 'count': 1234, 'me': 1200, 'other': 34},
                     {'date': '2026-09-02', 'count': 0, 'me': 0, 'other': 0},
                     {'date': '2026-09-03', 'count': 6, 'me': 3, 'other': 3}],
        'hours': [{'hour': hour, 'count': 1240 if hour == 13 else 0}
                  for hour in range(24)],
        'weekdays': [{'weekday': day, 'count': 1240 if day == 2 else 0}
                     for day in range(7)],
        'types': [{'type': 'text', 'count': 1240}, {'type': 'image', 'count': 0}],
        'response_times': {
            'me': {'samples': 8, 'median_seconds': 90, 'p90_seconds': 3600},
            'other': {'samples': 0, 'median_seconds': None, 'p90_seconds': None}},
        'methodology': ['完全虚构的 UI 统计夹具。'],
    }


@unittest.skipUnless(UI_ENABLED, 'Opt-in: set SHE_LOVE_ME_UI_TESTS=1')
class AnalysisChartProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception:
            cls.browser = cls.playwright.chromium.launch(headless=True, channel='msedge')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        from playwright.sync_api import expect

        self.expect = expect
        self.context = self.browser.new_context(viewport={'width': 1200, 'height': 1000},
                                                reduced_motion='reduce')
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors, self.dialogs, self.requests = [], [], []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('dialog', lambda dialog: (self.dialogs.append(dialog.message), dialog.dismiss()))
        self.page.on('request', lambda request: self.requests.append(request.url))
        self.page.route('**/*', lambda route: route.abort())
        self.page.set_content('<!doctype html><html lang="zh-CN"><body>'
                              '<main id="metrics" class="ai-metrics"></main></body></html>')
        for name in ('app.css', 'analysis-ui.css', 'apple-ui.css', 'chart-probe.css'):
            if (STATIC / name).exists():
                self.page.add_style_tag(path=str(STATIC / name))
        # A missing helper must fail at the requested visible behavior in the red phase.
        if (STATIC / 'chart-probe.js').exists():
            self.page.add_script_tag(path=str(STATIC / 'chart-probe.js'))
        self.page.add_script_tag(path=str(STATIC / 'analysis-charts.js'))
        self.render(synthetic_metrics())

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.requests, [], 'Chart interactions must issue no requests')

    def render(self, metrics):
        self.page.evaluate('(metrics) => AnalysisCharts.renderMetrics('
                           'document.querySelector("#metrics"), metrics, "合成范围")', metrics)

    def panel(self, title):
        return self.page.locator('.ai-chart-panel').filter(
            has=self.page.get_by_role('heading', name=title, exact=True))

    def move_in_svg(self, svg, x, y=60):
        svg.scroll_into_view_if_needed()
        point = svg.evaluate('''(svg, coords) => {
          const point = new DOMPoint(coords.x, coords.y).matrixTransform(svg.getScreenCTM());
          return {x: point.x, y: point.y};
        }''', {'x': x, 'y': y})
        self.page.mouse.move(point['x'], point['y'])

    def hover_row(self, row):
        row.scroll_into_view_if_needed()
        # Scrolling intentionally dismisses a probe. Finish the fixture's
        # programmatic scroll before the next physical pointer interaction.
        self.page.evaluate('() => new Promise(requestAnimationFrame)')
        row.hover()

    def tooltip(self, title, rows):
        tooltip = self.page.locator('.chart-probe-tooltip:visible')
        self.expect(tooltip).to_have_count(1)
        self.expect(tooltip.locator('.chart-probe-title')).to_have_text(title)
        rendered = {row.locator('dt').inner_text(): row.locator('dd').inner_text()
                    for row in tooltip.locator('.chart-probe-row').all()}
        for label, value in rows:
            self.assertIn(rendered.get(label), (value, f'{value} 条'),
                          f'Missing exact {label!r}: {value!r} in {rendered!r}')
        return tooltip

    def test_stacked_activity_snaps_in_gap_and_keeps_complete_date_and_table(self):
        panel = self.panel('按日的消息变化')
        # 3 bars have centers 140, 400, 660; x=275 is in their visual gap.
        self.move_in_svg(panel.locator('svg'), 275)
        self.tooltip('2026-09-02', [('我', '0'), ('对方 / 其他成员', '0'), ('合计', '0')])
        self.move_in_svg(panel.locator('svg'), 260)
        self.tooltip('2026-09-01', [('我', '1,200'), ('对方 / 其他成员', '34'), ('合计', '1,234')])
        panel.locator('.ai-chart-data summary').click()
        self.expect(panel.locator('tbody tr').first).to_have_text('2026-09-011,2341,20034')

    def test_monthly_activity_keeps_year_and_month(self):
        metrics = synthetic_metrics()
        metrics['activity_granularity'] = 'month'
        metrics['activity'] = [{'date': '2025-12', 'count': 4, 'me': 1, 'other': 3},
                               {'date': '2026-01', 'count': 0, 'me': 0, 'other': 0}]
        self.render(metrics)
        self.move_in_svg(self.panel('按月的消息变化').locator('svg'), 200)
        self.tooltip('2025-12', [('我', '1'), ('对方 / 其他成员', '3'), ('合计', '4')])

    def test_hour_and_weekday_zero_bars_remain_readable(self):
        self.move_in_svg(self.panel('一天中的消息分布').locator('svg'), 10 + 780 / 24 / 2)
        self.tooltip('0:00', [('消息数', '0')])
        self.move_in_svg(self.panel('一周中的消息分布').locator('svg'), 10 + 780 / 7 / 2)
        self.tooltip('周一', [('消息数', '0')])

    def test_all_zero_series_can_still_be_probed_and_empty_series_has_fallback(self):
        metrics = synthetic_metrics()
        for field in ('activity', 'hours', 'weekdays'):
            for row in metrics[field]:
                row['count'] = row['me'] = row['other'] = 0
        self.render(metrics)
        hours = self.panel('一天中的消息分布')
        self.expect(hours.locator('svg')).to_have_count(1)
        self.move_in_svg(hours.locator('svg'), 10 + 780 / 24 * 23.5)
        self.tooltip('23:00', [('消息数', '0')])
        metrics['hours'] = []
        self.render(metrics)
        self.expect(hours.locator('svg')).to_have_count(0)
        self.expect(hours.locator('.chart-empty')).to_be_visible()
        self.expect(hours.locator('.ai-chart-data')).to_have_count(1)

    def test_message_types_include_zero_values(self):
        rows = self.panel('消息构成').locator('.ai-distribution-row')
        self.hover_row(rows.first)
        self.tooltip('文字', [('消息数', '1,240')])
        self.hover_row(rows.nth(1))
        self.tooltip('图片', [('消息数', '0')])

    def test_message_types_continue_reading_through_overlapping_tooltip(self):
        rows = self.panel('消息构成').locator('.ai-distribution-row')
        rows.first.scroll_into_view_if_needed()
        self.page.evaluate('() => new Promise(requestAnimationFrame)')
        first = rows.first.bounding_box()
        self.page.mouse.move(first['x'] + 5, first['y'] + first['height'] / 2)
        tooltip = self.tooltip('文字', [('消息数', '1,240')])

        # Use actual overlapping geometry and a physical pointer move. A forced
        # row hover would bypass the card that intercepted the original event.
        second, card = rows.nth(1).bounding_box(), tooltip.bounding_box()
        left = max(second['x'], card['x'])
        right = min(second['x'] + second['width'], card['x'] + card['width'])
        top = max(second['y'], card['y'])
        bottom = min(second['y'] + second['height'], card['y'] + card['height'])
        self.assertGreater(right, left, 'Fixture must overlap the card and image row')
        self.assertGreater(bottom, top, 'Fixture must overlap the card and image row')
        overlap = {'x': (left + right) / 2, 'y': (top + bottom) / 2}
        self.assertTrue(self.page.evaluate('''({x, y}) => Boolean(
            document.elementFromPoint(x, y)?.closest('.chart-probe-tooltip'))''', overlap))
        self.page.mouse.move(overlap['x'], overlap['y'])
        self.tooltip('图片', [('消息数', '0')])

        # The part of the card below every data row remains hoverable, without
        # selecting a different row or dismissing the current reading.
        card = tooltip.bounding_box()
        outside = {'x': card['x'] + card['width'] / 2,
                   'y': card['y'] + card['height'] - 4}
        row_bottom = max(box['y'] + box['height'] for box in (first, second))
        self.assertGreater(outside['y'], row_bottom)
        self.assertTrue(self.page.evaluate('''({x, y}) => Boolean(
            document.elementFromPoint(x, y)?.closest('.chart-probe-tooltip'))''', outside))
        self.page.mouse.move(outside['x'], outside['y'])
        self.tooltip('图片', [('消息数', '0')])

    def test_sender_share_track_includes_zero_width_side_and_table(self):
        metrics = synthetic_metrics()
        metrics['totals'].update(me=0, other=1240)
        self.render(metrics)
        panel = self.panel('发言份额')
        self.hover_row(panel.locator('.ai-sender-track'))
        self.tooltip('发言份额', [('我', '0 条 · 0.0%'),
                                 ('对方 / 其他成员', '1,240 条 · 100.0%')])
        self.expect(panel.locator('tbody tr').first).to_have_text('我00.0%')

    def test_median_tracks_preserve_sample_count_and_missing_sample_state(self):
        rows = self.panel('相邻换人消息的间隔').locator('.ai-reply-row')
        self.hover_row(rows.first)
        self.tooltip('我', [('中位数', '1.5 分钟'), ('间隔样本', '8'), ('90% 的间隔不超过', '1.0 小时')])
        self.hover_row(rows.nth(1))
        self.tooltip('对方 / 其他成员', [('中位数', '暂无样本'), ('间隔样本', '0')])

    def test_rerender_clears_old_probe_and_supports_new_values(self):
        self.move_in_svg(self.panel('按日的消息变化').locator('svg'), 140)
        self.tooltip('2026-09-01', [('合计', '1,234')])
        metrics = synthetic_metrics()
        metrics['activity'] = [{'date': '2026-10-20', 'count': 8, 'me': 5, 'other': 3}]
        self.render(metrics)
        self.expect(self.page.locator('.chart-probe-tooltip:visible')).to_have_count(0)
        self.move_in_svg(self.panel('按日的消息变化').locator('svg'), 400)
        self.tooltip('2026-10-20', [('合计', '8')])
        self.render(None)
        self.expect(self.page.locator('.chart-probe-tooltip')).to_have_count(0)

    def test_untrusted_date_is_literal_text_without_network_or_html_execution(self):
        metrics = synthetic_metrics()
        metrics['activity'] = [{'date': XSS, 'count': 3, 'me': 1, 'other': 2}]
        self.render(metrics)
        self.move_in_svg(self.panel('按日的消息变化').locator('svg'), 400)
        tooltip = self.tooltip(XSS, [('合计', '3')])
        self.assertEqual(tooltip.locator('img, script, a').count(), 0)
        self.assertEqual(self.page.locator('#metrics img, #metrics script').count(), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
