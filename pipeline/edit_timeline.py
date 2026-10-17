"""Read-only, source-bound editing timeline for an adopted batch item.

This validates media and feedback bindings. Task, product, adoption and content
gates remain with their existing callers; this module never records approval.
"""
import copy
from fractions import Fraction
import math
from pathlib import Path

from quality_support import timestamp
from runtime import ROOT
from selector_support import file_hash, key, read, validate_transcript
import source_windows

FPS = 30
SIZE = [1080, 1920]


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(label + '须为有限数值')
    return value


def _range(value, maximum):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('范围须为起止秒数组')
    start, end = (_number(n, '时间') for n in value)
    if not 0 <= start < end <= maximum:
        raise ValueError('范围超出实际源或窗口时长')
    return start, end


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + '须为非空文字')
    return value


def _reference(value, inputs, local=True):
    if not isinstance(value, dict):
        raise ValueError('证据须为路径和SHA-256引用')
    path = Path(_text(value.get('path'), '证据路径')).expanduser()
    if not path.is_absolute():
        raise ValueError('证据路径须为绝对路径')
    path = path.resolve(strict=True)
    if not path.is_file() or (local and not path.is_relative_to(ROOT.resolve())):
        raise ValueError('核词或抽取证据须为当前工作区文件')
    digest = file_hash(path)
    if digest != value.get('sha256'):
        raise ValueError('源或证据文件哈希不匹配')
    inputs[str(path)] = digest
    return path


def _boundary(entry, segment, retained, source, inputs):
    if (not isinstance(entry, dict) or entry.get('segment_id') != segment['id']
            or entry.get('source_range') != retained):
        raise ValueError('边界核词须绑定被截segment及实际保留源范围')
    _range(entry['source_range'], source['duration_seconds'])
    text = _text(entry.get('text'), '保留原话')
    evidence = entry.get('evidence')
    expected = {'source_sha256': source['sha256'], 'segment_ids': [segment['id']],
                'range': retained, 'text': text}
    if (not isinstance(evidence, dict)
            or evidence.get('type') not in ('audio_listening', 'verified_transcript')
            or any(evidence.get(k) != expected[k] for k in ('source_sha256', 'segment_ids', 'range'))):
        raise ValueError('边界核词证据源、segment或实际保留范围不匹配')
    _range(evidence['range'], source['duration_seconds'])
    proof = read(_reference(evidence, inputs))
    entries = proof.get('boundary_transcripts') if isinstance(proof, dict) else None
    if not isinstance(entries, list):
        raise ValueError('核词证明缺少boundary_transcripts')
    matches = [item for item in entries if isinstance(item, dict)
               and all(item.get(k) == value for k, value in expected.items())]
    if len(matches) != 1:
        raise ValueError('边界原话证明须唯一匹配源、segment、范围和保留文字')
    _range(matches[0]['range'], source['duration_seconds'])
    confirmation = matches[0].get('confirmation')
    if (not isinstance(confirmation, dict) or confirmation.get('status') != 'confirmed'
            or confirmation.get('type') not in ('user_message', 'audio_listening')):
        raise ValueError('边界核词缺少实际用户反馈或原声听审确认')
    _text(confirmation.get('actor'), '核词执行者')
    timestamp(confirmation.get('at'))
    quote = _text(confirmation.get('quote'), '确认原话')
    feedback = _reference(confirmation, inputs).read_text('utf-8-sig')
    if text not in quote or quote not in feedback:
        raise ValueError('保留原话或确认原话未出现在原始反馈文件')
    return text, dict(copy.deepcopy(evidence), confirmation=copy.deepcopy(confirmation))


def _build(edit_plan, order, candidates_by_id, source, confirmed_ranges, default_speed, transcript):
    if (not isinstance(edit_plan, dict) or type(edit_plan.get('schema_version')) is not int
            or edit_plan['schema_version'] != 1
            or set(edit_plan) - {'schema_version', 'clips', 'end_hold_seconds'}):
        raise ValueError('edit_plan须为schema_version=1且仅含允许字段')
    clips = edit_plan.get('clips')
    if (not isinstance(order, list) or not order or any(not isinstance(cid, str) for cid in order)
            or len(set(order)) != len(order) or not isinstance(candidates_by_id, dict)
            or not isinstance(clips, list) or any(not isinstance(c, dict) for c in clips)
            or [c.get('candidate_id') for c in clips] != order
            or not set(order) <= candidates_by_id.keys()):
        raise ValueError('编辑片段须与已采用order中的真实候选一一同序')
    if not 1 <= _number(default_speed, '默认speed') <= 1.5:
        raise ValueError('默认speed须为1至1.5')
    source_duration = _number(source.get('duration_seconds'), '原片时长')
    if source_duration <= 0:
        raise ValueError('原片时长须为正')
    inputs = {}
    original = _reference(source, inputs, local=False)
    canonical_source = dict(path=str(original), sha256=source['sha256'], duration_seconds=source_duration)
    validate_transcript(transcript)
    transcript_source = transcript['source']
    if (Path(transcript_source['path']).resolve(strict=True) != original
            or transcript_source['sha256'] != source['sha256']
            or transcript_source['duration_seconds'] != source_duration):
        raise ValueError('转写与原片身份或绝对时间轴不匹配')
    if not isinstance(confirmed_ranges, list) or not confirmed_ranges:
        raise ValueError('缺少已确认同款范围')
    confirmed = []
    for a, b in sorted(_range(value, source_duration) for value in confirmed_ranges):
        if confirmed and a <= confirmed[-1][1]:
            confirmed[-1][1] = max(b, confirmed[-1][1])
        else:
            confirmed.append([a, b])

    def covered(span):
        if not any(a <= span[0] < span[1] <= b for a, b in confirmed):
            raise ValueError('实际音频或画面源范围未被真实同款窗口覆盖')

    windows = {}

    def window(reference):
        path = _reference(reference, inputs)
        if path not in windows:
            mapping = source_windows.import_entry(path)
            item = read(path)
            if (Path(item['source']['path']).resolve() != original
                    or item['source']['sha256'] != source['sha256']
                    or item['source']['duration_seconds'] != source_duration):
                raise ValueError('抽取记录与当前原片身份不一致')
            _reference(item['window'], inputs)
            windows[path] = item, mapping
        return path, *windows[path]

    def bind(reference, a, b):
        path, item, mapping = window(reference)
        offset = mapping['offset_seconds']
        return {'extraction': {'path': str(path), 'sha256': inputs[str(path)]},
                'window': {'path': mapping['window_video'], 'sha256': item['window']['sha256']},
                'window_range': [a, b], 'source_range': [offset+a, offset+b],
                'source_sha256': source['sha256']}

    hold = _number(edit_plan.get('end_hold_seconds', 0), 'end_hold_seconds')
    if not 0 <= hold <= 2:
        raise ValueError('end_hold_seconds须为0至2秒')
    positions = {segment['id']: i for i, segment in enumerate(transcript['segments'])}
    segments, mapped_segments, mapped_clips = [], [], []
    total_frames, raw_duration = 0, 0
    audio_ranges = []
    for index, clip in enumerate(clips, 1):
        if set(clip) - {'candidate_id', 'extraction', 'window_range', 'speed', 'purpose',
                        'visual', 'crop', 'boundary_transcripts'}:
            raise ValueError('编辑片段存在未支持字段')
        cid = clip['candidate_id']
        candidate = candidates_by_id[cid]
        kind = candidate.get('kind', 'speech')
        if candidate.get('id') != cid or kind not in ('speech', 'visual'):
            raise ValueError('候选ID或类型不匹配')
        ca, cb = _range([candidate['start_seconds'], candidate['end_seconds']], source_duration)
        purpose = _text(clip.get('purpose'), 'purpose')
        speed = _number(clip.get('speed', default_speed), 'speed')
        if not 1 <= speed <= 1.5:
            raise ValueError('speed须为1至1.5')
        _, item, _ = window(clip.get('extraction'))
        a, b = _range(clip.get('window_range'), item['window']['duration_seconds'])
        audio = bind(clip['extraction'], a, b)
        sa, sb = audio['source_range']
        if not ca <= sa < sb <= cb:
            raise ValueError('实际保留原声超出已采用候选包络')
        covered([sa, sb])
        if any(min(sb, y) > max(sa, x) for x, y in audio_ranges):
            raise ValueError('同条实际原声源范围不能重复重叠')
        audio_ranges.append([sa, sb])
        exact = (Fraction(str(b)) - Fraction(str(a))) / Fraction(str(speed))
        frames = math.ceil(exact * FPS)
        duration = frames / FPS
        final_start, final_end = total_frames / FPS, (total_frames + frames) / FPS
        visual_input = clip.get('visual')
        if visual_input is not None:
            if not isinstance(visual_input, dict) or set(visual_input) != {'extraction', 'window_range'}:
                raise ValueError('visual须明确抽取引用及范围')
            _, visual_item, _ = window(visual_input['extraction'])
            va, vb = _range(visual_input['window_range'], visual_item['window']['duration_seconds'])
            if vb - va + 1e-9 < b - a:
                raise ValueError('替换画面不足以按当前速度覆盖原声')
            visual = bind(visual_input['extraction'], va, min(vb, va + duration*speed))
            visual['requested_window_range'] = [va, vb]
        else:
            visual = copy.deepcopy(audio)
        covered(visual['source_range'])
        crop = clip.get('crop')
        if crop is not None and (not isinstance(crop, dict) or set(crop) != {'x', 'y', 'width', 'height'}
                or any(type(v) is not int or v % 2 for v in crop.values())
                or crop['x'] < 0 or crop['y'] < 0 or crop['width'] <= 0 or crop['height'] <= 0):
            raise ValueError('crop须为偶数像素，起点非负且尺寸为正；实际画面边界由渲染再次检查')
        nonsync = visual['source_range'] != audio['source_range']
        segments.append({'id': f'clip-{index}', 'clip_id': cid, 'candidate_id': cid,
                         'purpose': purpose, 'speed': speed, 'frame_range': [total_frames, total_frames+frames],
                         'frame_count': frames, 'video_range': [final_start, final_end],
                         'audio': audio, 'visual': visual, 'crop': copy.deepcopy(crop),
                         'non_synchronous_visual': nonsync,
                         'visual_note': '同源非同期画面；口型仍须实看' if nonsync else '同区间原声画面',
                         'audio_padding_seconds': max(0, duration-float(exact)),
                         'visual_padding_seconds': max(0, duration-(visual['window_range'][1]-visual['window_range'][0])/speed)})
        boundaries = clip.get('boundary_transcripts', [])
        if (not isinstance(boundaries, list) or any(not isinstance(v, dict) for v in boundaries)
                or any(not isinstance(v.get('segment_id'), str) for v in boundaries)
                or len({v['segment_id'] for v in boundaries}) != len(boundaries)):
            raise ValueError('boundary_transcripts须按被截segment唯一列出')
        boundary_by_id = {value['segment_id']: value for value in boundaries}
        needed, segment_ids = set(), []
        if kind == 'speech':
            first, last = positions[candidate['start_segment_id']], positions[candidate['end_segment_id']]
            speech = transcript['segments'][first:last+1]
            if (not speech or ca != speech[0]['start'] or cb != speech[-1]['end']
                    or candidate.get('text') != ''.join(s['text'] for s in speech)):
                raise ValueError('原候选须保留完整ASR边界和原文')
            for segment in speech:
                start, end = max(sa, segment['start']), min(sb, segment['end'])
                if start >= end:
                    continue
                row = dict(id=segment['id'], clip_id=cid, start=start, end=end, text=segment['text'],
                           final_start=final_start+(start-sa)/speed, final_end=final_start+(end-sa)/speed,
                           speed=speed, asr_start=segment['start'], asr_end=segment['end'], asr_text=segment['text'])
                if [start, end] != [segment['start'], segment['end']]:
                    needed.add(segment['id'])
                    if segment['id'] not in boundary_by_id:
                        raise ValueError('被截ASR段缺少实际保留原话的核词证据')
                    row['text'], row['boundary_evidence'] = _boundary(
                        boundary_by_id[segment['id']], segment, [start, end], source, inputs)
                mapped_segments.append(row)
                segment_ids.append(segment['id'])
            if not segment_ids:
                raise ValueError('保留口播范围没有真实ASR段，不能伪称已核词')
        if set(boundary_by_id) != needed:
            raise ValueError('边界核词项须恰好对应实际被截ASR段')
        mapped_clips.append({'clip_id': cid, 'kind': kind, 'start': sa, 'end': sb,
                             'final_start': final_start, 'final_end': final_end, 'segment_ids': segment_ids,
                             'speed': speed, 'frame_range': [total_frames, total_frames+frames],
                             'audio_source_range': audio['source_range'], 'visual_source_range': visual['source_range'],
                             'crop': copy.deepcopy(crop), 'non_synchronous_visual': nonsync})
        raw_duration += b-a
        total_frames += frames
    hold_frames = math.floor(hold*FPS + .5)
    end_hold = {'requested_seconds': hold, 'frame_count': hold_frames, 'seconds': hold_frames/FPS,
                'video_range': [total_frames/FPS, (total_frames+hold_frames)/FPS],
                'visual': '最后输出帧定格', 'audio': 'silence'}
    total_frames += hold_frames
    if not 2 <= total_frames <= 70*FPS:
        raise ValueError('含定格成片须至少2帧且不得超过70秒')
    return {'schema_version': 1, 'kind': 'edit_timeline', 'edit_plan_hash': key(edit_plan),
            'transcript_content_hash': key(transcript), 'source': canonical_source,
            'fps': FPS, 'size': list(SIZE), 'raw_duration': raw_duration,
            'frame_count': total_frames, 'duration_seconds': total_frames/FPS,
            'inputs': inputs, 'segments': segments, 'end_hold': end_hold,
            'mapped_segments': mapped_segments, 'mapped_clips': mapped_clips}


def build(edit_plan, order, candidates_by_id, source, confirmed_ranges, default_speed, transcript):
    """Validate and return a deterministic timeline; never mutate inputs or files."""
    try:
        return _build(edit_plan, order, candidates_by_id, source, confirmed_ranges, default_speed, transcript)
    except (OSError, KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError('编辑计划或来源证据不完整: ' + str(error)) from error
