"""Public-clone copy checks; only parse static HTML, with no runtime data or API."""
from html.parser import HTMLParser
from pathlib import Path
import unittest


class StaticPage(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, source):
        super().__init__()
        self.nodes, self.stack = [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attrs': dict(attrs), 'text': ''}
        self.nodes.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]['tag'] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node['text'] += data

    def with_class(self, name):
        return [node for node in self.nodes if name in node['attrs'].get('class', '').split()]


class PublicUICopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'dashboard' / 'static' / 'index.html'
        cls.page = StaticPage(source.read_text(encoding='utf-8'))

    def test_storage_label_does_not_assume_a_deployment_drive(self):
        self.assertEqual([node['text'] for node in self.page.with_class('storage-label')], ['本机 · 原件保留'])

    def test_sync_copy_requires_user_configuration_instead_of_claiming_a_schedule(self):
        nav = next(node for node in self.page.nodes if node['attrs'].get('data-view') == 'maintenance')
        self.assertIn('按需', nav['text'])
        self.assertNotIn('每日', nav['text'])
        summary = self.page.with_class('incremental-summary')[0]['text']
        self.assertIn('按需配置', summary)
        self.assertIn('手动', summary)
        self.assertIn('定时任务需另行配置', summary)
        self.assertNotIn('20:00', summary)
        self.assertNotIn('Codex', summary)
        maintenance = next(node for node in self.page.nodes if node['attrs'].get('id') == 'maintenance')
        self.assertNotIn('每日检查', maintenance['text'])

    def test_daily_message_chart_keeps_its_real_statistical_meaning(self):
        self.assertIn('每日消息', [node['text'] for node in self.page.with_class('eyebrow')])
        chart = next(node for node in self.page.nodes if node['attrs'].get('id') == 'pulse-chart')
        self.assertEqual(chart['attrs']['aria-label'], '每日消息轨迹')


if __name__ == '__main__':
    unittest.main()
