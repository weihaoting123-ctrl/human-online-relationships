"""Manually reconcile public GitHub management seeds, using only the stdlib.

No automatic retries: a failed POST can already have succeeded remotely. Rerun
only after checking GitHub. Existing issues/milestones are never patched, closed
or reopened. Configured label names are owned by this configuration; only their
color/description are reconciled. Nothing writes back to the local config.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

REPOSITORY = 'weihaoting123-ctrl/human-online-relationships'
API = 'https://api.github.com'
BASE = '/repos/' + REPOSITORY
MARKER_PREFIX = '<!-- human-online-relationships:seed:'
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PAGES = 100
CONFIG = Path(__file__).resolve().parents[1] / '.github' / 'project-management.json'


class ManagementError(Exception):
    """Safe, fixed diagnostic; never include response bodies, headers or URLs."""


def require(condition, code='INVALID_CONFIG'):
    if not condition:
        raise ManagementError(code)


def text(value, limit, multiline=False):
    require(isinstance(value, str) and 0 < len(value) <= limit and value == value.strip())
    require(not any(unicodedata.category(char) == 'Cs' or
                    (ord(char) < 32 and not (multiline and char in '\n\r\t')) or
                    ord(char) == 127 for char in value))
    require(MARKER_PREFIX not in value)


def key(value):
    return unicodedata.normalize('NFC', value).casefold().strip()


def unique(values):
    require(len(values) == len(set(map(key, values))))


def keys(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected))


def validate_config(config):
    """Validate the entire closed schema, including every reference, offline."""
    keys(config, ('schema_version', 'repository', 'application_status', 'description', 'labels', 'milestones', 'issues'))
    require(type(config['schema_version']) is int and config['schema_version'] == 1)
    keys(config['repository'], ('owner', 'name'))
    require(config['repository'] == dict(zip(('owner', 'name'), REPOSITORY.split('/'))))
    require(config['application_status'] in ('pending', 'applied'))
    text(config['description'], 4000, multiline=True)
    for collection, maximum in (('labels', 100), ('milestones', 20), ('issues', 50)):
        require(isinstance(config[collection], list) and 0 < len(config[collection]) <= maximum)
    for label in config['labels']:
        keys(label, ('name', 'color', 'description'))
        text(label['name'], 50)
        require(isinstance(label['color'], str) and re.fullmatch('[0-9a-fA-F]{6}', label['color']) is not None)
        text(label['description'], 100)
    for milestone in config['milestones']:
        keys(milestone, ('title', 'description', 'status'))
        text(milestone['title'], 256)
        text(milestone['description'], 60000, multiline=True)
        require(milestone['status'] == 'pending')  # Metadata, never GitHub state.
    for issue in config['issues']:
        keys(issue, ('title', 'body', 'labels', 'milestoneTitle'))
        text(issue['title'], 256)
        # Markdown commonly has a trailing newline; its content must still be valid.
        require(isinstance(issue['body'], str))
        text(issue['body'].strip(), 60000, multiline=True)
        require(len(issue['body']) <= 60000 and not any(ord(c) < 32 and c not in '\r\n\t' for c in issue['body']))
        require(isinstance(issue['labels'], list) and 0 < len(issue['labels']) <= len(config['labels']))
        for label in issue['labels']:
            text(label, 50)
        unique(issue['labels'])
        text(issue['milestoneTitle'], 256)
    names = [label['name'] for label in config['labels']]
    titles = [milestone['title'] for milestone in config['milestones']]
    unique(names)
    unique(titles)
    unique([issue['title'] for issue in config['issues']])
    for issue in config['issues']:
        require(set(issue['labels']) <= set(names) and issue['milestoneTitle'] in titles)
    return config


def validate_context(env):
    require(env.get('GITHUB_REPOSITORY') == REPOSITORY and
            env.get('GITHUB_REF') == 'refs/heads/main' and
            env.get('GITHUB_DEFAULT_BRANCH') == 'main' and
            env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'CONTEXT_REJECTED')
    token = env.get('GITHUB_TOKEN', '')
    require(isinstance(token, str) and bool(token) and token.isascii() and
            not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token), 'TOKEN_UNAVAILABLE')
    return token


def no_duplicate_keys(pairs):
    result = {}
    for name, value in pairs:
        require(name not in result, 'AMBIGUOUS_JSON')
        result[name] = value
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ManagementError('REDIRECT_REJECTED')


class GitHub:
    def __init__(self, token):
        self.token = token
        # Do not forward authorization through an environment-selected proxy.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, payload=None):
        require(method in ('GET', 'POST', 'PATCH') and
                (path == BASE or path.startswith(BASE + '/')) and
                not any(char in path for char in ('\r', '\n', '#', '\\')), 'REQUEST_REJECTED')
        request = Request(API + path, method=method,
                          data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Accept': 'application/vnd.github+json',
                                   'Content-Type': 'application/json',
                                   'X-GitHub-Api-Version': '2026-03-10',
                                   'User-Agent': 'human-online-relationships-management'})
        # There is deliberately no retry loop, including for 429/5xx/timeouts.
        try:
            with self.opener.open(request, timeout=30) as response:
                require(response.status == (201 if method == 'POST' else 200), 'HTTP_STATUS_REJECTED')
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                require(len(raw) <= MAX_RESPONSE_BYTES, 'RESPONSE_LIMIT')
                data = json.loads(raw, object_pairs_hook=no_duplicate_keys)
                return data, response.headers
        except HTTPError as error:
            # Do not read error bodies; they can repeat user text or credentials.
            error.close()
            raise ManagementError('API_HTTP_ERROR' if method == 'GET' else 'MUTATION_UNCONFIRMED') from None
        except (URLError, OSError, ValueError, ManagementError):
            raise ManagementError('API_READ_FAILED' if method == 'GET' else 'MUTATION_UNCONFIRMED') from None

    def all(self, resource):
        require(resource in ('labels', 'milestones', 'issues'), 'REQUEST_REJECTED')
        path = BASE + '/' + resource
        items = []
        for page in range(1, MAX_PAGES + 1):
            query = {'per_page': '100', 'page': str(page)}
            if resource != 'labels':
                query['state'] = 'all'
            if resource == 'issues':
                query.update(sort='created', direction='asc')
            data, headers = self.request('GET', path + '?' + urlencode(query))
            require(isinstance(data, list) and len(data) <= 100 and all(isinstance(item, dict) for item in data), 'INVALID_REMOTE_STATE')
            items.extend(data)
            link = headers.get('Link', '')
            require(isinstance(link, str) and len(link) <= 8192, 'PAGINATION_REJECTED')
            next_links = []
            for part in link.split(',') if link else []:
                match = re.fullmatch(r'\s*<([^>]+)>;\s*rel="([^"]+)"\s*', part)
                require(match is not None, 'PAGINATION_REJECTED')
                if 'next' in match[2].split():
                    next_links.append(match[1])
            require(len(next_links) <= 1, 'PAGINATION_REJECTED')
            if next_links:
                target = urlsplit(next_links[0])
                expected = {name: [value] for name, value in query.items()}
                expected['page'] = [str(page + 1)]
                require(target.scheme == 'https' and target.netloc == 'api.github.com' and
                        target.path == path and not target.fragment and
                        parse_qs(target.query, keep_blank_values=True) == expected and bool(data), 'PAGINATION_REJECTED')
                # Construct the next URL ourselves; never forward a response URL.
            elif len(data) < 100:
                return items
        raise ManagementError('PAGINATION_LIMIT')


def seed_marker(kind, title):
    # Based on config title, not the mutable remote title or Markdown body.
    digest = hashlib.sha256(key(title).encode('utf-8')).hexdigest()[:24]
    return f'{MARKER_PREFIX}{kind}:{digest} -->'


def validate_remote(items, kind):
    seen = set()
    for item in items:
        identifier = item.get('id') if kind == 'label' else item.get('number')
        require(type(identifier) is int and identifier > 0 and identifier not in seen, 'AMBIGUOUS_REMOTE_STATE')
        seen.add(identifier)
        name = item.get('name' if kind == 'label' else 'title')
        require(isinstance(name, str) and bool(name), 'INVALID_REMOTE_STATE')
        if kind == 'label':
            require(isinstance(item.get('color'), str) and re.fullmatch('[0-9a-fA-F]{6}', item['color']) is not None and
                    (item.get('description') is None or isinstance(item['description'], str)), 'INVALID_REMOTE_STATE')
        else:
            require(item.get('state') in ('open', 'closed'), 'INVALID_REMOTE_STATE')
            body = item.get('body' if kind == 'issue' else 'description')
            require(body is None or isinstance(body, str), 'INVALID_REMOTE_STATE')


def match_seed(seed, items, kind):
    marker = seed_marker(kind, seed['title'])
    field = 'body' if kind == 'issue' else 'description'
    matches = []
    for item in items:
        body = item.get(field) or ''
        count = body.count(marker)
        require(count <= 1, 'AMBIGUOUS_REMOTE_STATE')
        same_title = key(item['title']) == key(seed['title'])
        if count or same_title:
            # A title collision cannot adopt a different managed seed.
            require(body.count(MARKER_PREFIX) == count, 'AMBIGUOUS_REMOTE_STATE')
            matches.append(item)
    require(len(matches) <= 1, 'AMBIGUOUS_REMOTE_STATE')
    return matches[0] if matches else None


def plan(config, github):
    labels = github.all('labels')
    milestones = github.all('milestones')
    issues = github.all('issues')
    validate_remote(labels, 'label')
    validate_remote(milestones, 'milestone')
    validate_remote(issues, 'issue')
    issues = [issue for issue in issues if 'pull_request' not in issue]
    label_operations, milestone_matches, issue_matches = [], {}, {}
    for label in config['labels']:
        matches = [item for item in labels if key(item['name']) == key(label['name'])]
        require(len(matches) <= 1, 'AMBIGUOUS_REMOTE_STATE')
        if not matches:
            label_operations.append(('POST', BASE + '/labels', label.copy()))
        elif (matches[0]['color'].lower(), matches[0].get('description') or '') != (label['color'].lower(), label['description']):
            label_operations.append(('PATCH', BASE + '/labels/' + quote(matches[0]['name'], safe=''),
                                     {'color': label['color'], 'description': label['description']}))
    for milestone in config['milestones']:
        milestone_matches[milestone['title']] = match_seed(milestone, milestones, 'milestone')
    for issue in config['issues']:
        issue_matches[issue['title']] = match_seed(issue, issues, 'issue')
    return label_operations, milestone_matches, issue_matches


def apply_config(config, env, *, apply=False):
    validate_config(config)  # All config validation is before even constructing a client.
    token = validate_context(env)
    github = GitHub(token)
    repository, _ = github.request('GET', BASE)
    require(isinstance(repository, dict) and repository.get('full_name') == REPOSITORY and
            repository.get('default_branch') == 'main' and repository.get('private') is False, 'REPOSITORY_REJECTED')
    operations, milestones, issues = plan(config, github)
    planned = {'labels_create': sum(op[0] == 'POST' for op in operations),
               'labels_update': sum(op[0] == 'PATCH' for op in operations),
               'milestones_create': sum(value is None for value in milestones.values()),
               'issues_create': sum(value is None for value in issues.values())}
    result = {'mode': 'apply' if apply else 'plan', 'planned': planned, 'confirmed_mutations': 0}
    if not apply:
        return result
    try:
        for method, path, payload in operations:
            github.request(method, path, payload)
            result['confirmed_mutations'] += 1
        for seed in config['milestones']:
            if milestones[seed['title']] is None:
                created, _ = github.request('POST', BASE + '/milestones', {
                    'title': seed['title'], 'description': seed['description'] + '\n\n' + seed_marker('milestone', seed['title']),
                    'state': 'open'})
                require(isinstance(created, dict), 'MUTATION_UNCONFIRMED')
                validate_remote([created], 'milestone')
                require(match_seed(seed, [created], 'milestone') is not None, 'MUTATION_UNCONFIRMED')
                milestones[seed['title']] = created
                result['confirmed_mutations'] += 1
        for seed in config['issues']:
            if issues[seed['title']] is None:
                github.request('POST', BASE + '/issues', {
                    'title': seed['title'], 'body': seed['body'].rstrip() + '\n\n' + seed_marker('issue', seed['title']),
                    'labels': seed['labels'], 'milestone': milestones[seed['milestoneTitle']]['number']})
                result['confirmed_mutations'] += 1
        # Read-back catches ambiguous concurrent creation; it never marks local
        # application_status, roadmap checkboxes, releases, or issue states done.
        remaining_labels, final_milestones, final_issues = plan(config, github)
        require(not remaining_labels and all(final_milestones.values()) and all(final_issues.values()), 'READBACK_UNCONFIRMED')
        result['readback_verified'] = True
        return result
    except ManagementError:
        raise ManagementError(f'APPLY_UNCONFIRMED; confirmed mutations: {result["confirmed_mutations"]}; inspect GitHub before rerun') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Apply mutations; otherwise only read and plan.')
    parser.add_argument('--validate-only', action='store_true', help='Validate the fixed config offline, without a token.')
    args = parser.parse_args(argv)
    try:
        require(not (args.apply and args.validate_only), 'ARGUMENTS_REJECTED')
        require(CONFIG.stat().st_size <= 128 * 1024, 'CONFIG_LIMIT')
        config = json.loads(CONFIG.read_text(encoding='utf-8'), object_pairs_hook=no_duplicate_keys)
        validate_config(config)
        result = {'configuration': 'valid'} if args.validate_only else apply_config(config, os.environ, apply=args.apply)
        print(json.dumps(result, sort_keys=True))
        return 0
    except ManagementError as error:
        print(str(error), file=sys.stderr)
    except Exception:
        # File errors/tracebacks may contain a machine path; keep logs fixed.
        print('LOCAL_OR_INTERNAL_ERROR', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
