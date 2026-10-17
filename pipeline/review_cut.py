"""Render an explicitly unreviewed cut from bound original-source windows only."""
import argparse
import copy
from fractions import Fraction
import json
import math
from pathlib import Path
import re

from media_adapter import MediaAdapter
from runtime import ROOT, config
from selector_support import atomic, file_hash, read
import source_windows

FPS = 30
SIZE = [1080, 1920]


def number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(label + '须为有限数值')
    return value


def time_range(value, duration):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('window_range须为窗口内起止秒数组')
    start, end = (number(v, '区间') for v in value)
    if not 0 <= start < end <= duration:
        raise ValueError('window_range超出实际窗口或没有正时长')
    return start, end


def questions(value):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError('questions须为非空问题字符串的数组，可为空数组')
    return value


def plan(spec_path):
    """Read-only provenance and timeline validation; unknown review stays unknown."""
    path = Path(spec_path).resolve(strict=True)
    if not path.is_relative_to((ROOT / 'private-context').resolve()) or not path.is_file():
        raise ValueError('spec须保存在当前工作区private-context')
    spec = read(path)
    if not isinstance(spec, dict) or type(spec.get('schema_version')) is not int or spec['schema_version'] != 1:
        raise ValueError('spec须为schema_version=1的对象')
    card_path, card, context, source = source_windows.original_context(spec.get('task_record'))
    if (card.get('status') != 'NOT_UPLOADED_NOT_SUBMITTED' or card.get('submitted') is not False
            or context.get('batch_ref') or context.get('batch_adoption_fingerprint')):
        raise ValueError('仅可为未提交且未采用的原片任务制作独立待审预览')
    clips = spec.get('clips')
    if not isinstance(clips, list) or not clips:
        raise ValueError('clips须为非空片段数组')
    hold = number(spec.get('end_hold_seconds', 0), 'end_hold_seconds')
    if not 0 <= hold <= 2:
        raise ValueError('end_hold_seconds须为0至2秒')
    inputs = {str(path): file_hash(path), str(card_path): file_hash(card_path),
              str(source): context['source_sha256']}
    cache = {}

    def window(reference):
        if not isinstance(reference, str) or not reference:
            raise ValueError('extraction须为本工作区的原片抽取记录路径')
        extraction = Path(reference).resolve(strict=True)
        if extraction not in cache:
            mapping = source_windows.import_entry(extraction)
            item = read(extraction)
            if (item.get('task_id') != context['task_id'] or item.get('task_record') != str(card_path)
                    or Path(item['source']['path']).resolve() != source
                    or item['source']['sha256'] != context['source_sha256']):
                raise ValueError('抽取记录不是当前原片任务，不能借用旧成片或其他来源')
            inputs[str(extraction)] = mapping['mapping']['evidence']['sha256']
            inputs[mapping['window_video']] = mapping['mapping']['window_sha256']
            cache[extraction] = item, mapping
        return cache[extraction]

    def binding(reference, start, end):
        item, mapping = window(reference)
        offset = mapping['offset_seconds']
        return {'extraction': mapping['mapping']['evidence'],
                'window': {'path': mapping['window_video'], 'sha256': item['window']['sha256']},
                'window_range': [start, end], 'source_range': [offset + start, offset + end],
                'source_sha256': item['source']['sha256']}

    segments, total_frames = [], 0
    for index, clip in enumerate(clips, 1):
        if not isinstance(clip, dict):
            raise ValueError('每个clip须为对象')
        purpose = clip.get('purpose')
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError('每段须写清purpose')
        item, _ = window(clip.get('extraction'))
        start, end = time_range(clip.get('window_range'), item['window']['duration_seconds'])
        speed = number(clip.get('speed'), 'speed')
        if not 1 <= speed <= 1.5:
            raise ValueError('speed须为1.0至1.5')
        exact = (Fraction(str(end)) - Fraction(str(start))) / Fraction(str(speed))
        frames = math.ceil(exact * FPS)
        duration = frames / FPS
        visual = clip.get('visual')
        if visual is not None:
            if not isinstance(visual, dict):
                raise ValueError('visual须为对象')
            visual_item, _ = window(visual.get('extraction'))
            va, vb = time_range(visual.get('window_range'), visual_item['window']['duration_seconds'])
            if vb - va + 1e-9 < end - start:
                raise ValueError('替换画面长度不足以按当前speed覆盖原声段')
            visual_end = min(vb, va + duration * speed)
            visual_binding = binding(visual['extraction'], va, visual_end)
            visual_binding['requested_window_range'] = [va, vb]
        else:
            visual_binding = binding(clip['extraction'], start, end)
        crop = clip.get('crop')
        if crop is not None:
            if (not isinstance(crop, dict) or set(crop) != {'x', 'y', 'width', 'height'}
                    or any(type(v) is not int or v % 2 for v in crop.values())
                    or crop['x'] < 0 or crop['y'] < 0 or crop['width'] <= 0 or crop['height'] <= 0):
                raise ValueError('crop须为偶数像素x/y/width/height，起点非负且尺寸为正')
        audio_binding = binding(clip['extraction'], start, end)
        non_sync = (visual_binding['source_range'] != audio_binding['source_range']) if visual else False
        visual_duration = (visual_binding['window_range'][1] - visual_binding['window_range'][0]) / speed
        segments.append({'id': f'clip-{index}', 'purpose': purpose, 'speed': speed,
                         'video_range': [total_frames / FPS, (total_frames + frames) / FPS],
                         'frame_range': [total_frames, total_frames + frames], 'frame_count': frames,
                         'audio': audio_binding, 'visual': visual_binding, 'crop': crop,
                         'non_synchronous_visual': non_sync,
                         'visual_note': ('同源非同期画面；原声来自独立区间，口型未核验' if non_sync
                                         else '同区间原声画面；同步观感仍待实看'),
                         'audio_padding_seconds': max(0, duration - float(exact)),
                         'visual_padding_seconds': max(0, duration - visual_duration),
                         'questions': questions(clip.get('questions', []))})
        total_frames += frames
    hold_frames = math.floor(hold * FPS + .5)
    hold_record = {'requested_seconds': hold, 'frame_count': hold_frames, 'seconds': hold_frames / FPS,
                   'video_range': [total_frames / FPS, (total_frames + hold_frames) / FPS],
                   'visual': '预览最后一帧定格', 'audio': 'silence'}
    total_frames += hold_frames
    if not 2 <= total_frames <= 70 * FPS:
        raise ValueError('预览须至少2帧，含定格总长不得超过70秒')
    return {'schema_version': 1, 'kind': 'review_cut', 'status': 'unreviewed_review_cut',
            'task_record': str(card_path), 'task_id': context['task_id'],
            'source': {'path': str(source), 'sha256': context['source_sha256']},
            'spec': {'path': str(path), 'sha256': inputs[str(path)]},
            'fps': FPS, 'size': SIZE, 'frame_count': total_frames, 'duration_seconds': total_frames / FPS,
            'segments': segments, 'end_hold': hold_record, 'inputs': inputs,
            'questions': questions(spec.get('questions', [])),
            'frame_rounding': '每段向上取整到30fps；不足一帧末帧补齐、音频补静音；尾定格取最近帧',
            'approvals': {name: 'not_reviewed' for name in
                          ('same_product', 'transcript', 'continuous_visual', 'audio_listening', 'user_confirmation')},
            'limitations': '仅供比较选段、节奏和待核词；不代表同款、原话、口型或最终内容已通过'}


def verify_inputs(record):
    for path, sha in record['inputs'].items():
        if file_hash(path) != sha:
            raise ValueError('预览输入或任务卡发生变化，保留现场: ' + path)


def render(media, record, destination):
    args, filters, parts = [], [], []
    metadata = {}
    input_count = 0
    for index, segment in enumerate(record['segments']):
        audio, visual = segment['audio'], segment['visual']
        audio_path, visual_path = audio['window']['path'], visual['window']['path']
        for path in {audio_path, visual_path}:
            if path not in metadata:
                meta = media.probe(Path(path))
                if abs(meta['video_start']) > .001 or abs(meta['audio']['start']) > .001:
                    raise ValueError('窗口须从零起点保持原片映射')
                metadata[path] = meta
        audio_index = input_count
        args += ['-i', audio_path]
        input_count += 1
        if visual_path == audio_path:
            video_index = audio_index
        else:
            video_index = input_count
            args += ['-i', visual_path]
            input_count += 1
        a, b = audio['window_range']
        va, vb = visual['window_range']
        speed = segment['speed']
        duration = segment['frame_count'] / FPS
        meta = metadata[visual_path]
        crop = segment['crop']
        crop_filter = ''
        if crop:
            if crop['x'] + crop['width'] > meta['width'] or crop['y'] + crop['height'] > meta['height']:
                raise ValueError('crop超出实际画面')
            crop_filter = f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']},"
        filters.append(f'[{video_index}:v:0]trim=start={va:.9f}:end={vb:.9f},'
                       f'setpts=(PTS-STARTPTS)/{speed:.12f},{crop_filter}'
                       'scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,'
                       'pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,'
                       f'fps=30:start_time=0,tpad=stop_mode=clone:stop_duration=1,'
                       f'trim=end_frame={segment["frame_count"]},setpts=N/(30*TB),format=yuv420p[v{index}]')
        filters.append(f'[{audio_index}:a:0]atrim=start={a:.9f}:end={b:.9f},asetpts=PTS-STARTPTS,'
                       f'atempo={speed:.12f},aresample=48000:async=0:first_pts=0,'
                       f'apad,atrim=duration={duration:.9f},asetpts=N/SR/TB[a{index}]')
        parts.append(f'[v{index}][a{index}]')
    filters.append(''.join(parts) + f'concat=n={len(parts)}:v=1:a=1[jv][ja]')
    hold_frames = record['end_hold']['frame_count']
    filters.append(f'[jv]tpad=stop_mode=clone:stop={hold_frames},'
                   f'trim=end_frame={record["frame_count"]},setpts=N/(30*TB)[video]')
    filters.append(f'[ja]apad,atrim=duration={record["duration_seconds"]:.9f},asetpts=N/SR/TB[audio]')
    media.write_media(args + ['-filter_complex', ';'.join(filters), '-map', '[video]', '-map', '[audio]',
                             '-map_metadata', '-1', '-map_chapters', '-1', '-c:v', 'libx264',
                             '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
                             '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
                             '-movflags', '+faststart', str(destination)], destination, delivery=True)


def run(spec_path, task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', task_id):
        raise ValueError('task-id须为1至64个字母、数字、短横线或下划线')
    output = ROOT / 'outputs' / 'review-cuts' / task_id
    if output.exists() or output.is_symlink():
        raise FileExistsError('待审输出已存在，禁止覆盖；请使用新task-id')
    if not output.resolve().is_relative_to((ROOT / 'outputs').resolve()) or (ROOT / 'outputs').resolve().parent != ROOT.resolve():
        raise ValueError('待审输出不可重定向到工作区外')
    record = plan(spec_path)
    verify_inputs(record)
    output.mkdir(parents=True, exist_ok=False)
    technical = {'status': 'failed', 'note': '仅媒体技术检查，不是听审、同款或用户审批'}
    record['render_status'] = 'failed'
    try:
        media = MediaAdapter(config(), output / 'media.log')
        video = output / 'preview.mp4'
        render(media, record, video)
        meta, checks = media.check(video, record['duration_seconds'], SIZE, delivery=True)
        if meta['frame_timing']['frame_count'] != record['frame_count']:
            raise ValueError('实际成片帧数与预览时间映射不符')
        frames = media.look(video, record['duration_seconds'], output)
        verify_inputs(record)
        record.update(render_status='completed', video={'path': str(video), 'sha256': file_hash(video)},
                      frames=[{'path': path, 'sha256': file_hash(path)} for path in frames])
        technical.update(status='passed', metadata=meta, checks=checks, video=record['video'],
                         inputs_unchanged=True, task_card_unchanged=True)
    except Exception as error:
        record['error'] = str(error)
        technical['error'] = str(error)
        raise
    finally:
        atomic(output / 'preview.json', record)
        atomic(output / 'technical.json', technical)
    return {'output': str(output), 'preview': str(video), 'record': str(output / 'preview.json'),
            'status': record['status'], 'duration_seconds': record['duration_seconds'],
            'frame_count': record['frame_count'], 'approvals': copy.deepcopy(record['approvals'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True, type=Path)
    parser.add_argument('--task-id', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.spec, args.task_id), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({'status': 'ERROR', 'error': str(error)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
