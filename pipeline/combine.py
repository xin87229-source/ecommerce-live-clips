"""V0.2b: render an adopted batch order or an explicitly resumed legacy selection."""
import argparse
import itertools
import json
import math
import time
from pathlib import Path

from media_adapter import MediaAdapter
from run import component, write_json
from selector_support import ROOT, read, file_hash, key, validate_transcript
from suggest import load_job, load_block
from selector_support import materialize, dedupe
from quality_support import normalize_content_strategy, task_gate
from runtime import profile, stage_config


def choose_explicit(candidates, annotations):
    """Validate one adopted order; never optimize or enumerate the inventory."""
    speed = annotations.get('speed')
    if type(speed) not in (int, float) or not math.isfinite(speed) or not 1 <= speed <= 1.5:
        raise ValueError('必须按原声提供1.0至1.5倍速')
    for field in ('speed_reason', 'content_complete_reason'):
        if not isinstance(annotations.get(field), str) or not annotations[field].strip():
            raise ValueError(f'显式组合缺少{field}')
    order = annotations.get('order')
    by_id = {c['id']: c for c in candidates}
    if (len(by_id) != len(candidates) or not isinstance(order, list) or not order
            or any(not isinstance(i, str) for i in order) or len(set(order)) != len(order)
            or any(i not in by_id for i in order)):
        raise ValueError('采用方案必须指定有效且不重复的候选ID顺序')
    selected = [by_id[i] for i in order]
    if all(c.get('kind') == 'visual' for c in selected):
        raise ValueError('整条没有真实口播，当前原声字幕链路不制作；保留规划排除原因')
    spans = [(c['start_seconds'], c['end_seconds']) for c in selected]
    if any(not all(type(x) in (int, float) and math.isfinite(x) for x in (a, b))
           or not 0 <= a < b for a, b in spans):
        raise ValueError('候选源区间必须有效')
    if any(min(b, d) > max(a, c) for (a, b), (c, d) in itertools.combinations(spans, 2)):
        raise ValueError('同条视频不能重复使用重叠源区间')
    raw = sum(b-a for a, b in spans)
    maximum = profile()['max_duration_seconds']
    if raw <= 0 or ('edit_plan' not in annotations and raw/speed > maximum):
        raise ValueError(f'采用方案成片须大于0且不超过{maximum}秒')
    return dict(candidate_ids=list(order), order=list(order), raw_duration=raw, speed=speed,
                speed_reason=annotations['speed_reason'], expected_duration=raw/speed,
                content_complete_reason=annotations['content_complete_reason'],
                rule_version='single-style-batch-v1', evaluated_orders=1,
                reason=annotations['content_complete_reason'])


def choose(candidates, topics, speed, speed_reason, short_material_reason=None):
    if type(speed) not in (int, float) or not 1.0 <= speed <= 1.5:
        raise ValueError('必须按原声语速提供1.0至1.5倍速')
    if not isinstance(speed_reason, str) or not speed_reason.strip():
        raise ValueError('必须记录倍速选择依据')
    if not 1 <= len(candidates) <= 5 or set(topics) != {c['id'] for c in candidates}:
        raise ValueError("需1至5条候选及完整语义分组")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 40 for v in topics.values()):
        raise ValueError("语义分组无效")
    choices = []
    # ponytail: at most 85 permutations; reuse existing scores without a new model service.
    for triple in itertools.chain.from_iterable(itertools.permutations(candidates, n)
                                               for n in range(min(3, len(candidates)), 0, -1)):
        count = len(triple)
        raw = sum(c['end_seconds'] - c['start_seconds'] for c in triple)
        if raw < 20 or raw / speed > 70:
            continue
        if any(min(a['end_seconds'], b['end_seconds']) > max(a['start_seconds'], b['start_seconds'])
               for a, b in itertools.combinations(triple, 2)):
            continue
        quality = sum(c['score'] for c in triple) / count * .4
        complement = len({topics[c['id']] for c in triple}) / count * 30
        order = sum(c['scores'][role]/cap for c, (role, cap) in
                    zip(triple, (('hook', 15), ('product', 25), ('experience', 15)))) / count * 15
        duration = max(0, 15 - max(78-raw, raw-90, 0))
        choices.append((quality+complement+order+duration, triple, raw, speed))
    if not choices:
        raise ValueError("没有完整且不重复、原速至少20秒、成片不超过70秒的组合，请重选候选；不能据此判定整款不足20秒")
    score, triple, raw, speed = max(choices, key=lambda x: (len(x[1]), x[0], -abs(x[2]/x[3]-60)))
    shortened = len(triple) < 3 or raw / speed < 50
    if shortened and (not isinstance(short_material_reason, str) or not short_material_reason.strip()):
        raise ValueError('短素材组合须记录检索范围、素材不足及缩短依据short_material_reason')
    if len(triple) < 3 and any(
            sum(c['end_seconds']-c['start_seconds'] for c in group) <= 105
            and all(min(a['end_seconds'], b['end_seconds']) <= max(a['start_seconds'], b['start_seconds'])
                    for a, b in itertools.combinations(group, 2))
            for group in itertools.combinations(candidates, 3)):
        raise ValueError('已有三段可在允许倍速内组合，先调整倍速或候选，不以减少段数绕过')
    return {'candidate_ids': [c['id'] for c in triple], 'order': [c['id'] for c in triple],
            'raw_duration': raw, 'speed': speed, 'speed_reason': speed_reason, 'expected_duration': raw/speed,
            'score': round(score, 3), 'evaluated_orders': len(choices),
            'rule_version': 'adaptive-clips-v1', 'short_material_reason': short_material_reason if shortened else None,
            'reason': ' → '.join(c['category'] for c in triple) + '。优先三段，按质量、互补、顺序和时长综合评分。'
                      + (f' 缩短依据：{short_material_reason}' if shortened else '')}


def reviewed_material(review, data, manifest, source_hash, cart_required=None, selected_ids=None):
    """Review the full inventory; require matching identity for every rendered unit."""
    if (review.get('authorized_by_user') is not True
            or review.get('candidates_hash') != key(data)
            or review.get('source_sha256') != source_hash):
        raise ValueError('整场候选审核授权或哈希不匹配')
    material = review['material']
    if (not material.get('product_name') or not material.get('product_code')
            or Path(material['source_video']).resolve() != Path(manifest['source_video']).resolve()):
        raise ValueError('整场候选缺少精确商品或源文件记录')
    evidence = review['candidate_evidence']
    candidate_ids = {c['id'] for c in data['candidates']}
    if set(evidence) != candidate_ids:
        raise ValueError('必须逐条审核全部候选')
    batch = data.get('content_strategy', {}).get('version') == 'single-style-batch-v1'
    if batch and (not isinstance(selected_ids, list) or not selected_ids
                  or len(selected_ids) != len(set(selected_ids)) or not set(selected_ids) <= candidate_ids):
        raise ValueError('批次候选审核须绑定本次实际组合顺序')
    required_matches = set(selected_ids) if batch else candidate_ids
    for cid, item in evidence.items():
        if (type(item.get('matched')) is not bool or not item.get('visual') or not item.get('speech')
                or (cid in required_matches and item['matched'] is not True)):
            raise ValueError('候选商品尚未完成画面和讲解核验')
        if item['matched'] is False and (not isinstance(item.get('reason'), str) or not item['reason'].strip()):
            raise ValueError('未采用且同款未通过的候选须保留具体排除原因')
    return material


def execute(job_path, annotations_path, task_id, config_path, source_review=None, task_record=None):
    if source_review is None:
        raise ValueError('组合须提供逐候选source-review；目录名不替代同款证据')
    started = time.perf_counter()
    job, manifest = load_job(job_path, task_record)
    source = Path(manifest['source_video']).resolve(strict=True)
    context = task_gate(task_record, source, manifest)
    output_root = (ROOT/'outputs').resolve()
    if output_root.parent != ROOT.resolve():
        raise ValueError("outputs不可重定向到项目外")
    t = read(manifest['transcript_cache'])
    validate_transcript(t)
    before = file_hash(source)
    if before != t['source']['sha256']:
        raise ValueError("素材与转写哈希不匹配")
    data = read(job/'candidate-clips.json')
    if normalize_content_strategy(data.get('content_strategy')) != context['content_strategy']:
        raise ValueError('候选内容策略与当前任务不匹配，请重新prepare并按当前等级选段')
    source_review = Path(source_review).resolve(strict=True)
    if not source_review.is_relative_to((ROOT/'private-context').resolve()):
        raise ValueError('整场审核记录只能来自本地私有目录')
    review = read(source_review)
    annotations = read(annotations_path)
    if annotations['candidates_hash'] != key(data):
        raise ValueError("语义分组与候选不匹配")
    validated = {}
    for entry in manifest['blocks']:
        b = load_block(job, manifest, entry['id'])
        selection = read(job/'selections'/(b['id']+'.json'))
        if selection['block_hash'] != b['hash']:
            raise ValueError("候选块已改变")
        validated.update({c['id']: c for c in materialize(b, selection['candidates'], bool(manifest.get('material')))})
    if context['content_strategy'].get('version') == 'single-style-batch-v1':
        # Reproduce merge's deterministic risk flags before comparing the complete pool.
        validated = {c['id']: c for c in dedupe(list(validated.values()), preserve_all=True)[0]}
    for c in data['candidates']:
        if c != validated.get(c['id']):
            raise ValueError("候选必须由已校验segment ID完整映射")
    batch_gate = None
    if context['content_strategy'].get('version') == 'single-style-batch-v1':
        combo = choose_explicit(data['candidates'], annotations)
        from batch_plan import execution_gate
        batch_gate = execution_gate(context, stage='render', candidates=data,
                                    order=combo['order'], speed=combo['speed'],
                                    edit_plan=annotations.get('edit_plan'))
        if 'render_timeline' in batch_gate:
            timeline = batch_gate['render_timeline']
            combo.update(edit_plan=annotations['edit_plan'], raw_duration=timeline['raw_duration'],
                         expected_duration=timeline['duration_seconds'])
        context = dict(context, batch_adoption_fingerprint=batch_gate['adoption_fingerprint'])
    else:
        combo = choose(data['candidates'], annotations['topics'], annotations.get('speed'), annotations.get('speed_reason'), annotations.get('short_material_reason'))
    material = reviewed_material(review, data, manifest, before, context.get('cart_required'),
                                 selected_ids=combo['order'])
    product = context['product']
    identity_fields = [('code', 'product_code')]
    if context.get('cart_required') is True:
        identity_fields.append(('id', 'product_id'))
    if material['product_name'] != product['name'] or any(product.get(k) and product[k] != material.get(m) for k,m in identity_fields):
        raise ValueError('候选审核商品与制作前任务卡不同')
    directory = output_root / (component(task_id, 'task_id') + '-v0.2b')
    if source.is_relative_to(directory.resolve()) or directory.resolve().is_relative_to(source.parent):
        raise ValueError('本次输入输出不可重叠')
    directory.mkdir(parents=True, exist_ok=False)
    for name in ('final', 'preview', 'logs'):
        (directory/name).mkdir()
    result = {'version':'0.2b', 'material':material, 'source_sha256_before':before,
              'best_combination':combo, 'token_usage':None, 'status':'failed'}
    result.update(task_record=context['task_record'], task_context=context,
                  candidates_hash=key(data), content_decision_status='not_reviewed')
    if batch_gate is not None:
        result['batch_execution'] = batch_gate
        if 'render_timeline' in batch_gate:
            result['render_timeline'] = batch_gate['render_timeline']
    result['source_review'] = review
    try:
        media = MediaAdapter(stage_config(config_path), directory/'logs/media.log')
        meta = media.probe(source)
        if (any(not math.isfinite(t) or t < 0 for t in (meta['video_start'], meta['audio']['start']))
                or not 1 <= meta['fps'] <= 120):
            raise ValueError('原素材音画起点或帧率不支持')
        selected = [validated[i] for i in combo['order']]
        segments = [(c['start_seconds'], c['end_seconds']) for c in selected]
        raw = directory/'preview/joined.mp4'
        final = directory/'final'/(directory.name+'.mp4')
        delivery_size = profile()['size']
        cut_start = time.perf_counter()
        if 'render_timeline' in result:
            from review_cut import render, verify_inputs
            timeline = result['render_timeline']
            verify_inputs(timeline)
            render(media, timeline, final)
            verify_inputs(timeline)
        else:
            media.render(source, segments, meta, 'pad', None, delivery_size, raw,
                         limit_duration=True, exact_size=True)
            media.check(raw, combo['raw_duration'], delivery_size)
            speed = f"{combo['speed']:.12f}"
            media.write_media(['-i',str(raw),'-filter_complex',f'[0:v:0]setpts=(PTS-STARTPTS)/{speed}[v];[0:a:0]asetpts=PTS-STARTPTS,atempo={speed}[a]',
                              '-map','[v]','-map','[a]','-map_metadata','-1','-map_chapters','-1',
                              '-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',
                              '-c:a','aac','-b:a','192k','-ar','48000','-movflags','+faststart',
                              '-t',f"{combo['expected_duration']:.6f}",str(final)], final, delivery=True)
        result['render_seconds'] = time.perf_counter()-cut_start
        final_meta, checks = media.check(final, combo['expected_duration'], delivery_size, delivery=True)
        if ('render_timeline' in result
                and final_meta['frame_timing']['frame_count'] != result['render_timeline']['frame_count']):
            raise ValueError('实际成片帧数与已采用编辑时间轴不符')
        if not 0 < final_meta['duration'] <= profile()['max_duration_seconds'] + 1/profile()['fps']:
            raise ValueError('成片时长无效或超过当前预设上限（最多一帧编码舍入）')
        frames = media.look(final, final_meta['duration'], directory/'preview')
        after = file_hash(source)
        if after != before:
            raise ValueError('原始素材哈希改变')
        result.update(status='completed', business_status='成片已生成，观看情况在复盘记录', final_video=str(final),
                      metadata=final_meta, checks=checks, frames=frames, source_sha256_after=after,
                      visual_review='pending', source_unchanged=True, video_sha256=file_hash(final))
        write_json(directory/'candidate-clips.json', dict(data, best_combination=combo))
        pacing = f"{combo['speed']:.4f}x"
        if 'render_timeline' in result:
            timeline = result['render_timeline']
            pacing = '逐段倍速：' + '、'.join(f"{s['candidate_id']} {s['speed']:g}x" for s in timeline['segments'])
            pacing += f"；结尾定格{timeline['end_hold']['seconds']:.3f}秒"
        (directory/'组合说明.md').write_text(f"# {material['product_name']}｜组合说明\n\n{combo['reason']}\n\n顺序：{' → '.join(combo['order'])}\n\n采用原声{combo['raw_duration']:.3f}秒；{pacing}；成片{final_meta['duration']:.3f}秒。\n",encoding='utf-8')
    except Exception as error:
        result['error'] = str(error)
        raise
    finally:
        result['total_seconds'] = time.perf_counter()-started
        write_json(directory/'result.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job',required=True,type=Path)
    parser.add_argument('--annotations',required=True,type=Path)
    parser.add_argument('--task-id',required=True)
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--source-review',required=True,type=Path,help='用户授权Codex逐条核验全部候选及排除原因的本地记录')
    parser.add_argument('--task-record',type=Path,help='旧任务恢复时补当前任务卡')
    args = parser.parse_args()
    print(json.dumps(execute(args.job,args.annotations,args.task_id,args.config,args.source_review,args.task_record),ensure_ascii=False,indent=2))
