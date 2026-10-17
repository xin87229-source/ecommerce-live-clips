"""V0.2c: extract selected segments, review locally, map timestamps and burn ASS.

No transcription, candidate selection, content cutting or speed changes.
"""
import argparse
import json
import math
import os
import re
import shutil
import time
from pathlib import Path

from runtime import INSTALL_ROOT, config as runtime_config, profile as runtime_profile
from media_adapter import MediaAdapter
from run import component, write_json
from selector_support import ROOT, read, file_hash, key, validate_transcript, visual_span
from input_preflight import InputChecks, object_input
from quality_support import (VERSION as QUALITY_VERSION, task_gate, selection_binding,
                             content_decision, subtitle_risks, quality_evidence, audio_dependencies,
                             evidence_file, required, timestamp, span as evidence_span)

STYLE = INSTALL_ROOT / 'pipeline/subtitle_style.json'
DICTIONARY = INSTALL_ROOT / 'pipeline/subtitle_dictionary.json'
PRIVATE = ROOT / 'private-context/subtitle-v02c'
NUMBERS = r'[零〇一二两三四五六七八九十百千万亿\d]+(?:\.\d+)?'
APPAREL_KEYWORDS = ('显瘦', '遮小肚子', '腰线', '显白', '垂感', '刺绣', '提花',
                    '气质', '舒服', '显年轻')


def numbers(text):
    # Only established nonnumeric idioms; context-specific ASR fixes stay local.
    for phrase in ('于一体', '百搭', '富家千金'):
        text = text.replace(phrase, ' ')
    return re.findall(NUMBERS, text.translate(str.maketrans('兩萬億', '两万亿')))


def local(path, root):
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise ValueError('字幕输入记录必须来自本地任务目录')
    return path


def extract(composition):
    folder = local(composition, ROOT/'outputs')
    result = read(folder/'result.json')
    if result.get('version') != '0.2b' or result.get('status') != 'completed':
        raise ValueError('只接受成功的V0.2b结果')
    video = local(result['final_video'], folder)
    candidates = read(folder/'candidate-clips.json')
    transcript = local(candidates['transcript_cache'], ROOT/'private-context')
    t = read(transcript)
    validate_transcript(t)
    original = Path(result['material']['source_video']).resolve(strict=True)
    if file_hash(original) != result['source_sha256_after'] or t['source']['sha256'] != result['source_sha256_after']:
        raise ValueError('源素材、组合与转写哈希不一致')
    combo = result['best_combination']
    speed = combo['speed']
    if type(speed) not in (int, float) or not math.isfinite(speed) or not 1 <= speed <= 1.5:
        raise ValueError('倍速无效')
    order = combo['order']
    context = result.get('task_context') or {}
    batch = context.get('content_strategy', {}).get('version') == 'single-style-batch-v1'
    edited = 'edit_plan' in combo or 'render_timeline' in result
    if edited and (not batch or not isinstance(combo.get('edit_plan'), dict)
                   or not isinstance(result.get('render_timeline'), dict)):
        raise ValueError('编辑成片须保留已采用的编辑计划与完整渲染时间轴')
    if (not isinstance(order, list) or not order or len(set(order)) != len(order)
            or (not batch and len(order) > 3)):
        raise ValueError('组合必须保留一至三个不同片段')
    if batch:
        from batch_plan import execution_gate
        receipt = execution_gate(context, stage='render', candidates=candidates, order=order, speed=speed,
                                 **({'edit_plan': combo['edit_plan']} if edited else {}))
        if (not context.get('batch_adoption_fingerprint')
                or context['batch_adoption_fingerprint'] != receipt.get('adoption_fingerprint')):
            raise ValueError('当前采用版本与实际已渲染成片不一致，不能复用旧片')
        if receipt.get('render_timeline') is not None and not edited:
            raise ValueError('已采用编辑计划的成片不能删除编辑时间轴并退回旧映射')
    if edited:
        from review_cut import verify_inputs
        timeline = receipt.get('render_timeline')
        if (not isinstance(timeline, dict) or timeline != result['render_timeline']
                or timeline.get('edit_plan_hash') != key(combo['edit_plan'])
                or timeline.get('transcript_content_hash') != key(t)):
            raise ValueError('编辑时间轴与当前采用、原片或核词证据不一致，须重新制作并审核')
        verify_inputs(timeline)
        video_sha = file_hash(video)
        if result.get('video_sha256') != video_sha:
            raise ValueError('编辑成片音画文件哈希变化或缺失，不能复用旧审核')
        duration = result['metadata']['duration']
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or abs(combo['raw_duration'] - timeline['raw_duration']) > .001
                or abs(duration - timeline['duration_seconds']) > .12):
            raise ValueError('编辑时间轴的原声/总帧时长与实际成片不一致')
        selected, clips = timeline['mapped_segments'], timeline['mapped_clips']
        if not selected:
            raise ValueError('整条没有真实口播，不能生成伪字幕')
        if any(s['final_end'] > timeline['end_hold']['video_range'][0] + 1e-6 for s in selected):
            raise ValueError('字幕不得延伸到无声尾定格')
        return {'composition': str(folder), 'video': str(video), 'video_sha256': video_sha,
                'original': str(original), 'original_sha256': result['source_sha256_after'],
                'transcript_sha256': file_hash(transcript), 'speed': speed, 'duration': duration,
                'segments': selected, 'clips': clips, 'edit_plan': combo['edit_plan'],
                'render_timeline': timeline}
    by_id = {c['id']: c for c in candidates['candidates']}
    positions = {s['id']: i for i,s in enumerate(t['segments'])}
    selected, clips, offset = [], [], 0.0
    for clip_id in order:
        clip = by_id[clip_id]
        if batch and clip.get('kind') == 'visual':
            start, end = visual_span({'core_start': 0, 'core_end': t['source']['duration_seconds']}, clip)
            clips.append(dict(clip_id=clip_id, kind='visual', start=start, end=end,
                              final_start=offset/speed, final_end=(offset+end-start)/speed, segment_ids=[]))
            offset += end-start
            continue
        a, b = positions[clip['start_segment_id']], positions[clip['end_segment_id']]
        if a > b:
            raise ValueError('片段ID顺序错误')
        span = t['segments'][a:b+1]
        start, end = span[0]['start'], span[-1]['end']
        if abs(start-clip['start_seconds']) > 1e-6 or abs(end-clip['end_seconds']) > 1e-6:
            raise ValueError('组合时间与segment不符')
        for s in span:
            selected.append(dict(s, clip_id=clip_id, final_start=(offset+s['start']-start)/speed,
                                 final_end=(offset+s['end']-start)/speed))
        if batch:
            clips.append(dict(clip_id=clip_id, kind='speech', start=start, end=end,
                              final_start=offset/speed, final_end=(offset+end-start)/speed,
                              segment_ids=[s['id'] for s in span]))
        offset += end-start
    if batch and not selected:
        raise ValueError('整条没有真实口播，不能进入现有原声字幕链路；保留规划原因，不生成伪字幕')
    if abs(offset-combo['raw_duration']) > .001 or abs(offset/speed-result['metadata']['duration']) > .12:
        raise ValueError('映射时长与成片不一致')
    output = {'composition':str(folder), 'video':str(video), 'video_sha256':file_hash(video),
            'original':str(original), 'original_sha256':result['source_sha256_after'],
            'transcript_sha256':file_hash(transcript), 'speed':speed, 'duration':result['metadata']['duration'],
            'segments':selected}
    if batch:
        output['clips'] = clips
    return output


def clean(text, replacements):
    # Remove standalone fillers only; never globally strip meaningful "就是".
    text = text.strip()
    if text in {'嗯','啊','呃','然后呢'}:
        return ''
    text = re.sub(r'(这个){2,}', '这个', text)
    for wrong, right in replacements.items():
        if not wrong or not isinstance(right, str):
            raise ValueError('词典无效')
        if numbers(wrong) != numbers(right):
            raise ValueError('词典不得修改数字')
        text = text.replace(wrong, right)
    return re.sub(r'[，,。；;、\s]', '', text)


def confirmed_numeric_correction(correction, selected, segments):
    """Validate a local confirmation record, not the truth of the heard words."""
    expected = {'source_sha256':selected['original_sha256'],
                'segment_ids':[s['id'] for s in segments],
                'range':[segments[0]['start'], segments[-1]['end']],
                'from':correction['from'], 'to':correction['to']}
    evidence = correction.get('evidence', {})
    if (not isinstance(evidence, dict)
            or evidence.get('type') not in ('audio_listening', 'verified_transcript')
            or any(evidence.get(k) != expected[k] for k in ('source_sha256', 'segment_ids', 'range'))):
        raise ValueError('疑似数字纠错需要当前源、完整cue和精确源文件区间的核词证据')
    evidence_span(evidence['range'])
    record = read(evidence_file(evidence))
    entries = record.get('numeric_corrections') if isinstance(record, dict) else None
    if not isinstance(entries, list):
        raise ValueError('疑似数字纠错证据缺少numeric_corrections确认记录')
    matching = [entry for entry in entries if isinstance(entry, dict)
                and all(entry.get(k) == v for k, v in expected.items())]
    if len(matching) != 1:
        raise ValueError('疑似数字纠错须唯一匹配源、完整cue、源文件区间和原from/to')
    confirmation = matching[0].get('confirmation', {})
    if (not isinstance(confirmation, dict) or confirmation.get('status') != 'confirmed'
            or confirmation.get('type') not in ('user_message', 'audio_listening')):
        raise ValueError('疑似数字纠错只接受用户核词或实际原声听审确认')
    required(confirmation.get('actor'), '核词执行者')
    timestamp(confirmation.get('at'))
    quote = required(confirmation.get('quote'), '核实原话')
    proof = evidence_file(confirmation).read_text(encoding='utf-8-sig')
    if not correction['to'] or correction['to'] not in quote or quote not in proof:
        raise ValueError('核词原话未在所引用记录中出现或不包含本次准确改词')


def authorized_display_seconds(selected, item, segments):
    """Bind one local delegated decision; this does not authenticate its human source."""
    proof = item['display_exception']
    if (len(segments) != 1 or not isinstance(proof, dict)
            or set(proof) != {'path', 'sha256'}):
        raise ValueError('显示例外仅接受单segment及精确授权文件引用')
    path = evidence_file(proof)
    if not any(path.is_relative_to((ROOT/folder).resolve()) for folder in ('outputs', 'private-context')):
        raise ValueError('显示例外授权须保存在本地任务目录')
    record = read(path)
    if (not isinstance(record, dict)
            or record.get('schema_version') != 'delegated-display-decision-v1'
            or record.get('status') != 'AUTHORIZED_AGENT_IMPLEMENTATION_DECISION'
            or record.get('user_clicked_option') is not False):
        raise ValueError('显示例外缺少已核验委托下的实施决定')
    for field, keys in (('user_message', ('thread_id', 'id', 'turn_id', 'quote')),
                        ('agent_implementation_decision', ('source_thread_id', 'decision'))):
        value = record.get(field)
        if not isinstance(value, dict):
            raise ValueError('显示例外授权来源不完整')
        for name in keys:
            required(value.get(name), '显示例外授权来源 '+name)
    segment = segments[0]
    expected = dict(source_sha256=selected['original_sha256'], selected_hash=key(selected),
                    segment_ids=[segment['id']], source_range=[segment['start'], segment['end']],
                    final_range=[segment['final_start'], segment['final_end']],
                    original_quote=segment['text'], lines=item['lines'],
                    display_seconds=segment['final_end']-segment['final_start'])
    scope = record.get('exception_scope')
    if (not isinstance(scope, dict) or type(scope.get('display_seconds')) not in (int, float)
            or scope['display_seconds'] != 7
            or any(scope.get(k) != v for k, v in expected.items())):
        raise ValueError('显示例外须精确绑定当前源、选择、单段、区间、原句、排版和7秒时长')
    return scope['display_seconds']


def cues_from_review(selected, review, style, dictionary):
    if review['selected_hash'] != key(selected):
        raise ValueError('字幕审核与当前组合不匹配')
    segments, position, cues, audit = selected['segments'], 0, [], []
    clip_positions = review.get('clip_positions', {})
    clip_ids = {s['clip_id'] for s in segments}
    if (not isinstance(clip_positions, dict)
            or any(clip_id not in clip_ids or type(y) not in (int, float)
                   or not math.isfinite(y) or not 0 < y < 100
                   for clip_id, y in clip_positions.items())):
        raise ValueError('clip_positions须为当前有字幕片段clip_id到有限纵向百分比的映射（0至100之间）')
    replacements = dictionary['replacements']
    for item in review['cues']:
        if 'position_y_percent' in item or 'clip_positions' in item:
            raise ValueError('不得逐句移动字幕；仅在review.clip_positions按片段指定固定位置')
        ids = item['segment_ids']
        span = segments[position:position+len(ids)]
        if not ids or ids != [s['id'] for s in span] or len({s['clip_id'] for s in span}) != 1:
            raise ValueError('字幕须完整按组合顺序消费segment，不可跨剪辑接点')
        position += len(ids)
        text = ''.join(clean(s['text'], replacements) for s in span)
        raw = text
        for correction in item.get('corrections', []):
            old, new = correction['from'], correction['to']
            if not old or old not in text or not correction.get('reason'):
                raise ValueError('纠错必须精确匹配并记录原因')
            if numbers(old) != numbers(new):
                if text.count(old) != 1:
                    raise ValueError('疑似数字纠错须唯一匹配from；重复短语请连同上下文精确核词')
                confirmed_numeric_correction(correction, selected, span)
            text = text.replace(old, new)
        lines = item['lines']
        if (not 1 <= len(lines) <= style['max_lines'] or ''.join(lines) != text or not text
                or any(not line or re.search(r'[{}\\\r\n\x00-\x1f]',line) for line in lines)):
            raise ValueError('字幕行必须仅是已留痕清洗后的原话，不接受ASS代码或营销改写')
        start, end = span[0]['final_start'], span[-1]['final_end']
        limit = style.get('max_single_segment_seconds', 3.2) if len(span) == 1 else 3.2
        if type(limit) not in (int, float) or not 3.2 <= limit <= 5:
            raise ValueError('单段字幕上限须在3.2至5秒之间')
        if 'display_exception' in item:
            limit = authorized_display_seconds(selected, item, span)
        if not .8 <= end-start <= limit or (cues and start < cues[-1]['end']-.001):
            raise ValueError('字幕过短/过长/重叠，请按语义调整segment分组')
        if end > selected['duration']+.01:
            raise ValueError('字幕超出成片')
        highlights = item.get('highlights')
        if highlights is None:
            highlights = [word for word in style['keywords'] if any(word in line for line in lines)][:style['max_highlights']]
        if (not isinstance(highlights, list) or len(highlights) > style['max_highlights']
                or any(not isinstance(w, str) or not w or not any(w in line for line in lines) for w in highlights)
                or len(set(highlights)) != len(highlights)):
            raise ValueError('高亮必须是当前字幕内的完整短语，且不超过样式上限')
        cues.append({'start':start,'end':end,'lines':lines,'highlights':highlights,'segment_ids':ids})
        risks = list(item.get('risks', []))
        if re.search(r'\d|价格|库存|优惠|尺码|SKU|商品ID|面料比例|百分之|折扣|满减|\b(?:XX?S|XX?L|S|M|L)\b', raw, re.I):
            risks.append('高风险字段未据ASR确认事实')
        audit.append({'segment_ids':ids,'source_sha256':selected['original_sha256'],
                      'range':[span[0]['start'], span[-1]['end']],
                      'raw':raw,'cleaned':text,'corrections':item.get('corrections',[]),'risks':risks})
        if 'display_exception' in item:
            audit[-1]['display_exception'] = item['display_exception']
        if span[0]['clip_id'] in clip_positions:
            cues[-1]['position_y_percent'] = clip_positions[span[0]['clip_id']]
            audit[-1]['position_y_percent'] = clip_positions[span[0]['clip_id']]
    if position != len(segments):
        raise ValueError('仍有组合内segment未处理')
    return cues, audit


def font_choice(preferences, explicit_path=None):
    from PIL import ImageFont

    if explicit_path:
        path = Path(explicit_path).expanduser()
        if not path.is_absolute():
            raise ValueError('配置的font_path必须是绝对路径')
        if not path.is_file():
            raise ValueError('配置的font_path不是现有字体文件')
        try:
            family = ImageFont.truetype(str(path), 24).getname()[0]
        except OSError as error:
            raise ValueError('配置的font_path不是可读取字体') from error
        if not family or any(char in family for char in ',{}\r\n'):
            raise ValueError('配置的font_path字体名称不适合ASS')
        return family, path.resolve()

    found = []
    if os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                registry = winreg.OpenKey(hive, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts')
            except OSError:
                continue
            with registry:
                for i in range(winreg.QueryInfoKey(registry)[1]):
                    name, value, _ = winreg.EnumValue(registry, i)
                    if not isinstance(value, str):
                        continue
                    path = Path(value)
                    if not path.is_absolute():
                        path = Path(os.environ.get('WINDIR', r'C:\Windows'))/'Fonts'/path
                    if path.is_file():
                        found.append((name, path))
    for preferred in preferences:
        matches = [(n,p) for n,p in found if preferred.casefold() in n.casefold()]
        if matches:
            name, path = min(matches, key=lambda x: (0 if 'Medium' in x[0] or 'SemiBold' in x[0] else 1 if 'Bold' in x[0] else 2))
            family = re.sub(r' \(.*', '', name).split(' & ')[0]
            return family, path
    raise ValueError('未找到支持的已安装中文字体，不自动下载')


def style_for_profile(style, name):
    if name not in ('basic', 'apparel'):
        raise ValueError('字幕品类预设无效')
    configured = dict(style)
    configured['keywords'] = list(style['keywords'])
    if name == 'apparel':
        configured['keywords'].extend(word for word in APPAREL_KEYWORDS
                                      if word not in configured['keywords'])
    return configured


def installed_style(path):
    path = Path(path).resolve()
    if path != STYLE.resolve():
        raise ValueError('字幕样式必须使用安装目录资源')
    return style_for_profile(validate_style(object_input(read(path))), runtime_profile()['name'])


def color(value):
    if not re.fullmatch(r'#[0-9A-Fa-f]{6}',value):
        raise ValueError('颜色须为#RRGGBB')
    return '&H00'+value[5:7]+value[3:5]+value[1:3]


def stamp(seconds, ass=False):
    scale = 100 if ass else 1000
    n = round(seconds*scale)
    return f'{n//(3600*scale):01d}:{n//(60*scale)%60:02d}:{n//scale%60:02d}.{n%scale:02d}' if ass else f'{n//3600000:02d}:{n//60000%60:02d}:{n//1000%60:02d},{n%1000:03d}'


def highlight_spans(value, terms, maximum):
    if not terms or maximum <= 0:
        return []
    pattern = '|'.join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    return [(m.start(), m.end(), m[0]) for m in list(re.finditer(pattern, value))[:maximum]]


def emphasis_width(value, terms, maximum, font, highlight_font=None, bold=False):
    """Measure actual font runs, including an extra conservative bold overhang."""
    large = highlight_font or font
    width, end = 0.0, 0
    for start, stop, word in highlight_spans(value, terms, maximum):
        width += font.getlength(value[end:start]) + large.getlength(word)
        if bold:
            width += math.ceil(max(large.size, large.getlength(word)) * .12)
        end = stop
    return width + font.getlength(value[end:])


def emphasis_font(family, font_path, size):
    """Use an installed matching bold face when available, without downloads."""
    from PIL import ImageFont
    path = Path(font_path)
    base_family = ImageFont.truetype(str(path), size).getname()[0]
    mode = 'regular_font_with_conservative_bold_margin'
    try:
        _, candidate = font_choice([family])
        face = ImageFont.truetype(str(candidate), size)
        if face.getname()[0] == base_family and 'bold' in face.getname()[1].lower():
            path, mode = candidate, 'matching_bold_font_with_conservative_margin'
    except ValueError:
        pass
    return ImageFont.truetype(str(path), size), path, mode


def subtitle_files(cues, style, family):
    ass = ['[Script Info]','ScriptType: v4.00+','PlayResX: 1080','PlayResY: 1920',
           'ScaledBorderAndShadow: yes','WrapStyle: 2','', '[V4+ Styles]',
           'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding',
           f"Style: Default,{family},{style['font_size']},{color(style['primary_color'])},{color(style['primary_color'])},{color(style['outline_color'])},&H80000000,0,0,0,0,100,100,0,0,1,{style['outline']},{style['shadow']},2,60,60,0,1",
           '', '[Events]','Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    srt, preview = [], []
    for i,c in enumerate(cues,1):
        y = round(1920*c.get('position_y_percent', style['position_y_percent'])/100)
        lines = list(c['lines'])
        remaining = style['max_highlights']
        for j,line in enumerate(lines):
            if c['highlights']:
                pattern = '|'.join(re.escape(w) for w in sorted(c['highlights'],key=len,reverse=True))
                def mark(match):
                    nonlocal remaining
                    if remaining <= 0:
                        return match[0]
                    remaining -= 1
                    opening = '\\c' + color(style['highlight_color']) + '&'
                    closing = '\\c' + color(style['primary_color']) + '&'
                    if 'highlight_font_size' in style:
                        opening += '\\fs' + str(style['highlight_font_size'])
                        closing += '\\fs' + str(style['font_size'])
                    if style.get('highlight_bold'):
                        opening += '\\b1'
                        closing += '\\b0'
                    return '{' + opening + '}' + match[0] + '{' + closing + '}'
                lines[j] = re.sub(pattern,mark,line)
        position = f'\\pos(540,{y})'
        if c['highlights'] and style['animation'] == 'emphasis_rise':
            position = f'\\move(540,{y+18},540,{y},0,180)'
        tags = f"{{\\an2{position}\\fad({style['fade_ms']},0)}}"
        ass.append(f"Dialogue: 0,{stamp(c['start'],True)},{stamp(c['end'],True)},Default,,0,0,0,,"+tags+r'\N'.join(lines))
        srt.append(f"{i}\n{stamp(c['start'])} --> {stamp(c['end'])}\n"+'\n'.join(c['lines']))
        preview.append(' / '.join(c['lines']))
    return '\n'.join(ass)+'\n', '\n\n'.join(srt)+'\n', '\n'.join(preview)+'\n'


def filter_path(path):
    value = Path(path).resolve().as_posix()
    if any(c in value for c in "'\n\r"):
        raise ValueError('ASS路径不支持单引号或换行')
    return "'"+value.replace(':',r'\:')+"'"


def validate_style(style):
    if (style['max_lines'] != 2 or not 40 <= style['font_size'] <= 72
            or not 68 <= style['position_y_percent'] <= 75 or not 0 <= style['max_highlights'] <= 2
            or style['animation'] not in ('fade','emphasis_rise') or not 100 <= style['fade_ms'] <= 200
            or not 0 <= style['outline'] <= 8 or not 0 <= style['shadow'] <= 4):
        raise ValueError('字幕样式超出V0.2c安全范围')
    size = style.get('highlight_font_size', style['font_size'])
    if (type(size) not in (int, float) or not math.isfinite(size) or not style['font_size'] <= size <= 96
            or type(style.get('highlight_bold', False)) is not bool):
        raise ValueError('重点字号须在正文字号至96px之间，加粗须为布尔值')
    return style


def probe_input(video, config):
    # The existing adapter remains the only media command path. NUL avoids task/log writes.
    return MediaAdapter(local_config(config), Path(os.devnull)).probe(Path(video))


def local_config(path):
    if path is not None and Path(path).resolve() != (ROOT/'config.json').resolve():
        raise ValueError('字幕配置必须来自当前工作目录config.json')
    return runtime_config()


def render_inputs(composition, review_path, config, style_path, task_record):
    from PIL import ImageFont
    checks = InputChecks()
    selected = checks.run('composition', lambda: extract(composition))
    review = checks.run('review', lambda: object_input(read(local(review_path, ROOT/'private-context'))))
    style = checks.run('style', lambda: installed_style(style_path))
    dictionary = checks.run('dictionary', lambda: object_input(read(DICTIONARY)))
    context = cues = audit = font_info = input_meta = texts = None
    highlight_font_path = highlight_font_mode = None
    if selected is not None:
        prior = checks.run('composition_record', lambda: read(Path(composition)/'result.json'))
        if prior is not None:
            context = checks.run('task_gate', lambda: task_gate(task_record, selected['original'], prior,
                                                                execution_stage='render'))
        input_meta = checks.run('input_media', lambda: probe_input(selected['video'], config))
        if input_meta is not None:
            checks.run('input_format', lambda: check_input_format(input_meta))
        if review is not None:
            bound = review.get('selected_hash') == key(selected)
            checks.expect('review_binding', bound, '字幕审核与当前组合不匹配；先更新本次review')
            if bound:
                candidates = checks.run('candidates', lambda: read(Path(composition)/'candidate-clips.json'))
                if candidates is not None:
                    candidate_hash = key({k:v for k,v in candidates.items() if k != 'best_combination'})
                    checks.run('content_decision', lambda: content_decision(
                        review.get('content_decision'), selection_binding(selected), candidate_hash))
                review_cues = review.get('cues')
                if isinstance(review_cues, list):
                    for index, cue in enumerate(review_cues, 1):
                        checks.run(f'subtitle_risks[{index}]', lambda cue=cue: subtitle_risks({'cues':[cue]}, selected))
                else:
                    checks.expect('cues', False, 'review.cues必须为字幕数组')
                if style is not None and dictionary is not None:
                    mapped = checks.run('cue_mapping', lambda: cues_from_review(selected,review,style,dictionary))
                    if mapped is not None:
                        cues, audit = mapped
    if style is not None:
        font_info = checks.run('font', lambda: font_choice(
            style['font_preferences'], runtime_config().get('font_path')))
        if font_info is not None:
            family, font_path = font_info
            font = checks.run('font_load', lambda: ImageFont.truetype(str(font_path),style['font_size']))
            if font is not None and cues is not None:
                size = round(style.get('highlight_font_size', style['font_size']))
                large = font.font_variant(size=size)
                highlight_font_path, highlight_font_mode = font_path, 'same_font_size_only'
                if style.get('highlight_bold'):
                    emphasis = checks.run('emphasis_font', lambda: emphasis_font(family, font_path, size))
                    if emphasis is not None:
                        large, highlight_font_path, highlight_font_mode = emphasis
                for index, cue in enumerate(cues, 1):
                    checks.expect(f'safe_width[{index}]', all(
                        emphasis_width(line, cue['highlights'], style['max_highlights'], font, large,
                                       style.get('highlight_bold', False))+2*(style['outline']+style['shadow']) <= 960 for line in cue['lines']),
                        '字幕超出水平安全宽度，请按语义换行')
                    y = round(1920*cue.get('position_y_percent', style['position_y_percent'])/100)
                    # Conservative font line boxes include both lines, outline,
                    # shadow and the entire emphasis entrance, not just its end.
                    height = sum((large if cue['highlights'] else font).getmetrics())*len(cue['lines'])
                    border = style['outline']+style['shadow']
                    rise = 18 if cue['highlights'] and style['animation'] == 'emphasis_rise' else 0
                    checks.expect(f'safe_height[{index}]', y-height-border >= 0 and y+rise+border <= 1920,
                                  '字幕两行、描边或入场超出画面，请调整该片段固定位置')
                texts = checks.run('subtitle_format', lambda: subtitle_files(cues,style,family))
    return checks, dict(selected=selected, review=review, style=style, context=context,
                        cues=cues, audit=audit, font_info=font_info, input_meta=input_meta, texts=texts,
                        highlight_font_path=highlight_font_path, highlight_font_mode=highlight_font_mode)


def check_input_format(meta):
    if [meta['width'],meta['height']] != [1080,1920] or meta['audio']['codec'] != 'aac':
        raise ValueError('本版字幕模板只接受1080×1920与AAC原声')


def preflight(composition, review_path, config, style_path=STYLE, task_record=None):
    """Validate current input without creating output directories or changing task state."""
    checks = InputChecks()
    result = checks.run('input_schema', lambda: render_inputs(composition,review_path,config,style_path,task_record))
    if result is not None:
        checks = result[0]
    return checks.report()


def run(composition, review_path, task_id, config, style_path=STYLE, task_record=None):
    started = time.perf_counter()
    output_root = (ROOT/'outputs').resolve()
    if output_root.parent != ROOT.resolve():
        raise ValueError('outputs不得指向项目外')
    output = output_root/(component(task_id,'task_id')+'-v0.2c')
    if output.exists() or output.is_symlink():
        raise FileExistsError('V0.2c任务输出已存在，禁止覆盖')
    checks, inputs = render_inputs(composition,review_path,config,style_path,task_record)
    checks.require()
    selected, style, review, context = (inputs[k] for k in ('selected','style','review','context'))
    cues, audit = inputs['cues'], inputs['audit']
    family, font_path = inputs['font_info']
    highlight_path = inputs['highlight_font_path'] or font_path
    font_hashes = {str(path): file_hash(path) for path in (font_path, highlight_path)}
    ass,srt,preview_text = inputs['texts']
    input_meta = inputs['input_meta']
    output.mkdir(exist_ok=False)
    for name in ('final','preview','logs','fonts'):
        (output/name).mkdir()
    result = {'version':'0.2c','status':'failed','source_video':selected['video'],'style':style,
              'font':family,'font_path':str(font_path),'source_hashes_before':{k:selected[k] for k in ('video_sha256','original_sha256')},
              'subtitle_count':len(cues),'average_seconds':sum(c['end']-c['start'] for c in cues)/len(cues),
              'shortest_seconds':min(c['end']-c['start'] for c in cues),'longest_seconds':max(c['end']-c['start'] for c in cues),
              'highlight_occurrences':sum(min(style['max_highlights'],sum(len(highlight_spans(line, c['highlights'], style['max_highlights'])) for line in c['lines'])) for c in cues),
              'emphasis_cues':sum(bool(c['highlights']) for c in cues),
              'animated_emphasis_cues':sum(bool(c['highlights']) for c in cues) if style['animation']=='emphasis_rise' else 0,
              'risks':[a for a in audit if a['risks']], 'token_usage':None,
              'font_hashes': font_hashes, 'highlight_font_path': str(highlight_path),
              'highlight_font_mode': inputs['highlight_font_mode']}
    result.update(quality_version=QUALITY_VERSION, task_record=context['task_record'], task_context=context,
                  content_decision_ref='logs/review.json#content_decision')
    if 'render_timeline' in selected:
        result['render_timeline'] = selected['render_timeline']
        result['edit_plan'] = selected['edit_plan']
    try:
        for name,text in [('subtitle.ass',ass),('subtitle.srt',srt),('subtitle-preview.txt',preview_text)]:
            with (output/name).open('x',encoding='utf-8') as f:
                f.write(text)
        write_json(output/'logs/subtitle-audit.json',audit)
        write_json(output/'logs/review.json',review)
        write_json(output/'logs/cues.json',cues)
        write_json(output/'logs/selected.json',selected)
        shutil.copyfile(font_path,output/'fonts'/font_path.name)
        if highlight_path != font_path:
            shutil.copyfile(highlight_path, output/'fonts'/('emphasis-' + highlight_path.name))
        result['subtitle_generation_seconds'] = time.perf_counter()-started
        media = MediaAdapter(local_config(config),output/'logs/media.log')
        if file_hash(selected['video']) != selected['video_sha256']:
            raise ValueError('预检后输入视频发生变化')
        destination = output/'final'/(output.name+'.mp4')
        vf = 'ass=filename='+filter_path(output/'subtitle.ass')+':fontsdir='+filter_path(output/'fonts')
        burn_start = time.perf_counter()
        media.write_media(['-loglevel','info','-i',selected['video'],'-map','0:v:0','-map','0:a:0',
                           '-vf',vf,'-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',
                           '-c:a','copy','-map_metadata','-1','-map_chapters','-1',
                           '-movflags','+faststart',str(destination)],destination,delivery=True)
        result['burn_seconds'] = time.perf_counter()-burn_start
        meta,checks = media.check(destination,input_meta['duration'],[1080,1920],delivery=True)
        audio_hashes = [media.command('ffmpeg',['-v','error','-i',str(p),'-map','0:a:0','-c:a','copy','-f','hash','-hash','sha256','-']).strip()
                        for p in (Path(selected['video']),destination)]
        if audio_hashes[0] != audio_hashes[1]:
            raise ValueError('烧录前后音频数据不同')
        log_text = (output/'logs/media.log').read_text(encoding='utf-8')
        if 'fontselect:' not in log_text or re.search(r'Glyph .*not found|failed to find|Error opening',log_text,re.I):
            raise ValueError('ASS字体未确认或缺字，请检查日志')
        preview = output/'preview/subtitle-preview-12s.mp4'
        preview_duration = min(12, meta['duration'])
        media.write_media(['-i',str(destination),'-t',str(preview_duration),'-map','0:v:0','-map','0:a:0',
                           '-c:v','libx264','-preset','fast','-crf','18','-c:a','copy','-movflags','+faststart',str(preview)],preview,delivery=True)
        media.check(preview,preview_duration,[1080,1920],delivery=True)
        frames = media.look(destination,meta['duration'],output/'preview')
        after = {'video_sha256':file_hash(selected['video']),'original_sha256':file_hash(selected['original'])}
        if after != result['source_hashes_before']:
            raise ValueError('输入视频发生变化')
        if any(file_hash(path) != sha for path, sha in font_hashes.items()):
            raise ValueError('字幕制作期间字体发生变化')
        result.update(video=str(destination),preview=str(preview),metadata=meta,checks=checks,
                      frames=frames,source_hashes_after=after,audio_bitstream_unchanged=True,
                      timeline=('已采用编辑时间轴：各段整数帧起点＋保留原声偏移/该段speed；静音补边与尾定格不产生字幕；ASS量化到百分之一秒'
                                if 'render_timeline' in selected else
                                '原时间减片段起点，加累计前段时长，再除实际speed；ASS量化到百分之一秒'),
                      visual_review='pending',text_review='uncertain_terms_retained' if result['risks'] else 'cleaned_needs_listening')
        deps = audio_dependencies(selected, audio_hashes[1])
        result['audio_dependencies'] = deps
        result['quality_evidence'] = quality_evidence(review.get('quality_evidence'), file_hash(destination), meta['duration'], deps)
        from datetime import datetime, timezone
        result['quality_evidence']['technical'] = {
            'status':'passed', 'video_sha256':file_hash(destination), 'coverage':[0,meta['duration']],
            'evidence':{'type':'media_check','path':str(output/'logs/media.log'),'sha256':file_hash(output/'logs/media.log')},
            'actor':'subtitle.py','at':datetime.now(timezone.utc).isoformat(),
            'limitations':'技术检查不代表画面或原声审核'}
        result['status'] = 'completed'
    except Exception as error:
        result['error'] = str(error)
        raise
    finally:
        result['total_seconds'] = time.perf_counter()-started
        write_json(output/'result.json',result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','preflight','render'])
    parser.add_argument('--composition',type=Path,required=True)
    parser.add_argument('--review',type=Path)
    parser.add_argument('--task-id')
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--style',type=Path,default=STYLE)
    parser.add_argument('--task-record',type=Path)
    args = parser.parse_args()
    if args.action == 'prepare':
        selected = extract(args.composition)
        directory = PRIVATE/key(selected)
        directory.mkdir(parents=True,exist_ok=True)
        target = directory/'selected.json'
        if not target.exists():
            write_json(target,selected)
        candidates = read(Path(args.composition)/'candidate-clips.json')
        print(json.dumps({'selected':str(target),'selected_hash':key(selected),'segments':len(selected['segments']),
                          'selection_binding':selection_binding(selected), 'selection_hash':key(selection_binding(selected)),
                          'candidates_hash':key({k:v for k,v in candidates.items() if k!='best_combination'})},ensure_ascii=False))
    elif args.action == 'preflight':
        if not args.review:
            parser.error('preflight需要--review')
        report = preflight(args.composition,args.review,args.config,args.style,args.task_record)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        raise SystemExit(0 if report['status']=='passed' else 1)
    else:
        if not args.review or not args.task_id:
            parser.error('render需要--review和--task-id')
        print(json.dumps(run(args.composition,args.review,args.task_id,args.config,args.style,args.task_record),ensure_ascii=False,indent=2))
