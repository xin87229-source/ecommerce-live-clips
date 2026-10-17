"""Create an offline listening card; user feedback never changes pipeline state."""
import argparse
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import re
import shutil

from runtime import ROOT
from selector_support import clock, file_hash, key, read, validate_transcript
from source_windows import import_entry, _preview_window_duration
from quality_support import evidence_file


def local_file(value, base):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('听审卡输入必须使用绝对路径')
    path = path.resolve(strict=True)
    if (not path.is_relative_to(ROOT.resolve()) or not path.is_relative_to(base.resolve())
            or not path.is_file()):
        raise ValueError('听审卡输入文件须在当前工作目录的指定位置')
    return path


def span(value, duration, allow_empty=False):
    if (not isinstance(value, list) or len(value) != 2
            or any(type(n) not in (int, float) or not math.isfinite(n) for n in value)
            or not 0 <= value[0] <= value[1] <= duration
            or (not allow_empty and value[0] == value[1])):
        raise ValueError('听审时间范围无效或超出当前窗口')
    return value


def required_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('标题、片段名称、问题ID及问题必须为非空文字')
    return value


def file_ref(path):
    return {'path': str(path), 'sha256': file_hash(path)}


def review_window_duration(item):
    duration = item['window']['duration_seconds']
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError('听审卡窗口时长必须是有效正数')
    # Preserve legacy short-window records; only prove a >120s rounding allowance.
    return _preview_window_duration(item) if duration > 120 else duration


def bind_range(entry, sources, default):
    extraction = str(local_file(entry.get('extraction', default), ROOT / 'private-context/source-windows'))
    source = next((s for s in sources if s['extraction'] == extraction), None)
    if source is None:
        raise ValueError('片段或问题须引用本卡已绑定的原片窗口')
    a, b = span(entry['window_range'], source['duration_seconds'])
    return dict(entry, extraction=extraction, source_file=source['media_file'],
                source_range=[source['source_span'][0] + a, source['source_span'][0] + b],
                source_sha256=source['source_sha256'], window_sha256=source['window_sha256'])


def checked_inputs(spec_path):
    private = ROOT / 'private-context'
    spec_path = local_file(spec_path, private)
    spec = read(spec_path)
    extraction = local_file(spec['extraction'], private / 'source-windows')
    preview_path = local_file(spec['asr_preview'], private)
    item, preview = read(extraction), read(preview_path)
    if item.get('schema_version') != 1 or preview.get('schema_version') != 1:
        raise ValueError('听审卡只接受当前版本的窗口和ASR预览')
    window = local_file(item['window']['path'], extraction.parent)
    # The original stays outside the workspace; reuse the existing extraction
    # validator to hash it once and check the zero-origin/source-time mapping.
    mapping = import_entry(extraction)
    duration = review_window_duration(item)
    expected_extraction = {'path': str(extraction), 'sha256': file_hash(extraction)}
    if (preview.get('extraction') != expected_extraction
            or preview.get('source_sha256') != item['source']['sha256']
            or preview.get('window_sha256') != item['window']['sha256']
            or preview.get('source_span') != item['source_span']
            or preview.get('status') != 'unreviewed_asr_preview'
            or preview.get('same_product') != 'unknown'
            or preview.get('audio_listening') != 'not_performed'
            or preview.get('transcript_verified') is not False):
        raise ValueError('ASR预览与抽取记录、文件哈希或源范围不匹配')
    cache_ref = preview['transcript_cache']
    cache = local_file(cache_ref['path'], private)
    if file_hash(cache) != cache_ref['sha256']:
        raise ValueError('ASR缓存文件哈希不匹配')
    transcript = read(cache)
    validate_transcript(transcript)
    if (local_file(transcript['source']['path'], extraction.parent) != window
            or transcript['source']['sha256'] != item['window']['sha256']
            or transcript['source']['duration_seconds'] != duration
            or transcript['asr']['config_hash'] != preview['asr_config_hash']):
        raise ValueError('ASR缓存未绑定当前窗口')
    segments = preview['segments']
    if not isinstance(segments, list) or len(segments) != len(transcript['segments']):
        raise ValueError('ASR预览字幕不完整')
    offset, source_end = item['source_span']
    for segment, original in zip(segments, transcript['segments']):
        a, b = span(segment['window_range'], duration, allow_empty=True)
        if (segment['id'] != original['id'] or segment['text'] != original['text']
                or [a, b] != [original['start'], original['end']]
                or segment['source_range'] != [offset + a, min(offset + b, source_end)]):
            raise ValueError('ASR预览原文或原片时间范围与缓存不符')
    sources = [dict(extraction=str(extraction), path=str(window), media_file='review.mp4',
                    duration_seconds=duration, source_span=item['source_span'],
                    source_sha256=item['source']['sha256'], window_sha256=item['window']['sha256'])]
    refs = [file_ref(path) for path in (spec_path, extraction, preview_path, cache)]
    refs += [dict(item['source']), dict(item['window'])]
    additional = spec.get('additional_extractions', [])
    if not isinstance(additional, list):
        raise ValueError('附加原片抽取记录必须为列表')
    for number, value in enumerate(additional, 2):
        path = local_file(value, private / 'source-windows')
        extra = read(path)
        media = local_file(extra['window']['path'], path.parent)
        import_entry(path)
        length = review_window_duration(extra)
        if (extra.get('schema_version') != 1
                or extra['source']['sha256'] != item['source']['sha256']
                or any(s['extraction'] == str(path) for s in sources)):
            raise ValueError('附加窗口须唯一、来自同一原片且请求不超过120秒')
        sources.append(dict(extraction=str(path), path=str(media), media_file=f'source-{number}.mp4',
                            duration_seconds=length, source_span=extra['source_span'],
                            source_sha256=extra['source']['sha256'], window_sha256=extra['window']['sha256']))
        refs += [file_ref(path), dict(extra['source']), dict(extra['window'])]
    required_text(spec['title'])
    for field, label in (('proposed_segments', 'label'), ('questions', 'question')):
        if not isinstance(spec[field], list) or not spec[field]:
            raise ValueError('须至少提供一个拟片段和一个听审问题')
        for index, entry in enumerate(spec[field]):
            required_text(entry[label])
            spec[field][index] = bind_range(entry, sources, str(extraction))
    ids = [required_text(entry['id']) for entry in spec['questions']]
    if len(ids) != len(set(ids)) or any('\n' in name or '\r' in name for name in ids):
        raise ValueError('听审问题ID须唯一且为单行文字')
    versions = spec.get('versions', [])
    if not isinstance(versions, list) or len(versions) > 2:
        raise ValueError('对比只接受最多两个已有版本')
    version_ids = set()
    for index, version in enumerate(versions, 1):
        ident = required_text(version['id'])
        if ident in version_ids or ident == 'source' or '\n' in ident or '\r' in ident:
            raise ValueError('版本ID须唯一且不能使用保留名称source')
        version_ids.add(ident)
        required_text(version['label'])
        length = version['duration_seconds']
        span([0, length], 120)
        path = local_file(version['path'], ROOT)
        if path.suffix.lower() != '.mp4' or evidence_file(version) != path:
            raise ValueError('版本须为当前工作区内哈希匹配的MP4预览')
        for field in ('changes', 'segments'):
            if not isinstance(version[field], list) or not version[field]:
                raise ValueError('每版须列出实际改动和片段作用；不伪造自动检测结果')
        for change in version['changes']:
            required_text(change)
        for part_index, part in enumerate(version['segments']):
            required_text(part['label'])
            required_text(part['purpose'])
            span(part['video_range'], length)
            version['segments'][part_index] = bind_range(part, sources, str(extraction))
        version.update(path=str(path), media_file=f'version-{index}.mp4', player=f'version-{index}')
        refs.append(file_ref(path))
    for question in spec['questions']:
        seen = set()
        for target in question.get('video_ranges', []):
            version = next((v for v in versions if v['id'] == target['version']), None)
            if version is None or version['id'] in seen:
                raise ValueError('问题的成片范围须指向本卡唯一的已绑定版本')
            span(target['range'], version['duration_seconds'])
            seen.add(version['id'])
    poster = None
    if spec.get('poster'):
        path = local_file(spec['poster'], extraction.parent)
        frame = next((f for f in item.get('sample_frames', [])
                      if Path(f['path']).resolve() == path), None)
        if (not frame or path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp')
                or file_hash(path) != frame['sha256']
                or type(frame['window_at']) not in (int, float)
                or not 0 <= frame['window_at'] <= duration
                or frame['source_at'] != offset + frame['window_at']):
            raise ValueError('海报必须是当前抽取记录中哈希及时间均匹配的原帧')
        poster = (path, frame['sha256'])
        refs.append(file_ref(path))
    binding = dict(mapping['mapping'], preview_sha256=file_hash(preview_path),
                   spec_sha256=file_hash(spec_path), duration_seconds=duration)
    return spec, item, segments, binding, sources, versions, refs, poster


def time_label(seconds):
    stamp = clock(seconds)[:-1]
    return stamp[3:] if seconds < 3600 else stamp


def listen_button(times, offset, source='review.mp4'):
    a, b = times
    label = f'原速回听 {time_label(a)}–{time_label(b)} · 原片 {time_label(offset+a)}–{time_label(offset+b)}'
    return f'<button type="button" class="listen" data-source="{source}" data-start="{a}" data-end="{b}">{label}</button>'


def version_button(version, times):
    a, b = times
    return (f'<button type="button" class="version-listen" data-player="{version["player"]}" '
            f'data-start="{a}" data-end="{b}">{html.escape(version["label"])} '
            f'{time_label(a)}–{time_label(b)}</button>')


def render(spec, item, segments, binding, sources, versions, poster_name):
    esc = html.escape
    offset, end = item['source_span']
    proposed = []
    for index, part in enumerate(spec['proposed_segments'], 1):
        a, b = part['window_range']
        part_offset = part['source_range'][0] - a
        lines = [f'<article><h3>{index}. {esc(part["label"])}</h3>',
                 listen_button([a, b], part_offset, part['source_file']), '<ul class="transcript">']
        for segment in segments if part['source_file'] == 'review.mp4' else []:
            x, y = segment['window_range']
            if x < b and y > a:
                lines.append(f'<li>{listen_button([x, y], offset)}<p>{esc(segment["text"])}</p></li>')
        lines.append('</ul></article>')
        proposed.append(''.join(lines))
    questions = []
    for index, entry in enumerate(spec['questions']):
        source_offset = entry['source_range'][0] - entry['window_range'][0]
        buttons = ''.join(version_button(next(v for v in versions if v['id'] == target['version']), target['range'])
                          for target in entry.get('video_ranges', []))
        questions.append(f'<article><h3>{esc(entry["id"])}</h3>'
                         f'<p id="hint-{index}">{esc(entry["question"])}</p>'
                         f'{listen_button(entry["window_range"], source_offset, entry["source_file"])}{buttons}'
                         f'<label for="answer-{index}">你实际听到的原话</label>'
                         f'<textarea id="answer-{index}" rows="3" aria-describedby="hint-{index}" '
                         'placeholder="填写原话；听不清就写“听不清”。"></textarea></article>')
    comparison = []
    for version in versions:
        parts = []
        for part in version['segments']:
            parts.append(f'<li><strong>{esc(part["label"])}</strong>：{esc(part["purpose"])}<br>'
                         f'{version_button(version, part["video_range"])} '
                         f'{listen_button(part["window_range"], part["source_range"][0] - part["window_range"][0], part["source_file"])}</li>')
        changes = ''.join(f'<li>{esc(change)}</li>' for change in version['changes'])
        comparison.append(f'<article><h3>{esc(version["label"])}</h3>'
                          f'<video id="{version["player"]}" controls playsinline preload="metadata" '
                          f'src="{version["media_file"]}" aria-label="{esc(version["label"])}"></video>'
                          f'<h4>实际改动（制作者记录）</h4><ul>{changes}</ul><h4>片段作用与来源</h4><ul>{"".join(parts)}</ul>'
                          f'<label for="version-note-{version["player"]}">这版的实际反馈</label>'
                          f'<textarea id="version-note-{version["player"]}" rows="3" placeholder="直接写感受、突兀位置或听不清；偏好不等于审核通过。"></textarea></article>')
    comparison_html = ('<section class="panel comparison"><h2>已有版本对比</h2><p class="note">视频为已有预览的原样副本，只供比较，不作为新剪辑源。播放器保持 1 倍；文件自身可能已经加速。来源和作用是制作者说明，仍需实际听看核对。</p><div class="versions">'
                       + ''.join(comparison) + '</div></section>') if versions else ''
    data = {'binding': binding, 'proposed': spec['proposed_segments'], 'questions': spec['questions'],
            'sources': sources, 'versions': versions}
    # JSON is data, never executable source; escape HTML parser terminators too.
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
    for char in ('<', '>', '&', '\u2028', '\u2029'):
        payload = payload.replace(char, f'\\u{ord(char):04x}')
    replacements = {
        '__TITLE__': esc(spec['title']), '__SOURCE_RANGE__': f'{time_label(offset)}–{time_label(end)}',
        '__DURATION__': f'{binding["duration_seconds"]:.2f}',
        '__PROPOSED_DURATION__': f'{sum(p["window_range"][1]-p["window_range"][0] for p in spec["proposed_segments"]):.2f}',
        '__POSTER__': f' poster="{poster_name}"' if poster_name else '',
        '__COMPARISON__': comparison_html,
        '__PROPOSED__': ''.join(proposed), '__QUESTIONS__': ''.join(questions), '__DATA__': payload}
    # One pass: an input string resembling a template token remains plain input.
    return re.sub('|'.join(map(re.escape, replacements)), lambda m: replacements[m.group()], PAGE)


def generate(spec_path, output):
    output = Path(output).expanduser()
    reviews = (ROOT / 'outputs/reviews').resolve()
    if (not output.is_absolute() or output.name != 'index.html'
            or not output.resolve().is_relative_to(ROOT.resolve())
            or not output.resolve().is_relative_to(reviews) or output.parent.resolve() == reviews):
        raise ValueError('听审卡须写入当前工作目录outputs/reviews/<id>/index.html')
    output = output.resolve()
    if output.parent.exists():
        raise FileExistsError('听审卡输出目录已存在；不覆盖任何文件，请使用新目录')
    spec, item, segments, binding, sources, versions, refs, poster = checked_inputs(spec_path)
    poster_name = 'poster' + poster[0].suffix.lower() if poster else None
    copies = [(Path(s['path']), output.parent / s['media_file'], s['window_sha256']) for s in sources]
    copies += [(Path(v['path']), output.parent / v['media_file'], v['sha256']) for v in versions]
    if poster:
        copies.append((poster[0], output.parent / poster_name, poster[1]))
    binding['files_sha256'] = key([*refs, *({'path': str(target), 'sha256': expected} for _, target, expected in copies)])
    page = render(spec, item, segments, binding, sources, versions, poster_name)
    output.parent.mkdir(parents=True, exist_ok=False)
    # Exclusive files and verified byte copies; never copy the original full live.
    for source, target, expected in copies:
        with source.open('rb') as stream, target.open('xb') as destination:
            shutil.copyfileobj(stream, destination, 8 * 1024 * 1024)
        if file_hash(target) != expected:
            raise ValueError('复制期间文件变化；听审卡未完成，请使用新的输出目录重试')
        refs.append({'path': str(target), 'sha256': expected})
    with output.open('x', encoding='utf-8') as destination:
        destination.write(page)
    refs.append(file_ref(output))
    manifest = {'schema_version': 1, 'status': 'unreviewed_review_card', 'binding': binding,
                'sources': sources, 'versions': versions, 'questions': spec['questions'],
                'files': refs, 'review_state_changed': False}
    # Recheck inputs after copying too; never bless files that changed mid-generation.
    for ref in refs:
        evidence_file(ref)
    with (output.parent / 'review-card.json').open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return {'status': 'unreviewed_review_card', 'index': str(output),
            'review_video': str(output.parent / 'review.mp4'),
            'window_sha256': item['window']['sha256'], 'source_span': item['source_span'],
            'review_state_changed': False}


def checked_card(card):
    try:
        card = local_file(card, ROOT / 'outputs/reviews')
        manifest_path = local_file(card.parent / 'review-card.json', card.parent)
    except (ValueError, OSError) as error:
        raise ValueError('卡片或绑定记录缺失，无法核验，需要重新审核') from error
    if card.name != 'index.html':
        raise ValueError('请指定本次听审卡的index.html')
    manifest = read(manifest_path)
    if (manifest.get('schema_version') != 1 or manifest.get('status') != 'unreviewed_review_card'
            or not manifest.get('files') or not any(ref['path'] == str(card) for ref in manifest['files'])):
        raise ValueError('听审卡绑定记录不完整，需要重新生成并重新审核')
    for ref in manifest['files']:
        try:
            evidence_file(ref)
        except (ValueError, OSError) as error:
            raise ValueError(f'文件变化或缺失，需要重新审核；旧反馈只适用于旧文件：{ref["path"]}') from error
    embedded = re.search(r'<script id="card-data" type="application/json">(.*?)</script>', card.read_text('utf-8'), re.S)
    data = json.loads(embedded.group(1)) if embedded else {}
    if (any(manifest.get(field) != data.get(field) for field in ('binding', 'sources', 'versions', 'questions'))
            or manifest['files'][-1]['path'] != str(card)
            or key(manifest['files'][:-1]) != manifest['binding'].get('files_sha256')):
        raise ValueError('卡片与绑定记录不一致，需要重新审核')
    return manifest, manifest_path


def save_feedback(card, text_path, version_id, times, output, question_id=None):
    manifest, manifest_path = checked_card(card)
    text_path = local_file(text_path, ROOT / 'private-context')
    raw_text = text_path.read_bytes().decode('utf-8-sig')
    required_text(raw_text)
    version = next((v for v in manifest['versions'] if v['id'] == version_id), None)
    question = next((q for q in manifest['questions'] if q['id'] == question_id), None)
    if question_id is not None and question is None:
        raise ValueError('问题ID未绑定当前听审卡')
    if version_id == 'source':
        source = next(s for s in manifest['sources']
                      if s['media_file'] == (question['source_file'] if question else 'review.mp4'))
        media = {'path': str(Path(card).parent / source['media_file']), 'sha256': source['window_sha256']}
        span(times, source['duration_seconds'])
        expected = question['window_range'] if question else None
    elif version:
        media = {'path': str(Path(card).parent / version['media_file']), 'sha256': version['sha256']}
        span(times, version['duration_seconds'])
        expected = next((target['range'] for target in question.get('video_ranges', [])
                         if target['version'] == version_id), None) if question else None
    else:
        raise ValueError('反馈须指定本卡已有版本ID，原片核词使用source')
    if question and times != expected:
        raise ValueError('问题反馈区间须与该问题绑定的当前版本范围一致')
    output = Path(output).expanduser()
    if (not output.is_absolute() or output.suffix.lower() != '.json'
            or not output.resolve().is_relative_to((ROOT / 'private-context').resolve())):
        raise ValueError('反馈须写入当前工作目录private-context内的新JSON文件')
    result = {'schema_version': 1, 'recorded_at': datetime.now(timezone.utc).isoformat(),
              'raw_text': raw_text, 'raw_text_file': file_ref(text_path),
              'card': file_ref(manifest_path), 'version': version_id, 'media': media, 'range': times,
              'question': question, 'source_binding': manifest['binding'],
              'review_status': 'pending', 'review_state_changed': False,
              'note': '仅保存用户原文及文件范围绑定；不自动解释为同款、字幕或成片审核通过。'}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return {'status': 'feedback_saved_pending', 'feedback': str(output), 'review_state_changed': False}


PAGE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light;--ink:#172a28;--muted:#475569;--line:#a8b8b4;--accent:#175b50;--paper:#fff;--back:#f3f7f5}
*{box-sizing:border-box}body{margin:0;background:var(--back);color:var(--ink);font:16px/1.65 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header,main{max-width:1220px;margin:auto;padding:24px}header{padding-bottom:8px}h1{font-size:clamp(24px,3vw,36px);line-height:1.25;margin:12px 0}h2{font-size:23px;margin:0 0 12px}h3{font-size:18px;margin:0 0 10px}p{margin:8px 0 16px}a{color:var(--accent)}
.flag{display:inline-block;border:1px solid #80550d;border-radius:6px;padding:4px 10px;background:#fff2cf;color:#694400;font-weight:700}.muted{color:var(--muted)}main{display:grid;grid-template-columns:minmax(0,1fr);gap:24px;align-items:start}.player,.panel{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:20px;min-width:0}.player{align-self:start}video{display:block;width:100%;height:min(56vh,560px);background:#111;border-radius:6px;object-fit:contain}
.buttons{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}button{min-height:44px;padding:9px 14px;border:1px solid var(--accent);border-radius:6px;background:white;color:var(--accent);font:inherit;font-weight:600;cursor:pointer;touch-action:manipulation;transition:background .15s}button:hover,button[aria-pressed="true"]{background:#e5f1ed}button.primary{background:var(--accent);color:white}button:active{background:#cfdfd9}button.primary:active{background:#103e37}
:focus-visible{outline:3px solid #a9490c;outline-offset:3px}textarea{display:block;resize:vertical;width:100%;min-height:100px;border:1px solid #637973;border-radius:6px;padding:12px;font:inherit;color:var(--ink);background:white}textarea[readonly]{min-height:280px;background:#f8faf9}label{display:block;font-weight:600;margin:12px 0 8px}.choice{display:flex;align-items:center;gap:10px;min-height:44px;margin:4px 0;font-weight:400;cursor:pointer}.choice input{width:20px;height:20px;flex:none;accent-color:var(--accent)}fieldset{min-width:0;border:1px solid var(--line);border-radius:6px;padding:12px;margin:16px 0}legend{font-weight:700;padding:0 6px}article{padding:20px 0;border-top:1px solid var(--line)}.listen{font-size:14px;text-align:left;white-space:normal}.transcript{list-style:none;padding:0;margin:16px 0 0}.transcript li{margin:12px 0;padding:12px;background:#f3f7f5;border-radius:6px}.transcript p{white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0}.stack{display:grid;gap:24px;min-width:0}.note{padding:12px;background:#fff7e5;border-left:3px solid #80550d}.status{min-height:1.65em;color:var(--muted)}.skip{position:absolute;left:12px;top:-100px;background:white;padding:8px}.skip:focus{top:12px}details{margin-top:16px}summary{cursor:pointer;min-height:44px;padding:8px 0}#fingerprint{overflow-wrap:anywhere;font:13px/1.7 ui-monospace,monospace;white-space:pre-wrap}noscript{display:block;padding:16px;background:#fff2cf}
@media(min-width:960px){main{grid-template-columns:minmax(320px,.9fr) minmax(0,1.1fr)}.player{position:sticky;top:16px}}
@media(max-width:500px){header,main{padding:16px}.player,.panel{padding:16px}video{height:50vh}}
@media(prefers-reduced-motion:reduce){button{transition:none}}
.comparison{grid-column:1/-1}.versions{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:24px}.versions article{border:0;padding:0}.versions li{margin-bottom:12px}.versions video{height:420px}
</style></head><body>
<a class="skip" href="#review">跳到听审问题</a><header><span class="flag">未听审 · 不是最终成片</span><h1>__TITLE__</h1>
<p class="muted">先比较可播放的版本，再针对具体问题回听原片。机器字幕保留原样，可能有错。</p><p class="note">文件、字幕、问题范围或版本变化后，需要重新审核。页面展示生成时的绑定；打开页面不会自动重算文件哈希，保存反馈前由工具重新核验。</p></header>
<noscript>浏览器未启用脚本：仍可用视频原生控件听看，请手动记录问题答案。顺序预听和反馈复制需要脚本。</noscript>
<main>__COMPARISON__<section class="player" aria-labelledby="player-title"><h2 id="player-title">原速核词窗口</h2>
<p class="muted" id="source-description">原片 __SOURCE_RANGE__ · 本窗口 __DURATION__ 秒</p>
<video id="video" controls playsinline preload="metadata"__POSTER__ aria-label="本次待听审原片窗口"><source src="review.mp4" type="video/mp4">浏览器无法播放，请打开同目录的 review.mp4。</video>
<div class="buttons"><button type="button" id="play-all" class="primary">播放全部原片窗口</button><button type="button" id="play-order">按拟成片顺序试听</button></div>
<p class="muted">顺序预听约 __PROPOSED_DURATION__ 秒（1 倍速），仅供核词与衔接判断。</p>
<div class="buttons" role="group" aria-label="播放速度"><button type="button" data-rate="0.8" aria-pressed="false">0.8 倍</button><button type="button" data-rate="1" aria-pressed="true">1 倍</button><button type="button" data-rate="1.25" aria-pressed="false">1.25 倍</button></div>
<p id="play-status" class="status" role="status">点击播放后开始。播放记录不会作为听审证据。</p>
<details><summary>原片与媒体指纹</summary><div id="fingerprint"></div></details></section>
<div class="stack"><section class="panel"><details><summary><strong>拟成片顺序与 ASR 原文（展开）</strong></summary><p class="muted">时间按钮可回到对应原声；原片时间是完整直播中的位置。下列文字未经人工纠正。</p>__PROPOSED__</details></section>
<form id="review" class="panel" autocomplete="off"><h2>听审反馈</h2><p class="note">只填实际看听的结果。不确定可以保留未确认，疑词可以写“听不清”。</p>
__QUESTIONS__
<fieldset><legend>主原片窗口的实际听看范围（附加窗口与成片请另写范围）</legend><label class="choice"><input type="radio" name="listening" value="整段主原片窗口">整段主原片窗口</label><label class="choice"><input type="radio" name="listening" value="仅选段">仅选段</label><label class="choice"><input type="radio" name="listening" value="未完成">未完成</label></fieldset>
<fieldset><legend>主原片窗口是否始终为同一款商品？</legend><label class="choice"><input type="radio" name="same-product" value="是，我确认主窗同款">是，我确认主窗同款</label><label class="choice"><input type="radio" name="same-product" value="否，出现了换款">否，出现了换款</label><label class="choice"><input type="radio" name="same-product" value="不确定">不确定</label></fieldset>
<label for="notes">其他实际发现（可不填）</label><textarea id="notes" rows="3" placeholder="例如换款位置、画面与口播不一致。"></textarea>
<div class="buttons"><button type="button" id="preview">预览反馈</button><button type="button" id="copy" class="primary">复制反馈</button></div><p id="copy-status" class="status" role="status">反馈只在本页生成，不会自动保存或提交。关闭页面前请复制。</p>
<label for="feedback">反馈文本（复制失败时可直接选中复制）</label><textarea id="feedback" readonly rows="12"></textarea></form></div></main>
<script id="card-data" type="application/json">__DATA__</script>
<script>
'use strict';
const card = JSON.parse(document.getElementById('card-data').textContent);
const video = document.getElementById('video');
const playStatus = document.getElementById('play-status');
const form = document.getElementById('review');
const feedback = document.getElementById('feedback');
const copyStatus = document.getElementById('copy-status');
let queue = [], active = null, requestId = 0;
function selectPlayer(player) {
  if (player !== video) { ++requestId; queue = []; active = null; }
  document.querySelectorAll('video').forEach(other => {
    if (other !== player) { other.pause(); delete other.dataset.rangeEnd; }
  });
}
document.querySelectorAll('video').forEach(player => player.addEventListener('play', () => selectPlayer(player)));
async function playRange(range, message, source = 'review.mp4') {
  selectPlayer(video);
  const request = ++requestId;
  active = null;
  if (video.getAttribute('src') !== source) {
    const ready = new Promise(resolve => video.addEventListener('loadedmetadata', resolve, {once:true}));
    video.src = source;
    video.load();
    await ready;
    if (request !== requestId) return;
  }
  active = range;
  video.currentTime = range[0];
  playStatus.textContent = message;
  const info = card.sources.find(item => item.media_file === source);
  document.getElementById('source-description').textContent = '当前原片范围（秒）：' + info.source_span.join(' — ') + ' · 窗口 ' + info.duration_seconds + ' 秒';
  video.play().catch(() => { playStatus.textContent = '未能开始播放，请使用视频原生播放按钮。'; });
}
document.getElementById('play-all').addEventListener('click', () => {
  queue = []; setPlaybackRate(1); playRange([0, card.binding.duration_seconds], '正在播放全部主原片窗口；不会自动确认已听审。');
});
document.getElementById('play-order').addEventListener('click', () => {
  setPlaybackRate(1);
  queue = card.proposed.slice();
  const part = queue.shift();
  playRange(part.window_range, '正在按拟成片顺序试听；片段衔接仅供预览。', part.source_file);
});
document.querySelectorAll('.listen').forEach(button => button.addEventListener('click', () => {
  queue = []; setPlaybackRate(1); playRange([Number(button.dataset.start), Number(button.dataset.end)], button.textContent, button.dataset.source);
}));
document.querySelectorAll('.version-listen').forEach(button => button.addEventListener('click', () => {
  const player = document.getElementById(button.dataset.player);
  selectPlayer(player);
  player.playbackRate = 1;
  player.currentTime = Number(button.dataset.start);
  player.dataset.rangeEnd = button.dataset.end;
  player.play().catch(() => { playStatus.textContent = '版本播放失败，请使用该视频的原生控件。'; });
}));
card.versions.forEach(version => {
  const player = document.getElementById(version.player);
  player.addEventListener('timeupdate', () => {
    if (player.dataset.rangeEnd && !player.seeking && player.currentTime >= Number(player.dataset.rangeEnd) - 0.025) {
      player.pause(); delete player.dataset.rangeEnd;
    }
  });
});
function advance() {
  if (!active || video.seeking || video.currentTime < active[1] - 0.025) return;
  if (queue.length) {
    const part = queue.shift(); playRange(part.window_range, '继续试听下一段；不会自动确认已听审。', part.source_file);
  }
  else { video.pause(); active = null; playStatus.textContent = '本次试听结束。请按实际听看情况填写反馈。'; }
}
video.addEventListener('timeupdate', advance);
video.addEventListener('ended', advance);
video.addEventListener('error', () => { playStatus.textContent = '视频未能加载，请确认 index.html 和 review.mp4 位于同一文件夹。'; });
function setPlaybackRate(rate) {
  video.playbackRate = rate;
  document.querySelectorAll('[data-rate]').forEach(item => item.setAttribute('aria-pressed', String(Number(item.dataset.rate) === rate)));
}
document.querySelectorAll('[data-rate]').forEach(button => button.addEventListener('click', () => setPlaybackRate(Number(button.dataset.rate))));
const bindingText = [
  'review.mp4 SHA-256：' + card.binding.window_sha256,
  '原片 SHA-256：' + card.binding.source_sha256,
  '原片范围（秒）：' + card.binding.range.join(' — '),
  '窗口范围（秒）：0 — ' + card.binding.duration_seconds,
  '抽取记录 SHA-256：' + card.binding.evidence.sha256,
  'ASR 预览 SHA-256：' + card.binding.preview_sha256,
  '听审问题 SHA-256：' + card.binding.spec_sha256
].join('\n');
document.getElementById('fingerprint').textContent = bindingText;
function valueOf(name) { const input = form.querySelector('input[name="' + name + '"]:checked'); return input ? input.value : '未确认'; }
function previewFeedback() {
  const lines = ['原片听审反馈（本人填写，尚未写入审核记录）', '实际听看：' + valueOf('listening'), '整窗同款：' + valueOf('same-product')];
  card.questions.forEach((question, index) => {
    const answer = document.getElementById('answer-' + index).value;
    lines.push(question.id + ' 原话：' + (answer.trim() ? answer : '未填写'),
      '问题原片范围（秒）：' + question.source_range.join(' — ') + '；窗口 SHA-256：' + question.window_sha256);
    (question.video_ranges || []).forEach(target => lines.push('对应版本 ' + target.version + ' 范围（秒）：' + target.range.join(' — ')));
  });
  card.versions.forEach(version => {
    const note = document.getElementById('version-note-' + version.player).value;
    lines.push(version.label + '（' + version.id + '）反馈：' + (note.trim() ? note : '未填写'),
      '版本 SHA-256：' + version.sha256 + '；完整版本范围（秒）：0 — ' + version.duration_seconds + '（非实际听看声明）');
  });
  const notes = document.getElementById('notes').value;
  if (notes.trim()) lines.push('其他实际发现：' + notes);
  lines.push('', '媒体绑定（仅证明本次文件与范围，不等于审核通过）', bindingText);
  feedback.value = lines.join('\n');
  return feedback.value;
}
form.addEventListener('submit', event => event.preventDefault());
document.addEventListener('input', () => {
  previewFeedback(); copyStatus.textContent = '预览已更新。请重新复制最新填写内容；尚未保存或提交。';
});
document.getElementById('preview').addEventListener('click', () => {
  previewFeedback(); copyStatus.textContent = '已按当前填写内容更新预览，尚未保存或提交。';
});
document.getElementById('copy').addEventListener('click', async () => {
  const text = previewFeedback();
  try {
    await navigator.clipboard.writeText(text);
    copyStatus.textContent = '反馈已复制。请粘贴回对话；本页不会写入审核结果。';
  } catch (error) {
    feedback.focus(); feedback.select();
    copyStatus.textContent = '浏览器未允许自动复制。下方文本已选中，请按 Ctrl+C 或长按复制。';
  }
});
previewFeedback();
</script></body></html>
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('generate', 'check', 'save-feedback'), default='generate')
    parser.add_argument('--spec', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--card', type=Path)
    parser.add_argument('--feedback-text', type=Path)
    parser.add_argument('--version')
    parser.add_argument('--range', type=float, nargs=2, dest='times')
    parser.add_argument('--question')
    args = parser.parse_args()
    try:
        if args.action == 'generate':
            result = generate(args.spec, args.output)
        elif args.action == 'check':
            checked_card(args.card)
            result = {'status': 'bindings_current', 'review_status': 'pending', 'review_state_changed': False,
                      'note': '仅校验文件绑定仍一致，不代表同款、字幕或成片已审核。'}
        else:
            result = save_feedback(args.card, args.feedback_text, args.version, args.times,
                                   args.output, args.question)
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        print(json.dumps({'status': 'ERROR', 'error': str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
