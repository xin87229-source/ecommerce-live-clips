"""Import complete, reviewed zero-origin window jobs onto one canonical live timeline.

No ASR, decoding, cutting or recognition occurs here. Mapping offsets must come
from existing evidence, including TS mappings; no timestamps are guessed.
"""
import argparse
import copy
import json
import math
from pathlib import Path

from batch_plan import VERSION, evidence, interval, local_path, union_ranges
from quality_support import task_gate
from selector_support import ROOT, PROMPT, atomic, dedupe, file_hash, key, materialize, read, validate_transcript


def merge_jobs(spec_path, output_path):
    spec_path = local_path(spec_path)
    spec = read(spec_path)
    source = spec['source']
    canonical = Path(source['path']).resolve(strict=True)
    if file_hash(canonical) != source.get('sha256'):
        raise ValueError('原直播源哈希与导入说明不匹配')
    duration = source['duration_seconds']
    if type(duration) not in (int,float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('原直播时长无效')
    destination = local_path(output_path,False)
    if not destination.is_relative_to((ROOT/'private-context/selector-v02a').resolve()):
        raise ValueError('标准job须在本地private-context/selector-v02a，供既有组合入口读取')
    if destination.exists():
        raise FileExistsError('导入不覆盖原job或既有合并版本')
    imports = spec.get('jobs', [])
    if not imports:
        raise ValueError('至少提供一个已经完整转写的窗口job')
    prepared = []
    for item in imports:
        job = local_path(item['job_path'])
        manifest = read(job/'manifest.json')
        tp = local_path(manifest['transcript_cache'])
        if file_hash(tp) != manifest.get('transcript_hash'):
            raise ValueError('窗口完整转写哈希改变')
        transcript = read(tp)
        validate_transcript(transcript)
        window = Path(manifest['source_video']).resolve(strict=True)
        sha = file_hash(window)
        if (transcript['source'].get('sha256') != sha
                or Path(transcript['source']['path']).resolve() != window):
            raise ValueError('窗口原文件与完整转写身份不符')
        offset = item.get('offset_seconds')
        if type(offset) not in (int,float) or not math.isfinite(offset) or offset < 0:
            raise ValueError('窗口offset须为已有证据确认的非负偏移')
        if (transcript['source'].get('audio_offset_seconds') != 0
                or type(transcript.get('audio_audit',{}).get('first')) not in (int,float)
                or abs(transcript['audio_audit']['first']) > .05):
            raise ValueError('只导入已确认零起点窗口转写，不猜TS或音视频偏移')
        end = offset + transcript['source']['duration_seconds']
        mapping = item.get('mapping', {})
        if (mapping.get('source_sha256') != source['sha256'] or mapping.get('window_sha256') != sha
                or mapping.get('offset_seconds') != offset or interval(mapping.get('range')) != (offset,end)
                or end > duration):
            raise ValueError('窗口offset映射与原源/窗口哈希或范围不符')
        evidence(mapping.get('evidence'))
        blocks, coverage, included_segments = [], [], set()
        by_id = {s['id']:s for s in transcript['segments']}
        for entry in manifest['blocks']:
            block_path = job/'chunks'/(entry['id']+'.json')
            selection_path = job/'selections'/(entry['id']+'.json')
            block, selection = read(block_path),read(selection_path)
            if (block.get('hash') != entry.get('hash')
                    or key({k:v for k,v in block.items() if k!='hash'}) != block['hash']
                    or selection.get('block_hash') != block['hash']):
                raise ValueError('窗口块或候选选择哈希不匹配')
            if any(by_id.get(s['id']) != s for s in block['segments']):
                raise ValueError('窗口块segment内容与完整转写不匹配')
            included_segments.update(s['id'] for s in block['segments'])
            candidates = materialize(block,selection['candidates'],bool(manifest.get('material')))
            blocks.append((block,selection,candidates))
            coverage.append([block['core_start'],block['core_end']])
        if (union_ranges(coverage) != [[0,transcript['source']['duration_seconds']]]
                or included_segments != set(by_id)):
            raise ValueError('窗口job未完整分块/分类，不能以局部候选当完整导入')
        prepared.append({'job':job,'manifest':manifest,'transcript':transcript,'blocks':blocks,
                         'offset':offset,'end':end,'sha':sha,'mapping':mapping,
                         'manifest_sha256':file_hash(job/'manifest.json'),'transcript_sha256':file_hash(tp)})
    prepared.sort(key=lambda item:item['offset'])
    if any(left['end'] > right['offset'] for left,right in zip(prepared,prepared[1:])):
        raise ValueError('导入窗口在原直播时间轴重叠；先解决重叠转写，不自动去掉原话')
    # Validate actual first/middle/last evidence on the original task card.
    # Deliberately omit duration: this does NOT request or permit full-live ASR.
    context = task_gate(spec['task_record'],canonical)
    if context['content_strategy'] != {'version':VERSION}:
        raise ValueError('合并任务须采用single-style-batch-v1策略')
    confirmed = union_ranges(w['range'] for w in context['confirmed_windows'])
    for item in prepared:
        if not any(a <= item['offset'] and item['end'] <= z for a,z in confirmed):
            raise ValueError('导入窗口未被原源任务卡真实同款首中末/原声证据覆盖')
    segments, blocks, selections, all_candidates, provenance = [], [], {}, [], []
    for item in prepared:
        id_map = {}
        for old in item['transcript']['segments']:
            new = dict(old,id=f's{len(segments)+1:06d}',start=old['start']+item['offset'],end=old['end']+item['offset'])
            id_map[old['id']] = new['id']
            segments.append(new)
        mapped_by_id = {s['id']:s for s in segments}
        candidate_map = {}
        for old_block, old_selection, old_candidates in item['blocks']:
            block = {'id':f'block-{len(blocks)+1:03d}',
                     'core_start':old_block['core_start']+item['offset'],
                     'core_end':old_block['core_end']+item['offset'],
                     'segments':[mapped_by_id[id_map[s['id']]] for s in old_block['segments']],
                     'content_strategy':{'version':VERSION}}
            block['hash'] = key(block)
            proposals = []
            for original in old_selection['candidates']:
                proposal = copy.deepcopy(original)
                if proposal.get('kind') == 'visual':
                    for field in ('start_seconds','end_seconds'):
                        proposal[field] += item['offset']
                    proposal['visual_review']['at'] += item['offset']
                    proposal['speech_review']['range'] = [t+item['offset'] for t in proposal['speech_review']['range']]
                else:
                    for field in ('start_segment_id','end_segment_id'):
                        proposal[field] = id_map[proposal[field]]
                proposals.append(proposal)
            selection = {'block_hash':block['hash'],'candidates':proposals}
            new_candidates = materialize(block,proposals,True)
            for old,new in zip(old_candidates,new_candidates):
                candidate_map[old['id']] = new['id']
            blocks.append(block)
            selections[block['id']] = selection
            all_candidates.extend(new_candidates)
        provenance.append({'job_path':str(item['job']),'manifest_sha256':item['manifest_sha256'],
                           'transcript_sha256':item['transcript_sha256'],'window_sha256':item['sha'],
                           'offset_seconds':item['offset'],'source_range':[item['offset'],item['end']],
                           'mapping':item['mapping'],'segment_id_map':id_map,'candidate_id_map':candidate_map})
    if len({c['id'] for c in all_candidates}) != len(all_candidates):
        raise ValueError('合并后候选ID重复，须先解决源窗口候选重复')
    first_pass_count = len(all_candidates)
    all_candidates, removed = dedupe(all_candidates,preserve_all=True)
    t = {'schema_version':1,'status':'complete','coverage_scope':'reviewed_windows_only',
         'full_source_transcribed':False,
         'source':{'path':str(canonical),'sha256':source['sha256'],'duration_seconds':duration,
                   'audio_offset_seconds':None},
         'asr':{'config_hash':key([p['transcript_sha256'] for p in provenance]),
                'method':'import-existing-window-jobs-no-asr'},
         'covered_ranges':[[i['offset'],i['end']] for i in prepared],
         'provenance':provenance,'segments':segments,'segments_hash':key(segments)}
    validate_transcript(t)
    product = context['product']
    material = {'date':context.get('material_date'),'product_name':product['name'],
                'product_code':product['code'],'product_id':product.get('id'),'source_video':str(canonical)}
    data = {'source_video':str(canonical),'transcript_cache':str(destination/'transcript.json'),
            'content_strategy':{'version':VERSION},'status':'pending_human_review',
            'candidates':all_candidates,'material':material,
            'counts':{'first_pass':first_pass_count,'deduped':len(all_candidates),'final':len(all_candidates)},
            'removed':removed,
            'coverage_scope':'reviewed_windows_only','token_usage':None}
    # All validation precedes the first output write. Existing inputs remain untouched.
    destination.mkdir(parents=True,exist_ok=False)
    atomic(destination/'transcript.json',t)
    for block in blocks:
        atomic(destination/'chunks'/(block['id']+'.json'),block)
        atomic(destination/'selections'/(block['id']+'.json'),selections[block['id']])
    manifest = {'version':'0.2b','source_video':str(canonical),'transcript_cache':str(destination/'transcript.json'),
                'transcript_hash':file_hash(destination/'transcript.json'),'prompt_hash':file_hash(PROMPT),
                'blocks':[{k:v for k,v in block.items() if k!='segments'} for block in blocks],
                'material':material,'task_record':context['task_record'],'task_context':context,
                'coverage_scope':'reviewed_windows_only','import_spec':str(spec_path),
                'import_spec_sha256':file_hash(spec_path),'provenance':provenance}
    atomic(destination/'manifest.json',manifest)
    atomic(destination/'candidate-clips.json',data)
    return {'job_path':str(destination),'candidate_count':len(all_candidates),
            'coverage_scope':'reviewed_windows_only','provenance':provenance}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['merge-jobs'])
    parser.add_argument('--spec',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    print(json.dumps(merge_jobs(args.spec,args.output),ensure_ascii=False,indent=2))


if __name__=='__main__': main()
