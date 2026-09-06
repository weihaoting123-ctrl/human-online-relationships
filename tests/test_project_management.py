"""Offline contract tests. All GitHub responses are synthetic; no token/network."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'apply_project_management.py'
SPEC = importlib.util.spec_from_file_location('project_management', SCRIPT)
PM = importlib.util.module_from_spec(SPEC) if SCRIPT.exists() else None
if PM is not None:
    SPEC.loader.exec_module(PM)
REPO = 'weihaoting123-ctrl/human-online-relationships'
ENV = {'GITHUB_REPOSITORY': REPO, 'GITHUB_REF': 'refs/heads/main',
       'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_DEFAULT_BRANCH': 'main',
       'GITHUB_TOKEN': 'synthetic-token-not-a-credential'}


class Response:
    def __init__(self, data, headers=None, status=200):
        self.data = json.dumps(data).encode()
        self.headers = headers or {}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size=-1):
        return self.data if size < 0 else self.data[:size]


class Remote:
    def __init__(self):
        self.calls = []
        self.labels, self.milestones, self.issues = [], [], []
        self.repo = {'full_name': REPO, 'default_branch': 'main', 'private': False}
        self.page_size = 100

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.method, url.path, parse_qs(url.query), body))
        assert url.scheme == 'https' and url.netloc == 'api.github.com'
        assert timeout == 30
        base = '/repos/' + REPO
        if request.method == 'GET' and url.path == base:
            return Response(self.repo)
        kind = url.path[len(base) + 1:].split('/')[0]
        values = getattr(self, kind)
        if request.method == 'GET':
            query = parse_qs(url.query)
            if kind in ('milestones', 'issues'):
                assert query['state'] == ['all']
            page = int(query['page'][0])
            start = (page - 1) * self.page_size
            headers = {}
            if start + self.page_size < len(values):
                next_query = {name: values[0] for name, values in query.items()}
                next_query['page'] = str(page + 1)
                next_url = 'https://api.github.com' + url.path + '?' + urlencode(next_query)
                headers['Link'] = '<' + next_url + '>; rel="next"'
            return Response(values[start:start + self.page_size], headers)
        if request.method == 'POST':
            item = copy.deepcopy(body)
            item.update(id=1000 + len(values), number=100 + len(values))
            if kind in ('milestones', 'issues'):
                item.setdefault('state', 'open')
            values.append(item)
            return Response(item, status=201)
        if request.method == 'PATCH' and kind == 'labels':
            from urllib.parse import unquote
            name = unquote(url.path.rsplit('/', 1)[-1])
            item = next(value for value in values if value['name'] == name)
            item.update(body)
            return Response(item)
        raise AssertionError('Unexpected mutation')


class ProjectManagementTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(PM, 'The guarded project-management script is not implemented')
        self.config = json.loads((ROOT / '.github/project-management.json').read_text(encoding='utf-8'))
        self.remote = Remote()

    def run_apply(self, config=None, env=None, apply=True):
        with patch.object(PM, 'build_opener', return_value=self.remote):
            return PM.apply_config(config or self.config, env or ENV, apply=apply)

    def writes(self):
        return [call for call in self.remote.calls if call[0] != 'GET']

    def test_wrong_repository_and_untrusted_dispatch_context_make_zero_requests(self):
        for field, value in [('GITHUB_REPOSITORY', 'someone/fork'),
                             ('GITHUB_REF', 'refs/heads/feature'),
                             ('GITHUB_DEFAULT_BRANCH', 'develop'),
                             ('GITHUB_EVENT_NAME', 'push'), ('GITHUB_TOKEN', '')]:
            with self.subTest(field=field):
                with self.assertRaises(PM.ManagementError):
                    self.run_apply(env={**ENV, field: value})
                self.assertEqual(self.remote.calls, [])

    def test_all_configuration_validation_happens_before_any_request(self):
        mutations = [
            lambda c: c.update(schema_version=True),
            lambda c: c['repository'].update(owner='someone'),
            lambda c: c.update(unexpected=True),
            lambda c: c['labels'][0].update(color='not-hex'),
            lambda c: c['labels'].append(copy.deepcopy(c['labels'][0])),
            lambda c: c['milestones'][0].update(status='closed'),
            lambda c: c['issues'][-1].update(labels=['not-configured']),
            lambda c: c['issues'][-1].update(milestoneTitle='not-configured'),
            lambda c: c['issues'][-1].update(body='x\x00y'),
            lambda c: c['issues'][0].update(title='x\ny'),
            lambda c: c['issues'][0].update(body='<!-- human-online-relationships:seed:fake -->'),
        ]
        for mutate in mutations:
            config = copy.deepcopy(self.config)
            mutate(config)
            with self.subTest(config=mutations.index(mutate)):
                with self.assertRaises(PM.ManagementError):
                    self.run_apply(config=config)
                self.assertEqual(self.remote.calls, [])

    def test_remote_repository_identity_checked_before_mutations(self):
        self.remote.repo['full_name'] = 'someone/renamed'
        with self.assertRaises(PM.ManagementError):
            self.run_apply()
        self.assertEqual(len(self.remote.calls), 1)
        self.assertEqual(self.writes(), [])

    def test_default_plan_reads_but_never_writes(self):
        summary = self.run_apply(apply=False)
        self.assertEqual(self.writes(), [])
        self.assertEqual(summary['planned'], {'labels_create': 20, 'labels_update': 0,
                                             'milestones_create': 2, 'issues_create': 5})

    def test_creates_expected_resources_and_rerun_is_idempotent(self):
        self.run_apply()
        self.assertEqual((len(self.remote.labels), len(self.remote.milestones), len(self.remote.issues)), (20, 2, 5))
        self.assertEqual(len(self.writes()), 27)
        for issue in self.remote.issues:
            self.assertIn('<!-- human-online-relationships:seed:issue:', issue['body'])
            self.assertIn(issue['milestone'], [m['number'] for m in self.remote.milestones])
        self.remote.calls.clear()
        self.run_apply()
        self.assertEqual(self.writes(), [])

    def test_pagination_includes_closed_and_preserves_human_edits(self):
        self.run_apply()
        self.remote.page_size = 1
        self.remote.labels.append({'id': 9000, 'name': 'human-only', 'color': '000000', 'description': 'human'})
        self.remote.issues[0].update(title='Renamed by a human', state='closed', labels=['human-only'], milestone=None)
        self.remote.issues[0]['body'] += '\nHuman completion notes.'
        self.remote.milestones[0].update(title='Human milestone name', state='closed')
        self.remote.milestones[0]['description'] += '\nHuman milestone notes.'
        before = copy.deepcopy((self.remote.labels, self.remote.milestones, self.remote.issues))
        self.remote.calls.clear()
        self.run_apply()
        self.assertEqual(self.writes(), [])
        self.assertEqual(before, (self.remote.labels, self.remote.milestones, self.remote.issues))
        self.assertTrue(any(call[2].get('page') == ['2'] for call in self.remote.calls))

    def test_only_configured_label_color_description_updated_not_name(self):
        self.run_apply()
        self.remote.labels[0].update(color='000000', description='outdated')
        self.remote.calls.clear()
        self.run_apply()
        writes = self.writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], 'PATCH')
        self.assertEqual(set(writes[0][3]), {'color', 'description'})

    def test_existing_unmarked_titles_are_preserved_and_pull_requests_are_not_seeds(self):
        self.remote.issues = [
            {'id': 1, 'number': 1, 'title': self.config['issues'][0]['title'], 'body': 'Human edited', 'state': 'closed'},
            {'id': 2, 'number': 2, 'title': self.config['issues'][1]['title'], 'body': '', 'state': 'open', 'pull_request': {}},
        ]
        self.run_apply()
        self.assertEqual(self.remote.issues[0]['body'], 'Human edited')
        self.assertEqual(self.remote.issues[0]['state'], 'closed')
        self.assertEqual(len([call for call in self.writes() if call[1].endswith('/issues')]), 4)

    def test_ambiguity_anywhere_blocks_all_mutations(self):
        title = self.config['issues'][-1]['title']
        self.remote.issues = [{'id': i, 'number': i, 'title': title, 'body': '', 'state': 'closed'} for i in (1, 2)]
        with self.assertRaises(PM.ManagementError):
            self.run_apply()
        self.assertEqual(self.writes(), [])

    def test_duplicate_marker_and_marker_title_conflict_are_rejected(self):
        self.run_apply()
        self.remote.calls.clear()
        self.remote.issues[0]['body'] += '\n' + PM.seed_marker('issue', self.config['issues'][0]['title'])
        with self.assertRaises(PM.ManagementError):
            self.run_apply()
        self.assertEqual(self.writes(), [])

    def test_cross_host_pagination_is_rejected_without_following_or_writing(self):
        original = self.remote.open
        def redirect_link(request, timeout):
            result = original(request, timeout)
            if request.full_url.endswith('labels?per_page=100&page=1'):
                result.headers['Link'] = '<https://invalid.example/steal>; rel="next"'
            return result
        with patch.object(self.remote, 'open', side_effect=redirect_link):
            with self.assertRaises(PM.ManagementError):
                self.run_apply()
        self.assertEqual(self.writes(), [])

    def test_http_and_transport_errors_redacted_and_post_never_retried(self):
        original = self.remote.open
        for error in (HTTPError('https://secret.invalid', 422, 'sensitive-response', {}, io.BytesIO(b'secret-body')),
                      URLError('sensitive-proxy-and-token')):
            self.remote.calls.clear()
            def fail_post(request, timeout):
                if request.method == 'POST':
                    self.remote.calls.append(('POST', '', {}, None))
                    raise error
                return original(request, timeout)
            with patch.object(self.remote, 'open', side_effect=fail_post):
                with self.assertRaises(PM.ManagementError) as caught:
                    self.run_apply()
            self.assertEqual(len(self.writes()), 1)
            self.assertNotIn('sensitive', str(caught.exception))
            self.assertNotIn('secret', str(caught.exception))
            self.assertIn('UNCONFIRMED', str(caught.exception))

    def test_transport_denies_redirects(self):
        request = PM.Request('https://api.github.com/repos/' + REPO)
        handler = PM.NoRedirect()
        with self.assertRaises(PM.ManagementError):
            handler.redirect_request(request, None, 302, 'secret', {}, 'https://invalid.example')

    def test_workflow_is_manual_guarded_and_uses_pinned_actions(self):
        path = ROOT / '.github/workflows/project-management.yml'
        self.assertTrue(path.exists(), 'Manual workflow is missing')
        source = path.read_text(encoding='utf-8')
        for text in ('workflow_dispatch:', "refs/heads/main", REPO, "default_branch == 'main'",
                     'contents: read', 'issues: write', 'persist-credentials: false',
                     'cancel-in-progress: false', 'default: false',
                     'actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd',
                     'actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405'):
            self.assertIn(text, source)
        for forbidden in ('pull_request:', 'pull_request_target:', 'schedule:', 'push:', 'upload-artifact', 'pip install'):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
