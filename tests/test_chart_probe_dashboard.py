"""Dashboard chart probes exercised against invented, local browser fixtures."""

from __future__ import annotations

import unittest
from datetime import datetime

from tests import test_dashboard_ui as dashboard_fixture


@unittest.skipUnless(dashboard_fixture.UI_ENABLED, "Opt-in synthetic browser tests")
class DashboardChartProbeTests(unittest.TestCase):
    """Compose the shared fixture without inheriting its unrelated test cases."""

    @classmethod
    def setUpClass(cls):
        dashboard_fixture.DashboardBrowserTests.setUpClass()
        contacts = dashboard_fixture.DashboardBrowserTests.contacts
        bundle = dashboard_fixture.make_bundle(
            contacts, 0, "吸附图表合成", 5,
            first="2026-08-01", last="2026-08-03")
        dashboard_fixture.write_json(contacts / bundle / "messages.json", {
            "source": "synthetic-ui-fixture", "contact_display": "吸附图表合成",
            "my_display": "合成测试用户", "messages": [
                {"sender": sender, "timestamp": datetime.fromisoformat(date).timestamp(),
                 "type": "text", "content": "纯虚构的吸附图表测试"}
                for sender, date in [
                    ("me", "2026-08-01T09:00:00"),
                    ("me", "2026-08-01T09:01:00"),
                    ("me", "2026-08-01T09:02:00"),
                    ("them", "2026-08-03T20:00:00"),
                    ("them", "2026-08-03T20:01:00"),
                ]
            ],
        })
        dashboard_fixture.write_json(contacts / bundle / "stats.json", {
            "basic": {"total_messages": 5, "my_messages": 3, "their_messages": 2,
                      "date_range": ["2026-08-01", "2026-08-03"]},
            "initiative": {"my_starts": 1, "their_starts": 1},
            "message_length": {"my_avg_chars": 12.5, "their_avg_chars": 22.2},
            "goodnight": {"my_goodnight": 0, "their_goodnight": 0},
        })
        dashboard_fixture.make_bundle(
            contacts, 59, "吸附单日合成", 1,
            first="2026-08-04", last="2026-08-04")

    @classmethod
    def tearDownClass(cls):
        dashboard_fixture.DashboardBrowserTests.tearDownClass()

    def setUp(self):
        self.fixture = dashboard_fixture.DashboardBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.page, self.expect = self.fixture.page, self.fixture.expect
        self.open_bundle("吸附图表合成")

    def tearDown(self):
        self.fixture.tearDown()
        self.assertFalse(any("/api/ai/" in url for _, url in self.fixture.requests))

    def open_bundle(self, name):
        self.fixture.search_names(name, 1)
        self.page.locator("#catalog-body .open-bundle").click()
        self.expect(self.page.locator("#dashboard")).to_be_visible()
        self.expect(self.page.locator("#case-title")).to_have_text(name)

    def hover_svg(self, selector, x, y):
        svg = self.page.locator(selector)
        svg.scroll_into_view_if_needed()
        # Allow the browser's queued scroll event to finish before pointer input.
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        point = svg.evaluate("""(svg, point) => {
            const local = svg.createSVGPoint();
            local.x = point.x; local.y = point.y;
            const screen = local.matrixTransform(svg.getScreenCTM());
            return {x: screen.x, y: screen.y};
        }""", {"x": x, "y": y})
        self.page.mouse.move(point["x"], point["y"])

    def expect_probe(self, label, mine, theirs, total=None):
        tooltip = self.page.locator(".chart-probe-tooltip:visible")
        self.expect(tooltip).to_have_count(1)
        self.expect(tooltip.locator(".chart-probe-title")).to_have_text(label)
        expected_rows = [("你", mine), ("对方 / 其他成员", theirs)]
        if total is not None:
            expected_rows.append(("合计", total))
        for index, (row_label, value) in enumerate(expected_rows):
            row = tooltip.locator(".chart-probe-row").nth(index)
            self.expect(row.locator("dt")).to_have_text(row_label)
            self.expect(row.locator("dd")).to_have_text(value)
        return tooltip

    def test_daily_snaps_blank_space_to_real_dates_and_zero_values(self):
        # This position is well away from both curves and nearer Aug 2 than Aug 1.
        self.hover_svg("#pulse-chart", 340, 60)
        self.expect_probe("2026-08-02", "0 条", "0 条", "0 条")
        self.hover_svg("#pulse-chart", 90, 225)
        self.expect_probe("2026-08-01", "3 条", "0 条", "3 条")
        self.hover_svg("#pulse-chart", 810, 60)
        self.expect_probe("2026-08-03", "0 条", "2 条", "2 条")

    def test_hours_snap_between_bars_and_keep_zero_hours(self):
        # Bar centres are 57 + hour * 22.3; a 9-unit offset lies in the gap.
        self.hover_svg("#hour-chart svg", 57 + 9 * 22.3 + 9, 30)
        self.expect_probe("09:00", "3 条", "0 条", "3 条")
        # Leave the readable card before choosing another point beneath it.
        self.page.mouse.move(0, 0)
        self.expect(self.page.locator(".chart-probe-tooltip:visible")).to_have_count(0)
        self.hover_svg("#hour-chart svg", 57 + 10 * 22.3 + 9, 135)
        self.expect_probe("10:00", "0 条", "0 条", "0 条")
        self.page.mouse.move(0, 0)
        self.expect(self.page.locator(".chart-probe-tooltip:visible")).to_have_count(0)
        self.hover_svg("#hour-chart svg", 57 + 20 * 22.3, 30)
        self.expect_probe("20:00", "0 条", "2 条", "2 条")

    def test_balance_rows_keep_units_decimals_and_zero_counts(self):
        rows = self.page.locator("#balance-list .balance-row")
        self.page.locator("#balance-list").scroll_into_view_if_needed()
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        rows.nth(0).locator(".balance-track").hover()
        self.expect_probe("消息", "3 条", "2 条")
        rows.nth(2).locator(".balance-track").hover()
        self.expect_probe("平均字数", "12.5 字", "22.2 字")
        rows.nth(3).locator(".balance-track").hover()
        self.expect_probe("先说晚安", "0 次", "0 次")

    def test_hours_keep_snapping_when_pointer_crosses_visible_card(self):
        self.hover_svg("#hour-chart svg", 57 + 9 * 22.3 + 9, 30)
        self.expect_probe("09:00", "3 条", "0 条", "3 条")
        self.hover_svg("#hour-chart svg", 57 + 10 * 22.3 + 9, 135)
        self.expect_probe("10:00", "0 条", "0 条", "0 条")

    def test_keyboard_escape_dismisses_probe_without_closing_dashboard(self):
        svg = self.page.locator("#pulse-chart")
        svg.scroll_into_view_if_needed()
        svg.focus()
        self.page.keyboard.press("Home")
        self.expect_probe("2026-08-01", "3 条", "0 条", "3 条")
        self.page.keyboard.press("ArrowRight")
        self.expect_probe("2026-08-02", "0 条", "0 条", "0 条")
        self.page.keyboard.press("End")
        self.expect_probe("2026-08-03", "0 条", "2 条", "2 条")
        self.page.keyboard.press("Escape")
        self.expect(self.page.locator(".chart-probe-tooltip:visible")).to_have_count(0)
        self.expect(self.page.locator("#dashboard")).to_be_visible()

    def test_switching_bundle_drops_old_probe_and_supports_single_day(self):
        self.hover_svg("#hour-chart svg", 57 + 20 * 22.3, 30)
        self.expect_probe("20:00", "0 条", "2 条", "2 条")
        self.page.locator("#close-detail").click()
        self.open_bundle("吸附单日合成")
        self.expect(self.page.locator(".chart-probe-tooltip:visible")).to_have_count(0)
        self.hover_svg("#pulse-chart", 90, 210)
        self.expect_probe("2026-08-04", "1 条", "0 条", "1 条")
        self.page.evaluate("renderPulseChart([])")
        self.expect(self.page.locator(".chart-probe-tooltip:visible")).to_have_count(0)
        self.expect(self.page.locator("#chart-empty")).to_be_visible()
        self.hover_svg("#pulse-chart", 90, 210)
        self.expect(self.page.locator(".chart-probe-tooltip:visible")).to_have_count(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
