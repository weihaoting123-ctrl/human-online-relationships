"""Render a local, authenticated voice library; never print transcript text."""
from __future__ import annotations

from datetime import datetime
import html
import json
from pathlib import Path
import re

from message_merge import _atomic_write


def write_gallery(repo, catalog, results, status):
    export = Path(repo) / 'data/exports/wechat-voice'
    export.mkdir(parents=True, exist_ok=True)
    labels = {}
    rows = []
    for row in catalog.execute('SELECT * FROM voice_messages ORDER BY timestamp DESC,id'):
        bundle = row['bundle_id']
        if bundle not in labels:
            # Names stay in this local HTML, never in status or agent output.
            path = Path(repo) / 'data/contacts' / bundle / 'messages.json'
            try:
                from dashboard.search import _safe_path
                if not _safe_path(path, Path(repo) / 'data/contacts'):
                    raise ValueError
                value = json.loads(path.read_text('utf-8-sig'))
                labels[bundle] = str(value.get('contact_display') or value.get('contact_name') or '未命名会话')[:200]
            except (OSError, ValueError, TypeError):
                labels[bundle] = '未命名会话'
        cached = results.execute('SELECT * FROM transcripts WHERE sha=?', (row['sha'],)).fetchone()
        state = row['state']
        text, duration = '', 0
        if state == 'archived':
            state = cached['state'] if cached else 'pending'
            if cached:
                text, duration = cached['text'], cached['duration']
        try:
            timestamp = datetime.fromtimestamp(row['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, OSError, OverflowError):
            timestamp = ''
        sha = row['sha'] or ''
        playable = bool(re.fullmatch('[a-f0-9]{64}', sha) and (export / 'audio' / (sha + '.wav')).is_file())
        rows.append({'contact': labels[bundle], 'date': timestamp, 'state': state,
                     'text': text, 'duration': round(duration, 1),
                     'audio': 'audio/' + sha + '.wav' if playable else ''})
    # Media without a message association are preserved and visible separately.
    for item in catalog.execute("SELECT * FROM assets WHERE sha NOT IN (SELECT sha FROM voice_messages WHERE sha != '')"):
        cached = results.execute('SELECT * FROM transcripts WHERE sha=?', (item['sha'],)).fetchone()
        sha = item['sha']
        rows.append({'contact': '未关联消息的录音', 'date': '', 'state': cached['state'] if cached else 'pending',
                     'text': cached['text'] if cached else '', 'duration': round(cached['duration'], 1) if cached else 0,
                     'audio': 'audio/' + sha + '.wav' if re.fullmatch('[a-f0-9]{64}', sha) and (export / 'audio' / (sha + '.wav')).is_file() else ''})
    payload = json.dumps(rows, ensure_ascii=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    total = len(rows)
    completed = int(status.get('transcribed_audio', 0))
    document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>语音档案 · 人类线上关系可视化</title><link rel="stylesheet" href="/voice-gallery.css"><script src="/voice-gallery.js" defer></script></head>
<body><main><nav><a href="/#maintenance">← 返回本地档案</a><span>本机语音库</span></nav>
<header><p class="eyebrow">VOICE / 声音与文字，一起留下</p><h1>把语音，变成能查找的记录。</h1>
<p class="intro">保留录音原件，用本地模型生成转写。重要内容请播放原音核对，尤其是人名、金额、方言和嘈杂片段。</p>
<div class="metrics"><div><b>{total:,}</b><span>语音记录与独立录音</span></div><div><b>{completed:,}</b><span>已生成文字的独立录音</span></div><div><b>{int(status.get('playable_audio', 0)):,}</b><span>可播放的独立录音</span></div></div></header>
<section class="filters" aria-label="筛选语音"><label class="search"><span>搜索联系人或转写内容</span><input id="voice-query" type="search" placeholder="输入关键词" autocomplete="off"></label>
<label><span>识别状态</span><select id="voice-state"><option value="">所有状态</option><option value="done">已转文字</option><option value="pending">待转写</option><option value="empty">未识别到文字</option><option value="error">转写失败</option><option value="missing">缺少音频</option><option value="ambiguous">关联待确认</option></select></label>
<label><span>开始日期</span><input id="voice-from" type="date"></label><label><span>结束日期</span><input id="voice-to" type="date"></label>
<label><span>排列顺序</span><select id="voice-sort"><option value="newest">最新在前</option><option value="oldest">最早在前</option><option value="longest">时长从长到短</option><option value="contact">联系人名称</option></select></label></section>
<div class="list-heading"><p id="voice-count" role="status"></p><p>录音仅在点击播放后加载</p></div>
<section id="voice-list" class="voice-list" aria-label="语音记录"></section>
<div class="pager"><button id="voice-prev" type="button">上一页</button><span id="voice-page"></span><button id="voice-next" type="button">下一页</button></div>
<footer>自动转写可能有误 · 搜索与转写在本机完成 · 云端分析需另外选择范围并勾选包含语音转写</footer>
<script id="voice-data" type="application/json">{payload}</script></main></body></html>'''
    target = export / 'index.html'
    content = document.encode('utf-8')
    if not target.exists() or target.read_bytes() != content:
        _atomic_write(target, content)
