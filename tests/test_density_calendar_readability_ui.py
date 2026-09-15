"""Calendar readability checks using invented aggregates and an offline browser."""
import calendar
import os
import unittest

import test_density_calendar_ui as calendar_fixture


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1', 'Synthetic browser opt-in')
class DensityCalendarReadabilityTests(unittest.TestCase):
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
        self.page.set_default_timeout(2500)
        self.errors, self.requests = [], []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('request', lambda request: self.requests.append(request.url))
        self.page.set_content('''<!doctype html><html lang="zh-CN"><body style="margin:20px">
          <main id="frame" style="width:100%"><figure id="calendar"></figure></main>
          </body></html>''')
        for style in ('relationship-timeline.css', 'density-calendar.css', 'apple-ui.css'):
            self.page.add_style_tag(path=str(calendar_fixture.STATIC / style))
        self.page.add_script_tag(path=str(calendar_fixture.STATIC / 'density-calendar.js'))

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.requests, [], 'Calendar interactions must stay offline')

    def mount(self, value=None, year=False):
        self.page.evaluate('value => DensityCalendar.render(document.getElementById("calendar"), value)',
                           calendar_fixture.fixture() if value is None else value)
        if year:
            self.page.get_by_role('button', name='年视图', exact=True).click()

    def test_each_year_tile_has_visible_weekdays_and_complete_date_numbers(self):
        self.mount(year=True)
        self.expect(self.page.locator('.dc-month')).to_have_count(12)
        for month in range(1, 13):
            with self.subTest(month=month):
                tile = self.page.locator(f'.dc-month[data-month="{month}"]')
                self.assertEqual(tile.evaluate('node => node.tagName'), 'BUTTON')
                labels = tile.locator('.dc-mini-grid .dc-weekday, .dc-mini-grid .dc-mini-weekday')
                self.assertEqual(labels.all_text_contents(), ['一', '二', '三', '四', '五', '六', '日'])
                cells = tile.locator('.dc-mini-day')
                days = calendar.monthrange(2024, month)[1]
                self.assertEqual(cells.all_text_contents(), [str(day) for day in range(1, days + 1)])
                self.assertEqual(cells.evaluate_all('nodes => nodes.map(node => node.dataset.date)'),
                                 [f'2024-{month:02d}-{day:02d}' for day in range(1, days + 1)])
                self.assertTrue(cells.evaluate_all('nodes => nodes.every(node => node.checkVisibility())'))
                first_weekday = calendar.monthrange(2024, month)[0]
                self.assertAlmostEqual(cells.first.bounding_box()['x'],
                                       labels.nth(first_weekday).bounding_box()['x'], delta=1)
                self.expect(tile.locator('button, [tabindex]')).to_have_count(0)

    def test_year_pointer_exposes_exact_count_and_title_without_drilling_down(self):
        self.mount(year=True)
        for date, count in (('2024-02-29', 8), ('2024-02-14', 2), ('2024-02-02', 0)):
            with self.subTest(date=date):
                cell = self.page.locator(f'.dc-mini-day[data-date="{date}"]')
                self.expect(cell).to_have_count(1)
                self.assertIn(f'{date} · {count} 条', cell.get_attribute('title') or '')
                cell.hover()
                self.expect(self.page.locator('.dc-readout')).to_contain_text(f'{date} · {count} 条')
                nearby = self.page.locator('.dc-month[data-month="2"] .dc-month-detail')
                self.expect(nearby).to_be_visible()
                self.expect(nearby).to_contain_text(f'{date} · {count} 条')
                self.expect(self.page.locator('.dc-month')).to_have_count(12)
                self.expect(self.page.get_by_role('button', name='年视图', exact=True)).to_have_attribute('aria-pressed', 'true')

    def test_year_hover_moves_between_months_without_stale_nearby_readouts(self):
        self.mount(year=True)
        for month in (1, 2, 3, 4, 8, 12, 11, 7, 6, 5, 9, 10, 1):
            with self.subTest(month=month):
                date = f'2024-{month:02d}-10'
                cell = self.page.locator(f'.dc-mini-day[data-date="{date}"]')
                cell.hover()
                expected = f'{date} · {4 if month == 1 else 0} 条' if month <= 3 else f'{date} · 未来日期'
                nearby = self.page.locator(f'.dc-month[data-month="{month}"] .dc-month-detail')
                self.expect(nearby).to_contain_text(expected)
                self.expect(self.page.locator('.dc-readout')).to_have_text(nearby.inner_text())
                self.expect(self.page.locator('.dc-pointed')).to_have_count(1)
                self.expect(cell).to_have_class('dc-mini-day dc-pointed')
                inactive = self.page.locator(f'.dc-month:not([data-month="{month}"]) .dc-month-detail')
                self.assertEqual(inactive.all_text_contents(), ['指向日期查看条数'] * 11)
        self.page.locator('.dc-month[data-month="2"]').focus()
        self.expect(self.page.locator('.dc-readout')).to_contain_text('2024 年 2 月 · 11 条')
        self.expect(self.page.locator('.dc-pointed')).to_have_count(0)
        self.assertEqual(self.page.locator('.dc-month-detail').all_text_contents(), ['指向日期查看条数'] * 12)

    def test_year_hover_and_rerender_are_isolated_between_two_instances(self):
        self.mount(year=True)
        second_value = calendar_fixture.fixture()
        second_value['frequency']['daily'][-1]['count'] = 99
        self.page.evaluate('''value => {
          const second = document.createElement('figure'); second.id = 'second';
          document.getElementById('frame').append(second);
          DensityCalendar.render(second, value);
        }''', second_value)
        first = self.page.locator('#calendar')
        second = self.page.locator('#second')
        second.get_by_role('button', name='年视图', exact=True).click()
        first.locator('[data-date="2024-02-29"]').hover()
        second.locator('[data-date="2024-02-29"]').hover()
        for target, count in ((first, 8), (second, 99)):
            self.expect(target.locator('.dc-readout')).to_contain_text(f'2024-02-29 · {count} 条')
            self.expect(target.locator('.dc-month[data-month="2"] .dc-month-detail')).to_contain_text(f'{count} 条')
            self.expect(target.locator('.dc-pointed')).to_have_count(1)
        self.page.evaluate('value => DensityCalendar.render(document.getElementById("second"), value)', second_value)
        self.expect(second.locator('.dc-month-detail')).to_have_count(0)
        self.expect(second.locator('.dc-day')).to_have_count(29)
        self.expect(first.locator('.dc-month')).to_have_count(12)
        self.expect(first.locator('.dc-readout')).to_contain_text('2024-02-29 · 8 条')

    def test_legend_and_soft_unavailable_cells_preserve_distinct_state_meanings(self):
        value = calendar_fixture.fixture()
        value['frequency']['daily_coverage']['date_from'] = '2024-02-10'
        value['frequency']['daily'] = value['frequency']['daily'][2:]
        self.mount(value, year=True)
        legend = self.page.locator('.dc-state-key')
        for meaning in ('0 条', '统计范围外', '未来'):
            self.expect(legend).to_contain_text(meaning)
        for date, state, meaning in (('2024-02-11', 'zero', '0 条'),
                                     ('2024-02-01', 'outside', '统计范围外'),
                                     ('2024-03-16', 'future', '未来日期')):
            with self.subTest(state=state):
                cell = self.page.locator(f'.dc-mini-day[data-date="{date}"]')
                self.expect(cell).to_have_attribute('data-state', state)
                self.assertIn(meaning, cell.get_attribute('title') or '')
                cell.hover()
                self.expect(self.page.locator('.dc-readout')).to_contain_text(meaning)
                if state != 'zero':
                    self.assertNotIn('0 条', cell.get_attribute('title') or '')
                    self.expect(self.page.locator('.dc-readout')).not_to_contain_text('0 条')
        self.expect(self.page.locator('.dc-month[data-month="1"] .dc-month-total')).to_contain_text('统计范围外')
        self.expect(self.page.locator('.dc-month[data-month="4"] .dc-month-total')).to_contain_text('未来日期')
        for mode in ('year', 'month'):
            with self.subTest(mode=mode):
                if mode == 'month':
                    self.page.locator('.dc-month[data-month="3"]').click()
                unavailable = self.page.locator('#calendar [data-state="outside"], #calendar [data-state="future"]')
                styles = unavailable.evaluate_all('''nodes => nodes.map(node => {
                  const style = getComputedStyle(node);
                  return {border: style.borderTopStyle, image: style.backgroundImage};
                })''')
                self.assertTrue(styles)
                self.assertTrue(all(style['border'] not in ('dashed', 'dotted') for style in styles))
                self.assertTrue(all('gradient' not in style['image'] for style in styles))
        self.expect(self.page.locator('[data-date="2024-03-16"] .dc-day-count')).to_have_text('—')

    def test_month_explains_date_and_count_units_and_year_emphasizes_total(self):
        self.mount()
        caption = self.page.locator('.dc-grid-caption')
        self.expect(caption).to_be_visible()
        self.expect(caption).to_contain_text('日期')
        self.assertRegex(caption.inner_text(), r'(条数|消息数|消息数量)[^\n。]*条')
        self.expect(self.page.locator('[data-date="2024-02-29"] .dc-day-number')).to_have_text('29')
        self.expect(self.page.locator('[data-date="2024-02-29"] .dc-day-count')).to_have_text('8')
        self.page.get_by_role('button', name='年视图', exact=True).click()
        total = self.page.locator('.dc-month[data-month="2"] .dc-month-total')
        self.expect(total).to_contain_text('11 条')
        self.expect(total).to_contain_text('3 个活跃日')
        emphasized = total.evaluate(r'''node => [node, ...node.querySelectorAll('*')].some(part => {
          const style = getComputedStyle(part);
          return /^11(?:\s*条)?$/.test(part.textContent.trim())
            && parseFloat(style.fontSize) >= 13 && parseInt(style.fontWeight, 10) >= 600;
        })''')
        self.assertTrue(emphasized, 'The monthly count should be a visible, emphasized number')

    def assert_readable_geometry(self, width):
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width + 1)
        problems = self.page.locator('#calendar').evaluate('''root => {
          const problems = [];
          for (const node of root.querySelectorAll('.dc-mini-day, .dc-day-number, .dc-weekday, .dc-mini-weekday')) {
            const style = getComputedStyle(node);
            const cell = node.closest('.dc-mini-day, .dc-day') || node;
            const bounds = cell.getBoundingClientRect();
            const range = document.createRange(); range.selectNodeContents(node);
            const text = range.getBoundingClientRect();
            if (!node.textContent.trim() || parseFloat(style.fontSize) < 11 || text.width <= 0
                || text.left < bounds.left - 1 || text.right > bounds.right + 1
                || text.top < bounds.top - 1 || text.bottom > bounds.bottom + 1) {
              problems.push({date: cell.dataset.date, text: node.textContent, fontSize: style.fontSize});
            }
          }
          return problems.slice(0, 7);
        }''')
        self.assertEqual(problems, [], 'Date and weekday text must fit its own cell at a readable size')

    def test_readable_dates_at_small_viewports_and_inside_narrow_desktop_parent(self):
        self.mount(year=True)
        for width in (320, 390, 768, 1100):
            with self.subTest(viewport=width):
                self.page.set_viewport_size({'width': width, 'height': 900})
                self.assert_readable_geometry(width)
                widths = self.page.locator('.dc-month').evaluate_all('nodes => nodes.map(node => node.getBoundingClientRect().width)')
                self.assertTrue(all(value >= 219 for value in widths), widths)
        self.page.locator('#frame').evaluate('node => node.style.width = "420px"')
        self.assert_readable_geometry(1100)
        tiles = self.page.locator('.dc-month').evaluate_all('''nodes => nodes.map(node => {
          const rect = node.getBoundingClientRect(); return {left: rect.left, width: rect.width};
        })''')
        self.assertTrue(all(tile['width'] >= 219 for tile in tiles), tiles)
        self.assertEqual(len({round(tile['left']) for tile in tiles}), 1,
                         'A narrow parent at desktop width must use one readable month column')
        self.page.get_by_role('button', name='月视图', exact=True).click()
        self.page.locator('#frame').evaluate('node => node.style.width = "100%"')
        for width in (320, 390, 768, 1100):
            with self.subTest(month_viewport=width):
                self.page.set_viewport_size({'width': width, 'height': 900})
                self.assert_readable_geometry(width)

    def test_keyboard_month_drilldown_and_day_arrow_focus_survive_refinement(self):
        self.mount(year=True)
        february = self.page.locator('.dc-month[data-month="2"]')
        february.focus()
        february.press('Enter')
        first_day = self.page.locator('.dc-day[data-date="2024-02-01"]')
        self.expect(first_day).to_be_focused()
        first_day.press('ArrowDown')
        self.expect(self.page.locator('[data-date="2024-02-08"]')).to_be_focused()
        self.expect(self.page.locator('.dc-readout')).to_contain_text('2024-02-08 · 0 条')
        next_month = self.page.get_by_role('button', name='下一月', exact=True)
        next_month.focus()
        next_month.press('Enter')
        self.expect(next_month).to_be_focused()
        self.expect(self.page.locator('.dc-period-title')).to_have_text('2024 年 3 月')

    def test_tapping_a_miniature_date_and_space_key_both_open_the_month(self):
        self.mount(year=True)
        self.page.locator('.dc-mini-day[data-date="2024-02-29"]').tap()
        self.expect(self.page.locator('.dc-period-title')).to_have_text('2024 年 2 月')
        self.expect(self.page.locator('.dc-day[data-date="2024-02-01"]')).to_be_focused()
        leap_day = self.page.locator('.dc-day[data-date="2024-02-29"]')
        leap_day.tap()
        self.expect(self.page.locator('.dc-readout')).to_contain_text('2024-02-29 · 8 条')
        self.page.get_by_role('button', name='年视图', exact=True).click()
        january = self.page.locator('.dc-month[data-month="1"]')
        january.focus()
        january.press('Space')
        self.expect(self.page.locator('.dc-period-title')).to_have_text('2024 年 1 月')
        self.expect(self.page.locator('.dc-day[data-date="2024-01-01"]')).to_be_focused()


if __name__ == '__main__':
    unittest.main()
