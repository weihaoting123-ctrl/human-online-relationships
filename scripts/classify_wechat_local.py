"""Deterministic first-pass classification. Reads chats only inside this process.

No network or model calls. Report labels are candidates, never psychological
judgments; no message quotations are included. All outputs stay in this repo.
"""
from __future__ import annotations
import html
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path

REPO=Path(__file__).resolve().parents[1]
TOPICS={
    '商户与业务合作':('商户','门店','入驻','费率','结算','招商','代理商','渠道合作','商务合作'),
    '工作与项目协作':('项目','需求','进度','方案','会议','排期','测试','上线','同事','交付','汇报'),
    '交易与物流售后':('订单','快递','退款','收货','发货','运单','售后','物流','退货','包裹'),
    '付款与账务':('付款','转账','收款','发票','报销','账单','工资','对账','欠款','打款'),
    '生活与社交':('有空','见面','吃饭','朋友','生日','晚安','早安','周末','聚会','到家'),
}
RULESET_VERSION = 3
MIN_TOPIC_HITS = 5
DOMINANCE_RATIO = 1.4


def classify_messages(messages,kind='direct'):
    scores=Counter()
    media=Counter()
    for message in messages:
        text=str(message.get('content') or '')
        if message.get('type') == 'voice':
            text += '\n' + str(message.get('transcript') or message.get('voice_transcript') or '')
        for category,words in TOPICS.items():
            if any(word in text for word in words): scores[category]+=1
        media[str(message.get('type') or 'unknown')]+=1
    ranked=scores.most_common()
    primary='待确认／低信号'
    if ranked and ranked[0][1]>=MIN_TOPIC_HITS:
        primary=ranked[0][0] if len(ranked)==1 or ranked[0][1]>=DOMINANCE_RATIO*ranked[1][1] else '混合主题'
    return {'method':'本机固定关键词候选分类，不是人工确认或心理判断',
            'conversation_kind':kind,'primary_topic':primary,
            'topic_hits':dict(scores),'candidate_topics':[name for name,n in ranked if n>=MIN_TOPIC_HITS],
            'message_count':len(messages),'message_types':dict(media)}


def rules_fingerprint():
    # Bump RULESET_VERSION whenever classification semantics change.
    value = [RULESET_VERSION, MIN_TOPIC_HITS, DOMINANCE_RATIO, TOPICS]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False,
                                     sort_keys=True).encode('utf-8')).hexdigest()


def file_signature(path):
    from archive_wechat_local import fingerprint, is_link
    if is_link(path) or not path.is_file():
        raise ValueError('classification_input_invalid')
    return list(fingerprint(path))


def input_signature(bundle, rules):
    manifest = bundle / 'dashboard_manifest.json'
    return {'messages': file_signature(bundle / 'messages.json'),
            'manifest': file_signature(manifest) if manifest.exists() else None,
            'rules': rules}


def read_checkpoint(path):
    try:
        checkpoint = json.loads(path.read_text(encoding='utf-8'))
        if checkpoint.get('schema_version') == 1 and isinstance(checkpoint.get('bundles'), dict):
            return checkpoint
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {'schema_version': 1, 'bundles': {}}


def cached_result_valid(entry):
    if not isinstance(entry, dict):
        return False
    row, result = entry.get('row'), entry.get('result')
    if not isinstance(row, dict) or not isinstance(result, dict):
        return False
    return (isinstance(row.get('contact'), str)
            and row.get('kind') == result.get('conversation_kind')
            and row.get('primary_topic') == result.get('primary_topic')
            and row.get('candidate_topics') == result.get('candidate_topics')
            and isinstance(row.get('message_count'), int)
            and row['message_count'] >= 0
            and row['message_count'] == result.get('message_count')
            and isinstance(result.get('primary_topic'), str)
            and isinstance(result.get('candidate_topics'), list)
            and all(isinstance(topic, str) for topic in result['candidate_topics']))


def run(repo=None):
    from archive_wechat_local import archive_lock
    repo = repo or REPO
    if repo.drive.upper()=='C:': raise ValueError('non_c_project_required')
    with archive_lock(repo/'data/private/wechat-classification'):
        return run_locked(repo)


def run_locked(repo):
    from archive_wechat_local import atomic_json
    contacts=repo/'data/contacts'
    reports=repo/'data/exports/classification'
    reports.mkdir(parents=True,exist_ok=True)
    checkpoint_path = repo/'data/private/wechat-classification/checkpoint.json'
    checkpoint = read_checkpoint(checkpoint_path)
    rules = rules_fingerprint()
    totals=Counter()
    kinds=Counter()
    rows=[]
    messages_total=0
    failed=0
    processed=0
    skipped=0
    processed_messages=0
    current_bundles=[]
    for bundle in sorted(contacts.iterdir()):
        if not bundle.is_dir() or bundle.is_symlink() or not (bundle/'messages.json').is_file(): continue
        try:
            current_bundles.append(bundle.name)
            signature = input_signature(bundle, rules)
            previous = checkpoint['bundles'].get(bundle.name)
            classification_path = bundle/'classification.json'
            classification_signature = file_signature(classification_path) if classification_path.exists() else None
            if (cached_result_valid(previous) and previous.get('input') == signature
                    and classification_signature is not None
                    and previous.get('classification') == classification_signature):
                result, row = previous['result'], previous['row']
                # Recheck the cheap signatures before accepting the cache.
                if input_signature(bundle, rules) != signature:
                    raise ValueError('classification_input_changed')
                skipped += 1
                totals[result['primary_topic']] += 1
                kinds[row['kind']] += 1
                messages_total += row['message_count']
                rows.append(row)
                continue
            payload=json.loads((bundle/'messages.json').read_text(encoding='utf-8'))
            manifest=json.loads((bundle/'dashboard_manifest.json').read_text(encoding='utf-8')) if (bundle/'dashboard_manifest.json').exists() else {}
            messages=payload.get('messages',[])
            if not isinstance(messages, list) or not all(isinstance(message, dict) for message in messages):
                raise ValueError('classification_messages_invalid')
            kind=manifest.get('conversation_kind','direct')
            result=classify_messages(messages,kind)
            if input_signature(bundle, rules) != signature:
                raise ValueError('classification_input_changed')
            atomic_json(classification_path,result)
            totals[result['primary_topic']]+=1
            kinds[kind]+=1
            messages_total+=len(messages)
            row = {'contact':str(manifest.get('contact') or payload.get('contact') or bundle.name),
                         'kind':kind,'primary_topic':result['primary_topic'],
                         'message_count':len(messages),'candidate_topics':result['candidate_topics']}
            rows.append(row)
            checkpoint['bundles'][bundle.name] = {'input': signature,
                'classification': file_signature(classification_path), 'result': result, 'row': row}
            processed += 1
            processed_messages += len(messages)
        except (OSError,ValueError,TypeError,AttributeError): failed+=1
    rows_digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False,
                                           sort_keys=True).encode('utf-8')).hexdigest()
    rebuild = (bool(processed) or bool(failed)
               or checkpoint.get('report_bundles') != current_bundles
               or checkpoint.get('report_rows_digest') != rows_digest
               or not (reports/'index.html').is_file()
               or not (reports/'private-conversation-categories.json').is_file())
    summary={'state':'completed' if not failed else 'partial','classified_conversations':len(rows),'classified_messages':messages_total,'failed_conversations':failed,'processed_conversations':processed,'skipped_conversations':skipped,'processed_messages':processed_messages,'incremental':True,'report_rebuilt':rebuild,'topic_counts':dict(totals),'conversation_kinds':dict(kinds),'method':'本机固定规则初筛，标签待人工确认','updated_at':datetime.now(timezone.utc).isoformat()}
    if rebuild:
        atomic_json(reports/'private-conversation-categories.json',rows)
        write_report(reports, rows, totals, messages_total)
        checkpoint['report_bundles'] = current_bundles
        checkpoint['report_rows_digest'] = rows_digest
    atomic_json(checkpoint_path, checkpoint)
    atomic_json(repo/'data/classification-status.json',summary)
    print(json.dumps(summary,ensure_ascii=False))
    return 0 if not failed else 1


def write_report(reports, rows, totals, messages_total):
    body=''.join('<tr><td>'+html.escape(r['contact'])+'</td><td>'+('群聊' if r['kind']=='group' else '私聊／服务会话')+'</td><td>'+html.escape(r['primary_topic'])+'</td><td>'+str(r['message_count'])+'</td><td>'+html.escape('、'.join(r['candidate_topics']))+'</td></tr>' for r in rows)
    cards=''.join(f'<li>{html.escape(k)}：{v} 个会话</li>' for k,v in totals.items())
    report='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'"><title>微信会话第一轮分类</title><style>body{background:#f3f0e9;color:#233c35;max-width:1200px;margin:48px auto;padding:24px;font:16px system-ui;line-height:1.8}table{width:100%;border-collapse:collapse;background:white}td,th{padding:12px;text-align:left;border-bottom:1px solid #ddd}h1{font-size:36px}a{color:#245d51}</style><h1>微信会话 · 第一轮分类</h1><p>共 '+str(len(rows))+' 个会话，'+format(messages_total,',')+' 条消息。标签由本机固定关键词规则生成，允许多标签；低信号保留待确认，不代表对联系人身份或关系的结论。</p><p>本报告包含联系人称呼，仅供本机查看。</p><ul>'+cards+'</ul><p><a href="../wechat-media/index.html">打开图片分类与月份相册</a></p><table><tr><th>会话</th><th>类型</th><th>初筛主题</th><th>消息数</th><th>候选标签</th></tr>'+body+'</table></html>'
    (reports/'index.html').write_text(report,encoding='utf-8')


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    try: raise SystemExit(run())
    except Exception:
        print(json.dumps({'state':'error','error_code':'local_classification_failed'}))
        raise SystemExit(1)
