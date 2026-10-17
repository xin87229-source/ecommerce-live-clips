"""Local single-style batch evidence, reports and adopted-plan gates.

Codex supplies semantic judgements and real user evidence. This module checks
their bindings; it neither recognizes garments nor estimates a maximum yield.
"""
import argparse
import copy
import itertools
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from selector_support import ROOT, atomic, file_hash, key, read, validate_transcript
import quality_support as quality
from runtime import profile

VERSION = 'single-style-batch-v1'
PROTECTED = quality.PROTECTED
_NOT_PROVIDED = object()


def local_path(value, exists=True):
    p = Path(value).resolve(strict=exists)
    if not any(p.is_relative_to((ROOT/d).resolve()) for d in ('outputs', 'private-context')):
        raise ValueError('批次输入与输出须在本地outputs或private-context')
    return p


def required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('缺少' + label)
    return value


def evidence(item):
    if not isinstance(item, dict) or not item.get('path'):
        raise ValueError('缺少可追溯证据文件')
    p = local_path(item['path'])
    if not p.is_file() or file_hash(p) != item.get('sha256'):
        raise ValueError('证据文件哈希不匹配')
    return item


def time_value(value):
    try:
        t = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('时间须为含时区ISO格式') from None
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError('时间须含时区')
    return t


def interval(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
            or not 0 <= value[0] < value[1]):
        raise ValueError('源绝对时间区间无效')
    return tuple(value)


def union_ranges(ranges):
    result = []
    for a, z in sorted(interval(r) for r in ranges):
        if result and a <= result[-1][1] + 1e-6:
            result[-1][1] = max(result[-1][1], z)
        else:
            result.append([a, z])
    return result


def seconds(ranges):
    return sum(z-a for a, z in union_ranges(ranges))


def candidate_range(c):
    return interval([c['start_seconds'], c['end_seconds']])


def _full_review(plan):
    """Human whole-source inventory is separate from selective window ASR."""
    coverage, source = plan['coverage'], plan['source']
    reference = coverage.get('full_review')
    if not reference:
        raise ValueError('完整原直播须有独立全场盘点full_review证据')
    evidence(reference)
    review = read(local_path(reference['path']))
    if not isinstance(review, dict) or review.get('schema_version') != 1 \
            or review.get('conclusion') != 'full_source_reviewed' \
            or review.get('source_sha256') != source['sha256'] \
            or review.get('duration_seconds') != source['duration_seconds']:
        raise ValueError('全场盘点证据与当前原片身份或时长不匹配')
    for field in ('searched_ranges', 'confirmed_ranges', 'pending_ranges'):
        if union_ranges(review.get(field, [])) != union_ranges(coverage.get(field, [])):
            raise ValueError('全场盘点证据与当前检索／确认区间不匹配')
    required(review.get('reviewer'), '全场盘点执行者')
    time_value(review.get('reviewed_at'))
    required(review.get('method'), '全场盘点方法')
    required(review.get('finding'), '全场盘点结论依据')
    return review


def _transcribed_seconds(job, duration):
    manifest = read(local_path(job) / 'manifest.json')
    transcript = read(local_path(manifest['transcript_cache']))
    return seconds(transcript.get('covered_ranges', [[0, duration]]))


def _candidates(plan):
    job = local_path(plan['job_path'])
    manifest = read(job/'manifest.json')
    data = read(job/'candidate-clips.json')
    transcript_path = local_path(manifest['transcript_cache'])
    if file_hash(transcript_path) != manifest.get('transcript_hash'):
        raise ValueError('job转写哈希改变')
    transcript = read(transcript_path)
    validate_transcript(transcript)
    coverage = plan.get('coverage', {})
    if transcript.get('coverage_scope') == 'reviewed_windows_only':
        if coverage.get('scope') not in ('reviewed_windows_only', 'full_source'):
            raise ValueError('局部转写须明确盘点作用域，不能冒充整场盘点')
        covered = union_ranges(transcript.get('covered_ranges', []))
        if coverage['scope'] == 'full_source':
            _full_review(plan)
            bounded = union_ranges(coverage.get('confirmed_ranges', []))
        else:
            bounded = union_ranges(coverage.get('searched_ranges', []))
        if not covered or any(not any(x <= a < z <= y for x, y in covered)
                              for a, z in bounded):
            raise ValueError('已确认同款或局部检索范围超出实际局部转写窗口')
    source = plan['source']
    if (Path(manifest['source_video']).resolve() != Path(source['path']).resolve()
            or Path(data['source_video']).resolve() != Path(source['path']).resolve()
            or transcript['source'].get('sha256') != source['sha256']
            or transcript['source']['duration_seconds'] != source['duration_seconds']):
        raise ValueError('job与批次原源身份或绝对时间轴不匹配')
    segments = transcript['segments']
    positions = {s['id']:i for i,s in enumerate(segments)}
    candidates = {}
    for c in data['candidates']:
        cid = required(c.get('id'), '候选ID')
        if cid in candidates:
            raise ValueError('候选ID必须唯一')
        a,z = candidate_range(c)
        if z > source['duration_seconds']:
            raise ValueError('候选超出原片绝对时间范围')
        if transcript.get('coverage_scope') == 'reviewed_windows_only' \
                and not any(x <= a < z <= y for x, y in covered):
            raise ValueError('候选超出实际转写窗口')
        required(c.get('category'), '候选分类')
        required(c.get('reason'), '分类依据')
        if c.get('kind', 'speech') == 'visual':
            visual, audio = c.get('visual_review', {}), c.get('speech_review', {})
            evidence(visual); evidence(audio)
            audio_start,audio_end = interval(audio.get('range'))
            if (type(visual.get('at')) not in (int,float) or not a <= visual['at'] <= z
                    or audio.get('conclusion') != 'no_intelligible_speech'
                    or not audio_start <= a < z <= audio_end <= source['duration_seconds'] or c.get('text') != ''):
                raise ValueError('展示单元须有完整画面/音频审核，不能把ASR空白当作无声')
            required(audio.get('finding'), '无须字幕判断依据')
        elif c.get('kind', 'speech') == 'speech':
            try:
                first, last = positions[c['start_segment_id']], positions[c['end_segment_id']]
                span = segments[first:last+1]
            except KeyError:
                raise ValueError('候选未绑定真实转写segment') from None
            if (not span or a != span[0]['start'] or z != span[-1]['end']
                    or c.get('text') != ''.join(s['text'] for s in span)):
                raise ValueError('候选源区间或原话与完整转写不匹配')
        else:
            raise ValueError('未知素材单元类型')
        candidates[cid] = c
    return candidates, data


def _exclusion(value):
    if isinstance(value, str):
        return required(value, '未用候选处置原因'), True
    if not isinstance(value, dict) or type(value.get('usable')) is not bool:
        raise ValueError('未用候选处置须有reason及布尔usable')
    return required(value.get('reason'), '未用候选处置原因'), value['usable']


def _item_content(item):
    return {k:v for k,v in item.items() if k not in ('adoption', 'adoption_history', 'publish', 'status', 'submitted', 'originality_revision')}


def pair_content_hash(plan, item_ids, candidates):
    by_id = {item['item_id']:item for item in plan['items']}
    return key([{'item':_item_content(by_id[i]),
                 'candidates':[candidates[c] for c in by_id[i]['order']]} for i in sorted(item_ids)])


def _fingerprint(plan, item, candidates):
    binding = _item_content(item)
    reuse = [r for r in plan.get('reuse', []) if item['item_id'] in r['items']]
    # A refreshed comparison of another item does not revoke this item's adoption
    # when its content and the substantive comparison/reuse findings stay equal.
    pairs = [{k:v for k,v in r.items() if k != 'content_hash'}
             for r in plan.get('pair_reviews', []) if item['item_id'] in r['items']]
    return key({'version':VERSION, 'profile':profile(), 'batch_id':plan['batch_id'], 'source':plan['source'],
                'product':plan['product'], 'source_window':plan.get('source_window'),
                'coverage':plan['coverage'], 'item':binding,
                'candidates':[candidates[i] for i in item['order']], 'reuse':reuse,'pair_reviews':pairs})


def validate_plan(path, check_pair_bindings=True):
    path = local_path(path)
    plan = read(path)
    if plan.get('version') != VERSION:
        raise ValueError('批次方案版本须为single-style-batch-v1')
    required(plan.get('batch_id'), 'batch_id')
    source = plan['source']
    if file_hash(Path(source['path']).resolve(strict=True)) != source.get('sha256'):
        raise ValueError('原源文件哈希已改变')
    duration = source.get('duration_seconds')
    if type(duration) not in (int,float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('原源时长无效')
    for f in ('name', 'code'):
        required(plan.get('product', {}).get(f), '商品'+f)
    coverage = plan.get('coverage', {})
    coverage_scope = coverage.get('scope')
    if coverage_scope not in (None, 'full_source', 'reviewed_windows_only'):
        raise ValueError('coverage.scope只允许full_source或reviewed_windows_only')
    if plan.get('source_window') and coverage_scope != 'reviewed_windows_only':
        raise ValueError('抽取窗口任务不得冒充完整原直播覆盖')
    if coverage.get('source_sha256') != source['sha256']:
        raise ValueError('覆盖证据未绑定本场源哈希')
    evidence(coverage.get('evidence'))
    ranges = union_ranges(coverage.get('searched_ranges', []))
    if any(z > duration for a,z in ranges):
        raise ValueError('已检索范围超出原直播')
    complete = (coverage_scope == 'full_source'
                and len(ranges) == 1 and ranges[0] == [0,duration])
    if coverage_scope == 'full_source':
        _full_review(plan)
    if not all(field in coverage for field in ('confirmed_ranges','pending_ranges')):
        raise ValueError('覆盖记录须分别列明已确认同款区间和待确认区间')
    confirmed = union_ranges(coverage['confirmed_ranges'])
    pending = union_ranges(coverage['pending_ranges'])
    for a,z in confirmed+pending:
        if not any(x <= a < z <= y for x,y in ranges):
            raise ValueError('已确认或待确认同款区间超出已检索范围')
    if any(min(a[1],z[1]) > max(a[0],z[0]) for a,z in itertools.product(confirmed,pending)):
        raise ValueError('同一区间不能同时已确认和待确认')
    candidates, data = _candidates(plan)
    for c in candidates.values():
        a,z = candidate_range(c)
        if not any(x <= a and z <= y for x,y in ranges):
            raise ValueError('候选超出已盘点源范围')
    items = plan.get('items', [])
    if not isinstance(items, list):
        raise ValueError('items须为列表')
    ids, tasks, used = set(), set(), set()
    results = {}
    for item in items:
        iid, tid = required(item.get('item_id'), 'item_id'), required(item.get('task_id'), 'task_id')
        if iid in ids or tid in tasks:
            raise ValueError('各条item_id和task_id必须唯一且稳定')
        ids.add(iid); tasks.add(tid)
        order = item.get('order')
        if not isinstance(order, list) or not order or len(order) != len(set(order)) or not set(order) <= candidates.keys():
            raise ValueError('每条须明确不重复的真实候选顺序order')
        if not any(candidates[c].get('kind','speech') == 'speech' for c in order):
            raise ValueError('当前原声字幕链不支持全展示推荐；须包含有效口播单元')
        speed = item.get('speed')
        if type(speed) not in (int,float) or not 1 <= speed <= 1.5:
            raise ValueError('倍速须在1至1.5之间')
        for field in ('speed_reason', 'mainline', 'completeness'):
            required(item.get(field), field)
        if (set(item.get('roles', {})) != set(order)
                or any(v not in ('main', 'auxiliary') for v in item['roles'].values())):
            raise ValueError('每个候选须说明main或auxiliary作用')
        selected = [candidate_range(candidates[c]) for c in order]
        if any(min(a[1],z[1]) > max(a[0],z[0]) for a,z in itertools.combinations(selected,2)):
            raise ValueError('同一条内原区间重叠')
        raw = sum(z-a for a,z in selected)
        timeline = None
        expected = raw/speed
        if 'edit_plan' in item:
            from edit_timeline import build
            manifest = read(local_path(plan['job_path'])/'manifest.json')
            transcript = read(local_path(manifest['transcript_cache']))
            timeline = build(item['edit_plan'], order, candidates, source, confirmed, speed, transcript)
            raw, expected = timeline['raw_duration'], timeline['duration_seconds']
        if expected > profile()['max_duration_seconds']:
            raise ValueError(f"预计成片超过{profile()['max_duration_seconds']}秒")
        results[iid] = {'task_id':tid, 'raw_duration':raw, 'expected_duration':expected,
                        'adoption_fingerprint':_fingerprint(plan,item,candidates)}
        if timeline is not None:
            results[iid]['render_timeline'] = timeline
        used.update(order)
    exclusions = plan.get('exclusions', {})
    if not isinstance(exclusions,dict) or set(exclusions) != candidates.keys()-used:
        raise ValueError('所有未用候选必须有且仅有对应处置原因')
    excluded = {cid:_exclusion(value) for cid,value in exclusions.items()}
    allowed = {}
    for reuse in plan.get('reuse', []):
        pair = reuse.get('items', [])
        clips = reuse.get('candidate_ids', [])
        if (len(pair) != 2 or len(set(pair)) != 2 or not set(pair) <= ids
                or not clips or len(clips) != len(set(clips)) or not set(clips) <= candidates.keys()):
            raise ValueError('辅助复用须逐对列明条目和真实候选')
        required(reuse.get('reason'), '辅助复用披露原因')
        pair_key = frozenset(pair)
        if pair_key in allowed:
            raise ValueError('同一条目对重复声明辅助复用')
        allowed[pair_key] = set(clips)

    def media_ranges(item):
        timeline = results[item['item_id']].get('render_timeline')
        if timeline is not None:
            return [(s['candidate_id'], s['audio']['source_range'], s['visual']['source_range'])
                    for s in timeline['segments']]
        return [(cid, candidate_range(candidates[cid]), candidate_range(candidates[cid]))
                for cid in item['order']]

    for first,second in itertools.combinations(items,2):
        actual = set()
        for a,z in itertools.product(first['order'],second['order']):
            ar,zr = candidate_range(candidates[a]),candidate_range(candidates[z])
            if min(ar[1],zr[1]) <= max(ar[0],zr[0]):
                continue
            if a != z or first['roles'][a] != 'auxiliary' or second['roles'][z] != 'auxiliary':
                raise ValueError('同批主片段或未声明片段重叠；不得挪用其它条已分配素材')
            actual.add(a)
        if any('render_timeline' in results[item['item_id']] for item in (first, second)):
            for (a, aa, av), (z, za, zv) in itertools.product(media_ranges(first), media_ranges(second)):
                if not any(min(left[1], right[1]) > max(left[0], right[0])
                           for left, right in ((av, za), (av, zv), (aa, zv))):
                    continue
                if a != z or first['roles'][a] != 'auxiliary' or second['roles'][z] != 'auxiliary':
                    raise ValueError('同批替换画面与其它条音画重叠；不得挪用其它条已分配素材')
                actual.add(a)
        if actual != allowed.get(frozenset([first['item_id'],second['item_id']]),set()):
            raise ValueError('同批重叠须与逐对辅助复用声明完全相符')
    comparisons = {}
    for comparison in plan.get('pair_reviews',[]):
        pair = comparison.get('items',[])
        if len(pair) != 2 or len(set(pair)) != 2 or not set(pair) <= ids or frozenset(pair) in comparisons:
            raise ValueError('主线、口播和画面相似性须逐对记录且不重复')
        for field in ('mainline','speech','visual','basis'):
            required(comparison.get(field),'逐对相似性'+field)
        if comparison.get('conclusion') not in ('distinct','disclosed_overlap'):
            raise ValueError('逐对相似性须有可复核的差异或已披露重叠结论')
        if check_pair_bindings and comparison.get('content_hash') != pair_content_hash(plan,pair,candidates):
            raise ValueError('逐对比较未绑定双方当前版本；重新比较后更新content_hash，不沿用旧判断')
        comparisons[frozenset(pair)] = comparison
    if set(comparisons) != {frozenset(pair) for pair in itertools.combinations(ids,2)}:
        raise ValueError('每对视频均需主线、口播和画面相似性评估；时间不重叠不代表内容不同')
    confirmed_candidates = {cid:c for cid,c in candidates.items()
                            if any(a <= c['start_seconds'] < c['end_seconds'] <= z for a,z in confirmed)}
    usable = [candidate_range(c) for cid,c in confirmed_candidates.items() if cid not in excluded or excluded[cid][1]]
    usable_seconds = seconds(usable)
    confirmed_seconds = seconds(confirmed)
    total_seconds = seconds([candidate_range(c) for c in confirmed_candidates.values()])
    allocated_seconds = seconds([candidate_range(confirmed_candidates[cid]) for cid in used if cid in confirmed_candidates])
    inventory_ready = complete and not pending
    required(plan.get('why_not_more'), '为什么不能再多的内容依据')
    return {'plan_path':str(path), 'coverage_complete':complete,
            'coverage_scope':coverage_scope, 'searched_seconds':seconds(ranges),
            'transcribed_seconds':_transcribed_seconds(plan['job_path'], duration),
            'total_seconds':total_seconds, 'allocated_seconds':allocated_seconds,
            'unallocated_seconds':max(0,total_seconds-allocated_seconds),
            'inventory_candidate_seconds':seconds([candidate_range(c) for c in candidates.values()]),
            'usable_seconds':usable_seconds, 'confirmed_source_seconds':confirmed_seconds,
            'pending_seconds':seconds(pending), 'style_eligible':inventory_ready and confirmed_seconds >= profile()['min_source_seconds'],
            'recommended_count':(len(items) if confirmed_seconds >= profile()['min_source_seconds'] else 0) if inventory_ready else None,
            'draft_count':len(items),
            'items':results, 'plan':plan, 'candidates':candidates, 'candidate_data':data}


def _executable(result, context=None):
    if not result['coverage_complete']:
        coverage = result['plan']['coverage']
        if (not isinstance(context, dict) or context.get('scope') != 'test'
                or coverage.get('scope') != 'reviewed_windows_only'
                or not coverage.get('searched_ranges')):
            raise ValueError('整场盘点未完整，不能采用最终方案；局部执行须明确test和reviewed_windows_only')
    if result['pending_seconds']:
        raise ValueError('同款待确认区间尚未清零，不能采用最终方案')
    minimum = profile()['min_source_seconds']
    if result['confirmed_source_seconds'] < minimum:
        raise ValueError(f'完整盘点后整款已确认原始窗口去重总量低于{minimum}秒，不能制作')
    confirmed = union_ranges(result['plan']['coverage']['confirmed_ranges'])
    for item in result['plan']['items']:
        for cid in item['order']:
            a,z = candidate_range(result['candidates'][cid])
            if not any(x <= a < z <= y for x,y in confirmed):
                raise ValueError('拟采用单元不在已确认同款原始窗口内')


def _protected(plan, item, context=None, *, parent_adoption=False):
    context = context or {'task_id':item['task_id'], 'cross_device_state':'local_records_checked'}
    if parent_adoption:
        if (context.get('batch_ref') or context.get('batch_adoption_fingerprint')
                or context.get('source_sha256') != plan['source']['sha256']
                or Path(context.get('source_video', '')).resolve() != Path(plan['source']['path']).resolve()
                or context.get('product') != plan['product']
                or context.get('source_window') != plan.get('source_window')
                or union_ranges(w['range'] for w in context.get('confirmed_windows', []))
                   != union_ranges(plan['coverage']['confirmed_ranges'])):
            raise ValueError('采用父任务的源、商品、窗口或状态与当前批次不匹配')
        if context.get('originality_revision'):
            raise ValueError('原创性修订须使用对应子任务卡，不得从父任务授权')
    elif context.get('task_id') != item['task_id']:
        raise ValueError('采用任务身份不匹配')
    if item.get('originality_revision') != context.get('originality_revision'):
        raise ValueError('原创性修订须绑定当前任务卡与条目，不能借其他任务解锁')
    check_context = dict(context, task_id=item['task_id']) if parent_adoption else context
    quality.no_resubmit(check_context, [item, *quality.local_records(ROOT)], context.get('video_sha256'))


def _revision_binding(result, item, context, adopted_at):
    previous = quality.revision_previous(context)
    if previous is None:
        return
    revision = context['originality_revision']
    if time_value(adopted_at) <= time_value(revision['verified_at']):
        raise ValueError('修订方案须在拒绝核验之后重新采用')
    old = validate_plan(quality.evidence_file(revision['previous_plan']))
    old_item = next((i for i in old['plan']['items'] if i['task_id'] == item['task_id']), None)
    old_fingerprint = (previous.get('batch_adoption_fingerprint') or
                       (previous.get('task_context') or {}).get('batch_adoption_fingerprint'))
    if (not old_item or old_item.get('originality_revision')
            or old_item.get('adoption',{}).get('fingerprint') != old_fingerprint
            or old['items'][old_item['item_id']]['adoption_fingerprint'] != old_fingerprint):
        raise ValueError('前次发布与保留的采用方案不匹配，或已使用过修订')
    def selection(value, entry):
        return (value['plan']['source']['sha256'],
                [candidate_range(value['candidates'][cid]) for cid in entry['order']])
    if selection(old, old_item) == selection(result, item):
        raise ValueError('一次原创性修订须实质重剪，不能只改说明、倍速或封面')
    fingerprint = result['items'][item['item_id']]['adoption_fingerprint']
    for name in ('result.json', 'publish.json'):
        for path in (ROOT/'outputs').glob('**/' + name):
            record = read(path)
            snapshot = record.get('task_context') or {}
            used = snapshot.get('originality_revision') or {}
            if (snapshot.get('task_id') == item['task_id'] and used
                    and snapshot.get('batch_adoption_fingerprint') not in (None, fingerprint)):
                raise ValueError('该任务已制作过一次不同的原创性修订，不能循环返工')


def adopt(path, item_ids, user_evidence, adopted_at, task_record=None):
    result = validate_plan(path)
    context = quality.context_from(task_record) if task_record else None
    _executable(result, context)
    evidence(user_evidence); time_value(adopted_at)
    if not item_ids or len(item_ids) != len(set(item_ids)) or not set(item_ids) <= result['items'].keys():
        raise ValueError('采用须明确已有条目ID，各一次')
    plan = result['plan']
    for item in plan['items']:
        if item['item_id'] in item_ids:
            _protected(plan,item,context,parent_adoption=bool(context and context.get('task_id') != item['task_id']))
            if context:
                _revision_binding(result,item,context,adopted_at)
            old = item.get('adoption')
            if old:
                item.setdefault('adoption_history', []).append(copy.deepcopy(old))
            # History is audit metadata, not a creative change to the item.
            item['adoption'] = {'fingerprint':_fingerprint(plan,item,result['candidates']),
                                'user_evidence':user_evidence, 'adopted_at':adopted_at}
    atomic(local_path(path), plan)
    return {i:next(v['adoption'] for v in plan['items'] if v['item_id']==i) for i in item_ids}


def execution_gate(context, stage='render', candidates=None, order=None, speed=None,
                   edit_plan=_NOT_PROVIDED):
    if stage == 'publish':
        raise ValueError('full_publish在本地交付版中禁用')
    if stage != 'render':
        raise ValueError('批次执行阶段须为render或publish')
    ref = context.get('batch_ref', {})
    if not ref.get('plan_path') or not ref.get('item_id'):
        raise ValueError('缺少已采用批次引用batch_ref')
    result = validate_plan(ref['plan_path'])
    _executable(result, context)
    plan = result['plan']
    item = next((i for i in plan['items'] if i['item_id']==ref['item_id']), None)
    if item is None:
        raise ValueError('批次条目不存在')
    _protected(plan,item,context)
    source = plan['source']
    product = dict(context.get('product', {}))
    if ('id' in plan['product'] and plan['product']['id'] is None
            and product.get('id') is not None):
        # Supplemental identity evidence does not rewrite the adopted content
        # or its media fingerprint. All other recorded product fields still match.
        quality.required(product['id'], '补充商品ID')
        quality.evidence_file(context.get('product_id_evidence') or {})
        product['id'] = None
    if (context.get('task_id') != item['task_id'] or context.get('source_sha256') != source['sha256']
            or Path(context.get('source_video','')).resolve() != Path(source['path']).resolve()
            or context.get('source_window') != plan.get('source_window')
            or any(product.get(k) != v for k,v in plan['product'].items())):
        raise ValueError('当前任务ID、商品或源文件与批次条目身份不匹配')
    if union_ranges(w['range'] for w in context.get('confirmed_windows',[])) != union_ranges(plan['coverage']['confirmed_ranges']):
        raise ValueError('任务卡真实同款窗口与批次已确认源区间不匹配')
    adoption = item.get('adoption', {})
    fingerprint = result['items'][item['item_id']]['adoption_fingerprint']
    if adoption.get('fingerprint') != fingerprint:
        raise ValueError('尚未明确采用当前条目版本；改组合须重审受影响部分')
    evidence(adoption.get('user_evidence')); time_value(adoption.get('adopted_at'))
    _revision_binding(result,item,context,adoption['adopted_at'])
    if order is not None and order != item['order']:
        raise ValueError('实际顺序与已采用方案不符')
    if speed is not None and speed != item['speed']:
        raise ValueError('实际倍速与已采用方案不符')
    if edit_plan is not _NOT_PROVIDED and edit_plan != item.get('edit_plan'):
        raise ValueError('实际编辑参数与已采用方案不符；不得省略或改写编辑时间轴')
    if candidates is not None:
        actual = {c['id']:c for c in candidates.get('candidates',[])}
        if (Path(candidates.get('source_video','')).resolve() != Path(source['path']).resolve()
                or any(actual.get(cid) != result['candidates'][cid] for cid in item['order'])):
            raise ValueError('实际候选原话、区间或源身份不同于已采用版本')
    return dict(result['items'][item['item_id']],plan_path=result['plan_path'],item_id=item['item_id'])


def report(path):
    result = validate_plan(path)
    plan, candidates = result['plan'],result['candidates']
    if plan['coverage'].get('scope') == 'reviewed_windows_only':
        count = f"整场不可估（仅已审窗口局部计划{result['draft_count']}条）"
    else:
        count = str(result['recommended_count']) if result['recommended_count'] is not None else '待完成盘点及同款确认，当前仅为草案'
    lines = ['# 单场单款批次方案', '', f"批次：{plan['batch_id']}｜款式：{plan['product']['name']}（{plan['product']['code']}）",
             f"覆盖：{'完整' if result['coverage_complete'] else '未完整'}；人工检索{result['searched_seconds']:.3f}/{plan['source']['duration_seconds']:.3f}秒；ASR转写{result['transcribed_seconds']:.3f}秒。",
             f"已确认同款原始窗口去重总量{result['confirmed_source_seconds']:.3f}秒（当前预设门槛{profile()['min_source_seconds']}秒）；待确认{result['pending_seconds']:.3f}秒。",
             f"已确认同款候选原区间去重总量{result['total_seconds']:.3f}秒；有效量{result['usable_seconds']:.3f}秒。",
             f"已分配{result['allocated_seconds']:.3f}秒；未分配{result['unallocated_seconds']:.3f}秒（均按源区间去重，交叠部分不重复计算）。",
             f'推荐条数：{count}。该数量来自语义规划和本批分配，不表示数学最大产量。',
             f"为什么不能再多：{plan['why_not_more']}", '', '源区间均为本场原直播绝对时间；原话为转写记录，非逐词听审结论。']
    if plan['coverage'].get('scope') == 'reviewed_windows_only':
        lines += ['', '范围：仅已审窗口的局部test；整场盘点未完成，推荐条数不可解释为全场产量。']
    for item in plan['items']:
        r = result['items'][item['item_id']]
        valid = item.get('adoption',{}).get('fingerprint') == r['adoption_fingerprint']
        pacing = f"{item['speed']}倍"
        if 'render_timeline' in r:
            timeline = r['render_timeline']
            pacing = '逐段倍速：' + '、'.join(f"{s['candidate_id']} {s['speed']:g}倍" for s in timeline['segments'])
            pacing += f"；结尾定格{timeline['end_hold']['seconds']:.3f}秒"
        lines += ['', f"## {item['item_id']}｜{item['mainline']}", f"完整性：{item['completeness']}",
                  f"原速{r['raw_duration']:.3f}秒；{pacing}；预计{r['expected_duration']:.3f}秒。采用：{'当前版本已记录' if valid else '待明确采用'}。"]
        for cid in item['order']:
            c = candidates[cid]
            lines += [f"- {cid}｜{c['start_seconds']:.3f}–{c['end_seconds']:.3f}秒｜{c['category']}｜{item['roles'][cid]}：{c['text'] or '纯展示；无须字幕依据见音频审核记录'}"]
    lines += ['', '## 未用候选处置']
    for cid,value in plan['exclusions'].items():
        c=candidates[cid]
        lines += [f"- {cid}｜{c['start_seconds']:.3f}–{c['end_seconds']:.3f}秒｜{c['category']}｜原话：{c['text'] or '纯展示'}；处置：{_exclusion(value)[0]}"]
    lines += ['', '## 辅助复用披露']
    for r in plan.get('reuse',[]):
        spans=[candidate_range(candidates[c]) for c in r['candidate_ids']]
        formatted='、'.join(f'{a:.3f}–{z:.3f}秒' for a,z in spans)
        lines += [f"- {' / '.join(r['items'])}：{', '.join(r['candidate_ids'])}；{formatted}；去重复用{seconds(spans):.3f}秒；{r['reason']}"]
    if not plan.get('reuse'):lines += ['- 无']
    lines += ['', '## 逐对内容相似性（人工判断，不保证平台结果）']
    for r in plan.get('pair_reviews',[]):
        lines += [f"- {' / '.join(r['items'])}｜主线：{r['mainline']}；口播：{r['speech']}；画面：{r['visual']}；依据：{r['basis']}；结论：{r['conclusion']}"]
    return '\n'.join(lines)+'\n'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['validate','report','adopt','pair-hashes'])
    p.add_argument('--plan',required=True,type=Path)
    p.add_argument('--output',type=Path)
    p.add_argument('--items',nargs='+')
    p.add_argument('--evidence',type=Path)
    p.add_argument('--at')
    p.add_argument('--task-record',type=Path)
    args=p.parse_args()
    if args.action=='pair-hashes':
        result=validate_plan(args.plan,check_pair_bindings=False)
        values=[{'items':r['items'],'content_hash':pair_content_hash(result['plan'],r['items'],result['candidates'])}
                for r in result['plan'].get('pair_reviews',[])]
        print(__import__('json').dumps(values,ensure_ascii=False,indent=2))
    elif args.action=='adopt':
        if not args.evidence or not args.at or not args.items: p.error('adopt需要--items、--evidence、--at')
        ep=local_path(args.evidence)
        value=adopt(args.plan,args.items,{'path':str(ep),'sha256':file_hash(ep)},args.at,args.task_record)
        print(__import__('json').dumps(value,ensure_ascii=False,indent=2))
    elif args.action=='report':
        content=report(args.plan)
        if args.output: atomic(local_path(args.output,False),content,plain=True)
        else: print(content)
    else:
        value=validate_plan(args.plan)
        print(__import__('json').dumps({k:v for k,v in value.items() if k not in ('plan','candidates','candidate_data')},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
