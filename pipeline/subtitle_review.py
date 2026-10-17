"""Read-only batch subtitle doubts from prepared selections and local reviews.

This collects recorded questions; it does not hear audio, resolve them, change
reviews or authorize rendering. Only record bindings are checked here; original
and rendered media are not rehashed. Existing subtitle preflight is required.
"""
import argparse
import json
from pathlib import Path

import batch_plan
from selector_support import file_hash, key, read
from quality_support import evidence_file, required, span as evidence_span


def _reference(value, selected, ids, source_range, audio=False):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('疑点证据须为实际文件引用')
    if audio and any(value.get(k) != expected for k, expected in (
            ('source_sha256', selected['original_sha256']),
            ('segment_ids', ids), ('range', source_range))):
        raise ValueError('短音频引用须绑定当前源、完整cue和精确源区间')
    batch_plan.local_path(value.get('path', ''))
    evidence_file(value)
    return dict(value)


def _doubts(selected, review):
    if review.get('selected_hash') != key(selected):
        raise ValueError('字幕审核与当前组合不匹配')
    segments, position, rows = selected['segments'], 0, []
    for cue in review.get('cues', []):
        ids = cue['segment_ids']
        group = segments[position:position+len(ids)]
        if (not ids or ids != [s['id'] for s in group]
                or len({s['clip_id'] for s in group}) != 1):
            raise ValueError('疑点须按组合顺序消费segment，不可跨剪辑接点')
        source_range = evidence_span([group[0]['start'], group[-1]['end']])
        before = segments[position-1] if position else None
        position += len(ids)
        after = segments[position] if position < len(segments) else None
        for kind in ('risks', 'corrections'):
            for raw in cue.get(kind, []):
                # Legacy risk text can be collected, but still fails new-render preflight.
                entry = {'reason':raw, 'level':'unknown'} if isinstance(raw, str) and kind == 'risks' else raw
                if not isinstance(entry, dict):
                    raise ValueError('字幕疑点格式无效')
                reason = required(entry.get('reason'), '疑点原因')
                candidates = [required(entry.get('to'), '纠错候选')] if kind == 'corrections' else entry.get('candidates', [])
                if (not isinstance(candidates, list)
                        or any(not isinstance(s, str) or not s.strip() for s in candidates)):
                    raise ValueError('候选须为原记录中的非空文字列表，未知用空数组')
                rows.append({
                    'kind':kind, 'segment_ids':ids, 'source_sha256':selected['original_sha256'],
                    'range':source_range, 'final_range':[group[0]['final_start'], group[-1]['final_end']],
                    'original_quote':''.join(s['text'] for s in group), 'quote_source':'transcript_not_listening',
                    'context_before':before['text'] if before and before['clip_id'] == group[0]['clip_id'] else None,
                    'context_after':after['text'] if after and after['clip_id'] == group[-1]['clip_id'] else None,
                    'candidates':candidates, 'from':entry.get('from'), 'reason':reason,
                    'level':entry.get('level'), 'fact_field':entry.get('fact_field'),
                    'status':('recorded_correction' if kind == 'corrections' else
                              'recorded_resolved' if entry.get('resolved') is True else 'unresolved'),
                    'source_media':{'path':selected['original'], 'sha256':selected['original_sha256'],
                                    'range':source_range, 'verification':'not_rechecked'},
                    'audio':_reference(entry.get('audio'), selected, ids, source_range, audio=True),
                    'evidence':_reference(entry.get('evidence'), selected, ids, source_range),
                })
    if position != len(segments):
        raise ValueError('仍有组合内segment未纳入待审字幕')
    return rows


def _plan_records(plan_path):
    """Recompute adoption record fingerprints without calling media validation."""
    plan = read(plan_path)
    if plan.get('version') != batch_plan.VERSION:
        raise ValueError('批次方案版本须为single-style-batch-v1')
    data = read(batch_plan.local_path(plan['job_path'])/'candidate-clips.json')
    candidates = {candidate['id']:candidate for candidate in data['candidates']}
    items = {item['item_id']:item for item in plan['items']}
    if len(candidates) != len(data['candidates']) or len(items) != len(plan['items']):
        raise ValueError('候选和批次条目ID不可重复')
    if Path(data['source_video']).resolve() != Path(plan['source']['path']).resolve():
        raise ValueError('候选记录与批次源路径不匹配')
    fingerprints = {iid:batch_plan._fingerprint(plan, item, candidates) for iid,item in items.items()}
    adopted = []
    for iid, item in items.items():
        adoption = item.get('adoption') or {}
        if adoption.get('fingerprint') == fingerprints[iid]:
            batch_plan.evidence(adoption.get('user_evidence'))
            batch_plan.time_value(adoption.get('adopted_at'))
            adopted.append(iid)
    if not adopted:
        raise ValueError('尚无当前版本已采用条目')
    return plan, items, fingerprints, adopted


def _prepared_selection(entry, composition, result, plan, item, fingerprint):
    selected_path = batch_plan.local_path(required(entry.get('selected'), 'prepare返回的selected路径'))
    selected = read(selected_path)
    required_fields = ('composition', 'original', 'original_sha256', 'video', 'video_sha256',
                       'transcript_sha256', 'speed', 'duration', 'segments', 'clips')
    if any(field not in selected for field in required_fields):
        raise ValueError('selected缺少prepare生成的必要字段')
    combo = result.get('best_combination') or {}
    context = result.get('task_context') or {}
    source = plan['source']
    if (result.get('version') != '0.2b' or result.get('status') != 'completed'
            or context.get('batch_adoption_fingerprint') != fingerprint
            or context.get('task_id') != item['task_id']
            or combo.get('order') != item['order'] or combo.get('speed') != item['speed']):
        raise ValueError('组合记录与当前采用版本不一致')
    if (Path(selected['composition']).resolve() != composition
            or Path(selected['original']).resolve() != Path(source['path']).resolve()
            or Path(result['material']['source_video']).resolve() != Path(source['path']).resolve()
            or selected['original_sha256'] != source['sha256']
            or result.get('source_sha256_after') != source['sha256']
            or context.get('source_sha256') != source['sha256']
            or Path(selected['video']).resolve() != Path(result['final_video']).resolve()
            or selected['speed'] != combo['speed']
            or selected['duration'] != result['metadata']['duration']
            or [clip['clip_id'] for clip in selected['clips']] != combo['order']):
        raise ValueError('selected与当前组合、源身份或时间映射记录不匹配')
    return selected_path, selected


def collect(manifest_path):
    manifest_path = batch_plan.local_path(manifest_path)
    manifest = read(manifest_path)
    plan_path = batch_plan.local_path(manifest['batch_plan'])
    plan, by_id, fingerprints, adopted = _plan_records(plan_path)
    records = manifest.get('items')
    if not isinstance(records, list):
        raise ValueError('清单items须列出组合与本地review引用')
    items, seen = [], set()
    for entry in records:
        composition = batch_plan.local_path(entry['composition'])
        result_path = composition/'result.json'
        result = read(result_path)
        context = result.get('task_context') or {}
        ref = context.get('batch_ref') or {}
        if (context.get('content_strategy', {}).get('version') != batch_plan.VERSION
                or batch_plan.local_path(ref.get('plan_path', '')) != plan_path):
            raise ValueError('各条待审字幕须属于清单指定的同一批次')
        item_id = ref.get('item_id')
        if item_id in seen:
            raise ValueError('同一批次条目不可重复汇总')
        if item_id not in adopted:
            raise ValueError('只汇总当前版本已采用条目')
        selected_path, selected = _prepared_selection(entry, composition, result, plan,
                                                      by_id[item_id], fingerprints[item_id])
        seen.add(item_id)
        review_path = batch_plan.local_path(entry['review'])
        review = read(review_path)
        items.append({'item_id':item_id, 'composition':str(composition),
                      'composition_sha256':file_hash(result_path),
                      'review':{'path':str(review_path), 'sha256':file_hash(review_path)},
                      'selected':{'path':str(selected_path), 'sha256':file_hash(selected_path)},
                      'selected_hash':key(selected),
                      'adoption_fingerprint':context['batch_adoption_fingerprint'],
                      'doubts':_doubts(selected, review)})
    missing = [item_id for item_id in adopted if item_id not in seen]
    return {'version':'subtitle-review-v1', 'batch_id':plan['batch_id'],
            'batch_plan':{'path':str(plan_path), 'sha256':file_hash(plan_path)},
            'collection_status':'partial' if missing else 'complete',
            'verification_scope':'record_bindings_only', 'source_media_verification':'not_rechecked',
            'missing_item_ids':missing, 'items':items, 'audio_review':'not_performed',
            'limitations':'仅检查已有记录绑定；未重新读取或核验原素材/成片。complete仅表示已列齐当前采用条目，'
                          '不表示原素材本轮已核验、核词或烧录预检通过；正式preflight仍须完整核验。'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(collect(args.manifest), ensure_ascii=False, indent=2))
