"""Synthetic relationship timeline acceptance; no private files or cloud calls."""
from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timedelta, timezone

import test_library_ui as fixtures
from dashboard.relationship_timeline import NOTICES


def local_timeline():
    return {
        'version': 1, 'basis': 'local_archive', 'as_of': '2026-09-06T12:00:00+08:00',
        'source_max_date': '2026-09-01T12:00:00+08:00',
        'last_contact': {'last_record_at': '2026-09-01T12:00:00+08:00', 'elapsed_days': 5,
                         'last_me_at': '2026-09-01T12:00:00+08:00', 'last_other_at': '2026-08-30T12:00:00+08:00',
                         'basis': 'whole_archive_non_system_non_future', 'reason_status': 'unknown',
                         'reason': '仅凭消息间隔无法确定原因。'},
        'scope': {'date_from': '2026-01-01', 'date_to': '2026-02-28', 'message_count': 124,
                  'first_record_at': '2026-01-01T12:00:00+08:00', 'last_record_at': '2026-02-28T12:00:00+08:00'},
        'frequency': {'week_start': 'monday', 'threshold': 20, 'baseline_active_week_median': 8,
                      'active_weeks': 8, 'weekly': [
                          {'week_start': (datetime(2025, 12, 29) + timedelta(weeks=i)).date().isoformat(),
                           'count': count, 'frequent': count >= 20}
                          for i, count in enumerate([3, 8, 24, 35, 29, 7, 0, 18])],
                      'phases': [{'start_date': '2026-01-12', 'end_date': '2026-02-01', 'weeks': 3, 'message_count': 88}]},
        'gaps': [{'start_at': '2026-02-02T12:00:00+08:00', 'end_at': '2026-02-20T12:00:00+08:00', 'elapsed_days': 18}],
        'nodes': [{'type': 'first_record', 'at': '2026-01-01T12:00:00+08:00'},
                  {'type': 'frequent_phase', 'at': '2026-01-12', 'end_at': '2026-02-01', 'message_count': 88},
                  {'type': 'gap', 'start_at': '2026-02-02T12:00:00+08:00', 'at': '2026-02-20T12:00:00+08:00', 'elapsed_days': 18},
                  {'type': 'last_record', 'at': '2026-02-28T12:00:00+08:00'}],
        'excluded': {'invalid_timestamp': 0, 'future_timestamp': 0, 'system_messages': 2},
        'limits': {'nodes': {'total': 4, 'returned': 4, 'truncated': False}},
        'notices': list(NOTICES),
    }


def semantic_timeline():
    return {'version': 1, 'generated': True, 'events': [
        {'id': 'event-1', 'date_from': '2026-01-15', 'date_to': '2026-01-15', 'kind': 'plan',
         'title': '提出合成周末计划', 'summary': '样本中讨论了周末一起看展的可能。', 'status': 'planned',
         'evidence_level': 'reported', 'related_event_id': None, 'evidence': [{'date': '2026-01-15', 'sample_index': 3}]},
        {'id': 'event-2', 'date_from': '2026-01-24', 'date_to': '2026-01-25', 'kind': 'outcome',
         'title': '样本中自述已经看展', 'summary': '当事人在后续文字中提及看展经历，未独立核实。', 'status': 'realized',
         'evidence_level': 'inferred', 'related_event_id': 'event-1', 'evidence': [{'date': '2026-01-25', 'sample_index': 7}]},
    ], 'no_contact_reason': {'kind': 'unknown', 'summary': '无法确定少联系的原因。', 'evidence': [],
                              'limitations': ['样本不能证明对方动机。']},
        'coverage': {'date_from': '2026-01-01', 'date_to': '2026-02-28', 'analysis_mode': 'sample',
                     'analyzed_messages': 8, 'complete': True, 'scope': 'selected_sample', 'limitations': ['只代表所选样本。']}}


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1', 'Opt-in synthetic browser tests')
class RelationshipTimelineBrowserTests(unittest.TestCase):
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
        self.page, self.expect = self.fixture.page, self.fixture.expect
        self.fixture.no_ai = False
        self.local, self.semantic = local_timeline(), semantic_timeline()
        self.partial = False
        self.hold_detail = False
        self.pending_detail = None
        self.hold_preview = False
        self.pending_preview = None
        self.page.route('**/api/bundles/*', self.detail_response)
        self.page.route('**/api/ai/preview', self.preview_response)
        self.page.route('**/api/ai/history', lambda route: self.fixture.reply(route, {'reports': [self.report()]}))
        self.page.route('**/api/ai/reports/timeline-fixture', lambda route: self.fixture.reply(route, self.report()))
        self.page.emulate_media(color_scheme='light', reduced_motion='reduce')
        self.page.reload(wait_until='networkidle')

    def tearDown(self):
        self.fixture.tearDown()
        self.assertFalse(any(path == '/api/ai/run' for path, _ in self.fixture.mutations))

    def detail_response(self, route):
        item = next(item for item in self.fixture.conversations if route.request.url.endswith('/' + item['bundle_id']))
        payload = {'status': 'ok', 'bundle': {**item['source'], 'stats': {}, 'preview': {}, 'reports': [],
                   'library': item, 'relationship_timeline': copy.deepcopy(self.local)}}
        if self.hold_detail:
            self.pending_detail = (route, payload)
        else:
            self.fixture.reply(route, payload)

    def report(self):
        return {'id': 'timeline-fixture', 'created_at': '2026-09-06T12:00:00+08:00',
                'provider': 'openai', 'model': 'synthetic', 'mode': 'sample',
                'scope': {'bundle_id': 'fixture-0', 'date_from': '2026-01-01', 'date_to': '2026-02-28', 'analysis_mode': 'sample'},
                'coverage': {'complete': not self.partial, 'analyzed_messages': 8, 'eligible_messages': 124,
                             'total_segments': 2, 'completed_segments': 1 if self.partial else 2},
                'metrics': {'timeline': self.local}, 'result': {'summary': '合成报告', 'timeline': self.semantic}}

    def preview_response(self, route):
        scope = route.request.post_data_json
        self.fixture.mutations.append(('/api/ai/preview', scope))
        payload = {'status': 'ok', 'preview_id': 'f' * 48, 'scope': scope, 'metrics': {'timeline': self.local},
                   'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                   'recipient': {'configured': False}, 'eligible_messages': 8, 'sample_messages': 8,
                   'stats': {'scope_messages': 124}, 'plan': {'mode': 'sample', 'segments': 1, 'new_calls': 1}}
        if self.hold_preview:
            self.pending_preview = (route, payload)
        else:
            self.fixture.reply(route, payload)

    def open_detail(self):
        self.page.locator('#catalog-body .open-bundle').first.click()
        self.expect(self.page.locator('#relationship-timeline')).to_be_visible()

    def open_report(self):
        self.page.locator('[data-view="analysis-workspace"]').click()
        self.page.locator('#ai-history-list .history-item').click()
        self.expect(self.page.locator('#ai-relationship-timeline')).to_be_visible()

    def capture(self, name, full_page=True):
        if os.environ.get('SHE_LOVE_ME_UI_SCREENSHOTS') != '1':
            return
        directory = fixtures.ROOT / 'scripts' / 'tmp'
        directory.mkdir(parents=True, exist_ok=True)
        if full_page:
            self.page.wait_for_load_state('networkidle')
            self.page.evaluate('''async () => {
                document.activeElement?.blur(); window.scrollTo({top: 0, behavior: 'instant'});
                await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame);
                window.scrollTo({top: 0, behavior: 'instant'});
            }''')
            self.page.wait_for_function('window.scrollY === 0')
        self.page.screenshot(path=str(directory / name), full_page=full_page)

    def test_local_statistics_keep_whole_archive_recency_separate_from_scope(self):
        self.open_detail()
        target = self.page.locator('#relationship-timeline')
        self.expect(target.locator('.rt-recency')).to_contain_text('5 天')
        self.expect(target.locator('.rt-recency')).to_contain_text('2026-09-01')
        self.expect(target.locator('.rt-scope')).to_contain_text('2026-02-28')
        self.expect(target).to_contain_text('原因未知')
        self.expect(target.locator('.rt-density')).to_be_visible()
        self.expect(target.locator('.rt-node')).to_have_count(4)
        self.capture('relationship-detail-desktop-light.png')

    def test_product_identity_describes_human_online_relationships(self):
        self.assertEqual(self.page.title(), '人类线上关系可视化')
        self.expect(self.page.locator('.brand-name')).to_have_text('人类线上关系可视化')
        self.expect(self.page.locator('.brand-lockup')).to_have_attribute('aria-label', '人类线上关系可视化首页')

    def test_type_filter_and_reverse_order(self):
        self.open_detail()
        target = self.page.locator('#relationship-timeline')
        self.expect(target.locator('.rt-node').first).to_contain_text('最早记录')
        target.locator('.rt-order').select_option('desc')
        self.expect(target.locator('.rt-node').first).to_contain_text('区间末条记录')
        target.locator('.rt-filter').select_option('gap')
        self.expect(target.locator('.rt-node')).to_have_count(1)
        self.expect(target.locator('.rt-node')).to_contain_text('18 天')

    def test_dialog_escape_focus_restore_and_reduced_motion(self):
        self.open_detail()
        trigger = self.page.locator('#relationship-timeline .rt-node').first
        trigger.focus()
        self.page.keyboard.press('Enter')
        dialog = self.page.locator('#relationship-event-dialog')
        self.expect(dialog).to_be_visible()
        self.expect(dialog).to_contain_text('本机统计')
        self.assertEqual(dialog.evaluate('n => getComputedStyle(n).animationName'), 'none')
        self.page.keyboard.press('Escape')
        self.expect(dialog).to_be_hidden()
        self.expect(trigger).to_be_focused()
        self.expect(self.page.locator('#dashboard')).to_be_visible()

    def test_normal_motion_is_short_non_looping(self):
        self.page.emulate_media(reduced_motion='no-preference')
        self.open_detail()
        self.page.locator('#relationship-timeline .rt-node').first.click()
        values = self.page.locator('#relationship-event-dialog').evaluate('''n => {
            const s = getComputedStyle(n); return [s.animationName, s.animationDuration, s.animationIterationCount];
        }''')
        self.assertNotEqual(values[0], 'none')
        self.assertTrue(all(.1 <= float(value.strip().rstrip('s')) <= .25 for value in values[1].split(',')), values)
        self.assertEqual(values[2], '1')

    def test_semantic_progress_and_safe_evidence_do_not_claim_verified_fact(self):
        self.open_report()
        target = self.page.locator('#ai-relationship-timeline')
        self.expect(target).to_contain_text('AI 样本解释')
        self.expect(target).to_contain_text('未独立核实')
        self.expect(target).to_contain_text('报告生成时统计')
        target.locator('.rt-filter').select_option('outcome')
        self.expect(target.locator('.rt-node')).to_contain_text('可能已落实')
        target.locator('.rt-node').click()
        dialog = self.page.locator('#relationship-event-dialog')
        self.expect(dialog).to_contain_text('2026-01-25')
        self.expect(dialog).to_contain_text('样本序号 7')
        self.expect(dialog).to_contain_text('提出合成周末计划')
        self.expect(dialog).to_contain_text('AI 推断')
        self.page.keyboard.press('Escape')
        self.capture('relationship-analysis-desktop-light.png')

    def test_partial_and_legacy_reports_do_not_invent_reasons(self):
        self.partial = True
        self.semantic['no_contact_reason'] = {'kind': 'explicit', 'summary': '不应显示的确定原因', 'evidence': []}
        self.open_report()
        target = self.page.locator('#ai-relationship-timeline')
        self.expect(target.locator('.rt-coverage').first).to_contain_text('部分结果')
        self.expect(target).to_contain_text('原因未知')
        self.expect(target).not_to_contain_text('不应显示的确定原因')
        self.semantic = None
        self.page.locator('#ai-history-list .history-item').click()
        self.expect(target).to_contain_text('这份报告没有语义时间线')
        self.expect(target.locator('.rt-node[data-basis="ai"]')).to_have_count(0)

    def test_completed_legacy_report_missing_semantics_is_not_mislabeled_partial(self):
        self.semantic = {'version': 1, 'generated': False, 'events': [], 'coverage': {'complete': False},
                         'no_contact_reason': {'kind': 'unknown', 'summary': '', 'evidence': []}}
        self.open_report()
        target = self.page.locator('#ai-relationship-timeline')
        self.expect(target).to_contain_text('这份报告没有语义时间线')
        self.expect(target).not_to_contain_text('部分结果')

    def test_long_semantic_list_is_paged_without_losing_filter(self):
        template = self.semantic['events'][0]
        self.semantic['events'] = [{**template, 'id': f'event-{i}', 'title': f'合成计划 {i}',
                                    'date_from': (datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat()}
                                   for i in range(63)]
        self.open_report()
        target = self.page.locator('#ai-relationship-timeline')
        target.locator('.rt-filter').select_option('plan')
        self.expect(target.locator('.rt-node')).to_have_count(25)
        target.locator('.rt-next').click()
        self.expect(target.locator('.rt-node')).to_have_count(25)
        self.expect(target.locator('.rt-page-status')).to_contain_text('2 / 3')
        target.locator('.rt-next').click()
        self.expect(target.locator('.rt-node')).to_have_count(13)
        self.expect(target.locator('.rt-filter')).to_have_value('plan')
        self.expect(target.locator('.rt-next')).to_be_disabled()

    def test_untrusted_semantic_text_never_becomes_markup_or_quotes(self):
        attack = '<img src=x onerror="window.timelineXss=1">'
        self.semantic['events'][0].update(title=attack, summary=attack, quote='PRIVATE QUOTATION NOT FOR UI')
        self.semantic['events'][0]['evidence'][0]['quote'] = 'PRIVATE QUOTATION NOT FOR UI'
        self.open_report()
        target = self.page.locator('#ai-relationship-timeline')
        target.locator('.rt-filter').select_option('plan')
        target.locator('.rt-node').click()
        self.expect(self.page.locator('#relationship-event-dialog')).to_contain_text(attack)
        self.assertEqual(self.page.locator('#relationship-event-dialog img').count(), 0)
        self.assertIsNone(self.page.evaluate('window.timelineXss'))
        self.assertNotIn('PRIVATE QUOTATION NOT FOR UI', self.page.locator('body').inner_text())

    def test_select_range_only_prefills_existing_ai_controls(self):
        self.open_detail()
        self.page.locator('#relationship-timeline .rt-filter').select_option('frequent_phase')
        self.page.locator('#relationship-timeline .rt-node').click()
        self.page.locator('#relationship-event-dialog .rt-analyze-range').click()
        self.expect(self.page.locator('#analysis-workspace')).to_be_visible()
        self.expect(self.page.locator('#ai-bundle')).to_have_value('fixture-0')
        self.expect(self.page.locator('#ai-date-from')).to_have_value('2026-01-12')
        self.expect(self.page.locator('#ai-date-to')).to_have_value('2026-02-01')
        self.expect(self.page.locator('#ai-preview')).to_be_hidden()
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()
        self.assertEqual(self.fixture.mutations, [])

    def test_selection_switch_closes_details_and_stale_bundle_cannot_replace_new_timeline(self):
        self.open_detail()
        self.page.locator('#relationship-timeline .rt-node').first.click()
        self.page.evaluate("loadBundle('fixture-1')")
        self.expect(self.page.locator('#relationship-event-dialog')).to_be_hidden()
        self.expect(self.page.locator('#case-title')).to_have_text('合成云舟')
        self.hold_detail = True
        self.page.evaluate("void loadBundle('fixture-0')")
        self.page.wait_for_function("document.querySelector('#dashboard').getAttribute('aria-busy') === 'true'")
        self.hold_detail = False
        self.page.evaluate("loadBundle('fixture-1')")
        route, payload = self.pending_detail
        payload['bundle']['relationship_timeline']['nodes'][0]['type'] = 'stale-fake'
        self.fixture.reply(route, payload)
        self.expect(self.page.locator('#case-title')).to_have_text('合成云舟')
        self.expect(self.page.locator('#relationship-timeline')).not_to_contain_text('stale-fake')

    def test_scope_change_invalidates_pending_preview_and_closes_report_details(self):
        self.open_report()
        self.page.locator('#ai-relationship-timeline .rt-node').first.click()
        self.page.evaluate("AiWorkspace.selectBundle('fixture-1')")
        self.expect(self.page.locator('#relationship-event-dialog')).to_be_hidden()
        self.expect(self.page.locator('#ai-relationship-timeline')).to_be_hidden()
        self.hold_preview = True
        self.page.locator('#ai-preview-button').click()
        self.page.wait_for_function("document.querySelector('#ai-preview-button').disabled")
        self.page.locator('#ai-date-from').fill('2026-01-01')
        route, payload = self.pending_preview
        self.fixture.reply(route, payload)
        self.expect(self.page.locator('#ai-relationship-timeline')).to_be_hidden()
        self.expect(self.page.locator('#ai-consent')).not_to_be_checked()

    def test_opening_history_invalidates_an_older_pending_preview(self):
        self.page.locator('[data-view="analysis-workspace"]').click()
        self.page.locator('#ai-bundle').select_option('fixture-1')
        self.hold_preview = True
        self.page.locator('#ai-preview-button').click()
        self.page.wait_for_function("document.querySelector('#ai-preview-button').disabled")
        self.page.locator('#ai-history-list .history-item').click()
        self.expect(self.page.locator('#ai-relationship-timeline')).to_contain_text('合成青禾')
        route, payload = self.pending_preview
        self.fixture.reply(route, payload)
        self.expect(self.page.locator('#ai-preview-button')).to_be_enabled()
        self.expect(self.page.locator('#ai-relationship-timeline')).to_contain_text('合成青禾')
        self.expect(self.page.locator('#ai-preview')).to_be_hidden()

    def test_small_screen_and_dark_dialog_fit_viewport(self):
        self.open_report()
        for width in (320, 390, 768, 1024):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 844})
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width + 1)
                self.page.locator('#ai-relationship-timeline .rt-node').first.click()
                bounds = self.page.locator('#relationship-event-dialog').bounding_box()
                self.assertGreaterEqual(bounds['x'], 0)
                self.assertGreaterEqual(bounds['y'], 0)
                self.assertLessEqual(bounds['width'], width)
                self.assertLessEqual(bounds['y'] + bounds['height'], 845)
                self.expect(self.page.locator('#relationship-event-dialog .rt-dialog-close')).to_be_in_viewport()
                self.page.keyboard.press('Escape')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.capture('relationship-analysis-mobile-light.png')
        self.page.locator('#ai-relationship-timeline .rt-node').first.click()
        self.capture('relationship-dialog-mobile-light.png', full_page=False)
        self.page.keyboard.press('Escape')
        self.page.emulate_media(color_scheme='dark')
        self.page.set_viewport_size({'width': 1440, 'height': 1000})
        self.capture('relationship-analysis-desktop-dark.png')

    def test_local_timeline_and_keyboard_targets_work_without_ai_script(self):
        self.fixture.no_ai = True
        self.page.reload(wait_until='networkidle')
        self.open_detail()
        trigger = self.page.locator('#relationship-timeline .rt-node').first
        trigger.focus()
        self.page.keyboard.press('Tab')
        self.page.keyboard.press('Shift+Tab')
        style = trigger.evaluate('n => ({outline: getComputedStyle(n).outlineStyle, width: parseFloat(getComputedStyle(n).outlineWidth), height: n.getBoundingClientRect().height})')
        self.assertNotEqual(style['outline'], 'none')
        self.assertGreaterEqual(style['width'], 2)
        self.assertGreaterEqual(style['height'], 44)
        self.page.keyboard.press('Enter')
        self.expect(self.page.locator('#relationship-event-dialog .rt-analyze-range')).to_be_hidden()
        self.page.keyboard.press('Escape')
        self.page.locator('[data-view="settings"]').click()
        self.expect(self.page.locator('#settings')).to_be_visible()

    def test_mobile_two_hundred_percent_text_stays_readable_in_long_dialog(self):
        self.semantic['events'][0]['summary'] = '这是一条只用于合成布局验收的较长进展说明。' * 20
        self.open_report()
        self.page.set_viewport_size({'width': 320, 'height': 844})
        self.page.locator('#ai-relationship-timeline .rt-filter').select_option('plan')
        self.page.locator('#ai-relationship-timeline .rt-node').click()
        self.page.evaluate('''() => {
            const nodes = [...document.querySelectorAll('#relationship-event-dialog, #relationship-event-dialog *')];
            const sizes = nodes.map(node => parseFloat(getComputedStyle(node).fontSize));
            nodes.forEach((node, index) => node.style.fontSize = `${sizes[index] * 2}px`);
        }''')
        dialog = self.page.locator('#relationship-event-dialog')
        self.assertLessEqual(dialog.evaluate('n => n.scrollWidth - n.clientWidth'), 1)
        dialog.evaluate('n => n.scrollTop = n.scrollHeight')
        self.expect(dialog.locator('.rt-dialog-close')).to_be_in_viewport()
        self.expect(dialog.locator('.rt-dialog-close')).to_be_visible()
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 321)

    def test_methodology_is_collapsed_but_truncation_stays_visible(self):
        self.assertEqual(len(self.local['notices']), 10)
        self.local['limits']['nodes'].update(total=100, truncated=True)
        self.open_detail()
        target = self.page.locator('#relationship-timeline')
        details = target.locator('details.rt-methodology')
        self.expect(details.locator('summary')).to_have_text('统计口径与覆盖说明')
        self.expect(details).not_to_have_attribute('open', '')
        self.expect(details.locator('p')).to_have_count(10)
        self.expect(details.locator('p').first).to_be_hidden()
        self.expect(target.locator('.rt-coverage')).to_be_visible()
        details.locator('summary').click()
        self.expect(details.locator('p').last).to_be_visible()
        self.assertEqual(self.fixture.mutations, [])

    def test_recency_shows_both_sides_without_assuming_one_other_person(self):
        self.open_detail()
        recent = self.page.locator('#relationship-timeline .rt-recency')
        self.expect(recent).to_contain_text('我方最近发言：2026-09-01')
        self.expect(recent).to_contain_text('其他参与者最近发言：2026-08-30')
        self.local['last_contact']['last_other_at'] = None
        self.page.locator('#close-detail').click()
        self.open_detail()
        self.expect(recent).to_contain_text('其他参与者最近发言：未知')

    def test_unfiltered_detail_uses_archive_record_bounds_not_missing_filter_dates(self):
        self.local['scope'].update(date_from=None, date_to=None)
        self.open_detail()
        scope = self.page.locator('#relationship-timeline .rt-scope')
        self.expect(scope).to_contain_text('时间轴统计范围：全归档 ·')
        self.expect(scope).to_contain_text('2026-01-01 — 2026-02-28')
        self.expect(scope).not_to_contain_text('日期未记录')
        self.local['scope'].update(first_record_at=None, last_record_at=None, message_count=0)
        self.page.locator('#close-detail').click()
        self.open_detail()
        self.expect(scope).to_contain_text('日期未记录')
        self.expect(scope).not_to_contain_text('2026-01-01')


if __name__ == '__main__':
    unittest.main()
