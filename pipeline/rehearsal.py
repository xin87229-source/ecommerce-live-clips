"""Complete local draft rehearsal; never approves content or operates a platform."""
import argparse
import copy
import html
import json
import math
from pathlib import Path
import re
import shutil

from PIL import ImageFont

import review_card
import review_cut
import subtitle
from cover_layout import render as cover_render
from media_adapter import MediaAdapter
from runtime import INSTALL_ROOT, ROOT, config
from selector_support import atomic, file_hash, read, validate_transcript
from source_windows import import_entry

WATERMARK = '待审演练·机器字幕未核词'
STATUS = 'SIMULATED_NOT_SUBMITTED'
SIMULATION_CHECKS = {'media_decodes': True, 'subtitle_files_present': True, 'covers_present': True,
                     'copy_fields_present': True, 'original_and_task_unchanged': True,
                     'human_content_review': 'not_reviewed', 'platform_submission': 'not_performed'}


def local(value, base):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('输入须为当前工作区真实绝对路径')
    path = path.resolve(strict=True)
    if not path.is_file() or not path.is_relative_to(base.resolve()):
        raise ValueError('输入不在当前工作区允许的目录')
    return path


def text(value, label, limit=2000):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 for char in value)):
        raise ValueError(label + '须为有效单行文字')
    return value


def asr_input(path):
    """Check the same original-window/cache binding used by listening cards."""
    path = local(path, ROOT / 'private-context')
    preview = read(path)
    extraction = local(preview['extraction']['path'], ROOT / 'private-context/source-windows')
    import_entry(extraction)
    item = read(extraction)
    window = local(item['window']['path'], extraction.parent)
    cache = local(preview['transcript_cache']['path'], ROOT / 'private-context')
    transcript = read(cache)
    validate_transcript(transcript)
    if (preview.get('schema_version') != 1 or preview.get('status') != 'unreviewed_asr_preview'
            or preview.get('same_product') != 'unknown' or preview.get('audio_listening') != 'not_performed'
            or preview.get('transcript_verified') is not False
            or preview['extraction'] != {'path': str(extraction), 'sha256': file_hash(extraction)}
            or preview.get('source_sha256') != item['source']['sha256']
            or preview.get('window_sha256') != item['window']['sha256']
            or preview.get('source_span') != item['source_span']
            or file_hash(cache) != preview['transcript_cache'].get('sha256')
            or Path(transcript['source']['path']).resolve() != window
            or transcript['source']['sha256'] != item['window']['sha256']
            or transcript['source']['duration_seconds'] != item['window']['duration_seconds']
            or transcript['asr']['config_hash'] != preview.get('asr_config_hash')):
        raise ValueError('ASR须保持未核词状态并匹配当前抽取记录、原片、窗口和缓存')
    segments = preview.get('segments')
    if not isinstance(segments, list) or len(segments) != len(transcript['segments']):
        raise ValueError('ASR原文列表不完整')
    offset, end = item['source_span']
    for segment, original in zip(segments, transcript['segments']):
        if (segment.get('id') != original['id'] or segment.get('text') != original['text']
                or segment.get('window_range') != [original['start'], original['end']]
                or segment.get('source_range') != [offset + original['start'], min(offset + original['end'], end)]):
            raise ValueError('ASR原文或时间范围与原始缓存不符')
    timed = ({segment['id']: copy.deepcopy(segment.get('words', [])) for segment in transcript['segments']}
             if transcript['asr'].get('word_timestamps') is True else {})
    return dict(preview=preview, path=str(path), extraction=str(extraction), item=item, timed_words=timed,
                inputs={str(p): file_hash(p) for p in (path, extraction, cache, window)})


def plan(spec_path):
    path = local(spec_path, ROOT / 'private-context')
    spec = read(path)
    required = {'schema_version', 'cut_spec', 'asr_previews', 'selection', 'title', 'caption',
                'hashtags', 'cover_lines', 'cover_time'}
    if (not isinstance(spec, dict) or not required.issubset(spec) or set(spec) - required - {'highlight_terms'}
            or type(spec['schema_version']) is not int or spec['schema_version'] != 1):
        raise ValueError('演练仅接受固定说明字段，不接受自填字幕、审批、账号或发布参数')
    for name, limit in (('title', 80), ('caption', 2000)):
        text(spec[name], name, limit)
    if re.search(r'最低|最后一天|限时|库存|销量|优惠|减肥|瘦.{0,4}斤|折扣|百分之|https?://', spec['title'] + spec['caption']):
        raise ValueError('演练文案不得加入未经核实交易、效果承诺或链接')
    tags = spec['hashtags']
    if (not isinstance(tags, list) or not 1 <= len(tags) <= 5 or len(set(tags)) != len(tags)
            or any(not isinstance(tag, str) or not re.fullmatch(r'#[\w\u4e00-\u9fff]{2,16}', tag) for tag in tags)):
        raise ValueError('hashtags须为1至5个不同话题')
    lines = spec['cover_lines']
    if not isinstance(lines, list) or not 1 <= len(lines) <= 2:
        raise ValueError('cover_lines须为1至2行；系统另加待审标记')
    for line in lines:
        text(line, '封面文字', 30)
    selection = spec['selection']
    if not isinstance(selection, dict) or set(selection) != {'selected', 'reason', 'alternatives', 'uncertainties'}:
        raise ValueError('selection须记录选款、理由、候选取舍和未知事实')
    for name in ('selected', 'reason'):
        text(selection[name], '选款' + name)
    for name in ('alternatives', 'uncertainties'):
        if not isinstance(selection[name], list):
            raise ValueError('选款候选和未知事实须为列表')
        for value in selection[name]:
            text(value, '选款' + name)
    cut = review_cut.plan(local(spec['cut_spec'], ROOT / 'private-context'))
    paths = spec['asr_previews']
    if not isinstance(paths, list) or not paths:
        raise ValueError('每个原声窗口须提供真实ASR预览')
    asrs = [asr_input(value) for value in paths]
    required_extractions = {segment['audio']['extraction']['path'] for segment in cut['segments']}
    if len(asrs) != len(required_extractions) or {a['extraction'] for a in asrs} != required_extractions:
        raise ValueError('ASR预览须与当前剪法全部原声窗口一一对应')
    inputs = {str(path): file_hash(path), **cut['inputs']}
    for asr in asrs:
        inputs.update(asr['inputs'])
    by_extraction = {asr['extraction']: asr for asr in asrs}
    cues = []
    for clip in cut['segments']:
        start, end = clip['audio']['window_range']
        asr = by_extraction[clip['audio']['extraction']['path']]
        for segment in asr['preview']['segments']:
            a, b = segment['window_range']
            if not segment['text'].strip() or b <= start or a >= end:
                continue
            if a < start - 1e-6 or b > end + 1e-6 or b <= a:
                raise ValueError(f"剪口截入ASR段 {segment['id']}；请使用完整边界 [{a}, {b}] 或换句，不能配整句字幕")
            text(segment['text'], 'ASR原文')
            final_start = clip['video_range'][0] + (a - start) / clip['speed']
            final_end = clip['video_range'][0] + (b - start) / clip['speed']
            if cues and final_start < cues[-1]['end'] - .001:
                raise ValueError('ASR字幕重叠，保持待审，请换用无重叠完整片段')
            cues.append(dict(id=segment['id'], clip_id=clip['id'], text=segment['text'],
                             start=final_start, end=final_end, source_range=segment['source_range'],
                             window_range=[a, b], extraction=asr['extraction']))
            if segment['id'] in asr['timed_words']:
                cues[-1].update(words=copy.deepcopy(asr['timed_words'][segment['id']]),
                                word_timing='machine_alignment_not_verified')
    if not cues:
        raise ValueError('没有完整非空ASR原句，不能生成伪字幕')
    terms = spec.get('highlight_terms', [])
    if (not isinstance(terms, list) or len(terms) > 24
            or any(not isinstance(term, str) or not term.strip() or len(term) > 24
                   or re.search(r'[{}\\\x00-\x1f]', term) for term in terms)
            or len(set(terms)) != len(terms)
            or any(not any(term in cue['text'] for cue in cues) for term in terms)):
        raise ValueError('highlight_terms须为选中机器原句内的不同完整词，不能补词或加入样式代码')
    at = spec['cover_time']
    if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at < cut['end_hold']['video_range'][0]:
        raise ValueError('封面时间须在实际内容内，不能取静音尾定格')
    clip = next(part for part in cut['segments'] if part['video_range'][0] <= at < part['video_range'][1])
    visual = clip['visual']
    window_at = visual['window_range'][0] + (at - clip['video_range'][0]) * clip['speed']
    if window_at >= visual['window_range'][1]:
        raise ValueError('封面时间落在补帧内，请选实际原片画面')
    return {'schema_version': 1, 'kind': 'local_rehearsal', 'status': 'unreviewed_rehearsal',
            'is_draft': True, 'uploaded': False, 'submitted': False, 'platform_ui_verified': False,
            'transcript_verified': False, 'copy_status': 'draft_not_fact_checked', 'spec': spec,
            'highlight_terms': terms, 'highlight_semantics': 'visual_emphasis_only_not_fact_approval',
            'cut': cut, 'asrs': asrs, 'cues': cues, 'inputs': inputs,
            'cover_source': {'window': visual['window'], 'window_at': window_at,
                             'source_at': visual['source_range'][0] + window_at - visual['window_range'][0]},
            'approvals': copy.deepcopy(cut['approvals'])}


def wrap(raw, font, width=960, highlights=(), highlight_font=None, bold=False, border=0):
    width -= border
    if highlights:
        # Keep selected phrases atomic; never split a highlighted source word.
        spans = subtitle.highlight_spans(raw, highlights, 2)
        units, end = [], 0
        for start, stop, word in spans:
            units.extend(raw[end:start])
            units.append(word)
            end = stop
        units.extend(raw[end:])
        measure = lambda value: subtitle.emphasis_width(value, highlights, 2, font, highlight_font, bold)
    else:
        units, measure = list(raw), font.getlength
    if raw and measure(raw) <= width:
        return [raw]
    choices = []
    for at in range(1, len(units)):
        left, right = ''.join(units[:at]), ''.join(units[at:])
        a, b = measure(left), measure(right)
        if a <= width and b <= width and left.strip() and right.strip():
            # Balance two lines instead of leaving one or two orphan characters.
            # No dictionary or transcription correction is implied by this break.
            penalty = width if right[0] in '，。！？、；：,.!?;:' or left[-1] in '（《“「([' else 0
            choices.append((abs(a-b)+penalty, left, right))
    if not choices:
        raise ValueError('完整ASR句或重点词超过两行安全宽度；请换较短完整句，不可猜改原文')
    _, left, right = min(choices, key=lambda item: item[0])
    return [left, right]


def timed_pages(cue, fit, highlights=()):
    """Paginate one intact ASR segment at bound machine word times, never edit its text."""
    words = cue.get('words')
    if (cue.get('word_timing') != 'machine_alignment_not_verified'
            or not isinstance(words, list) or not words):
        raise ValueError('长句缺少机器词时间；请重新转写或换完整句，不能按字数均摊时间')
    a, b = cue['window_range']
    previous = a
    for word in words:
        if (not isinstance(word, dict) or not isinstance(word.get('word'), str) or not word['word']
                or any(type(word.get(k)) not in (int, float) or not math.isfinite(word[k])
                       for k in ('start', 'end', 'probability'))
                or not 0 <= word['probability'] <= 1
                or not a <= previous <= word['start'] < word['end'] <= b):
            raise ValueError('机器词时间缺失、零时长、重叠或越界；不能静默补齐或裁短')
        previous = word['end']
    if ''.join(word['word'] for word in words) != cue['text']:
        raise ValueError('机器词串与绑定原句不一致；不能借用其他转写的时间')
    scale = (cue['end']-cue['start'])/(b-a)
    # All selected phrases stay atomic, including phrases spanning several machine words.
    spans = subtitle.highlight_spans(cue['text'], highlights, len(cue['text']))
    ends, total = [], 0
    for word in words:
        total += len(word['word'])
        ends.append(total)
    boundaries = {i+1 for i, end in enumerate(ends) if not any(x < end < y for x, y, _ in spans)}

    def fits(start, stop):
        value = ''.join(word['word'] for word in words[start:stop])
        if not value.strip():
            return False
        try:
            fit(value)
            return True
        except ValueError:
            return False

    pages, start = [], 0
    while start < len(words):
        choices = []
        for stop in range(start+1, len(words)+1):
            if stop not in boundaries:
                continue
            if not fits(start, stop):
                break
            choices.append(stop)
        if not choices:
            raise ValueError('单个机器词或完整重点词超过字幕安全宽度；不能拆词或伪造时间')
        stop = choices[-1]
        if stop < len(words) and fits(stop, len(words)):
            # Balance the last two pages using real durations, without moving their boundaries.
            balanced = [end for end in choices if end < len(words) and fits(end, len(words))]
            stop = min(balanced, key=lambda end: abs((words[end-1]['end']-words[start]['start'])
                                                    -(words[-1]['end']-words[end]['start'])))
        wa, wb = words[start]['start'], words[stop-1]['end']
        first, last = cue['start']+(wa-a)*scale, cue['start']+(wb-a)*scale
        if round(first*100) >= round(last*100):
            raise ValueError('机器词页不足ASS时间精度；不能延长或移动口播时间')
        pages.append(dict(cue, text=''.join(word['word'] for word in words[start:stop]),
                          start=first, end=last, window_range=[wa, wb],
                          source_range=[cue['source_range'][0]+wa-a, cue['source_range'][0]+wb-a],
                          words=copy.deepcopy(words[start:stop]), word_span=[start, stop],
                          source_cues=[copy.deepcopy(cue)]))
        start = stop
    return pages


def readable_cues(original, fit):
    """Group short machine segments, preserving every original and its own range."""
    groups = []
    for cue in original:
        current = dict(cue, source_cues=copy.deepcopy(cue.get('source_cues', [cue])))
        if groups and 'word_span' not in cue and 'word_span' not in groups[-1]:
            previous = groups[-1]
            last = previous['source_cues'][-1]
            adjacent = (previous['clip_id'] == cue['clip_id']
                        and previous['extraction'] == cue['extraction']
                        and abs(cue['start']-previous['end']) <= .08
                        and abs(cue['source_range'][0]-last['source_range'][1]) <= .08
                        and abs(cue['window_range'][0]-last['window_range'][1]) <= .08)
            short = min(previous['end']-previous['start'], cue['end']-cue['start']) < 1.2
            if adjacent and short and cue['end']-previous['start'] <= 4.5:
                joined = previous['text'] + ' ' + cue['text']
                try:
                    fit(joined)
                except ValueError:
                    pass
                else:
                    previous.update(text=joined, end=cue['end'],
                                    source_range=[previous['source_range'][0], cue['source_range'][1]],
                                    window_range=[previous['window_range'][0], cue['window_range'][1]])
                    previous['source_cues'].append(copy.deepcopy(cue))
                    previous.pop('words', None)
                    previous.pop('word_timing', None)
                    continue
        groups.append(current)
    return groups


def escaped_ass(value):
    # An empty override separates a literal slash from N/n/h; doubling the slash
    # still triggers libass line-break escapes. SRT and source text stay unchanged.
    replacements = {'\\': r'\{}', '{': r'\{', '}': r'\}'}
    return ''.join(replacements.get(char, char) for char in value)


def subtitles(record, style, family, font, highlight_font=None):
    def fit(raw):
        hits = subtitle.highlight_spans(raw, record.get('highlight_terms', []), style['max_highlights'])
        terms = list(dict.fromkeys(hit[2] for hit in hits))
        return wrap(raw, font, highlights=terms, highlight_font=highlight_font,
                    bold=style.get('highlight_bold', False), border=2*(style['outline']+style['shadow']))

    display = []
    for cue in record['cues']:
        try:
            fit(cue['text'])
        except ValueError:
            display.extend(timed_pages(cue, fit, record.get('highlight_terms', [])))
        else:
            display.append(cue)
    cues = []
    for cue in readable_cues(display, fit):
        hits = subtitle.highlight_spans(cue['text'], record.get('highlight_terms', []), style['max_highlights'])
        terms = list(dict.fromkeys(hit[2] for hit in hits))
        lines = fit(cue['text'])
        cues.append(dict(cue, lines=lines, highlights=terms, highlight_occurrences=len(hits)))
    _, srt, _ = subtitle.subtitle_files(cues, style, family)
    safe = [dict(cue, lines=[escaped_ass(line) for line in cue['lines']]) for cue in cues]
    ass, _, _ = subtitle.subtitle_files(safe, style, family)
    ass += (f"Dialogue: 10,0:00:00.00,{subtitle.stamp(record['cut']['duration_seconds'], True)},Default,,0,0,0,,"
            r'{\an8\pos(540,80)\fs38\c&H00D6FF&\bord2}' + WATERMARK + '\n')
    return ass, srt, cues


def readability(cues, original_count):
    short, dense = [], []
    for index, cue in enumerate(cues, 1):
        seconds = cue['end']-cue['start']
        cps = len(re.sub(r'\s', '', cue['text']))/seconds
        entry = {'cue': index, 'video_range': [cue['start'], cue['end']],
                 'source_segment_ids': [item['id'] for item in cue['source_cues']],
                 'seconds': round(seconds, 3), 'characters_per_second': round(cps, 2)}
        if seconds < 1.2:
            short.append(entry)
        if cps > 12:
            dense.append(entry)
    return {'original_segment_count': original_count, 'display_cue_count': len(cues),
            'short_cues': short, 'dense_cues': dense,
            'machine_timed_pages': sum('word_span' in cue for cue in cues),
            'grouping': 'same_clip_contiguous_source_only_max_4.5_seconds',
            'pagination': 'complete_raw_segment_preserved_validated_machine_word_boundaries_only',
            'original_texts_preserved': True,
            'note': '仅阅读风险提示；1.2秒/每秒12字为本模板检查阈值，不证明口播同步、语义或听审通过。'}


def cards(record, output, video):
    links = []
    for index, asr in enumerate(record['asrs'], 1):
        clips = [part for part in record['cut']['segments'] if part['audio']['extraction']['path'] == asr['extraction']]
        parts = [dict(label=part['purpose'], purpose=part['purpose'],
                      window_range=part['audio']['window_range'], video_range=part['video_range']) for part in clips]
        questions = [dict(id=f'Q{number}', window_range=part['window_range'],
                          question='；'.join(clip['questions']) or '核实本段机器原话、同款与切口；听不清则保留未知。',
                          video_ranges=[{'version': 'draft', 'range': part['video_range']}])
                     for number, (part, clip) in enumerate(zip(parts, clips), 1)]
        spec = dict(extraction=asr['extraction'], asr_preview=asr['path'],
                    title=record['spec']['selection']['selected'] + '｜待审原速问题',
                    proposed_segments=parts, questions=questions,
                    versions=[dict(id='draft', label='完整待审演练', path=str(video), sha256=file_hash(video),
                                   duration_seconds=record['cut']['duration_seconds'],
                                   changes=['本次从原片窗口重制，使用未核词机器字幕；无审批变化'], segments=parts)])
        target = ROOT / 'private-context/rehearsals' / output.name / f'card-{index}.json'
        if target.exists():
            raise FileExistsError('原速卡说明已存在，使用新的task-id')
        atomic(target, spec)
        record['inputs'][str(target.resolve())] = file_hash(target)
        relative = f'review-{index}/index.html'
        review_card.generate(target, output / relative)
        links.append(relative)
    return links


def page(record, links):
    esc = html.escape
    spec = record['spec']
    questions = ''.join(f'<li><a href="{link}">第{i}组：逐段原速回听与反馈</a></li>' for i, link in enumerate(links, 1))
    alternatives = ''.join(f'<li>{esc(value)}</li>' for value in spec['selection']['alternatives'])
    label = review_card.time_label
    timeline = ''.join(f'<tr><td>{esc(part["purpose"])}</td><td>{label(part["video_range"][0])}–{label(part["video_range"][1])}</td>'
                       f'<td>{label(part["audio"]["source_range"][0])}–{label(part["audio"]["source_range"][1])}</td>'
                       f'<td>{part["speed"]} 倍</td><td>{esc(part["visual_note"])}</td></tr>' for part in record['cut']['segments'])
    reading = record.get('subtitle_readability')
    reading_note = (f'<p>字幕阅读检查：{reading["original_segment_count"]} 个机器原段整理为 '
                    f'{reading["display_cue_count"]} 条显示字幕；短停留提醒 {len(reading["short_cues"])} 条，'
                    f'高密度提醒 {len(reading["dense_cues"])} 条。原文字段与各自来源保留；分组仅改善显示，不代表核词或同步通过。</p>'
                    if reading else '')
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; connect-src 'none'; form-action 'none'">
<title>{esc(spec['title'])}｜本地待审演练</title><style>body{{max-width:1000px;margin:32px auto;padding:0 20px;background:#f6f4ef;color:#232323;font:17px/1.6 system-ui}}video{{width:min(100%,340px);max-height:70vh;background:#000}}img{{width:min(100%,280px)}}section{{padding:24px;background:white;margin:20px 0;border-radius:12px}}strong{{color:#915d00}}a{{color:#075a87}}pre{{white-space:pre-wrap}}.media{{display:flex;gap:24px;flex-wrap:wrap}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #ddd}}.scroll{{overflow:auto}}</style>
<h1>{esc(spec['selection']['selected'])}</h1><p><strong>{WATERMARK}</strong> · 未上传、未提交</p>
<section><h2>选款与素材</h2><p>{esc(spec['selection']['reason'])}</p><ul>{alternatives}</ul><p>待确认：{esc('；'.join(spec['selection']['uncertainties']) or '仍需核词、同款和最终听看')}</p></section>
<section><h2>本地发帖预览</h2><div class="media"><video src="draft-video.mp4" controls preload="metadata"></video><img src="cover-3x4.jpg" alt="待审演练封面"></div><h3>{esc(spec['title'])}</h3><p>{esc(spec['caption'])}</p><p>{esc(' '.join(spec['hashtags']))}</p>
<p>这里只验证本地素材与字段齐备。没有真实平台登录、上传、挂车、排期或提交；抖音真实后台交互未验证。</p>
<p><a href="draft-subtitle.srt">机器字幕 SRT</a> · <a href="draft-subtitle.ass">机器字幕 ASS</a> · <a href="cover-4x3.jpg">横封面</a> · <a href="simulation.json">文件检查记录</a></p></section>
<section><h2>选段作用与原片时间</h2><div class="scroll"><table><thead><tr><th>作用</th><th>成片位置</th><th>原片原声区间</th><th>速度</th><th>画面关系</th></tr></thead><tbody>{timeline}</tbody></table></div><p>尾部定格 {record['cut']['end_hold']['seconds']} 秒；字幕不延伸到静音尾部。<a href="rehearsal.json">完整来源、窗口与版本绑定</a></p></section>
<section><h2>分项质量</h2><p>媒体解码、帧率、字幕文件、中文字体、封面排版：技术检查通过。原话核词、同款、连续画面、原声听感、最终用户验收：均待真实审核。</p>{reading_note}<p><a href="quality.md">可读质量清单</a> · <a href="quality.json">技术结果与内容状态</a></p></section>
<section><h2>逐段核实</h2><ul>{questions}</ul><p>所有内容审核均为未通过审核；自然语言反馈不自动变成审批。</p></section></html>'''


def build(spec_path, task_id):
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', task_id):
        raise ValueError('task-id须为1至64个字母、数字、短横线或下划线')
    output = ROOT / 'outputs/reviews/rehearsals' / task_id
    if output.exists() or output.is_symlink():
        raise FileExistsError('演练输出已存在，不覆盖旧版本')
    if not output.resolve().is_relative_to((ROOT / 'outputs').resolve()) or (ROOT / 'outputs').resolve().parent != ROOT.resolve():
        raise ValueError('演练输出不可重定向到工作区外')
    record = plan(spec_path)
    style = dict(subtitle.installed_style(subtitle.STYLE), font_size=48, animation='fade', max_highlights=0)
    style.pop('highlight_font_size', None)
    style.pop('highlight_bold', None)
    if record['highlight_terms']:
        style.update(highlight_font_size=68, highlight_bold=True, highlight_color='#FFD75A', max_highlights=2)
    family, font_path = subtitle.font_choice(style['font_preferences'], config().get('font_path'))
    font = ImageFont.truetype(str(font_path), style['font_size'])
    highlight_path = font_path
    highlight_font = None
    highlight_font_mode = 'not_used'
    if record['highlight_terms']:
        highlight_font, highlight_path, highlight_font_mode = subtitle.emphasis_font(family, font_path, 68)
    ass, srt, cues = subtitles(record, style, family, font, highlight_font)
    record['subtitle_readability'] = readability(cues, len(record['cues']))
    for path in (Path(__file__), INSTALL_ROOT / 'pipeline/review_cut.py', INSTALL_ROOT / 'pipeline/subtitle.py',
                 INSTALL_ROOT / 'pipeline/cover_layout.py', subtitle.STYLE, INSTALL_ROOT / 'pipeline/cover_config.json',
                 ROOT / 'config.json', font_path, highlight_path):
        record['inputs'][str(path.resolve())] = file_hash(path)
    review_cut.verify_inputs(record)
    output.mkdir(parents=True, exist_ok=False)
    record['build_status'] = 'failed'
    try:
        (output / 'fonts').mkdir()
        shutil.copyfile(font_path, output / 'fonts' / font_path.name)
        if highlight_path != font_path:
            shutil.copyfile(highlight_path, output / 'fonts' / ('emphasis-' + highlight_path.name))
        atomic(output / 'draft-subtitle.ass', ass, plain=True)
        atomic(output / 'draft-subtitle.srt', srt, plain=True)
        media = MediaAdapter(config(), output / 'media.log')
        base = output / 'source-cut.mp4'
        review_cut.render(media, record['cut'], base)
        video = output / 'draft-video.mp4'
        vf = 'ass=filename=' + subtitle.filter_path(output / 'draft-subtitle.ass') + ':fontsdir=' + subtitle.filter_path(output / 'fonts')
        media.write_media(['-loglevel', 'info', '-i', str(base), '-map', '0:v:0', '-map', '0:a:0', '-vf', vf,
                           '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'copy',
                           '-map_metadata', '-1', '-map_chapters', '-1', '-movflags', '+faststart', str(video)], video, delivery=True)
        metadata, checks = media.check(video, record['cut']['duration_seconds'], review_cut.SIZE, delivery=True)
        if metadata['frame_timing']['frame_count'] != record['cut']['frame_count']:
            raise ValueError('草稿实际帧数与原片剪辑映射不符')
        hashes = [media.command('ffmpeg', ['-v', 'error', '-i', str(path), '-map', '0:a:0', '-c:a', 'copy',
                                         '-f', 'hash', '-hash', 'sha256', '-']).strip() for path in (base, video)]
        if hashes[0] != hashes[1]:
            raise ValueError('字幕烧录改变了音频数据')
        log = (output / 'media.log').read_text('utf-8')
        if 'fontselect:' not in log or re.search(r'Glyph .*not found|failed to find|Error opening', log, re.I):
            raise ValueError('字幕中文字体加载或字形检查失败')
        frames = media.look(video, record['cut']['duration_seconds'], output)
        frame = output / 'cover-source.jpg'
        media.write_media(['-ss', str(record['cover_source']['window_at']), '-i', record['cover_source']['window']['path'],
                           '-frames:v', '1', '-q:v', '2', '-update', '1', str(frame)], frame)
        cover_config = read(INSTALL_ROOT / 'pipeline/cover_config.json')
        cover_config['lines'] = [*record['spec']['cover_lines'], '待审演练']
        layouts = {ratio: cover_render(frame, output / f'cover-{ratio}.jpg', cover_config, ratio, font_path)
                   for ratio in ('3x4', '4x3')}
        quality = {'technical': {'status': 'passed', 'metadata': metadata, 'checks': checks,
                                'audio_bitstream_unchanged': True, 'chinese_font_loaded': True,
                                'frames': frames, 'note': '仅媒体检查，不代表画面、声音、商品或原话审核'},
                   'content': copy.deepcopy(record['approvals']), 'cover_layout': layouts,
                   'subtitle': {'source': 'unverified_machine_transcript', 'complete_segments_only': True,
                                'original_text_unchanged': True, 'watermark': WATERMARK,
                                'font_size': style['font_size'], 'highlight_font_size': style.get('highlight_font_size'),
                                'highlight_bold': style.get('highlight_bold', False),
                                'highlight_color': style['highlight_color'], 'highlight_terms': record['highlight_terms'],
                                'highlight_occurrences': sum(c['highlight_occurrences'] for c in cues),
                                'max_highlights_per_cue': style['max_highlights'],
                                'highlight_font_path': str(highlight_path),
                                'highlight_font_mode': highlight_font_mode,
                                'width_check': '实际字号字体测宽，粗体额外留12%文字宽度或字号（取大值）外扩余量；重点词不拆行',
                                'meaning': '仅视觉强调，不代表词句或商品事实已核实'},
                   'readability': record['subtitle_readability'],
                   'copy': 'draft_not_fact_checked', 'platform_ui_verified': False, 'submitted': False}
        atomic(output / 'quality.json', quality)
        atomic(output / 'quality.md', '# 待审演练质量清单\n\n'
               '| 项目 | 实际状态 |\n| --- | --- |\n'
               '| 媒体解码、30 fps、帧数、音频保留 | 技术检查通过 |\n'
               '| 中文字体、字幕安全宽度、封面文字区 | 技术检查通过 |\n'
               '| 原话核词 | 未核实；字幕来自机器原文完整段 |\n'
               '| 商品同款 | 未确认 |\n| 连续画面与实际原声听感 | 未完成真实审核 |\n'
               '| 封面与文案内容事实 | 草稿，待核实 |\n| 用户最终验收 | 未通过审核 |\n'
               '| 平台后台交互 | 未验证，仅本地演练 |\n| 上传、提交 | 均未执行 |\n\n'
               '源文件、任务卡、输入与产物均绑定哈希。文件变化后须重新执行 check；旧检查不能用于变化后的文件。', plain=True)
        for name in ('title', 'caption'):
            atomic(output / (name + '.txt'), record['spec'][name], plain=True)
        atomic(output / 'hashtags.txt', ' '.join(record['spec']['hashtags']), plain=True)
        links = cards(record, output, video)
        atomic(output / 'index.html', page(record, links), plain=True)
        review_cut.verify_inputs(record)
        record.update(build_status='completed', video={'path': str(video), 'sha256': file_hash(video)},
                      technical=quality['technical'],
                      subtitle_cues=cues, covers=layouts, review_cards=links)
    except Exception as error:
        record['error'] = str(error)
        raise
    finally:
        atomic(output / 'rehearsal.json', record)
    files = {str(path.relative_to(output).as_posix()): file_hash(path) for path in sorted(output.rglob('*')) if path.is_file()}
    simulation = {'schema_version': 1, 'kind': 'local_rehearsal', 'status': STATUS,
                  'package': str(output.resolve()), 'is_draft': True, 'platform_ui_verified': False,
                  'uploaded': False, 'submitted': False, 'inputs': record['inputs'], 'files': files,
                  'checks': copy.deepcopy(SIMULATION_CHECKS),
                  'limitation': '仅本地文件和字段演练；不证明平台后台可用、发布成功或人工验收'}
    atomic(output / 'simulation.json', simulation)
    return check(output)


def check(package):
    output = Path(package).resolve(strict=True)
    if (not output.is_dir() or not output.is_relative_to(ROOT.resolve())
            or not output.is_relative_to((ROOT / 'outputs/reviews/rehearsals').resolve())):
        raise ValueError('须检查当前工作区演练包')
    simulation = read(output / 'simulation.json')
    record = read(output / 'rehearsal.json')
    for item in (simulation, record):
        if (item.get('kind') != 'local_rehearsal' or item.get('is_draft') is not True
                or any(item.get(key) is not False for key in ('platform_ui_verified', 'uploaded', 'submitted'))):
            raise ValueError('演练状态矛盾，不能标记发布或审核通过')
    if (simulation.get('status') != STATUS or simulation.get('package') != str(output)
            or record.get('status') != 'unreviewed_rehearsal' or record.get('build_status') != 'completed'
            or record.get('transcript_verified') is not False
            or record.get('approvals') != {name: 'not_reviewed' for name in
                ('same_product', 'transcript', 'continuous_visual', 'audio_listening', 'user_confirmation')}
            or simulation.get('inputs') != record.get('inputs') or simulation.get('checks') != SIMULATION_CHECKS):
        raise ValueError('演练记录不完整或矛盾')
    review_cut.verify_inputs(record)
    files = simulation.get('files')
    actual = {str(path.relative_to(output).as_posix()) for path in output.rglob('*')
              if path.is_file() and path != output / 'simulation.json'}
    if not isinstance(files, dict) or set(files) != actual:
        raise ValueError('演练包文件清单发生变化')
    for name, sha in files.items():
        path = (output / name).resolve(strict=True)
        if not path.is_relative_to(output) or file_hash(path) != sha:
            raise ValueError('演练包文件发生变化，须重建受影响版本：' + name)
    return simulation


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
