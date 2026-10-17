"""Local derived summaries and append-only observations; no platform operations."""
import argparse
import copy
import hashlib
import html
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import batch_plan
import quality_support
from quality_support import PROTECTED, required, timestamp, no_resubmit, local_records
from selector_support import ROOT, atomic, file_hash, key, read


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name+'.lock')
    with lock.open('x', encoding='utf-8'):
        pass
    try:
        yield
    finally:
        lock.unlink()


def initialize(card_path, *, task_context, at=None):
    """Record startup before environment checks; resume never resets task truth."""
    at = at or now()
    timestamp(at)
    if not isinstance(task_context, dict):
        raise ValueError('task_context须为对象')
    required(task_context.get('task_id'), 'task_id')
    card_path = local(card_path)
    with locked(card_path):
        existed = card_path.exists()
        if not existed and any(p.name != card_path.name + '.lock' for p in card_path.parent.iterdir()):
            raise ValueError('任务卡缺失但已有产物或记录；须先只读核实接续，不能初始化为新任务')
        if not existed and card_path.with_name('publish-result.json').exists():
            raise ValueError('已有发布结果但任务卡缺失，须只读核实接续，不能初始化为未提交')
        card = document(card_path) if existed else {
            'task_context': copy.deepcopy(task_context), 'status': 'NOT_UPLOADED_NOT_SUBMITTED',
            'submitted': False, 'started_at': at, 'next_action': '检查当前环境与原始素材'}
        context = card.get('task_context')
        if not isinstance(context, dict) or any(context.get(k) != v for k, v in task_context.items()):
            raise ValueError('接续任务身份或上下文不匹配；初始化不改写已有任务')
        metrics_path = local(card.get('run_metrics_ref') or context.get('run_metrics_ref')
                             or 'run-metrics.json', card_path.parent)
        if metrics_path.exists():
            metrics = document(metrics_path)
            # Legacy batches may intentionally share one metrics file.
            allowed_ids = {context['task_id']}
            batch_ref = context.get('batch_ref') or {}
            if batch_ref.get('plan_path'):
                allowed_ids.add(document(local(batch_ref['plan_path'], card_path.parent)).get('batch_id'))
            if metrics.get('task_id') not in allowed_ids:
                raise ValueError('run-metrics任务身份不匹配')
            if metrics.get('started_at'):
                timestamp(metrics['started_at'])
            if not existed:
                # Recover a crash between metrics creation and initial card creation.
                if metrics.get('initialized_card') != str(card_path):
                    raise ValueError('已有计时记录不能作为新任务覆盖；请核实接续记录')
                card['started_at'] = metrics.get('started_at')
        else:
            metrics = {'task_id': context['task_id'], 'started_at': card.get('started_at'),
                       'initialized_card': str(card_path), 'record_events': []}
            if existed:
                metrics['resume_observed_at'] = at
            atomic(metrics_path, metrics)
        updated = copy.deepcopy(card)
        updated.setdefault('started_at', metrics.get('started_at') if metrics.get('task_id') == context['task_id'] else None)
        updated.setdefault('run_metrics_ref', str(metrics_path))
        if not existed or updated != card:
            atomic(card_path, updated)
        return updated


PRE_SUBMISSION = {'NOT_UPLOADED_NOT_SUBMITTED', 'ENVIRONMENT_NOT_READY', 'READY_TO_UPLOAD',
                  'READY_TO_SUBMIT', 'IN_PROGRESS', 'BLOCKED', 'FAILED', 'MEDIA_READY',
                  'DRAFT_CONFLICT', 'PAGE_STATE_LOST', 'VIDEO_PREVIEW_FAILED', 'PRODUCT_BOUND_MISMATCH',
                  'PRODUCT_BOUND_DUPLICATED', 'PRODUCT_BOUND_ERROR', 'NEEDS_LOGIN', 'NEEDS_CAPTCHA',
                  'AWAITING_USER_CONFIRMATION', 'PRODUCT_QUOTA_BLOCKED',
                  'running', 'completed', 'failed', 'blocked', 'reused', 'ready_for_manual_publish'}
OUTCOME_TRANSITIONS = {
    'SUBMITTING': PROTECTED,
    'SUBMIT_UNKNOWN': PROTECTED-{'SUBMITTING'},
    'SUBMITTED_PENDING_REVIEW': {'SUBMITTED_PENDING_REVIEW', 'SUBMITTED_AWAITING_REVIEW',
                                 'SCHEDULED_VERIFIED', 'PUBLISHED_VERIFIED'},
    'SUBMITTED_AWAITING_REVIEW': {'SUBMITTED_PENDING_REVIEW', 'SUBMITTED_AWAITING_REVIEW',
                                 'SCHEDULED_VERIFIED', 'PUBLISHED_VERIFIED'},
    'SCHEDULED_VERIFIED': {'SCHEDULED_VERIFIED', 'PUBLISHED_VERIFIED'},
    'PUBLISHED_VERIFIED': {'PUBLISHED_VERIFIED'},
    'REJECTED_ORIGINALITY': {'REJECTED_ORIGINALITY'},
}
PUBLISH_FIELDS = {'completed_steps', 'verification_checks', 'product_verification',
                  'manual_confirmation_required', 'pre_submit_checks', 'unconfirmed_items',
                  'work_id', 'work_url', 'published', 'pending_review', 'published_at',
                  'published_evidence'}


def merge_facts(previous, updates):
    """Merge observation deltas; explicit scalar/list/null values replace the old value."""
    if not isinstance(previous, dict):
        raise ValueError('已有facts须为对象，不能丢弃损坏的观察记录')
    merged = copy.deepcopy(previous)
    for field, value in updates.items():
        merged[field] = (merge_facts(merged[field], value)
                         if isinstance(merged.get(field), dict) and isinstance(value, dict)
                         else copy.deepcopy(value))
    return merged


def checkpoint(card_path, *, event_id, status, submitted, facts, evidence, next_action,
               at=None, publish_result=False, package=None, publish_fields=None):
    """Save before an external action. Exact retries only finish interrupted local writes."""
    if status in PROTECTED or submitted is not False or publish_result or publish_fields:
        raise ValueError('full_publish在本地交付版中禁用')
    card_path = local(card_path)
    required(event_id, 'event_id'); required(next_action, 'next_action')
    if not isinstance(facts, dict) or not isinstance(evidence, list) or not evidence:
        raise ValueError('检查点须含facts对象和非空evidence列表')
    if type(publish_result) is not bool or (submitted is not None and type(submitted) is not bool):
        raise ValueError('publish_result须布尔；submitted须布尔或null')
    publish_fields = publish_fields or {}
    if not isinstance(publish_fields, dict) or set(publish_fields)-PUBLISH_FIELDS:
        raise ValueError('publish_fields仅接受发布事实；不能覆盖身份、状态或采用')
    if publish_fields and not publish_result:
        raise ValueError('publish_fields须同时开启publish_result')
    refs = [proof(e, card_path.parent) for e in evidence]
    with locked(card_path):
        card = document(card_path)
        previous_state = {field: copy.deepcopy(card[field]) for field in
                          ('status', 'submitted', 'updated_at', 'started_at', 'facts', 'evidence',
                           'result_evidence', 'next_action', 'package', 'published', 'pending_review') if field in card}
        history = card.get('checkpoints', [])
        if not isinstance(history, list) or any(not isinstance(e, dict) for e in history):
            raise ValueError('检查点历史损坏')
        previous = [e for e in history if e.get('event_id') == event_id]
        if len(previous) > 1:
            raise ValueError('event_id重复')
        at = at or (previous[0]['observed_at'] if previous else now())
        instant = timestamp(at)
        request = dict(event_id=event_id, status=status, submitted=submitted, facts=facts,
                       evidence=refs, next_action=next_action, observed_at=at,
                       publish_result=publish_result, publish_fields=publish_fields,
                       package=str(local(package, card_path.parent)) if package else None)
        if previous and previous[0]['request'] != request:
            raise ValueError('event_id已存在且内容不同')
        pending = card.get('pending_checkpoint')
        if pending and pending['event_id'] != event_id:
            raise ValueError('存在未完成的检查点写入，须先用原event_id接续')
        result_path = card_path.with_name('publish-result.json')
        if previous:
            if not pending:
                if publish_result and history[-1]['event_id'] == event_id:
                    current = document(result_path)
                    for field in ('status', 'submitted', 'task_id', 'video_sha256'):
                        if current.get(field) != previous[0].get(field):
                            raise ValueError('检查点完成后发布记录发生冲突，须只读核实')
                return previous[0]
            desired = pending['result']
            current_hash = file_hash(result_path) if result_path.exists() else None
            current = document(result_path) if result_path.exists() else None
            if current != desired:
                if current_hash != pending['previous_result_sha256']:
                    raise ValueError('发布记录在中断后已改变，不能覆盖；须只读核对冲突')
                atomic(result_path, desired)
            card.pop('pending_checkpoint')
            atomic(card_path, card)
            return previous[0]
        if any(timestamp(e['observed_at']) > instant for e in history):
            raise ValueError('检查点时间早于已有记录')
        for value in (card.get('started_at'), card.get('updated_at')):
            if value and timestamp(value) > instant:
                raise ValueError('检查点时间早于任务起点或状态')
        if package:
            if card.get('package') and local(card['package'], card_path.parent) != local(package, card_path.parent):
                raise ValueError('检查点不能替换已有发布包')
            card['package'] = str(local(package, card_path.parent))
        context, metadata = package_context(card, card_path, required_package=status in OUTCOME_TRANSITIONS or publish_result)
        old_result = document(result_path) if result_path.exists() else None
        if old_result:
            check_known_identity(context, old_result, '发布记录')
            for field in ('task_id', 'video_sha256', 'schedule_at', 'account_key'):
                if old_result.get(field) != context.get(field):
                    raise ValueError('发布记录身份不匹配: '+field)
            if any(old_result.get(k) != card.get(k) for k in ('status', 'submitted')):
                raise ValueError('逐条状态记录冲突，须先只读核实')
        old_status = card.get('status')
        if old_status in OUTCOME_TRANSITIONS:
            allowed = set(OUTCOME_TRANSITIONS[old_status])
            if old_status != 'PUBLISHED_VERIFIED':
                allowed.add('REJECTED_ORIGINALITY')
        elif card.get('submitted') is False and old_status in PRE_SUBMISSION:
            allowed = PRE_SUBMISSION | {'REJECTED_ORIGINALITY'}
            if old_status == 'READY_TO_SUBMIT':
                allowed.add('SUBMITTING')
        else:
            raise ValueError('未知或已提交记录受保护，不能重置或重传')
        if status not in allowed:
            raise ValueError('非法状态转换；提交前必须先保存SUBMITTING，保护状态不能降级')
        if status == 'REJECTED_ORIGINALITY':
            if (type(submitted) is not bool or publish_fields.get('published') is not False
                    or publish_fields.get('pending_review') is not False
                    or (old_status in PRE_SUBMISSION and submitted is not False)
                    or (card.get('submitted') is True and submitted is not True)):
                raise ValueError('明确原创性拒绝须记录真实submitted布尔及未公开、不再待审事实')
        else:
            expected = None if status in {'SUBMITTING', 'SUBMIT_UNKNOWN'} else status in PROTECTED
            if submitted is not expected:
                raise ValueError('submitted与状态不一致')
        if status == 'SUBMITTING' and old_status != 'SUBMITTING':
            strategy = quality_support.normalize_content_strategy(context.get('content_strategy'))
            if strategy['version'] == quality_support.BATCH_STRATEGY_VERSION:
                ref = context.get('batch_ref') or {}
                plan_path = local(ref.get('plan_path'), card_path.parent)
                plan = document(plan_path)
                candidates = document(local(local(plan['job_path'], plan_path.parent)/'candidate-clips.json'))
                items = [i for i in plan['items'] if i.get('item_id') == ref.get('item_id')]
                if len(items) != 1 or items[0].get('task_id') != context['task_id']:
                    raise ValueError('提交检查点与当前采用条目身份不匹配')
                adoption = items[0].get('adoption') or {}
                fingerprint = batch_plan._fingerprint(plan, items[0], {c['id']: c for c in candidates['candidates']})
                if (adoption.get('fingerprint') != fingerprint or
                        metadata['task_context'].get('batch_adoption_fingerprint') != fingerprint):
                    raise ValueError('提交检查点与当前采用不匹配')
                proof(adoption.get('user_evidence'), plan_path.parent)
                timestamp(adoption.get('adopted_at'))
            else:
                if any(value.get('batch_ref') or value.get('batch_adoption_fingerprint')
                       for value in (context, metadata['task_context'])):
                    raise ValueError('批次记录不能降级为旧任务绕过采用')
                if context.get('scope') != 'full_publish':
                    raise ValueError('旧任务提交仍须完整发布范围及本次授权')
                quality_support.task_gate(card_path, context.get('source_video'), metadata, execution_stage='publish')
                if timestamp(context['schedule_at']) <= timestamp(now()):
                    raise ValueError('旧任务排期已过期，不得自动改为即时')
            no_resubmit(context, local_records(ROOT), context.get('video_sha256'))
        if old_result and not publish_result:
            raise ValueError('已有publish-result时须同时写入，避免跨文件状态冲突')
        event = dict(request, request=request, task_id=context['task_id'],
                     video_sha256=context.get('video_sha256'), previous_result=old_result,
                     previous_state_snapshot=previous_state)
        merged_facts = merge_facts((old_result or {}).get('facts', {}), card.get('facts', {}))
        merged_facts = merge_facts(merged_facts, facts)
        card.update(status=status, submitted=submitted, updated_at=at, facts=merged_facts,
                    evidence=refs, next_action=next_action, checkpoints=history+[event])
        if metadata:
            card['task_context'] = context
        if status == 'REJECTED_ORIGINALITY':
            card.update(published=False, pending_review=False)
        if publish_result:
            result = {field: None for field in PUBLISH_FIELDS | {'job_id', 'product_id'}}
            result.update(old_result or {})
            result.update(publish_fields)
            for field in ('account_key', 'account_source', 'publish_mode', 'cart_required', 'authorization_source'):
                if context.get(field) is not None:
                    if result.get(field) not in (None, context[field]):
                        raise ValueError('发布身份字段冲突: '+field)
                    result[field] = context[field]
                else:
                    result.setdefault(field, None)
            product = context.get('product') or {}
            if context.get('cart_required') is False:
                result['product_id'] = None
            elif product.get('id') is not None:
                result['product_id'] = product['id']
            if product.get('code') is not None:
                result['product_code'] = product['code']
            elif context.get('cart_required') is False:
                result.setdefault('product_code', None)
            result.update(task_id=context['task_id'], status=status, submitted=submitted,
                          video_sha256=context.get('video_sha256'), schedule_at=context.get('schedule_at'),
                          updated_at=at, evidence=refs, facts=merged_facts, next_action=next_action)
            result.setdefault('schema_version', '1.0')
            if not result.get('job_id') and result.get('account_key') and result.get('publish_mode'):
                identity = {field: result.get(field) for field in
                            ('account_key', 'video_sha256', 'product_id', 'publish_mode', 'schedule_at')}
                if result.get('cart_required') is False:
                    identity['product_code'] = result.get('product_code')
                result['job_id'] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                                            separators=(',', ':')).encode('utf-8')).hexdigest()
            if metadata['task_context'].get('batch_adoption_fingerprint'):
                result.setdefault('batch_adoption_fingerprint', metadata['task_context']['batch_adoption_fingerprint'])
            if context.get('originality_revision'):
                result.setdefault('originality_revision', context['originality_revision'])
            result.pop('result_evidence', None)
            card['pending_checkpoint'] = {'event_id': event_id, 'result': result,
                                         'previous_result_sha256': file_hash(result_path) if old_result else None}
        # The protective state reaches disk first. Failure never grants a fresh submit attempt.
        atomic(card_path, card)
        if publish_result:
            atomic(result_path, result)
            card.pop('pending_checkpoint')
            atomic(card_path, card)
        return event


def local(value, base=None):
    path = Path(required(str(value) if isinstance(value, Path) else value, '本地路径'))
    path = ((base or ROOT)/path).resolve() if not path.is_absolute() else path.resolve()
    root = ROOT.resolve()
    if not any(path.is_relative_to(root/name) for name in ('outputs', 'private-context')):
        raise ValueError('记录路径须在本项目outputs或private-context内')
    return path


def proof(value, base):
    ref = value if isinstance(value, dict) else {'path': value}
    path = local(ref.get('path'), base)
    actual = file_hash(path)
    if ref.get('sha256') is not None and ref['sha256'] != actual:
        raise ValueError('证据哈希不匹配')
    return {'path': str(path), 'sha256': actual}


def document(path):
    value = read(path)
    if not isinstance(value, dict):
        raise ValueError('记录须为JSON对象')
    return value


def check_known_identity(context, other, label):
    """Reject explicit product/cart/mode contradictions without erasing missing legacy fields."""
    identities = []
    for value in (context, other):
        product = value.get('product') or {}
        if not isinstance(product, dict):
            raise ValueError(label+'商品身份字段损坏')
        identities.append({'product_id': value.get('product_id') or product.get('id'),
                           'product_code': value.get('product_code') or product.get('code'),
                           'cart_required': value.get('cart_required'), 'publish_mode': value.get('publish_mode'),
                           'account_key': value.get('account_key')})
    for field, current in identities[0].items():
        previous = identities[1][field]
        if current is not None and previous is not None and current != previous:
            raise ValueError(label+'身份冲突: '+field)
    if context.get('cart_required') is False and other.get('product_id') is not None:
        raise ValueError(label+'无车身份与既有product_id冲突')


def package_context(card, path, required_package=False, read_document=document):
    context = card.get('task_context', {})
    if not isinstance(context, dict):
        raise ValueError('task_context无效')
    required(context.get('task_id'), '稳定task_id')
    package = card.get('package')
    if not package:
        if required_package:
            raise ValueError('缺少当前发布包')
        return context, None
    metadata = read_document(local(local(package, path.parent)/'publish.json'))
    other = metadata.get('task_context', {})
    if not isinstance(other, dict) or not isinstance(metadata.get('video'), dict):
        raise ValueError('发布包上下文或视频字段损坏')
    check_known_identity(context, other, '发布包')
    for key in ('task_id', 'batch_ref'):
        if other.get(key) != context.get(key):
            raise ValueError('发布包与当前任务身份不匹配: '+key)
    for field in ('account_key', 'schedule_at'):
        if other.get(field) != context.get(field):
            raise ValueError('发布包与当前任务身份不匹配: '+field)
    video_hash = context.get('video_sha256') or metadata.get('video', {}).get('sha256')
    if not video_hash or metadata.get('video', {}).get('sha256') != video_hash:
        raise ValueError('发布包与当前视频身份不匹配')
    if other.get('video_sha256') not in (None, video_hash):
        raise ValueError('发布包上下文视频身份不匹配')
    return dict(context, video_sha256=video_hash), metadata


def derive_summary(plan_path, refs_path, at=None, check_media=False):
    plan_path, refs_path = local(plan_path), local(refs_path)
    at = at or now()
    timestamp(at)
    sources = {}
    def capture(path):
        raw = path.read_bytes()
        value = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('记录须为JSON对象')
        ref = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
        if str(path) in sources and sources[str(path)] != ref:
            raise ValueError('汇总期间源记录改变，请重新收尾')
        sources[str(path)] = ref
        return value
    def source_proof(value, base):
        ref = proof(value, base)
        if ref['path'] in sources and sources[ref['path']] != ref:
            raise ValueError('汇总期间源记录改变，请重新收尾')
        sources[ref['path']] = ref
        return ref
    plan, refs = capture(plan_path), capture(refs_path)
    if plan.get('version') != batch_plan.VERSION:
        raise ValueError('批次方案版本不支持')
    items = plan.get('items')
    rows = refs.get('items')
    if not isinstance(items, list) or not isinstance(rows, list):
        raise ValueError('方案及引用须包含items列表')
    if rows and all(not row.get('publish_result') for row in rows):
        raise ValueError('本地交付无publish_result；请使用local-close与当前package证据')
    ids = [required(i.get('item_id'), 'item_id') for i in items]
    ref_ids = [required(i.get('item_id'), '引用item_id') for i in rows]
    if len(set(ids)) != len(ids) or len(set(ref_ids)) != len(ref_ids) or not set(ref_ids) <= set(ids):
        raise ValueError('条目或引用重复/不存在')
    by_id = {r['item_id']: r for r in rows}
    result = {'version': 'batch-summary-v1', 'derived_at': at, 'platform_reverified': False,
              'batch_timing': {'started_at': None, 'observed_at': at, 'elapsed_seconds': None, 'issues': []},
              'plan': sources[str(plan_path)], 'references': sources[str(refs_path)], 'items': []}
    for item in items:
        issues, conflicts, snapshots, documents = [], [], {}, {}
        row = by_id.get(item['item_id'], {})
        for name in ('run_state', 'publish_result'):
            if not row.get(name):
                issues.append('缺少'+name)
                continue
            path = local(row[name], refs_path.parent)
            try:
                documents[name] = (capture(path), path)
                snapshots[name] = sources[str(path)]
            except (OSError, ValueError) as exc:
                issues.append(name+': '+str(exc))
        card, card_path = documents.get('run_state', ({}, None))
        published, published_path = documents.get('publish_result', ({}, None))
        statuses = {name: value[0].get('status') for name, value in documents.items()}
        protected = any(d.get('status') in PROTECTED | {'REJECTED_ORIGINALITY'} or d.get('submitted') is True
                        for d in (card, published, item))
        context, metadata = {}, None
        if card_path:
            raw_context = card.get('task_context')
            if not isinstance(raw_context, dict) or raw_context.get('task_id') != item.get('task_id'):
                conflicts.append('任务身份不匹配')
            try:
                context, metadata = package_context(card, card_path, required_package=True, read_document=capture)
                ref = context.get('batch_ref', {})
                if ref.get('item_id') != item['item_id'] or local(ref.get('plan_path'), card_path.parent) != plan_path:
                    conflicts.append('批次引用不匹配')
                job = local(plan.get('job_path'), plan_path.parent)
                candidate_doc = capture(local(job/'candidate-clips.json'))
                candidates = {c['id']: c for c in candidate_doc['candidates']}
                current = batch_plan._fingerprint(plan, item, candidates)
                adoption = item.get('adoption', {})
                if adoption.get('fingerprint') != current:
                    conflicts.append('当前方案采用已失效')
                if metadata['task_context'].get('batch_adoption_fingerprint') != current:
                    conflicts.append('发布包与当前采用不匹配')
                source_proof(adoption.get('user_evidence'), plan_path.parent)
                timestamp(adoption.get('adopted_at'))
                snapshots['package'] = sources[str(local(card['package'], card_path.parent)/'publish.json')]
            except (OSError, KeyError, TypeError, ValueError) as exc:
                conflicts.append('当前条目绑定未核验: '+str(exc))
        if published_path:
            try:
                check_known_identity(context, published, '发布结果')
            except ValueError as exc:
                conflicts.append(str(exc))
            if published.get('task_id') != item.get('task_id'):
                conflicts.append('发布结果任务身份不匹配')
            if context and published.get('video_sha256') != context.get('video_sha256'):
                conflicts.append('发布结果视频不匹配')
            expected_schedule = item.get('publish', {}).get('schedule_at')
            if published.get('schedule_at') != expected_schedule or (context and context.get('schedule_at') != expected_schedule):
                conflicts.append('发布记录与当前排期不同')
            try:
                timestamp(published.get('updated_at'))
                evidence = published.get('evidence', published.get('result_evidence'))
                if not isinstance(evidence, list) or not evidence:
                    raise ValueError('发布结果无证据引用')
                snapshots['evidence'] = [source_proof(e, published_path.parent) for e in evidence]
                if 'evidence' in published and 'result_evidence' in published:
                    alias = [source_proof(e, published_path.parent) for e in published['result_evidence']]
                    if alias != snapshots['evidence']:
                        conflicts.append('evidence与result_evidence冲突')
            except (OSError, TypeError, ValueError) as exc:
                issues.append('结果证据未核验: '+str(exc))
        if card and published:
            if card.get('status') != published.get('status') or card.get('submitted') != published.get('submitted'):
                conflicts.append('逐条状态记录冲突')
            if not isinstance(card.get('status'), str) or not card['status'].strip():
                issues.append('状态缺失')
            if card.get('submitted') is not None and type(card['submitted']) is not bool:
                issues.append('submitted必须为布尔或null')
            if card.get('status') in PROTECTED-{'SUBMITTING', 'SUBMIT_UNKNOWN'} and card.get('submitted') is not True:
                conflicts.append('已提交状态缺少submitted=true')
            if card.get('status') == 'SUBMIT_UNKNOWN' and card.get('submitted') is not None:
                conflicts.append('提交未知不能声称已提交或未提交')
        if card.get('pending_checkpoint'):
            conflicts.append('检查点跨文件写入未完成，须用原event_id接续')
        timing = {'started_at': card.get('started_at'), 'observed_at': at,
                  'elapsed_seconds': None, 'issues': []}
        events = copy.deepcopy(card.get('checkpoints', []))
        metrics_ref = card.get('run_metrics_ref') or context.get('run_metrics_ref')
        if metrics_ref and card_path:
            try:
                metrics_path = local(metrics_ref, card_path.parent)
                metrics = capture(metrics_path)
                if metrics.get('task_id') not in {item.get('task_id'), plan.get('batch_id')}:
                    raise ValueError('计时任务身份不匹配')
                snapshots['metrics'] = sources[str(metrics_path)]
                if metrics.get('task_id') == plan.get('batch_id'):
                    batch_timing = result['batch_timing']
                    start = metrics.get('started_at')
                    if batch_timing['started_at'] not in (None, start):
                        raise ValueError('批次计时起点冲突')
                    batch_timing['started_at'] = start
                    if start:
                        elapsed = (timestamp(at)-timestamp(start)).total_seconds()
                        if elapsed < 0:
                            raise ValueError('收尾早于批次起点')
                        batch_timing['elapsed_seconds'] = elapsed
                elif timing['started_at'] is None:
                    timing['started_at'] = metrics.get('started_at')
                events += [e for e in metrics.get('record_events', []) if e.get('task_id') == item.get('task_id')]
            except (OSError, TypeError, ValueError) as exc:
                timing['issues'].append(str(exc))
        if timing['started_at'] is not None:
            try:
                elapsed = (timestamp(at)-timestamp(timing['started_at'])).total_seconds()
                if elapsed < 0:
                    raise ValueError('收尾时间早于真实开工起点')
                timing['elapsed_seconds'] = elapsed
            except ValueError as exc:
                timing['issues'].append(str(exc))
        else:
            timing['issues'].append('历史起点缺失，未倒填')
        media = {'verified': False, 'verification': 'not_checked', 'issues': []}
        if check_media and metadata and card_path:
            try:
                sha = metadata['video'].get('sha256')
                if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
                    raise ValueError('发布包缺少有效视频SHA-256')
                media['record_ref'] = source_proof(metadata['video'], local(card['package'], card_path.parent))
                media.update(verified=True, verification='sha256_only')
            except (OSError, TypeError, ValueError) as exc:
                media.update(verification='unverified', issues=[str(exc)])
        previews = []
        preview_paths = [local(p.get('path') if isinstance(p, dict) else p, refs_path.parent)
                         for p in row.get('previews', [])]
        if metadata and card_path:
            preview_paths += [local(metadata[k], local(card['package'], card_path.parent))
                              for k in ('main_cover_3x4', 'main_cover_4x3') if metadata.get(k)]
        for path in dict.fromkeys(preview_paths):
            previews.append({'path': str(path), 'exists': path.is_file(), 'verified': False})
        state = 'CONFLICT' if conflicts else 'UNKNOWN' if issues else card.get('status', 'UNKNOWN')
        result['items'].append({'item_id': item['item_id'], 'task_id': item.get('task_id'),
                                'status': state, 'platform_status': state,
                                'submitted': None if conflicts or issues else card.get('submitted'),
                                'protected': protected or bool(conflicts or issues), 'resubmit_allowed': False,
                                'recorded_statuses': statuses, 'issues': conflicts+issues, 'record_refs': snapshots,
                                'next_action': card.get('next_action') or published.get('next_action') or '只读核对记录与平台状态',
                                'facts': card.get('facts', {}), 'events': events,
                                'timing': timing, 'media': media, 'previews': previews})
    result['source_refs'] = list(sources.values())
    if result['batch_timing']['started_at'] is None:
        result['batch_timing']['issues'].append('批次真实起点缺失，未用子条目起点替代')
    return result


def summarize(plan_path, refs_path, out, at=None):
    out = local(out)
    if out.exists():
        raise FileExistsError('摘要输出已存在，禁止覆盖')
    result = derive_summary(plan_path, refs_path, at)
    out.mkdir(parents=True, exist_ok=False)
    atomic(out/'batch-summary.json', result)
    lines = ['# 批次记录摘要', '', '仅汇总本地引用；未重新核验平台，不提供重传或重提授权。', '']
    for item in result['items']:
        lines += [f"- {item['item_id']}: {item['status']}；保护={item['protected']}"]
        lines += ['  - '+issue for issue in item['issues']]
    atomic(out/'report.md', '\n'.join(lines), plain=True)
    return result


def close(plan_path, refs_path, out, at=None):
    """Derive all delivery views once; expose the directory only after all writes succeed."""
    out = local(out)
    if out.exists():
        raise FileExistsError('收尾输出已存在，请使用新的快照目录；禁止覆盖')
    result = derive_summary(plan_path, refs_path, at, check_media=True)
    result['snapshot_id'] = key(result)
    note = '仅汇总本地证据；未重新核验平台，不提供重传或重提授权。成片核验仅指SHA-256一致。'
    common = [f"快照：{result['snapshot_id']}", f"收尾观察时间：{result['derived_at']}",
              f"批次实际开工：{result['batch_timing']['started_at']}；截至收尾秒数：{result['batch_timing']['elapsed_seconds']}", note, '']
    report, checklist = ['# 批次交付报告', '', *common], ['# 批次交付清单', '', *common]
    cards = []
    for item in result['items']:
        timing = item['timing']
        lines = [f"## {item['item_id']} — {item['status']}", '',
                 '下一步：'+item['next_action'],
                 '原记录状态：'+json.dumps(item['recorded_statuses'], ensure_ascii=False),
                 f"实际开工：{timing['started_at']}；截至收尾秒数：{timing['elapsed_seconds']}",
                 '成片哈希：'+('已核对' if item['media']['verified'] else '未核验'),
                 '事实：'+json.dumps(item['facts'], ensure_ascii=False)]
        lines += ['- '+issue for issue in item['issues']+timing['issues']+item['media']['issues']]
        lines += [f"- 预览：{p['path']}；存在={p['exists']}；未核验" for p in item['previews']]
        report += lines+['']
        assets = ([f"成片：{item['media']['record_ref']['path']}（SHA-256已核对）"]
                  if item['media']['verified'] else ['成片：未核验，不提供播放入口'])
        assets += [f"预览：{p['path']}；存在={p['exists']}；未核验" for p in item['previews']]
        report += assets+['']
        checklist += [f"- {item['item_id']}：{item['status']}（仅记录状态）",
                      '  下一步：'+item['next_action'],
                      '  原记录状态：'+json.dumps(item['recorded_statuses'], ensure_ascii=False),
                      f"  实际开工：{timing['started_at']}；截至收尾秒数：{timing['elapsed_seconds']}",
                      *['  '+asset for asset in assets], '']
        card_html = '<article><pre>'+html.escape('\n'.join(lines))+'</pre>'
        if item['media']['verified']:
            uri = html.escape(Path(item['media']['record_ref']['path']).as_uri(), quote=True)
            card_html += f'<video controls preload="metadata" src="{uri}"></video><p>成片SHA-256已核对</p>'
        for preview in item['previews']:
            if preview['exists']:
                uri = html.escape(Path(preview['path']).as_uri(), quote=True)
                card_html += f'<figure><img src="{uri}" alt="封面预览"><figcaption>预览存在，未核验</figcaption></figure>'
        cards.append(card_html+'</article>')
    report_text = '\n'.join(report)
    overview = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>本地交付总览</title><style>body{max-width:960px;margin:2rem auto;padding:0 1rem;'
                'font:16px/1.7 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}'
                'article{border:1px solid #ddd;border-radius:12px;padding:1rem;margin:1rem 0}'
                'video{max-width:100%;max-height:600px}figure{display:inline-block;margin:1rem 1rem 1rem 0}'
                'img{max-width:260px;max-height:320px}</style>'
                '<h1>本地交付总览</h1><pre>'+html.escape('\n'.join(common))+'</pre>'+''.join(cards)+'</html>')
    out.parent.mkdir(parents=True, exist_ok=True)
    staged = out.with_name(out.name+'.pending-'+uuid.uuid4().hex)
    staged.mkdir()
    atomic(staged/'batch-summary.json', result)
    atomic(staged/'report.md', report_text, plain=True)
    atomic(staged/'delivery-checklist.md', '\n'.join(checklist), plain=True)
    atomic(staged/'delivery-overview.html', overview, plain=True)
    # Catch concurrent upstream changes before publishing a coherent read-only delivery.
    for ref in result['source_refs']:
        proof(ref, ROOT)
    staged.rename(out)
    return result


def derive_local_summary(plan_path, refs_path, at=None):
    """Summarize local deliverables without inventing platform publication state."""
    plan_path, refs_path = local(plan_path), local(refs_path)
    at = at or now()
    timestamp(at)
    plan, refs = document(plan_path), document(refs_path)
    if plan.get('version') != batch_plan.VERSION:
        raise ValueError('批次方案版本不支持')
    items, rows = plan.get('items'), refs.get('items')
    if not isinstance(items, list) or not isinstance(rows, list):
        raise ValueError('方案及本地引用须包含items列表')
    item_ids = [required(i.get('item_id'), 'item_id') for i in items]
    row_ids = [required(r.get('item_id'), '引用item_id') for r in rows]
    if len(set(item_ids)) != len(item_ids) or sorted(item_ids) != sorted(row_ids):
        raise ValueError('本地交付引用须与批次条目逐一对应且不重复')
    if any(row.get('publish_result') for row in rows):
        raise ValueError('本地交付摘要不得混入平台publish_result')
    refs_by_id = {r['item_id']: r for r in rows}
    sources = [proof(plan_path, ROOT), proof(refs_path, ROOT)]
    result = {'version': 'local-delivery-summary-v1', 'derived_at': at,
              'batch_id': plan.get('batch_id'), 'platform_reverified': False,
              'publication_status': 'outside_local_workflow', 'items': []}
    for item in items:
        row = refs_by_id[item['item_id']]
        card_path = local(row.get('run_state'), refs_path.parent)
        card = document(card_path)
        context = card.get('task_context')
        if (not isinstance(context, dict) or context.get('task_id') != item.get('task_id')
                or card.get('submitted') is not False or card.get('status') in PROTECTED
                or card.get('status') not in PRE_SUBMISSION or card.get('pending_checkpoint')):
            raise ValueError('本地任务卡身份或提交状态冲突；须先核实接续')
        batch_ref = context.get('batch_ref') or {}
        if (batch_ref.get('item_id') != item['item_id']
                or local(batch_ref.get('plan_path'), card_path.parent) != plan_path):
            raise ValueError('本地任务卡批次引用不匹配')
        if (context.get('source_sha256') and plan.get('source', {}).get('sha256')
                and context['source_sha256'] != plan['source']['sha256']):
            raise ValueError('本地任务卡原片身份与批次冲突')
        card_ref = proof(card_path, ROOT)
        sources.append(card_ref)
        package_value = row.get('package') or card.get('package')
        if row.get('package') and card.get('package') \
                and local(row['package'], refs_path.parent) != local(card['package'], card_path.parent):
            raise ValueError('本地任务卡与引用的交付包身份冲突')
        package = local(package_value, refs_path.parent) if package_value else None
        if package and package.name == 'publish.json':
            package = package.parent
        facts = card.get('facts') or {}
        if not isinstance(facts, dict):
            raise ValueError('本地任务卡facts无效')
        issues = []
        reason = facts.get('reason')
        if (card['status'] in {'BLOCKED', 'FAILED', 'blocked', 'failed'}
                and isinstance(reason, str) and reason.strip()):
            issues.append(reason)
        if not package:
            issues.append('尚无本地交付包；未验证成片或质量')
            status = card['status'] if card['status'] in {'BLOCKED', 'FAILED', 'blocked', 'failed'} \
                     else 'not_ready_for_local_delivery'
            result['items'].append({'item_id': item['item_id'], 'task_id': item['task_id'],
                                    'status': status, 'recorded_status': card['status'],
                                    'submitted': False, 'publication_status': 'outside_local_workflow',
                                    'issues': issues, 'facts': facts, 'next_action': card.get('next_action'),
                                    'quality_evidence': {}, 'video': None, 'cover': None,
                                    'package': None, 'record_refs': [card_ref]})
            continue
        artifact_refs = [card_ref]
        info = None
        try:
            if not package.is_dir():
                raise ValueError('本地交付包目录不存在')
            info = document(package / 'publish.json')
            artifact_refs.append(proof(package / 'publish.json', ROOT))
        except (OSError, ValueError) as exc:
            issues.append(str(exc))
        if info is not None:
            other = info.get('task_context')
            if isinstance(other, dict):
                for field in ('task_id', 'batch_ref', 'source_sha256', 'batch_adoption_fingerprint'):
                    if other.get(field) is not None and other.get(field) != context.get(field):
                        raise ValueError('本地交付包任务身份冲突: ' + field)
                check_known_identity(context, other, '本地交付包')
        media, cover, qa, status = None, None, {}, 'needs_repair'
        if info is not None:
            try:
                manifest = info.get('delivery_files')
                if (info.get('status') not in {'ready_for_local_delivery', 'awaiting_content_review'}
                        or not isinstance(manifest, dict)
                        or not {'video.mp4', 'title.txt', 'caption.txt'} <= manifest.keys()
                        or not any(name in manifest for name in ('cover-3x4.jpg', 'cover-4x3.jpg'))):
                    raise ValueError('本地包状态或交付文件哈希清单不完整')
                gate = quality_support.check_package(package, card_path)
                if gate.get('evidence_gate') != 'passed' or gate.get('delivery_status') != info.get('status'):
                    raise ValueError('本地包与质量检查状态冲突')
                if info['status'] == 'ready_for_local_delivery' and any(
                        gate.get('quality_evidence', {}).get(kind, {}).get('status') != 'passed'
                        for kind in ('technical', 'continuous_visual', 'audio_review')):
                    raise ValueError('交付包声称就绪，但当前完整技术／视听QA尚未全部通过')
                if gate.get('video_sha256') != info.get('video', {}).get('sha256'):
                    raise ValueError('本地交付视频身份不匹配')
                media = proof(package / 'video.mp4', ROOT)
                if media['sha256'] != gate['video_sha256']:
                    raise ValueError('本地交付视频文件已改变')
                checked_refs = []
                for name, sha in manifest.items():
                    path = (package / name).resolve(strict=True)
                    if not path.is_relative_to(package) or not path.is_file():
                        raise ValueError('交付文件必须在当前包内')
                    file_ref = proof({'path': path, 'sha256': sha}, ROOT)
                    checked_refs.append(file_ref)
                    if name in ('cover-3x4.jpg', 'cover-4x3.jpg'):
                        cover = file_ref
                artifact_refs.extend(r for r in checked_refs if r not in artifact_refs)
                qa, status = gate['quality_evidence'], info['status']
            except (OSError, ValueError, KeyError, TypeError) as exc:
                issues.append(str(exc))
                media, cover = None, None
        sources.extend(artifact_refs)
        result['items'].append({'item_id': item['item_id'], 'task_id': item['task_id'],
                                'status': status, 'recorded_status': card['status'],
                                'submitted': False, 'publication_status': 'outside_local_workflow',
                                'issues': issues, 'facts': facts, 'next_action': card.get('next_action'),
                                'quality_evidence': qa, 'video': media, 'cover': cover,
                                'package': str(package), 'record_refs': artifact_refs})
    result['source_refs'] = list({r['path']: r for r in sources}.values())
    return result


def local_close(plan_path, refs_path, out, at=None):
    """Write an immutable, coherent local handoff snapshot."""
    out = local(out)
    if out.exists():
        raise FileExistsError('本地收尾快照已存在，禁止覆盖')
    result = derive_local_summary(plan_path, refs_path, at)
    def asset_link(label, asset):
        target = Path(os.path.relpath(asset['path'], out)).as_posix()
        return f'[{label}](<{target}>)'
    result['snapshot_id'] = key(result)
    report = ['# 本地交付报告', '', '仅核验本地包；本流程未查询平台发布状态。',
              f"快照：{result['snapshot_id']}", f"观察时间：{result['derived_at']}", '']
    checklist = ['# 本地交付清单', '', '本清单不代表已上传或已发布。', '']
    for item in result['items']:
        qa = {name: value.get('status') for name, value in item['quality_evidence'].items()}
        report += [f"## {item['item_id']}：{item['status']}", '',
                   f"任务：{item['task_id']}；提交：否；平台状态：本工具未检查",
                   f"原任务状态：{item['recorded_status']}",
                   f"交付包：{item['package'] or '无'}",
                   '任务事实：' + json.dumps(item['facts'], ensure_ascii=False),
                   '质量审核：' + (json.dumps(qa, ensure_ascii=False) if qa else '未通过完整核验'),
                   *['原因：' + issue for issue in item['issues']],
                   '下一步：' + (item.get('next_action') or '核实当前任务并补齐证据')]
        if item['video']:
            report.append(asset_link('观看成片', item['video']))
        if item['cover']:
            report.append(asset_link('查看封面', item['cover']))
        report += ['', '']
        checklist += [f"- {item['item_id']}：{item['status']}；包：{item['package'] or '无'}",
                      *['  - 原因：' + issue for issue in item['issues']]]
        if item['video']:
            checklist.append('  - ' + asset_link('观看成片', item['video']))
        if item['cover']:
            checklist.append('  - ' + asset_link('查看封面', item['cover']))
    out.parent.mkdir(parents=True, exist_ok=True)
    staged = out.with_name(out.name + '.pending-' + uuid.uuid4().hex)
    staged.mkdir()
    atomic(staged / 'batch-summary.json', result)
    atomic(staged / 'report.md', '\n'.join(report), plain=True)
    atomic(staged / 'delivery-checklist.md', '\n'.join(checklist), plain=True)
    for ref in result['source_refs']:
        proof(ref, ROOT)
    staged.rename(out)
    return result


def record_event(card_path, *, event_id, kind, phase, stage, evidence, at=None, attempt=None,
                 facts=None, next_action=None):
    if kind == 'upload':
        raise ValueError('full_publish在本地交付版中禁用')
    card_path = local(card_path)
    if at is not None:
        timestamp(at)
    if facts is not None and not isinstance(facts, dict):
        raise ValueError('facts须为对象')
    if next_action is not None:
        required(next_action, 'next_action')
    required(event_id, 'event_id'); required(stage, 'stage')
    if kind not in ('stage', 'upload') or phase not in ('started', 'completed_observed', 'blocked'):
        raise ValueError('不支持的观察事件')
    if kind == 'upload':
        required(attempt, 'upload attempt')
    elif attempt is not None:
        raise ValueError('stage事件不使用upload attempt')
    card = document(card_path)
    context, _ = package_context(card, card_path, required_package=kind == 'upload')
    ev = proof(evidence, card_path.parent)
    metrics_path = local(card.get('run_metrics_ref') or context.get('run_metrics_ref'), card_path.parent)
    if not metrics_path.is_file():
        raise FileNotFoundError('须先建立既有run-metrics记录')
    lock = metrics_path.with_name(metrics_path.name+'.lock')
    # Exclusive lock prevents concurrent lost updates; never remove another writer's lock.
    with lock.open('x', encoding='utf-8'):
        pass
    try:
        metrics = document(metrics_path)
        events = metrics.get('record_events', [])
        if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
            raise ValueError('既有事件记录损坏')
        prior = [e for e in events if e.get('event_id') == event_id]
        at = at or (prior[0]['observed_at'] if prior else now())
        instant = timestamp(at)
        identity = {'task_id': context['task_id'], 'video_sha256': context.get('video_sha256')}
        match_identity = identity if kind == 'upload' else {'task_id': context['task_id']}
        same = [e for e in events if all(e.get(k) == v for k, v in match_identity.items())
                and e.get('kind') == kind and e.get('stage') == stage and e.get('attempt') == attempt]
        starts = [e for e in same if e.get('phase') == 'started']
        event = dict(identity, event_id=event_id, kind=kind, phase=phase, stage=stage, attempt=attempt,
                     evidence=ev, observed_at=at, finished_at=None,
                     started_at=at if phase == 'started' else starts[-1]['started_at'] if starts else None)
        if facts is not None:
            event['facts'] = facts
        if next_action is not None:
            event['next_action'] = next_action
        if prior:
            request_keys = set(event)-{'started_at', 'finished_at'}
            if kind == 'stage':
                request_keys.remove('video_sha256')
            if len(prior) != 1 or any(prior[0].get(k) != event[k] for k in request_keys):
                raise ValueError('event_id已存在且内容不同')
            return prior[0]
        if any(timestamp(e['observed_at']) > instant for e in events):
            raise ValueError('观察时间早于既有事件；不能倒填起止')
        if metrics.get('started_at') and timestamp(metrics['started_at']) > instant:
            raise ValueError('观察时间早于真实开工起点')
        if phase == 'started' and starts and (kind == 'upload' or same[-1].get('phase') == 'started'):
            raise ValueError('同attempt/阶段已有开始事件')
        if kind == 'upload' and phase == 'started':
            if card.get('pending_checkpoint'):
                raise ValueError('检查点写入未完成，不能开始上传')
            previous_path = local(card_path.with_name('publish-result.json'))
            states = [card]
            if previous_path.exists():
                previous = document(previous_path)
                if previous.get('task_id') != context['task_id'] or previous.get('video_sha256') != context.get('video_sha256'):
                    raise ValueError('提交记录身份不匹配')
                states.append(previous)
            elif card.get('status') not in ('NOT_UPLOADED_NOT_SUBMITTED', 'READY_TO_UPLOAD'):
                raise ValueError('无结果记录时仅接受明确预上传任务卡；不推断从未提交')
            if any(d.get('status') in PROTECTED | {'REJECTED_ORIGINALITY'} or d.get('submitted') is not False for d in states):
                raise ValueError('提交未知或已提交记录受保护，不记录新上传开始')
            no_resubmit(context, local_records(ROOT), context.get('video_sha256'))
        metrics['record_events'] = events+[event]
        atomic(metrics_path, metrics)
        return event
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('summarize', 'close', 'local-close'):
        summary = sub.add_parser(name)
        for flag in ('plan', 'records', 'out'):
            summary.add_argument('--'+flag, required=True)
        summary.add_argument('--at')
    init = sub.add_parser('init')
    init.add_argument('--run-state', required=True)
    init.add_argument('--context', required=True, help='本地task_context JSON文件')
    init.add_argument('--at')
    check = sub.add_parser('checkpoint')
    check.add_argument('--run-state', required=True)
    check.add_argument('--record', required=True, help='本地检查点JSON，字段对应checkpoint参数')
    event = sub.add_parser('event')
    for flag in ('run-state', 'event-id', 'kind', 'phase', 'stage', 'evidence'):
        event.add_argument('--'+flag, required=True)
    event.add_argument('--at')
    event.add_argument('--attempt')
    event.add_argument('--facts', help='本地facts JSON文件')
    event.add_argument('--next-action')
    args = vars(parser.parse_args())
    command = args.pop('command')
    if command in ('summarize', 'close', 'local-close'):
        action = {'summarize': summarize, 'close': close, 'local-close': local_close}[command]
        result = action(args['plan'], args['records'], args['out'], args['at'])
    elif command == 'init':
        result = initialize(args['run_state'], task_context=document(local(args['context'])), at=args['at'])
    elif command == 'checkpoint':
        result = checkpoint(args['run_state'], **document(local(args['record'])))
    else:
        if args.get('facts'):
            args['facts'] = document(local(args['facts']))
        result = record_event(args.pop('run_state'), **args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
