"""Heatmap acceptance with wholly synthetic API responses and random ports."""
import os
import unittest
from datetime import date, timedelta

import test_library_ui as fixtures


def heatmap(scope=None):
    scope = scope or {}
    start = date.fromisoformat(scope.get('date_from') or '2026-01-01')
    end = date.fromisoformat(scope.get('date_to') or '2026-03-31')
    days = [{'date': (start + timedelta(days=i)).isoformat(), 'count': (i * 13) % 61 if i % 4 else 0}
            for i in range((end - start).days + 1)]
    hours = [[0] * 24 for _ in range(7)]
    for day in days:
        hours[date.fromisoformat(day['date']).weekday()][20] += day['count']
    top = sorted((day for day in days if day['count']), key=lambda day: (-day['count'], day['date']))[:7]
    total = sum(day['count'] for day in days)
    return {'version': 1, 'as_of': '2026-09-10T12:00:00+08:00', 'timezone': 'server_local',
            'available_range': {'date_from': '2025-01-01', 'date_to': '2026-03-31'},
            'scope': {'date_from': start.isoformat(), 'date_to': end.isoformat(), 'days': len(days)},
            'days': days, 'weekday_hour': hours,
            'summary': {'message_count': total, 'active_days': sum(day['count'] > 0 for day in days),
                        'daily_average': round(total / len(days), 2), 'peak_day': top[0] if top else None},
            'top_days': top, 'excluded': {'invalid_timestamp': 2, 'future_timestamp': 1, 'system_messages': 3}}


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1', 'Opt-in synthetic browser tests')
class ChatHeatmapBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.LibraryBrowserTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.LibraryBrowserTests.tearDownClass()

    def setUp(self):
        self.fixture = fixtures.LibraryBrowserTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.page, self.expect = self.fixture.page, self.fixture.expect
        self.calls = []
        self.hold, self.fail, self.empty = False, False, False
        self.pending = None
        self.page.route('**/api/activity/heatmap', self.respond)
        self.page.emulate_media(color_scheme='light', reduced_motion='reduce')

    def tearDown(self):
        self.fixture.tearDown()
        self.assertTrue(all(path == '/api/library/conversations' for path, _ in self.fixture.mutations),
                        'Only explicit synthetic trash operations may mutate; heatmap must not call AI')

    def respond(self, route):
        self.assertEqual(route.request.method, 'POST')
        scope = route.request.post_data_json
        self.calls.append(scope)
        if self.hold:
            self.pending = route
            return
        if self.fail:
            route.fulfill(status=503, content_type='text/html', body='synthetic failure')
            return
        payload = heatmap(scope)
        if self.empty:
            for day in payload['days']:
                day['count'] = 0
            payload['weekday_hour'] = [[0] * 24 for _ in range(7)]
            payload['summary'] = {'message_count': 0, 'active_days': 0, 'daily_average': 0, 'peak_day': None}
            payload['top_days'] = []
        self.fixture.reply(route, {'status': 'ok', 'heatmap': payload})

    def open_heatmap(self):
        self.page.locator('#catalog-body .open-bundle').first.click()
        self.expect(self.page.locator('#chat-heatmap')).to_be_visible()
        self.page.locator('#chat-heatmap > summary').click()
        self.expect(self.page.locator('.ch-calendar')).to_be_visible()

    def test_lazy_read_calendar_counts_and_semantic_hour_table(self):
        self.page.locator('#catalog-body .open-bundle').first.click()
        self.expect(self.page.locator('#chat-heatmap')).to_be_visible()
        self.assertEqual(self.calls, [])
        self.page.locator('#chat-heatmap > summary').click()
        self.expect(self.page.locator('.ch-day')).to_have_count(90)
        self.expect(self.page.locator('.ch-month')).to_have_count(3)
        self.expect(self.page.locator('.ch-hour-table tbody tr')).to_have_count(7)
        self.expect(self.page.locator('.ch-hour-table td')).to_have_count(168)
        self.expect(self.page.locator('.ch-results')).to_contain_text('2026-03-31')
        self.expect(self.page.locator('.ch-results')).to_contain_text('消息频率不代表关系质量')
        self.assertEqual(self.calls, [{'bundle_id': 'fixture-0'}])
        self.assertEqual(self.page.url.split('#')[0], self.fixture.url)

    def test_dates_dirty_clear_old_results_apply_reset_and_local_validation(self):
        self.open_heatmap()
        self.page.locator('.ch-from').fill('2026-02-01')
        self.expect(self.page.locator('.ch-results')).to_be_empty()
        self.page.locator('.ch-to').fill('2026-02-28')
        self.page.locator('.ch-apply').click()
        self.expect(self.page.locator('.ch-day')).to_have_count(28)
        self.assertEqual(self.calls[-1]['date_from'], '2026-02-01')
        self.page.locator('.ch-from').fill('2024-01-01')
        self.page.locator('.ch-apply').click()
        self.expect(self.page.locator('.ch-status')).to_contain_text('366')
        self.assertEqual(len(self.calls), 2)
        self.page.locator('.ch-reset').click()
        self.expect(self.page.locator('.ch-day')).to_have_count(90)
        self.assertEqual(self.calls[-1], {'bundle_id': 'fixture-0'})

    def test_failure_is_not_empty_and_retry_is_explicit(self):
        self.fail = True
        self.page.locator('#catalog-body .open-bundle').first.click()
        self.page.locator('#chat-heatmap > summary').click()
        self.expect(self.page.locator('.ch-status')).to_contain_text('无法读取热度')
        self.expect(self.page.locator('.ch-results')).to_be_empty()
        self.assertEqual(len(self.calls), 1)
        self.fail = False
        self.page.locator('.ch-retry').click()
        self.expect(self.page.locator('.ch-day')).to_have_count(90)

    def test_zero_messages_explains_archive_not_real_world_contact(self):
        self.empty = True
        self.open_heatmap()
        self.expect(self.page.locator('.ch-results')).to_contain_text('此范围没有有效归档消息')
        self.expect(self.page.locator('.ch-results')).to_contain_text('不等于现实中没有联系')
        self.assertEqual(self.page.locator('.ch-day[data-level="0"]').count(), 90)

    def test_old_request_cannot_fill_another_conversation_or_closed_detail(self):
        self.hold = True
        self.page.locator('#catalog-body .open-bundle').first.click()
        self.page.locator('#chat-heatmap > summary').click()
        self.expect(self.page.locator('.ch-status')).to_contain_text('正在读取')
        self.page.locator('#catalog-body .open-bundle').nth(1).click()
        self.expect(self.page.locator('#case-title')).to_have_text('合成云舟')
        self.fixture.reply(self.pending, {'status': 'ok', 'heatmap': heatmap()})
        self.expect(self.page.locator('.ch-results')).to_be_empty()
        self.hold = False
        self.page.locator('#chat-heatmap > summary').click()
        self.expect(self.page.locator('.ch-day')).to_have_count(90)
        self.assertEqual(self.calls[-1]['bundle_id'], 'fixture-1')
        self.page.locator('#close-detail').click()
        self.expect(self.page.locator('#chat-heatmap')).to_be_hidden()

    def test_keyboard_responsive_dark_and_reduced_motion(self):
        self.page.locator('#catalog-body .open-bundle').first.click()
        summary = self.page.locator('#chat-heatmap > summary')
        summary.focus()
        self.page.keyboard.press('Enter')
        self.expect(self.page.locator('.ch-calendar')).to_be_visible()
        for width in (320, 390, 768, 1440):
            for scheme in ('light', 'dark'):
                self.page.set_viewport_size({'width': width, 'height': 1000})
                self.page.emulate_media(color_scheme=scheme)
                self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
                self.assertTrue(self.page.locator('.ch-calendar').evaluate('n => n.scrollWidth <= n.clientWidth + 1'))
                self.assertGreaterEqual(self.page.locator('.ch-apply').bounding_box()['height'], 44)
        self.page.set_viewport_size({'width': 1440, 'height': 1200})
        self.page.emulate_media(color_scheme='light')
        if os.environ.get('SHE_LOVE_ME_UI_SCREENSHOTS') == '1':
            self.page.locator('#chat-heatmap').screenshot(path=str(fixtures.ROOT / 'scripts/tmp/chat-heatmap-synthetic.png'))

    def test_trashing_active_conversation_clears_heatmap_and_late_response(self):
        self.open_heatmap()
        self.hold = True
        self.page.locator('.ch-reset').click()
        self.expect(self.page.locator('.ch-status')).to_contain_text('正在读取')
        self.page.locator('#manage-conversation').click()
        self.page.locator('#conversation-hide').click()
        self.expect(self.page.locator('#dashboard')).to_be_hidden()
        self.fixture.reply(self.pending, {'status': 'ok', 'heatmap': heatmap()})
        self.expect(self.page.locator('#chat-heatmap')).to_be_empty()
        self.expect(self.page.locator('.ch-day')).to_have_count(0)

    def test_unapplied_dates_survive_collapse_without_another_request(self):
        self.open_heatmap()
        self.page.locator('.ch-from').fill('2026-02-01')
        self.page.locator('.ch-to').fill('2026-02-28')
        self.page.locator('#chat-heatmap > summary').click()
        self.page.locator('#chat-heatmap > summary').click()
        self.expect(self.page.locator('.ch-status')).to_contain_text('范围已修改')
        self.expect(self.page.locator('.ch-from')).to_have_value('2026-02-01')
        self.expect(self.page.locator('.ch-results')).to_be_empty()
        self.assertEqual(len(self.calls), 1)
        self.page.locator('.ch-apply').click()
        self.expect(self.page.locator('.ch-day')).to_have_count(28)

    def test_catalog_refresh_revokes_heatmap_when_source_is_no_longer_visible(self):
        self.open_heatmap()
        self.fixture.conversations[0]['hidden_at'] = '2026-09-10T00:00:00Z'
        self.page.locator('#refresh-button').click()
        self.expect(self.page.locator('#catalog-body .open-bundle')).to_have_count(1)
        self.expect(self.page.locator('#chat-heatmap')).to_be_empty()


if __name__ == '__main__':
    unittest.main()
