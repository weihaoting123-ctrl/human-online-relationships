"""Application metadata service: source summaries in, managed views out.

No message parsing, credentials, network or HTTP handling belongs in this layer.
"""
from dashboard.library import (LibraryRepository, LibraryValidationError,
                               LibraryNotFoundError)
from dashboard.modules import ModulePolicyError


class LibraryService:
    def __init__(self, data_dir, summary_reader):
        self.repository = LibraryRepository(data_dir)
        self.summary_reader = summary_reader

    def snapshot(self):
        # Reconcile receives the complete successful scan, never a partial list.
        # Source refresh and metadata edits have separate column ownership.
        self.repository.reconcile(self.summary_reader())
        return {'status': 'ok', **self.repository.snapshot()}

    def visible(self):
        snapshot = self.snapshot()
        names = {tag['id']: tag['name'] for tag in snapshot['tags'] if not tag['deleted']}
        bundles = []
        for item in snapshot['conversations']:
            if item['hidden_at'] or item.get('source_missing'):
                continue
            metadata = {key: item[key] for key in ('alias', 'note', 'pinned', 'hidden_at', 'version', 'tags')}
            metadata['tag_names'] = [names[key] for key in item['tags'] if key in names]
            bundles.append({**item['source'], 'library': metadata})
        return sorted(bundles, key=lambda item: item.get('updated_at') or '', reverse=True)

    def require_visible(self, bundle_id):
        # This check also rejects stale previews for newly hidden conversations.
        if not isinstance(bundle_id, str):
            raise LibraryValidationError('会话标识无效')
        items = self.snapshot()['conversations']
        item = next((value for value in items if value['bundle_id'] == bundle_id), None)
        if item is None or item.get('source_missing'):
            raise LibraryNotFoundError('会话来源暂不可用')
        if item['hidden_at']:
            raise ModulePolicyError('此会话在回收站中，请先恢复后再分析')

    def update_conversation(self, request):
        if not isinstance(request, dict) or not {'bundle_id','expected_version'} <= set(request):
            raise LibraryValidationError('会话管理请求格式无效')
        allowed = {'bundle_id','expected_version','alias','note','pinned','hidden','tag_ids'}
        if set(request) - allowed:
            raise LibraryValidationError('仅支持别名、备注、标签、置顶和回收站操作')
        self.snapshot()
        patch = {key:value for key,value in request.items() if key not in {'bundle_id','expected_version'}}
        return {'status':'ok', 'conversation': self.repository.update_conversation(
            request['bundle_id'], request['expected_version'], patch)}

    def update_tag(self, request):
        if not isinstance(request, dict):
            raise LibraryValidationError('标签请求格式无效')
        if 'id' not in request:
            if set(request) != {'name','color'}:
                raise LibraryValidationError('标签请求格式无效')
            tag = self.repository.create_tag(request['name'], request['color'])
        else:
            if not {'id','expected_version'} <= set(request) or set(request) - {'id','expected_version','name','color','deleted'}:
                raise LibraryValidationError('标签请求格式无效')
            patch = {key:value for key,value in request.items() if key not in {'id','expected_version'}}
            tag = self.repository.update_tag(request['id'], request['expected_version'], patch)
        return {'status':'ok', 'tag':tag}
