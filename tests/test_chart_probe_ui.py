"""Offline synthetic browser checks for the shared chart reader."""
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'dashboard' / 'static'


@unittest.skipUnless(os.environ.get('SHE_LOVE_ME_UI_TESTS') == '1', 'Synthetic browser opt-in')
class ChartProbeTests(unittest.TestCase):
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
        self.context = self.browser.new_context(viewport={'width': 1000, 'height': 750},
                                                reduced_motion='reduce', has_touch=True)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors, self.requests = [], []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.on('request', lambda request: self.requests.append(request.url))
        self.page.set_content('''<!doctype html><html><body style="margin:20px">
          <div id="frame" style="width:100%;max-width:700px">
          <svg id="chart" viewBox="0 0 600 200" style="width:100%;display:block" role="img" aria-label="合成轨迹"><path d="M50 100 L300 50 L550 100" fill="none" stroke="blue"/></svg>
          </div><button id="outside">外部</button><div id="rows"><div id="row1">一</div><div id="row2">二</div></div>
        </body></html>''')
        self.page.add_style_tag(path=str(STATIC / 'apple-ui.css'))
        if (STATIC / 'chart-probe.css').exists():
            self.page.add_style_tag(path=str(STATIC / 'chart-probe.css'))
        if (STATIC / 'chart-probe.js').exists():
            self.page.add_script_tag(path=str(STATIC / 'chart-probe.js'))

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.requests, [], 'Probe must not request or upload anything')

    def mount(self, single=False):
        self.assertTrue(self.page.evaluate('Boolean(window.ChartProbe)'), 'Shared probe is not implemented')
        self.page.evaluate('''single => {
          window.fixturePoints = [50,300,550].map((x,i) => ({x,
            label: `2026-01-0${i+1}`, rows:[
              {label:'你',value:`${i*2} 条`,tone:'me'},
              {label:'对方 / 其他成员',value:`${i*3} 条`,tone:'other'},
              {label:'合计',value:`${i*5} 条`}],
            markers:[{y:100-i*10,tone:'me'},{y:100+i*10,tone:'other'}]}));
          window.disposeProbe = ChartProbe.attach(document.querySelector('#chart'),{
            points:single ? [fixturePoints[1]] : fixturePoints,
            plot:{left:50,right:550,top:20,bottom:180}});
        }''', single)

    def move(self, x, y=70):
        point = self.page.locator('#chart').evaluate('''(svg,p) => {
          const q = new DOMPoint(p[0],p[1]).matrixTransform(svg.getScreenCTM());
          return {x:q.x,y:q.y};}''', [x,y])
        self.page.mouse.move(point['x'], point['y'])

    def test_nearest_point_in_blank_space_and_exact_zero_values(self):
        self.mount()
        self.move(360)
        tip = self.page.locator('.chart-probe-tooltip')
        self.expect(tip).to_be_visible()
        self.expect(tip.locator('.chart-probe-title')).to_have_text('2026-01-02')
        self.expect(tip).to_contain_text('2 条')
        self.expect(tip).to_contain_text('3 条')
        self.expect(tip).to_contain_text('5 条')
        self.expect(self.page.locator('.chart-probe-guide')).to_have_attribute('x1','300')
        self.move(52)
        self.expect(tip.locator('.chart-probe-title')).to_have_text('2026-01-01')
        self.assertEqual(tip.locator('dd').all_text_contents(), ['0 条','0 条','0 条'])
        self.page.mouse.move(950, 700)
        self.expect(tip).to_be_hidden()

    def test_keyboard_navigation_and_escape_is_consumed(self):
        self.mount()
        self.page.evaluate("window.outerEscapes=0;document.addEventListener('keydown',e=>{if(e.key==='Escape')outerEscapes++})")
        chart=self.page.locator('#chart')
        chart.focus()
        tip=self.page.locator('.chart-probe-tooltip')
        self.expect(tip).to_be_visible()
        chart.press('End')
        self.expect(tip).to_contain_text('2026-01-03')
        chart.press('ArrowLeft')
        self.expect(tip).to_contain_text('2026-01-02')
        chart.press('Home')
        self.expect(tip).to_contain_text('2026-01-01')
        chart.press('Escape')
        self.expect(tip).to_be_hidden()
        self.expect(chart).to_be_focused()
        self.assertEqual(self.page.evaluate('outerEscapes'),0)
        chart.press('ArrowRight')
        self.expect(tip).to_be_visible()

    def test_single_point_touch_cancel_and_cleanup(self):
        self.mount(single=True)
        chart=self.page.locator('#chart')
        box=chart.bounding_box()
        self.page.touchscreen.tap(box['x']+box['width']*.5,box['y']+box['height']*.5)
        tip=self.page.locator('.chart-probe-tooltip')
        self.expect(tip).to_contain_text('2026-01-02')
        chart.dispatch_event('pointercancel',{'pointerType':'touch'})
        self.expect(tip).to_be_hidden()
        self.page.evaluate('disposeProbe()')
        self.assertEqual(self.page.locator('.chart-probe-tooltip').count(),0)
        self.assertIsNone(chart.get_attribute('tabindex'))
        self.assertEqual(chart.get_attribute('aria-label'),'合成轨迹')
        self.assertEqual(self.page.locator('#frame > #chart').count(),1)

    def test_touch_selects_another_point_through_the_card(self):
        self.mount()
        # Keep desktop floating-card layout but a narrow synthetic host.
        self.page.locator('#frame').evaluate("el => el.style.maxWidth='280px'")
        for x, expected in [(50, '2026-01-01'), (300, '2026-01-02')]:
            point = self.page.locator('#chart').evaluate('''(svg,x) => {
              const p = new DOMPoint(x,100).matrixTransform(svg.getScreenCTM());
              return {x:p.x,y:p.y};}''', x)
            self.page.touchscreen.tap(point['x'], point['y'])
            self.expect(self.page.locator('.chart-probe-tooltip')).to_be_visible()
            self.expect(self.page.locator('.chart-probe-title')).to_have_text(expected)

    def test_touch_on_card_text_keeps_the_new_reading_visible(self):
        self.mount()
        self.page.locator('#frame').evaluate("el => el.style.maxWidth='280px'")
        self.move(52)
        label = self.page.locator('.chart-probe-tooltip dt').nth(1)
        box = label.bounding_box()
        self.page.touchscreen.tap(box['x'] + box['width'] / 2,
                                  box['y'] + box['height'] / 2)
        self.expect(self.page.locator('.chart-probe-title')).to_have_text('2026-01-02')
        self.expect(self.page.locator('.chart-probe-tooltip')).to_be_visible()

    def test_touch_drag_from_card_text_reaches_the_last_point(self):
        self.mount()
        self.page.locator('#frame').evaluate("el => el.style.maxWidth='280px'")
        self.move(52)
        box = self.page.locator('.chart-probe-tooltip dt').nth(1).bounding_box()
        x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
        session = self.context.new_cdp_session(self.page)
        try:
            session.send('Input.dispatchTouchEvent', {
                'type': 'touchStart', 'touchPoints': [{'x': x, 'y': y}]})
            for distance in (10, 30, 60, 90, 120):
                session.send('Input.dispatchTouchEvent', {
                    'type': 'touchMove', 'touchPoints': [{'x': x + distance, 'y': y}]})
            session.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        finally:
            session.detach()
        self.expect(self.page.locator('.chart-probe-title')).to_have_text('2026-01-03')
        self.expect(self.page.locator('.chart-probe-tooltip')).to_be_visible()

    def test_mobile_card_is_below_the_chart_not_over_the_selected_point(self):
        self.page.set_viewport_size({'width':320,'height':750})
        self.mount()
        self.move(300)
        tip = self.page.locator('.chart-probe-tooltip')
        self.expect(tip).to_be_visible()
        chart_box, tip_box = self.page.locator('#chart').bounding_box(), tip.bounding_box()
        self.assertGreaterEqual(tip_box['y'], chart_box['y'] + chart_box['height'])
        self.page.mouse.move(tip_box['x'] + tip_box['width'] / 2,
                             tip_box['y'] + tip_box['height'] / 2, steps=30)
        self.expect(tip).to_be_visible()

    def test_rerender_empty_and_stale_disposer_do_not_remove_new_probe(self):
        self.mount()
        self.move(300)
        self.page.evaluate('''() => {ChartProbe.attach(document.querySelector('#chart'),{
          points:[{x:300,label:'新数据',rows:[{label:'消息',value:'9 条'}]}],
          plot:{left:50,right:550,top:20,bottom:180}});disposeProbe();}''')
        self.move(310)
        self.expect(self.page.locator('.chart-probe-tooltip')).to_contain_text('新数据')
        self.assertEqual(self.page.locator('.chart-probe-tooltip').count(),1)
        self.page.evaluate("ChartProbe.attach(document.querySelector('#chart'),{points:[],plot:{left:50,right:550,top:20,bottom:180}})")
        self.assertEqual(self.page.locator('.chart-probe-tooltip').count(),0)

    def test_small_dark_scaled_chart_literal_labels_and_hoverable_card(self):
        self.mount()
        self.page.set_viewport_size({'width':320,'height':750})
        self.page.emulate_media(color_scheme='dark', reduced_motion='reduce')
        self.page.evaluate('''() => {fixturePoints[2].label='<img src=x onerror=alert(1)>';
          ChartProbe.attach(document.querySelector('#chart'),{points:fixturePoints,
          plot:{left:50,right:550,top:20,bottom:180}});}''')
        self.move(547)
        tip=self.page.locator('.chart-probe-tooltip')
        self.expect(tip).to_contain_text('<img src=x onerror=alert(1)>')
        self.assertEqual(tip.locator('img').count(),0)
        bounds=tip.bounding_box()
        self.assertGreaterEqual(bounds['x'],0)
        self.assertLessEqual(bounds['x']+bounds['width'],320)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),320)
        tip.hover()
        self.expect(tip).to_be_visible()
        bounds = tip.bounding_box()
        self.page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + 5)
        self.expect(tip).to_be_visible()
        self.assertEqual(tip.evaluate('el=>getComputedStyle(el).transitionDuration'),'0s')
        self.page.locator('#outside').click()
        self.expect(tip).to_be_hidden()

    def test_horizontal_rows_nearest_vertical_and_keyboard(self):
        self.assertTrue(self.page.evaluate('Boolean(window.ChartProbe)'), 'Shared probe is not implemented')
        self.page.evaluate('''() => ChartProbe.attachRows(document.querySelector('#rows'),[
          {element:document.querySelector('#row1'),label:'文字',rows:[{label:'消息数',value:'12 条'}]},
          {element:document.querySelector('#row2'),label:'语音',rows:[{label:'消息数',value:'0 条'}]}
        ])''')
        rows=self.page.locator('#rows')
        rows.focus()
        tip=rows.locator('.chart-probe-tooltip')
        self.expect(tip).to_contain_text('文字')
        rows.press('ArrowDown')
        self.expect(tip).to_contain_text('语音')
        self.expect(tip).to_contain_text('0 条')
        box = tip.bounding_box()
        self.page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        self.expect(tip).to_be_visible()
        self.page.evaluate("ChartProbe.detach(document.querySelector('#rows'))")
        self.assertEqual(self.page.locator('.chart-probe-tooltip').count(),0)
