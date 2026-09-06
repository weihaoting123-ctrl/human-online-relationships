"""Optional feature admission policy. Disabling never removes data or kills jobs.

Add optional features to DEFINITIONS, then gate each new operation with require.
Core authentication, archive metadata, recovery and historical reads are not flags.
"""
from __future__ import annotations

from dataclasses import dataclass


class ModulePolicyError(ValueError):
    """Safe dependency/conflict message suitable for HTTP 409."""


class ModuleDisabledError(ModulePolicyError):
    pass


@dataclass(frozen=True)
class Module:
    id: str
    label: str
    description: str
    requires: tuple[str, ...] = ()


DEFINITIONS = (
    Module('analysis', 'AI 分析', '允许新建选定范围分析；仍需逐次确认。历史报告始终保留。'),
    Module('media', '图片与附件', '本机媒体归档与浏览，不改变微信原文件。'),
    Module('voice', '语音与转写', '本机语音归档、播放与转写；依赖媒体模块。', ('media',)),
    Module('sync', '同步与导入', '允许定时增量读取和手动文件导入；关闭后不再启动新同步。'),
    Module('backup', '自动与手动备份', '允许创建增量快照；关闭后已有快照、核验与恢复工具仍可用。'),
)
_BY_ID = {item.id: item for item in DEFINITIONS}


class ModuleRegistry:
    def __init__(self, repository):
        self.repository = repository

    def _settings(self):
        raw = self.repository.module_settings()
        if not isinstance(raw, dict) or set(raw) - set(_BY_ID):
            raise RuntimeError('本机模块设置不可用，请检查管理数据库')
        settings = {}
        for definition in DEFINITIONS:
            value = raw.get(definition.id, {'enabled': True, 'version': 0})
            if (not isinstance(value, dict) or type(value.get('enabled')) is not bool
                    or type(value.get('version')) is not int or value['version'] < 0):
                raise RuntimeError('本机模块设置不可用，请检查管理数据库')
            settings[definition.id] = value
        for definition in DEFINITIONS:
            if settings[definition.id]['enabled'] and any(not settings[key]['enabled'] for key in definition.requires):
                raise RuntimeError('本机模块依赖设置不一致，请检查管理数据库')
        return raw, settings

    def snapshot(self):
        _, settings = self._settings()
        return {'status': 'ok', 'modules': [
            {'id': item.id, 'label': item.label, 'description': item.description,
             'requires': list(item.requires), **settings[item.id]} for item in DEFINITIONS
        ], 'health': self.repository.health()}

    def require(self, module_id):
        if module_id not in _BY_ID:
            raise ValueError('未知模块')
        _, settings = self._settings()
        if not settings[module_id]['enabled']:
            raise ModuleDisabledError('该模块已关闭；请先在设置中启用。已有数据未删除。')

    def update(self, request):
        if (not isinstance(request, dict) or set(request) != {'id', 'enabled', 'expected_version'}
                or not isinstance(request['id'], str) or request['id'] not in _BY_ID
                or type(request['enabled']) is not bool or type(request['expected_version']) is not int
                or request['expected_version'] < 0):
            raise ValueError('模块设置格式无效')
        raw, settings = self._settings()
        key, enabled = request['id'], request['enabled']
        if settings[key]['version'] != request['expected_version']:
            raise ModulePolicyError('设置已被其他页面修改，请刷新后重试')
        if enabled:
            if any(not settings[dep]['enabled'] for dep in _BY_ID[key].requires):
                raise ModulePolicyError('请先启用所依赖的模块，再开启此功能')
        elif any(settings[item.id]['enabled'] and key in item.requires for item in DEFINITIONS):
            raise ModulePolicyError('请先关闭依赖此功能的模块；系统不会自动关闭其他功能')
        # The full snapshot is checked inside the repository transaction, so two
        # processes cannot each change a different row and break the dependency.
        self.repository.update_module(key, enabled, request['expected_version'], expected_settings=raw)
        return self.snapshot()
