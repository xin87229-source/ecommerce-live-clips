"""Rebuild an explicitly accepted rehearsal from original windows for local delivery.

Natural-language interpretation belongs to the operator. This module checks its
explicit declarations and their unchanged file/range evidence, never invents them.
"""
import argparse
import html
import json
import math
from pathlib import Path
import shutil

import rehearsal
import review_card
import review_cut
import subtitle
from cover_layout import render as cover_render
from media_adapter import MediaAdapter
from quality_support import timestamp
from run import component
from runtime import ROOT, INSTALL_ROOT, config
from selector_support import atomic, file_hash, read

ITEMS = ('same_product', 'transcript', 'continuous_visual', 'audio_listening', 'user_confirmation')
STATUS = 'LOCAL_DELIVERY_NOT_SUBMITTED'


def ref(path):
    path = Path(path).resolve(strict=True)
    return {'path': str(path), 'sha256': file_hash(path)}


def checked_ref(value, base):
    if not isinstance(value, dict):
        raise ValueError('缺少文件证据')
    path = rehearsal.local(value.get('path', ''), base)
    if file_hash(path) != value.get('sha256'):
        raise ValueError('文件证据已经变化，需要重新审核：' + str(path))
    return path


def evidence_quote(value, label):
    # Real feedback can span lines; preserve the exact quoted text for matching.
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + '须为非空原文引用')
    return value


def validate_acceptance(record, feedback, approval):
    video = checked_ref(record['video'], ROOT / 'outputs/reviews/rehearsals')
    reviewed = checked_ref(feedback['media'], ROOT / 'outputs/reviews')
    duration = record['cut']['duration_seconds']
    coverage = feedback.get('range')
    # Shell/JSON decimal roundtrips can differ by ~1e-14 seconds. Do not allow
    # even one audio sample of genuinely unreviewed tail to inherit acceptance.
    full = (isinstance(coverage, list) and len(coverage) == 2
            and all(type(t) in (int, float) and math.isfinite(t) for t in coverage)
            and abs(coverage[0]) <= 1e-9 and abs(coverage[1] - duration) <= 1e-9)
    if (file_hash(video) != file_hash(reviewed)
            or not full or feedback.get('question') is not None
            or feedback.get('review_status') != 'pending' or feedback.get('review_state_changed') is not False
            or feedback.get('source_binding', {}).get('source_sha256') != record['cut']['source']['sha256']):
        raise ValueError('反馈须绑定当前成片的完整范围及同一原片，不得扩大局部反馈')
    # Match save-feedback exactly, including Windows CRLF and optional UTF-8 BOM.
    raw = checked_ref(feedback['raw_text_file'], ROOT / 'private-context').read_bytes().decode('utf-8-sig')
    if raw != feedback.get('raw_text'):
        raise ValueError('保存的原始反馈与原文文件不一致')
    items = approval.get('confirmed_items')
    if (not isinstance(items, list) or any(not isinstance(item, str) for item in items)
            or len(items) != len(ITEMS) or set(items) != set(ITEMS)):
        raise ValueError('须逐项明确确认同款、字幕、连续画面、原声和用户接受；整体偏好不能补齐')
    rehearsal.text(approval.get('actor'), '实际审核人')
    timestamp(approval.get('at'))
    quote = evidence_quote(approval.get('confirmation_quote'), '原始完整内容确认')
    if quote not in raw:
        raise ValueError('确认引用不是用户原始反馈')
    acceptance = approval.get('acceptance', {})
    original = checked_ref(acceptance, ROOT / 'private-context').read_bytes().decode('utf-8-sig')
    if evidence_quote(acceptance.get('quote'), '用户接受原话') not in original:
        raise ValueError('接受结论缺少原始用户消息')
    return {'actor': approval['actor'], 'at': approval['at'], 'confirmed_items': list(ITEMS),
            'scope': 'accepted_video_and_retained_segments_only', 'video': ref(video),
            'range': [0, duration], 'confirmation_quote': quote, 'acceptance': acceptance}


def watermark_line(duration):
    return (f'Dialogue: 10,0:00:00.00,{subtitle.stamp(duration, True)},Default,,0,0,0,,'
            r'{\an8\pos(540,80)\fs38\c&H00D6FF&\bord2}' + rehearsal.WATERMARK + '\n')


def delivery_ass(accepted_ass, duration):
    marker = watermark_line(duration)
    lines = accepted_ass.splitlines(keepends=True)
    if lines.count(marker) != 1 or accepted_ass.count(rehearsal.WATERMARK) != 1:
        raise ValueError('待审水印行不符合已知生成器，不能猜删字幕')
    return ''.join(line for line in lines if line != marker)


def validate_copy(record, draft):
    for name, maximum in (('title', 80), ('caption', 500)):
        rehearsal.text(draft.get(name), name, maximum)
    for name, lo, hi in (('hashtags', 3, 5), ('cover_lines', 1, 3)):
        values = draft.get(name)
        if not isinstance(values, list) or not lo <= len(values) <= hi:
            raise ValueError('文案列表数量不符：' + name)
        for value in values:
            rehearsal.text(value, name, 50)
        if len(set(values)) != len(values):
            raise ValueError('文案列表重复：' + name)
    rehearsal.text(draft.get('reviewer'), '文案实际审核者')
    rehearsal.text(draft.get('finding'), '文案事实核对依据')
    timestamp(draft.get('reviewed_at'))
    evidence = draft.get('evidence')
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('文案须引用已确认字幕，不得填空证据')
    for item in evidence:
        cue = item.get('cue')
        quote = rehearsal.text(item.get('quote'), '文案字幕引用')
        if (type(cue) is not int or not 1 <= cue <= len(record['subtitle_cues'])
                or quote not in record['subtitle_cues'][cue-1]['text']):
            raise ValueError('文案引用不属于已确认字幕')
    return draft


def plan(spec_path):
    spec_path = rehearsal.local(spec_path, ROOT / 'private-context')
    spec = read(spec_path)
    if set(spec) != {'schema_version', 'rehearsal', 'feedback', 'approval', 'copy'} or spec['schema_version'] != 1:
        raise ValueError('交付输入字段不完整或包含未知字段')
    package = Path(spec['rehearsal']).resolve(strict=True)
    simulation = rehearsal.check(package)
    record = read(package / 'rehearsal.json')
    feedback_path = rehearsal.local(spec['feedback'], ROOT / 'private-context')
    feedback = read(feedback_path)
    manifest_path = checked_ref(feedback['card'], ROOT / 'outputs/reviews')
    card, _ = review_card.checked_card(manifest_path.parent / 'index.html')
    version = next((v for v in card['versions'] if v['id'] == feedback.get('version')), None)
    if (not version or version['sha256'] != record['video']['sha256']
            or feedback.get('source_binding') != card['binding']
            or Path(feedback['media']['path']).resolve() != (manifest_path.parent / version['media_file']).resolve()):
        raise ValueError('反馈不属于当前已审卡片版本')
    approved = validate_acceptance(record, feedback, spec['approval'])
    copy = validate_copy(record, spec['copy'])
    clean_ass = delivery_ass((package / 'draft-subtitle.ass').read_text('utf-8'), record['cut']['duration_seconds'])
    inputs = dict(record['inputs'])
    for path in (spec_path, feedback_path, manifest_path, package / 'simulation.json',
                 package / 'rehearsal.json', Path(feedback['raw_text_file']['path']),
                 Path(spec['approval']['acceptance']['path']), Path(__file__), INSTALL_ROOT / 'cli.py'):
        inputs[str(path.resolve())] = file_hash(path)
    inputs.update({str((package / name).resolve()): sha for name, sha in simulation['files'].items()})
    return {'spec': spec, 'package': str(package), 'record': record, 'approved': approved,
            'copy': copy, 'ass': clean_ass, 'inputs': inputs}


def output_path(task_id):
    component(task_id, 'task_id')
    output = ROOT / 'outputs/deliveries' / task_id
    if output.exists() or output.is_symlink():
        raise FileExistsError('交付目录已存在，请用新任务号；不覆盖任何旧结果')
    if not output.resolve().is_relative_to(ROOT.resolve()) or (ROOT / 'outputs').resolve().parent != ROOT.resolve():
        raise ValueError('交付输出不能重定向到工作区外')
    return output


def audio_hash(media, video):
    return media.command('ffmpeg', ['-v', 'error', '-i', str(video), '-map', '0:a:0',
                                   '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-']).strip()


def build(spec_path, task_id):
    output = output_path(task_id)
    planned = plan(spec_path)
    record, old = planned['record'], Path(planned['package'])
    output.mkdir(parents=True, exist_ok=False)
    try:
        media = MediaAdapter(config(), output / 'media.log')
        # Rebuild from the original windows; the accepted video is comparison-only.
        base = output / 'source-cut.mp4'
        review_cut.render(media, record['cut'], base)
        if file_hash(base) != file_hash(old / 'source-cut.mp4'):
            raise ValueError('原片重建与已审剪辑不一致，不能继承内容确认；保留失败产物供检查')
        atomic(output / 'subtitle.ass', planned['ass'], plain=True)
        shutil.copyfile(old / 'draft-subtitle.srt', output / 'subtitle.srt')
        shutil.copytree(old / 'fonts', output / 'fonts')
        video = output / 'video.mp4'
        vf = ('ass=filename=' + subtitle.filter_path(output / 'subtitle.ass')
              + ':fontsdir=' + subtitle.filter_path(output / 'fonts'))
        media.write_media(['-loglevel', 'info', '-i', str(base), '-map', '0:v:0', '-map', '0:a:0', '-vf', vf,
                           '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
                           '-c:a', 'copy', '-map_metadata', '-1', '-map_chapters', '-1',
                           '-movflags', '+faststart', str(video)], video, delivery=True)
        meta, checks = media.check(video, record['cut']['duration_seconds'], review_cut.SIZE, delivery=True)
        if meta['frame_timing']['frame_count'] != record['cut']['frame_count']:
            raise ValueError('正式成片帧数与已审时间轴不符')
        original_audio = audio_hash(media, old / 'draft-video.mp4')
        if audio_hash(media, video) != original_audio:
            raise ValueError('正式成片音频与已审版本不同，不能继承原声审核')
        frames = media.look(video, record['cut']['duration_seconds'], output)
        source_frame = output / 'cover-source.jpg'
        source = record['cover_source']
        media.write_media(['-ss', str(source['window_at']), '-i', source['window']['path'],
                           '-frames:v', '1', '-q:v', '2', '-update', '1', str(source_frame)], source_frame)
        font = subtitle.font_choice(subtitle.installed_style(subtitle.STYLE)['font_preferences'], config().get('font_path'))[1]
        if planned['inputs'].get(str(Path(font).resolve())) != file_hash(font):
            raise ValueError('封面字体与已验证环境不符')
        cover_config = read(INSTALL_ROOT / 'pipeline/cover_config.json')
        cover_config['lines'] = planned['copy']['cover_lines']
        covers = {ratio: cover_render(source_frame, output / f'cover-{ratio}.jpg', cover_config, ratio, font)
                  for ratio in ('3x4', '4x3')}
        for name in ('title', 'caption'):
            atomic(output / (name + '.txt'), planned['copy'][name], plain=True)
        atomic(output / 'hashtags.txt', ' '.join(planned['copy']['hashtags']), plain=True)
        quality = {'technical': {'status': 'passed', 'metadata': meta, 'checks': checks, 'frames': frames},
                   'accepted_content': planned['approved'], 'source_cut_byte_identical': True,
                   'subtitle_text_and_timing_unchanged': file_hash(output / 'subtitle.srt') == file_hash(old / 'draft-subtitle.srt'),
                   'ass_change': 'removed_exact_draft_watermark_only', 'audio_bitstream_identical': True,
                   'audio_bitstream_hash': original_audio, 'covers': covers, 'copy_review': planned['copy'],
                   'new_video_visual_review': 'not_reviewed', 'new_covers_visual_review': 'not_reviewed',
                   'new_video_user_acceptance': 'not_requested; prior confirmation covers unchanged content only',
                   'platform_ui_verified': False, 'uploaded': False, 'submitted': False}
        atomic(output / 'quality.json', quality)
        atomic(output / 'changes.md', '# 本地交付变更\n\n按已审原片窗口重新制作，原剪辑字节重建一致；原声逐比特一致。'
               '字幕正文、时间、大小、颜色及重点强调保持，仅移除待审水印。封面去掉待审标签，文案依据已确认原话整理。'
               '\n\n用户确认属于前版内容；新视频画面和封面须由操作者检查，不能声称用户看过新文件。'
               '没有平台上传或提交。\n', plain=True)
        e = html.escape
        page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>本地交付与发帖预览</title><style>body{max-width:1100px;margin:32px auto;padding:0 20px;'
                'font:17px/1.7 system-ui;background:#f4f7f6;color:#17362e}video{width:100%;max-height:640px;'
                'background:#111}img{max-width:46%;max-height:500px;object-fit:contain}a{color:#185e4c}</style>'
                '<h1>' + e(planned['copy']['title']) + '</h1><p>本地交付 · 未上传、未提交。原版内容已获用户确认；'
                '新画面及封面检查另记。</p><video controls preload="metadata" src="video.mp4"></video><p>'
                + e(planned['copy']['caption']) + '</p><p>' + e(' '.join(planned['copy']['hashtags']))
                + '</p><img alt="竖版封面" src="cover-3x4.jpg"><img alt="横版封面" src="cover-4x3.jpg">'
                '<p><a href="subtitle.srt">字幕</a> · <a href="changes.md">实际改动</a> · '
                '<a href="quality.json">分项质量记录</a> · <a href="delivery.json">完整来源与文件清单</a></p></html>')
        atomic(output / 'index.html', page, plain=True)
        review_cut.verify_inputs(planned)
        files = {p.relative_to(output).as_posix(): file_hash(p) for p in output.rglob('*') if p.is_file()}
        receipt = {'schema_version': 1, 'kind': 'reviewed_local_delivery', 'status': STATUS,
                   'package': str(output), 'inputs': planned['inputs'], 'files': files,
                   'prior_content_acceptance': planned['approved'], 'source_cut': record['cut'],
                   'changes': ['remove_draft_watermark', 'fact_checked_copy_and_unlabelled_covers'],
                   'video': ref(video), 'quality': quality, 'platform_ui_verified': False,
                   'uploaded': False, 'submitted': False, 'is_draft': False}
        atomic(output / 'delivery.json', receipt)
        return check(output)
    except Exception as error:
        atomic(output / 'failure.json', {'status': 'failed', 'error': str(error), 'submitted': False})
        raise


def check(package):
    output = Path(package).resolve(strict=True)
    if (not output.is_relative_to((ROOT / 'outputs/deliveries').resolve())
            or not output.is_relative_to(ROOT.resolve())):
        raise ValueError('只检查当前工作区正式交付目录')
    result = read(output / 'delivery.json')
    if (result.get('schema_version') != 1 or result.get('kind') != 'reviewed_local_delivery'
            or result.get('status') != STATUS or result.get('package') != str(output)
            or result.get('is_draft') is not False
            or any(result.get(k) is not False for k in ('submitted', 'uploaded', 'platform_ui_verified'))):
        raise ValueError('交付记录状态不一致')
    review_cut.verify_inputs(result)
    actual = {p.relative_to(output).as_posix() for p in output.rglob('*')
              if p.is_file() and p != output / 'delivery.json'}
    required = {'video.mp4', 'source-cut.mp4', 'subtitle.srt', 'subtitle.ass', 'cover-3x4.jpg',
                'cover-4x3.jpg', 'title.txt', 'caption.txt', 'hashtags.txt', 'quality.json',
                'index.html', 'changes.md'}
    if not required.issubset(result['files']) or set(result['files']) != actual:
        raise ValueError('交付文件清单发生变化')
    for name, sha in result['files'].items():
        checked_ref({'path': str(output / name), 'sha256': sha}, output)
    checked_ref(result['video'], output)
    return {'status': STATUS, 'package': str(output), 'video': result['video'],
            'content_confirmation': 'prior_accepted_content_preserved',
            'new_visual_review': result['quality']['new_video_visual_review'], 'submitted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    build_parser = sub.add_parser('build')
    build_parser.add_argument('--spec', required=True, type=Path)
    build_parser.add_argument('--task-id', required=True)
    sub.add_parser('check').add_argument('--package', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = build(args.spec, args.task_id) if args.action == 'build' else check(args.package)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({'status': 'ERROR', 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
