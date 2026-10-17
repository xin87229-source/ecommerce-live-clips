"""Inspect an original live, extract explicit zero-origin windows, and record actual review.

Every output stays in the selected private workspace. A sampled frame or a media
hash is evidence of bytes and timing, never an automatic same-product decision.
"""
import argparse
import copy
import json
import math
import uuid
from datetime import datetime
from pathlib import Path

from media_adapter import MediaAdapter
import ffmpeg_subset
from quality_support import evidence_file, local_records, no_resubmit, timestamp, validate_task
from run_records import initialize, locked
from runtime import ROOT, config, profile
from selector_support import atomic, file_hash, key, read

WINDOWS = ROOT / 'private-context' / 'source-windows'
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.mkv', '.flv', '.ts', '.webm', '.m4v', '.avi'}


def original_context(task_record):
    card_path = Path(task_record).resolve(strict=True)
    if card_path.name != 'run-state.json' or not card_path.is_relative_to((ROOT / 'outputs').resolve()):
        raise ValueError('原任务卡必须在当前工作目录outputs中')
    card = read(card_path)
    context = card.get('task_context') or {}
    source = Path(context.get('source_video', '')).resolve(strict=True)
    if (not source.is_file() or source.suffix.lower() not in VIDEO_EXTENSIONS
            or source.is_relative_to(ROOT.resolve()) or ROOT.resolve().is_relative_to(source.parent)):
        raise ValueError('原始素材必须是工作目录外的独立视频文件，不能把旧剪辑或产物当原片')
    configured = config().get('source_root')
    if configured and not source.is_relative_to(Path(configured).resolve(strict=True)):
        raise ValueError('原始素材不在本地配置的source_root中')
    if file_hash(source) != context.get('source_sha256'):
        raise ValueError('原始素材哈希与任务卡不同')
    if context.get('profile') != profile() or context.get('scope') not in ('production', 'test'):
        raise ValueError('任务范围或当前预设与原任务卡不符')
    evidence_file((context.get('product') or {}).get('reference') or {})
    if context.get('cross_device_state') != 'local_records_checked':
        raise ValueError('本地历史尚未核对')
    return card_path, card, context, source


def _new_folder(task_id, prefix):
    folder = WINDOWS / task_id / f'{prefix}-{uuid.uuid4().hex[:12]}'
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def _media(folder):
    return MediaAdapter(config(), folder / 'media.log')


def _source_meta(media, source):
    meta = media.probe(source)
    starts = (meta.get('video_start'), (meta.get('audio') or {}).get('start'))
    if (any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in starts)
            or type(meta.get('fps')) not in (int, float) or not 1 <= meta['fps'] <= 120):
        raise ValueError('原片音画起点或帧率无法作精确窗口映射')
    return meta


def inspect(task_record):
    card_path, _, context, source = original_context(task_record)
    folder = _new_folder(context['task_id'], 'inspection')
    media = _media(folder)
    before = file_hash(source)
    meta = _source_meta(media, source)
    frames = media.look(source, meta['duration'], folder)
    if file_hash(source) != before:
        raise ValueError('检查期间原始素材发生变化')
    samples = []
    for fraction, path in zip((.1, .5, .9), frames):
        samples.append({'path': str(path), 'sha256': file_hash(path),
                        'source_at': meta['duration'] * fraction, 'review_status': 'unknown'})
    result = {'schema_version': 1, 'status': 'needs_actual_product_review',
              'task_record': str(card_path), 'source': {'path': str(source), 'sha256': before},
              'metadata': meta, 'sample_frames': samples,
              'limits': '采样帧不证明全场覆盖或同款；需在原声和画面核验后显式抽取窗口'}
    atomic(folder / 'inspection.json', result)
    return dict(result, inspection_path=str(folder / 'inspection.json'))


def _verify_frame_grid(media, source, start, end, fps, video_start=0):
    # Decode PTS for the selected span; a trim boundary between source frames
    # would shift visible content relative to the claimed original offset.
    requested_start = video_start + start
    requested_end = video_start + end
    begin = max(0, requested_start - 20)
    length = requested_end - begin + 20
    raw = json.loads(media.command('ffprobe', ['-v', 'error', '-select_streams', 'v:0',
                            '-read_intervals', f'{begin:.6f}%+{length:.6f}',
                            '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(source)]))
    pts = sorted(float(frame['best_effort_timestamp_time']) for frame in raw.get('frames', [])
                 if frame.get('best_effort_timestamp_time') is not None)
    expected = 1 / fps
    if not pts or any(not math.isfinite(point) for point in pts):
        raise ValueError('窗口原片帧时间戳缺失或无效')
    aligned_start = min(pts, key=lambda point: abs(point-requested_start))
    aligned_end = min(pts, key=lambda point: abs(point-requested_end))
    if (abs(aligned_start-requested_start) > expected/2 + .002
            or abs(aligned_end-requested_end) > expected/2 + .002
            or aligned_start >= aligned_end):
        raise ValueError('请求窗口边界无法对应原片实际视频帧；须重新选择源时间范围')
    chosen = [point for point in pts if aligned_start <= point <= aligned_end]
    if (len(chosen) < 2
            or any(abs(right-left-expected) > .002 for left, right in zip(chosen, chosen[1:]))):
        raise ValueError('窗口源画面时间戳不连续或非恒定帧率，拒绝猜测原时间映射')
    return {'first_pts': chosen[0], 'last_pts': chosen[-1], 'frame_count': len(chosen),
            'aligned_start_seconds': aligned_start-video_start,
            'aligned_end_seconds': aligned_end-video_start,
            'requested_start_pts': requested_start, 'requested_end_pts': requested_end,
            'video_start_pts': video_start,
            'expected_step_seconds': expected, 'max_step_error_seconds': max(
                abs(right-left-expected) for left, right in zip(chosen, chosen[1:]))}


def _verify_audio_span(media, source, start_pts, end_pts, sample_rate):
    """Require real audio throughout this absolute-PTS span before resampling.

    Stream starts may differ at the beginning of a long recording. That says
    nothing about a later selected window. Check the selected source frames so
    aresample cannot silently replace missing original audio with padding.
    """
    if type(sample_rate) not in (int, float) or not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError('原片音频采样率无效，不能核对时间覆盖')
    begin = max(0, start_pts - 20)
    # Seeking explicitly to 0 can drop the first AAC frame after encoder priming.
    # An omitted start reads from the actual beginning and preserves that frame.
    interval_start = f'{begin:.6f}' if begin else ''
    raw = json.loads(media.command('ffprobe', ['-v', 'error', '-select_streams', 'a:0',
                    '-read_intervals', f'{interval_start}%+{end_pts-begin+20:.6f}',
                    '-show_entries', 'frame=best_effort_timestamp_time,nb_samples',
                    '-of', 'json', str(source)]))
    spans = []
    for frame in raw.get('frames', []):
        try:
            point = float(frame['best_effort_timestamp_time'])
            samples = int(frame['nb_samples'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('原片音频帧时间戳或采样数缺失，不能核对覆盖') from None
        if not math.isfinite(point) or samples <= 0:
            raise ValueError('原片音频帧时间戳或采样数无效，不能核对覆盖')
        finish = point + samples / sample_rate
        if point < end_pts and finish > start_pts:
            spans.append((point, finish))
    tolerance = .002  # Covers container timestamp rounding, not an audio frame.
    if (not spans or spans[0][0] > start_pts + tolerance
            or spans[-1][1] < end_pts - tolerance):
        raise ValueError('原片音频未完整覆盖所选窗口；请避开缺失音频的起止部分')
    gaps = [right[0] - left[1] for left, right in zip(spans, spans[1:])]
    if any(abs(gap) > tolerance for gap in gaps):
        raise ValueError('原片音频在所选窗口内不连续，拒绝静默补齐或错移原声')
    return {'first_pts': spans[0][0], 'last_end_pts': spans[-1][1],
            'frame_count': len(spans), 'requested_start_pts': start_pts,
            'requested_end_pts': end_pts, 'tolerance_seconds': tolerance,
            'max_gap_seconds': max((abs(gap) for gap in gaps), default=0)}


def extract(task_record, start, end):
    card_path, card, context, source = original_context(task_record)
    if (card.get('status') != 'NOT_UPLOADED_NOT_SUBMITTED'
            or card.get('submitted') is not False or context.get('batch_ref') or
            context.get('batch_adoption_fingerprint')):
        raise ValueError('任务状态已有进展、采用或保护，不得新增原片窗口')
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not all(math.isfinite(value) for value in (start, end)) or not 0 <= start < end):
        raise ValueError('窗口须显式给出有效的原片起止秒数')
    for old in (WINDOWS / context['task_id']).glob('window-*/extraction.json'):
        prior = read(old)
        a, b = prior['source_span']
        if min(end, b) > max(start, a):
            raise ValueError('新窗口与既有原片窗口重叠；先处理重叠，不自动删除原话')
    folder = _new_folder(context['task_id'], 'window')
    media = _media(folder)
    before = file_hash(source)
    meta = _source_meta(media, source)
    if end > meta['duration'] or end - start < max(1, 3/meta['fps']):
        raise ValueError('窗口超出原片或过短，不能提供首中末及完整口播审核')
    frame_grid = _verify_frame_grid(media, source, start, end, meta['fps'], meta['video_start'])
    aligned_start = frame_grid['aligned_start_seconds']
    aligned_end = frame_grid['aligned_end_seconds']
    source_audio = _verify_audio_span(media, source, frame_grid['first_pts'], frame_grid['last_pts'],
                                     meta['audio']['sample_rate'])
    from transcribe_adapter import audio_audit
    window = folder / 'window.mp4'
    args = ffmpeg_subset.cut(source, [(aligned_start, aligned_end)], meta, '', window)
    args = [*args[:-1], '-t', f'{aligned_end-aligned_start:.6f}', args[-1]]
    media.write_media(args, window)
    win_meta = media.probe(window)
    actual = win_meta['duration']
    tolerance = min(1/meta['fps'], .05)
    if (abs(win_meta['video_start']) > .001 or abs(win_meta['audio']['start']) > .001
            or not aligned_end-aligned_start - tolerance <= actual <= aligned_end-aligned_start + tolerance):
        raise ValueError('窗口成品起点或实际时长不支持安全原时间映射')
    if aligned_start + actual > meta['duration'] + .001:
        raise ValueError('窗口实际末端超出原直播')
    window_audio = audio_audit(window, win_meta)
    media.command('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-nostdin', '-xerror',
                    '-i', str(window), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'])
    audio = folder / 'audio-review.m4a'
    media.write_media(['-i', str(window), '-vn', '-c:a', 'copy', str(audio)], audio)
    frames = media.look(window, actual, folder)
    if file_hash(source) != before:
        raise ValueError('抽取期间原始素材发生变化，窗口证据失效')
    samples = []
    for fraction, path in zip((.1, .5, .9), frames):
        window_at = actual * fraction
        samples.append({'path': str(path), 'sha256': file_hash(path),
                        'window_at': window_at, 'source_at': aligned_start + window_at})
    result = {'schema_version': 1, 'task_id': context['task_id'], 'task_record': str(card_path),
              'source': {'path': str(source), 'sha256': before, 'duration_seconds': meta['duration']},
              'requested_range': [start, end],
              'source_span': [aligned_start, aligned_start+actual],
              'offset_seconds': aligned_start, 'frame_grid': frame_grid,
              'source_timeline': {'video_start_seconds': meta['video_start'],
                                  'audio_start_seconds': meta['audio']['start'],
                                  'audio_minus_video_start_seconds': meta['audio']['start']-meta['video_start']},
              'window_audio_audit': window_audio,
              'source_audio_audit': source_audio,
              'window': {'path': str(window), 'sha256': file_hash(window), 'duration_seconds': actual},
              'sample_frames': samples,
              'audio_review': {'path': str(audio), 'sha256': file_hash(audio)},
              'review_status': 'unknown'}
    path = folder / 'extraction.json'
    atomic(path, result)
    return {'extraction': str(path), 'window': result['window'], 'source_span': result['source_span'],
            'sample_frames': samples, 'audio_review': result['audio_review'],
            'import_mapping': import_entry(path)['mapping'],
            'review_status': 'unknown'}


def import_entry(extraction_path):
    path = Path(extraction_path).resolve(strict=True)
    if path.name != 'extraction.json' or not path.is_relative_to(WINDOWS.resolve()):
        raise ValueError('窗口映射证据须来自当前工作目录的原片抽取记录')
    item = read(path)
    source = Path(item['source']['path']).resolve(strict=True)
    window = Path(item['window']['path']).resolve(strict=True)
    if (window.parent != path.parent or window.name != 'window.mp4'
            or file_hash(source) != item['source']['sha256']
            or file_hash(window) != item['window']['sha256']):
        raise ValueError('原直播或窗口文件哈希与抽取记录不符')
    start, requested_end = item['requested_range']
    offset, actual = item['offset_seconds'], item['window']['duration_seconds']
    grid = item.get('frame_grid') or {}
    step = grid.get('expected_step_seconds', 0)
    aligned = grid.get('aligned_start_seconds', start)
    if (type(offset) not in (int, float) or type(actual) not in (int, float)
            or not math.isfinite(offset) or not math.isfinite(actual)
            or offset != aligned or not 0 <= start < requested_end
            or type(step) not in (int, float) or not math.isfinite(step) or step < 0
            or abs(offset-start) > max(.001, step/2+.002)
            or abs(offset+actual-requested_end) > max(.05, step*2)
            or item['source_span'] != [offset, offset+actual]
            or offset + actual > item['source']['duration_seconds'] + .001):
        raise ValueError('窗口实际时长、source_span或原片offset无法安全对应')
    return {'offset_seconds': offset, 'window_video': str(window),
            'mapping': {'source_sha256': item['source']['sha256'],
                        'window_sha256': item['window']['sha256'],
                        'offset_seconds': offset, 'range': [offset, offset+actual],
                        'evidence': {'path': str(path), 'sha256': file_hash(path)}}}


def transcribe_preview(extraction_path):
    """ASR for one reviewed-length extracted file; never asserts human listening."""
    path = Path(extraction_path).resolve(strict=True)
    mapping = import_entry(path)
    item = read(path)
    window_info = item['window']
    window = Path(mapping['window_video'])
    duration = window_info['duration_seconds']
    if (type(duration) not in (int, float) or not math.isfinite(duration)
            or not 0 < duration <= 120):
        raise ValueError('ASR预览只允许当前工作目录已抽取且不超过120秒的窗口')
    if (not path.parent.name.startswith('window-') or item.get('schema_version') != 1
            or item.get('review_status') != 'unknown' or not item.get('frame_grid')
            or not item.get('source_timeline') or not item.get('window_audio_audit')
            or len(item.get('sample_frames', [])) != 3):
        raise ValueError('ASR预览须引用真实抽取窗口及完整映射证据')
    card_path, _, context, source = original_context(item['task_record'])
    if (item.get('task_id') != context['task_id']
            or path.parent.parent != WINDOWS / context['task_id']
            or item['source']['sha256'] != context['source_sha256']
            or Path(item['source']['path']).resolve() != source
            or item['task_record'] != str(card_path)):
        raise ValueError('ASR预览窗口与当前原片任务卡不一致')
    for frame in item['sample_frames']:
        evidence_file(frame)
    evidence_file(item.get('audio_review') or {})
    media = _media(path.parent)
    meta = media.probe(window)
    if (abs(meta['video_start']) > .001 or abs(meta['audio']['start']) > .001
            or abs(meta['duration'] - duration) > .001 or meta['duration'] > 120):
        raise ValueError('ASR预览窗口起点或实际时长与抽取证据不符')
    from transcribe_adapter import transcribe
    transcript, cache, metrics = transcribe(window, meta)
    if (transcript.get('source', {}).get('sha256') != window_info['sha256']
            or abs(transcript['source']['duration_seconds'] - duration) > .001):
        raise ValueError('ASR预览转写与当前窗口哈希或时长不匹配')
    cache = Path(cache).resolve(strict=True)
    if not cache.is_relative_to((ROOT / 'private-context').resolve()):
        raise ValueError('ASR预览转写缓存须留在当前工作目录私有区')
    segments = []
    offset, source_end = item['source_span']
    for segment in transcript['segments']:
        a, b = segment['start'], segment['end']
        if (type(a) not in (int, float) or type(b) not in (int, float)
                or not math.isfinite(a) or not math.isfinite(b)
                or not 0 <= a <= duration or not a <= b <= duration + .001):
            raise ValueError('ASR预览片段超出实际窗口')
        segments.append({'id': segment['id'], 'window_range': [a, b],
                         'source_range': [offset+a, min(offset+b, source_end)],
                         'text': segment['text'],
                         'avg_logprob': segment.get('avg_logprob'),
                         'no_speech_prob': segment.get('no_speech_prob')})
    if file_hash(window) != window_info['sha256'] or file_hash(source) != item['source']['sha256']:
        raise ValueError('ASR预览期间原片或窗口文件变化')
    preview = {'schema_version': 1, 'status': 'unreviewed_asr_preview',
               'created_at': datetime.now().astimezone().isoformat(),
               'extraction': {'path': str(path), 'sha256': file_hash(path)},
               'source_sha256': item['source']['sha256'],
               'window_sha256': window_info['sha256'], 'source_span': item['source_span'],
               'transcript_cache': {'path': str(cache), 'sha256': file_hash(cache)},
               'asr_config_hash': transcript['asr']['config_hash'],
               'cache_hit': metrics.get('cache_hit'), 'segments': segments,
               'same_product': 'unknown', 'audio_listening': 'not_performed',
               'transcript_verified': False,
               'limits': '机器转写仅供对照画面和短原声；不构成同款、逐词核验或人工听审结论'}
    output = path.parent / f'asr-preview-{uuid.uuid4().hex[:12]}.json'
    if output.exists():
        raise FileExistsError('ASR预览文件已存在，不覆盖')
    atomic(output, preview)
    return {'preview_path': str(output), 'status': preview['status'],
            'source_span': preview['source_span'], 'cache_hit': preview['cache_hit'],
            'same_product': 'unknown', 'audio_listening': 'not_performed',
            'segments': segments}


def _checked_review_window(entry, context, source):
    extraction_path = Path(entry['extraction']).resolve(strict=True)
    mapping = import_entry(extraction_path)
    extracted = read(extraction_path)
    if (extraction_path.parent.parent != WINDOWS / context['task_id']
            or extracted['task_record'] != str(source)
            or Path(extracted['source']['path']).resolve() != Path(context['source_video']).resolve()
            or extracted['source']['sha256'] != context['source_sha256']):
        raise ValueError('窗口不属于当前原片任务卡或当前原片路径')
    start, end = extracted['source_span']
    visual = entry.get('visual')
    if not isinstance(visual, list) or len(visual) != len(extracted['sample_frames']):
        raise ValueError('须逐张记录窗口首中末真实画面审核')
    for actual, sample in zip(visual, extracted['sample_frames']):
        if (actual.get('path') != sample['path'] or actual.get('sha256') != sample['sha256']
                or actual.get('at') != sample['source_at']
                or not isinstance(actual.get('finding'), str) or not actual['finding'].strip()):
            raise ValueError('代表画面与实际抽帧、时间或审核结论不符')
        evidence_file(actual)
    speech = entry.get('speech') or {}
    audio = extracted['audio_review']
    if (speech.get('type') != 'audio_listening' or speech.get('path') != audio['path']
            or speech.get('sha256') != audio['sha256'] or speech.get('range') != [start, end]
            or not isinstance(speech.get('finding'), str) or not speech['finding'].strip()):
        raise ValueError('完整窗口原声审核须引用真实音频及原片时间范围')
    evidence_file(speech)
    return {'range': [start, end], 'visual': visual, 'speech': speech}, extracted, mapping


def create_review(task_record, review_path):
    card_path, _, context, source = original_context(task_record)
    review_path = Path(review_path).resolve(strict=True)
    if not review_path.is_relative_to((ROOT / 'private-context').resolve()):
        raise ValueError('审查输入须保存在当前工作目录private-context')
    submitted = read(review_path)
    if submitted.get('conclusion') != 'matched':
        raise ValueError('只有显式matched且具备真实画面与原声证据才能更新任务卡；unknown保持未确认')
    reviewer = submitted.get('reviewer')
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError('须记录实际审核来源')
    timestamp(submitted.get('reviewed_at'))
    entries = submitted.get('windows')
    if not isinstance(entries, list) or not entries:
        raise ValueError('至少提供一个已实际核验的窗口')
    reviewed = [_checked_review_window(entry, context, card_path) for entry in entries]
    windows = [record[0] for record in reviewed]
    ranges = sorted(window['range'] for window in windows)
    if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
        raise ValueError('已审窗口在原片时间轴重叠，须先处理')
    revised = copy.deepcopy(context)
    revised['confirmed_windows'] = windows
    revised['source_review'] = {'conclusion': 'matched', 'source_sha256': context['source_sha256'],
                                'identity_hash': key(context['product']), 'windows_hash': key(windows),
                                'reviewer': reviewer, 'reviewed_at': submitted['reviewed_at'],
                                'review_input': {'path': str(review_path), 'sha256': file_hash(review_path)}}
    validate_task(revised, source)
    outputs = []
    with locked(card_path):
        card_hash = file_hash(card_path)
        current = read(card_path)
        old = current.get('task_context') or {}
        if (old != context or current.get('status') != 'NOT_UPLOADED_NOT_SUBMITTED'
                or current.get('submitted') is not False or old.get('batch_ref')
                or old.get('batch_adoption_fingerprint') or old.get('confirmed_windows')
                or (old.get('source_review') or {}).get('conclusion') != 'unknown'):
            raise ValueError('原任务已变化或已有采用／审核；禁止覆盖与状态回滚')
        no_resubmit(revised, [current, *local_records(ROOT)])
        for original, extraction, mapping in reviewed:
            window = Path(extraction['window']['path'])
            start = extraction['offset_seconds']
            duration = extraction['window']['duration_seconds']
            child_windows = [dict(original,
                                  range=[0, duration],
                                  visual=[dict(frame, at=frame['at']-start) for frame in original['visual']],
                                  speech=dict(original['speech'], range=[0, duration]))]
            child = copy.deepcopy(revised)
            child['task_id'] = 'w-' + key([context['task_id'], mapping['mapping']])[:24]
            child['source_video'] = str(window)
            child['source_sha256'] = extraction['window']['sha256']
            child['confirmed_windows'] = child_windows
            child['source_review'] = dict(revised['source_review'],
                                          source_sha256=child['source_sha256'],
                                          windows_hash=key(child_windows))
            child['source_window'] = {'parent_task_id': context['task_id'],
                                      'parent_source_sha256': context['source_sha256'],
                                      'offset_seconds': start, 'source_span': extraction['source_span'],
                                      'mapping': mapping['mapping']}
            validate_task(child, window, duration)
            child_path = ROOT / 'outputs' / child['task_id'] / 'run-state.json'
            if child_path.parent.exists():
                raise ValueError('窗口任务已存在；不覆盖已有状态')
            outputs.append((child_path, child, extraction, mapping))
        # Persist child cards before changing the parent. An interrupted write
        # leaves the original unknown rather than falsely matched.
        for child_path, child, _, _ in outputs:
            initialize(child_path, task_context=child)
        if file_hash(card_path) != card_hash:
            raise ValueError('原任务卡在窗口任务建立期间被并发修改；拒绝覆盖')
        current['task_context'] = revised
        current['updated_at'] = datetime.now().astimezone().isoformat()
        current['next_action'] = '已记录同款窗口；按各窗口任务卡转写并核对完整材料池'
        atomic(card_path, current)
    return {'task_record': str(card_path), 'source_review': revised['source_review'],
            'window_tasks': [{'task_record': str(path), 'source_video': str(extraction['window']['path']),
                              'import_entry': mapping} for path, _, extraction, mapping in outputs]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('inspect')
    p.add_argument('--task-record', required=True, type=Path)
    p = sub.add_parser('extract')
    p.add_argument('--task-record', required=True, type=Path)
    p.add_argument('--start', required=True, type=float, help='原直播起点秒数，须落在真实视频帧上')
    p.add_argument('--end', required=True, type=float, help='原直播终点秒数')
    p = sub.add_parser('create-review')
    p.add_argument('--task-record', required=True, type=Path)
    p.add_argument('--review', required=True, type=Path, help='实际看听后填写的private-context JSON')
    p = sub.add_parser('transcribe-preview', help='仅对已抽取的<=120秒窗口作未审核ASR预览')
    p.add_argument('--extraction', required=True, type=Path)
    args = parser.parse_args()
    if args.action == 'inspect':
        result = inspect(args.task_record)
    elif args.action == 'extract':
        result = extract(args.task_record, args.start, args.end)
    elif args.action == 'transcribe-preview':
        result = transcribe_preview(args.extraction)
    else:
        result = create_review(args.task_record, args.review)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
