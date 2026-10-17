"""Local delivery package. No account, upload, or paid generation."""
import argparse
import json
import math
import re
import shutil
import time
from pathlib import Path
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageStat
from media_adapter import MediaAdapter
from run import component, write_json
from selector_support import ROOT, read, file_hash, key, atomic
from cover_layout import render as render_template
from input_preflight import InputChecks, object_input
from runtime import INSTALL_ROOT, config as local_config, stage_config
from quality_support import (task_gate, no_resubmit, local_records, current_media_quality,
                             package_quality, cover_heading, subtitle_files_evidence,
                             delivery_files_evidence, batch_execution_gate, BATCH_STRATEGY_VERSION)

DEFAULT_COVER_MODE = 'source_frame'
PIPELINE_VERSION = '0.1.0'
PRODUCTION = ['run.py','media_adapter.py','ffmpeg_subset.py','suggest.py','selector_support.py',
              'transcribe_adapter.py','combine.py','subtitle.py','edit_timeline.py',
              'review_cut.py','batch_plan.py','quality_support.py',
              'subtitle_style.json','subtitle_dictionary.json','input_preflight.py',
              'publish_package.py','cover_layout.py','cover_config.json']


def production_hashes():
    return {str((INSTALL_ROOT/'pipeline'/name).resolve()):file_hash(INSTALL_ROOT/'pipeline'/name) for name in PRODUCTION}


def git_revision():
    import subprocess
    if not (INSTALL_ROOT/'.git').exists():
        return None
    try:
        result = subprocess.run(['git','rev-parse','HEAD'], cwd=INSTALL_ROOT,
                                capture_output=True, text=True, timeout=3)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def release_identity():
    version = (INSTALL_ROOT / 'VERSION').read_text(encoding='utf-8-sig').strip()
    if not version:
        raise ValueError('安装目录VERSION缺少实际发行版本')
    return {'release_version': version, 'git_revision': git_revision()}


def inside(path, root):
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise ValueError('路径不在允许的本地目录')
    return path


def copy_new(source, destination):
    with Path(source).open('rb') as src, Path(destination).open('xb') as dst:
        shutil.copyfileobj(src,dst,1024*1024)


def text_new(path, text):
    with Path(path).open('x',encoding='utf-8') as stream:
        stream.write(text+'\n')


def delivery_manifest(package, info):
    names = ('video.mp4', info['cover_source']['path'], info['cover']['path'],
             'title.txt', 'caption.txt')
    files = {name:file_hash(inside(package/name,package)) for name in names}
    files.update(subtitle_files_evidence(package, info))
    return files


def frame_times(cues,duration):
    starts = sorted({round(c['start'],2) for c in cues if 1 <= c['start'] < duration-1})
    if any(not math.isfinite(s) for s in starts):
        raise ValueError('字幕时间无效')
    # Exact-second fade starts avoid captions on common frame rates; no inpainting.
    whole = [s for s in starts if abs(s-round(s)) < .001]
    times = whole[:7] if len(whole) >= 5 else starts[:7]
    if len(times) < 5:
        times = [round(duration*f,3) for f in (.08,.18,.28,.4,.55,.7,.85)]
    return times


def preview(package, info):
    covers = [f"![竖封面]({info['main_cover_3x4']})"] if info.get('main_cover_3x4') else []
    if info.get('main_cover_4x3'):
        covers.append(f"![横封面]({info['main_cover_4x3']})")
    cover = '\n\n'.join(covers) if covers else '待完成'
    qa = info.get('quality_evidence') or {}
    reviewed = all((qa.get(k) or {}).get('status') == 'passed' for k in ('continuous_visual','audio_review'))
    status = '素材齐备，内容待审' if info.get('status') == 'awaiting_content_review' or not reviewed else '素材齐备，技术与内容证据见清单'
    subtitles = ' · '.join(f'[{kind.upper()} 字幕]({entry["path"]})'
                           for kind,entry in (info.get('subtitles') or {}).items()) or '待准备'
    return (f"# {info['product']['name']}｜本地交付包\n\n视频：[video.mp4](video.mp4)\n\n"
            f"字幕：{subtitles}\n\n封面：{cover}\n\n标题：{info.get('title') or '待生成'}\n\n"
            f"文案：{info.get('caption') or '待生成'}\n\n话题：{' '.join(info.get('hashtags',[]))}\n\n"
            f"状态：{status}\n\n仅本地交付；未登录、上传或发布。\n")


def prepare(source_dir, task_id, config, task_record=None):
    started = time.perf_counter()
    settings = stage_config(config)
    source_dir = inside(source_dir,ROOT/'outputs')
    source_result = read(source_dir/'result.json')
    if source_result.get('version') != '0.2c' or source_result.get('status') != 'completed':
        raise ValueError('只接受成功的V0.2c最终成片')
    video = inside(source_result['video'],source_dir)
    selected = read(source_dir/'logs/selected.json')
    context = task_gate(task_record, selected['original'], source_result, execution_stage='render')
    receipt = (batch_execution_gate(context, 'render')
               if context.get('content_strategy', {}).get('version') == BATCH_STRATEGY_VERSION else {})
    composition_record = read(inside(selected['composition'], ROOT/'outputs') / 'result.json')
    if (receipt.get('render_timeline') is not None
            or 'render_timeline' in selected or 'render_timeline' in source_result
            or 'render_timeline' in composition_record
            or 'edit_plan' in composition_record.get('best_combination', {})):
        from subtitle import extract
        current = extract(selected['composition'])
        if ((receipt.get('render_timeline') is not None
                and current.get('render_timeline') != receipt['render_timeline'])
                or current != selected or source_result.get('render_timeline') != current.get('render_timeline')
                or source_result.get('edit_plan') != current.get('edit_plan')
                or source_result.get('source_hashes_after') !=
                   {k: current[k] for k in ('video_sha256', 'original_sha256')}):
            raise ValueError('字幕成片与当前编辑时间轴或音画来源不一致，须重新审核受影响版本')
    no_resubmit(context, local_records(), file_hash(video))
    qa = current_media_quality(source_dir/'result.json', video)
    if not (source_dir/'subtitle.srt').is_file():
        raise ValueError('缺少实际SRT字幕文件 subtitle.srt；不能准备交付包')
    subtitle_sources = {'srt':inside(source_dir/'subtitle.srt',source_dir)}
    if (source_dir/'subtitle.ass').exists():
        subtitle_sources['ass'] = inside(source_dir/'subtitle.ass',source_dir)
    outputs = (ROOT/'outputs').resolve()
    if outputs.parent != ROOT.resolve():
        raise ValueError('outputs不得重定向到项目外')
    identity = release_identity()
    task = outputs/component(task_id,'task_id')
    task.mkdir(exist_ok=False)
    package = task/'publish-package'
    package.mkdir()
    (package/'logs').mkdir()
    (package/'frames').mkdir()
    info = {'schema_version':1,'pipeline_version':PIPELINE_VERSION,'status':'preparing',
            'product':{'date':None,'name':'待确认'},'video':{'path':'video.mp4'},'subtitles':{},'cover':None,
            'title':None,'caption':None,'hashtags':[],
            'publish':{'account':None,'product_link':None,'scheduled_time':None}}
    info.update(task_record=context['task_record'], task_context=context, quality_evidence=qa,
                content_decision_ref=str(source_dir/'logs/review.json')+'#content_decision')
    info['production'] = {'segment_count':(len(selected['clips']) if 'clips' in selected
                                         else len(set(s['clip_id'] for s in selected['segments']))),
                          'speed':selected['speed'],'quality_version':source_result.get('quality_version'),
                          'code_version':identity['release_version'], **identity,
                          'run_metrics_ref':context.get('run_metrics_ref')}
    if 'render_timeline' in selected:
        timeline = selected['render_timeline']
        speeds = [segment['speed'] for segment in timeline['segments']]
        info['production'].update(base_speed=selected['speed'], speed_mode='per_segment',
            speed=speeds[0] if len(set(speeds)) == 1 else None,
            segment_speeds=[{'clip_id': clip['clip_id'], 'speed': clip['speed'],
                            'frame_range': clip['frame_range']} for clip in timeline['mapped_clips']],
            end_hold_seconds=timeline['end_hold']['seconds'], edit_plan_hash=timeline['edit_plan_hash'],
            render_timeline_hash=key(timeline))
    stage = 'video_failed'
    try:
        sha = file_hash(video)
        copy_new(video,package/'video.mp4')
        if file_hash(package/'video.mp4') != sha:
            raise ValueError('视频复制校验失败')
        info['video'].update(sha256=sha,duration=source_result['metadata']['duration'])
        stage = 'subtitle_failed'
        for kind,source in subtitle_sources.items():
            name = 'subtitle.' + kind
            subtitle_sha = file_hash(source)
            copy_new(source,package/name)
            if file_hash(package/name) != subtitle_sha:
                raise ValueError('字幕复制校验失败：' + name)
            info['subtitles'][kind] = {'path':name,'sha256':subtitle_sha}
        stage = 'metadata_failed'
        selected = read(source_dir/'logs/selected.json')
        composition = inside(selected['composition'],ROOT/'outputs')
        product = read(composition/'result.json')['material']
        info['product'] = {'date':product['date'],'name':product['product_name']}
        cues = read(source_dir/'logs/cues.json')
        facts = {'product':info['product'],'video_sha256':sha,
                 'subtitles':[{'id':f'cue-{i:03d}','text':''.join(c['lines'])} for i,c in enumerate(cues,1)]}
        facts['positioning'] = context.get('positioning', {'kind':'unknown'})
        facts['hash'] = key(facts)
        write_json(package/'logs/facts.json',facts)
        sources = [video,*subtitle_sources.values(),Path(selected['video']),Path(selected['original'])]
        before = {str(p.resolve()):file_hash(p) for p in sources}
        write_json(package/'logs/source-hashes.json',before)
        write_json(package/'logs/production-code-hashes.json',production_hashes())
        media = MediaAdapter(settings,package/'logs/media.log')
        stage = 'video_failed'
        info['video']['frame_timing'] = media.check_frame_timing(package/'video.mp4')
        stage = 'cover_failed'
        meta = media.probe(package/'video.mp4')
        if [meta['width'],meta['height']] != [1080,1920]:
            raise ValueError('本版封面需要1080×1920竖屏成片')
        frames = []
        for i,t in enumerate(frame_times(cues,meta['duration']),1):
            path = package/'frames'/f'frame-{i:02d}.jpg'
            media.write_media(['-ss',str(t),'-i',str(package/'video.mp4'),'-frames:v','1','-q:v','2','-update','1',str(path)],path)
            with Image.open(path) as im:
                gray = im.resize((270,480)).convert('L')
                stat = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES).crop((1,1,269,479)))
                frames.append({'id':i,'time':t,'path':str(path.resolve()),'sharpness':round(stat.var[0],3),'sha256':file_hash(path)})
        write_json(package/'logs/frames.json',frames)
        sheet = Image.new('RGB',(280*4,535*2),'#f0ece6')
        draw = ImageDraw.Draw(sheet)
        for i,frame in enumerate(frames):
            with Image.open(frame['path']) as im:
                sheet.paste(im.resize((270,480)),((i%4)*280,(i//4)*535+28))
            draw.text(((i%4)*280+8,(i//4)*535+8),f"{frame['id']} | {frame['time']}s",fill='black')
        with (package/'frames/contact-sheet.jpg').open('xb') as f:
            sheet.save(f,format='JPEG',quality=90)
        info['status'] = 'awaiting_package_content'
    except Exception as error:
        info.update(status=stage,error=str(error))
        raise
    finally:
        info['prepare_seconds'] = time.perf_counter()-started
        write_json(package/'publish.json',info)
        text_new(package/'publish-preview.md',preview(package,info))
    return package


def validate_draft(draft,facts):
    checks = InputChecks()
    expected = {'facts_hash','title_candidates','selected_title','selection_reason','caption','hashtags',
                'cover_reason','evidence','template_cover'}
    optional = {'cover_frame','frame_review'}
    if (not isinstance(draft,dict) or not expected.issubset(draft)
            or set(draft)-expected-optional or draft['facts_hash'] != facts['hash']):
        raise ValueError('文案必须匹配本次素材，禁止附带账号或发布字段')
    titles = draft['title_candidates']
    valid_titles = isinstance(titles,list) and all(isinstance(t,str) for t in titles)
    checks.expect('titles', valid_titles and len(titles)==3 and len(set(titles))==3, '需三个不同标题')
    checks.expect('title_length', valid_titles and all(10<=len(t)<=16 and '#' not in t for t in titles),
                  '标题须10至16字，话题分开')
    checks.expect('selected_title', type(draft['selected_title']) is int and draft['selected_title'] in range(3),
                  '主标题索引无效')
    checks.expect('caption', isinstance(draft['caption'],str) and 20<=len(draft['caption'])<=40, '正文须20至40字')
    tags = draft['hashtags']
    valid_tags = isinstance(tags,list) and all(isinstance(t,str) for t in tags)
    checks.expect('hashtags', valid_tags and 3<=len(tags)<=5 and len(set(tags))==len(tags)
                  and all(re.fullmatch(r'#[\w\u4e00-\u9fff]{2,16}',t) for t in tags),
                  '需要3至5个不同的相关话题，每项须以#开头并含2至16字')
    if valid_titles and isinstance(draft['caption'],str):
        content = '\n'.join(titles+[draft['caption']])
        checks.expect('claims', not re.search(r'\d|最低|最后一天|限时|库存|销量|优惠|减肥|瘦.{0,4}斤|元|折扣|百分之|https?://',content),
                      '首版文案不加入未经业务核验的交易或效果信息')
        checks.expect('control_characters', not any(ord(c)<32 for t in titles+[draft['caption']] for c in t),
                      '文案不接受控制字符')
    by_id = {s['id']:s['text'] for s in facts['subtitles']}
    evidence = draft['evidence']
    checks.expect('evidence', isinstance(evidence,list) and bool(evidence), '文案必须附字幕依据')
    if isinstance(evidence,list):
        for index,e in enumerate(evidence,1):
            checks.expect(f'evidence[{index}]', isinstance(e,dict) and isinstance(e.get('quote'),str)
                          and bool(e['quote']) and e['quote'] in by_id.get(e.get('cue_id'),''), '文案依据不在最终字幕中')
    checks.expect('reasons', bool(draft['selection_reason']) and bool(draft['cover_reason']), '需记录标题和封面选择原因')
    checks.require()


def template_config(draft, facts, config):
    """Only text/layout depend on copy. Base images and video stay outside this key."""
    import copy
    cover = draft.get('template_cover', {})
    lines = cover.get('lines', [])
    if not 1 <= len(lines) <= 3 or any(not isinstance(s,str) or not s.strip() for s in lines):
        raise ValueError('封面需要1至3行本次实际文字')
    if any(any(ord(c)<32 for c in line) or re.search(r'最低|最后一天|限时|库存|销量|优惠|减肥|瘦.{0,4}斤|折扣|https?://',line) for line in lines):
        raise ValueError('封面含控制字符或未经核验交易／效果承诺')
    quotes = {s['id']:s['text'] for s in facts['subtitles']}
    supported = False
    for e in cover.get('evidence', []):
        i = e.get('line_index')
        if type(i) is not int or not 0 <= i < len(lines) or not e.get('quote') or e['quote'] not in quotes.get(e.get('cue_id'),''):
            raise ValueError('封面卖点引用不在当前字幕事实中')
        if e['quote'] in lines[i]:
            supported = True
    if not supported:
        raise ValueError('至少一行含有本条讲清楚的具体原话短语')
    result = copy.deepcopy(config)
    result['lines'] = lines
    return result


def image_review(image, review):
    """Bind a human or AI visual inspection note to the exact image bytes."""
    if isinstance(review, (str, Path)):
        review = read(review)
    if not isinstance(review, dict) or review.get('sha256') != file_hash(image):
        raise ValueError('封面来源缺少与实际图片一致的查看哈希')
    if (review.get('conclusion') != 'matched' or not review.get('reviewer')
            or not isinstance(review.get('finding'), str) or not review['finding'].strip()):
        raise ValueError('封面来源需实际查看并记录同款结论、检查人和发现')
    try:
        datetime.fromisoformat(review['reviewed_at'].replace('Z','+00:00'))
    except (TypeError, KeyError, ValueError) as error:
        raise ValueError('封面查看时间须为带时区的ISO时间') from error
    if datetime.fromisoformat(review['reviewed_at'].replace('Z','+00:00')).tzinfo is None:
        raise ValueError('封面查看时间须包含时区')
    return review


def finish_inputs(package,draft_path,font_path,bases,cover_mode,cover_config,task_record,
                  cover_ratio='3x4',user_image=None,image_review_path=None):
    """Collect input errors before any persistent cover/package writes."""
    if cover_mode not in ('source_frame','user_image'):
        raise ValueError('首版仅支持source_frame或user_image；AI生成封面暂不支持')
    if cover_ratio not in ('3x4','4x3'):
        raise ValueError('封面比例仅支持3x4或4x3')
    package = inside(package,ROOT/'outputs')
    info = object_input(read(package/'publish.json'))
    if info['status'] != 'awaiting_package_content':
        raise FileExistsError('素材包已完成或失败，不覆盖；失败时使用新任务编号')
    from quality_support import context_from
    # Identity and submission gates stay strict prerequisites, never guessed from a draft.
    context = context_from(task_record, info)
    context = task_gate(context['task_record'], context['source_video'], info, execution_stage='render')
    no_resubmit(context, local_records(), file_hash(package/'video.mp4'))
    checks = InputChecks()
    qa = checks.run('current_media_quality', lambda: package_quality(info, package/'video.mp4'))
    checks.run('subtitles', lambda: subtitle_files_evidence(package, info))
    if qa is not None:
        info['quality_evidence'] = qa
    checks.expect('publish_fields', info.get('publish') == {'account':None,'product_link':None,'scheduled_time':None},
                  'V0.3a发布字段必须留空')
    facts = checks.run('facts', lambda: object_input(read(package/'logs/facts.json')))
    draft = checks.run('draft', lambda: object_input(read(inside(draft_path,ROOT/'private-context'))))
    font = checks.run('font', lambda: ImageFont.truetype(str(font_path),42))
    if facts is not None:
        valid_facts = facts.get('hash') == key({k:v for k,v in facts.items() if k!='hash'})
        checks.expect('facts_hash', valid_facts, '事实快照改变')
        checks.expect('positioning', facts.get('positioning',{'kind':'unknown'}) == context.get('positioning',{'kind':'unknown'}),
                      '定位已变化，请核对事实与草稿；不自动复用旧封面文字')
        checks.expect('video_hash', file_hash(package/'video.mp4') == facts.get('video_sha256'), '素材包视频已改变')
        if valid_facts and draft is not None:
            checks.run('draft_fields', lambda: validate_draft(draft,facts))
    chosen = source = template = review = None
    frames = checks.run('frames', lambda: read(package/'logs/frames.json'))
    if cover_mode == 'source_frame' and frames is not None and draft is not None:
        frame_id = draft.get('cover_frame')
        chosen = next((f for f in frames if type(frame_id) is int and f['id']==frame_id),None)
        checks.expect('cover_frame', chosen is not None, '封面帧必须为抽帧候选的整数ID')
        if chosen is not None:
            source = checks.run('frame_path', lambda: inside(chosen['path'],package/'frames'))
            if source is not None:
                checks.expect('frame_hash', file_hash(source)==chosen.get('sha256'), '候选帧改变')
                review = checks.run('frame_review', lambda: image_review(source,draft.get('frame_review')))
                if review is not None:
                    checks.expect('frame_review_time', review.get('frame_time') == chosen['time'],
                                  '查看证据须绑定被选中的原视频时间')
    if cover_mode == 'user_image':
        source = checks.run('user_image', lambda: Path(user_image).resolve(strict=True))
        if source is not None:
            review = checks.run('image_review', lambda: image_review(source,image_review_path))
    if facts is not None and valid_facts and draft is not None:
        template = checks.run('template_config', lambda: template_config(
            draft,facts,read(cover_config or INSTALL_ROOT/'pipeline/cover_config.json')))
    if template is not None and font is not None and source is not None:
        checks.run('cover_layout', lambda: render_template(source,None,template,cover_ratio,font_path))
    before = checks.run('source_hashes', lambda: read(package/'logs/source-hashes.json'))
    if before is not None:
        code_paths = production_hashes()
        for source_path,sha in before.items():
            if str(Path(source_path).resolve()) not in code_paths:
                actual = checks.run('source_integrity', lambda source_path=source_path: file_hash(source_path))
                if actual is not None:
                    checks.expect('source_integrity', actual==sha, '原生产文件变化：'+source_path)
    return checks, dict(package=package,info=info,context=context,facts=facts,draft=draft,
                        chosen=chosen,source=source,template=template,review=review,
                        cover_mode=cover_mode,cover_ratio=cover_ratio)


def preflight(package,draft_path,font_path=None,bases=None,cover_mode=DEFAULT_COVER_MODE,
              cover_config=None,task_record=None,cover_ratio='3x4',user_image=None,image_review_path=None):
    checks = InputChecks()
    result = checks.run('package_gate', lambda: finish_inputs(
        package,draft_path,font_path or local_config().get('font_path'),bases,cover_mode,
        cover_config,task_record,cover_ratio,user_image,image_review_path))
    if result is not None:
        checks = result[0]
    return checks.report()


def finish(package,draft_path,font_path=None,bases=None,cover_mode=DEFAULT_COVER_MODE,
           cover_config=None,task_record=None,cover_ratio='3x4',user_image=None,image_review_path=None):
    started = time.perf_counter()
    font_path = font_path or local_config().get('font_path')
    checks, inputs = finish_inputs(package,draft_path,font_path,bases,cover_mode,cover_config,
                                  task_record,cover_ratio,user_image,image_review_path)
    checks.require()
    package,info,context,facts,draft,chosen,source,template,review = (
        inputs[k] for k in ('package','info','context','facts','draft','chosen','source','template','review'))
    code_before = production_hashes()
    info['production_code_check'] = {'before':code_before,'after':None,'unchanged':None}
    stage = 'metadata_failed'
    try:
        if file_hash(package/'video.mp4') != facts['video_sha256']:
            raise ValueError('素材包视频已改变')
        info.update(title=draft['title_candidates'][draft['selected_title']],
                    title_candidates=draft['title_candidates'],caption=draft['caption'],hashtags=draft['hashtags'])
        text_new(package/'title.txt',info['title'])
        text_new(package/'caption.txt',info['caption'])
        write_json(package/'logs/draft.json',draft)
        stage = 'cover_failed'
        if file_hash(source)!=review['sha256']:
            raise ValueError('封面来源图片改变')
        suffix = source.suffix.lower()
        if suffix not in ('.jpg','.jpeg','.png','.webp'):
            raise ValueError('封面来源仅接受JPG、PNG或WebP图片')
        source_name = 'cover-source'+suffix
        copy_new(source,package/source_name)
        ratio = inputs['cover_ratio']
        boxes = render_template(package/source_name,package/f'cover-{ratio}.jpg',template,ratio,font_path)
        layout = {'config':template,'layout':boxes,'facts_hash':facts['hash'],
                  'source_sha256':file_hash(package/source_name),
                  'text_layout_hash':key([template,file_hash(font_path)]),
                  'cover_hashes':{ratio:file_hash(package/f'cover-{ratio}.jpg')},
                  'positioning':facts.get('positioning',{'kind':'unknown'}),
                  'evidence':draft['template_cover']['evidence']}
        write_json(package/'logs/cover-layout.json',layout)
        info.update(cover_method=inputs['cover_mode'],
                    cover_source={'path':source_name,'sha256':review['sha256'],
                                  'review':review,'original_path':str(source)},
                    cover_review_note=review['finding'])
        info['main_cover_'+ratio] = f'cover-{ratio}.jpg'
        stage = 'integrity_failed'
        before = read(package/'logs/source-hashes.json')
        code_after = production_hashes()
        info['production_code_check'].update(after=code_after,unchanged=code_before==code_after)
        # Legacy ledgers include these exact code paths; retain that evidence, but
        # compare media with its original hashes and code within this finish run.
        changed = [p for p,h in before.items() if str(Path(p).resolve()) not in code_before and file_hash(p)!=h]
        if changed:
            raise ValueError('原生产文件变化：'+', '.join(changed))
        if code_before != code_after:
            raise ValueError('本次执行期间生产代码变化，保留现场并停止')
        qa = info.get('quality_evidence') or {}
        reviewed = all((qa.get(k) or {}).get('status') == 'passed'
                       for k in ('continuous_visual','audio_review'))
        info.update(status='ready_for_local_delivery' if reviewed else 'awaiting_content_review',
                    cover={'path':f'cover-{ratio}.jpg','source_path':source_name,'text':template['lines']},
                    v02_unchanged=True,
                    task_record=context['task_record'],task_context=context,
                    readiness_note='仅本地素材齐备；技术、听审和连续观看按质量证据分别判断。')
        info['delivery_files'] = delivery_manifest(package,info)
    except Exception as error:
        info.update(status=stage,error=str(error))
        raise
    finally:
        info['finish_seconds'] = time.perf_counter()-started
        atomic(package/'publish.json',info)
        atomic(package/'publish-preview.md',preview(package,info),plain=True)
    return info


def retext(package, draft_path, font_path, task_id, cover_config=None, task_record=None):
    """Copy an immutable package to a new task, then revise only text and cover pixels."""
    from quality_support import context_from
    package = inside(package, ROOT/'outputs')
    info = read(package/'publish.json')
    context = context_from(task_record, info)
    context = task_gate(context['task_record'], context['source_video'], info, execution_stage='render')
    no_resubmit(context, [info, *local_records()], file_hash(package/'video.mp4'))
    if info.get('cover_method') not in ('source_frame','user_image') or info.get('status') not in (
            'ready_for_local_delivery','awaiting_content_review'):
        raise ValueError('只对当前未提交的本地包做文字修订')
    delivery_files_evidence(package, info)
    facts = read(package/'logs/facts.json')
    if facts['hash'] != key({k:v for k,v in facts.items() if k!='hash'}):
        raise ValueError('事实文件哈希改变')
    if facts.get('video_sha256') != file_hash(package/'video.mp4'):
        raise ValueError('包视频与原事实不符；禁止沿用旧封面证据')
    source_name = info.get('cover_source',{}).get('path')
    if not source_name or Path(source_name).name != source_name or file_hash(package/source_name) != info['cover_source']['sha256']:
        raise ValueError('封面来源哈希改变；不能仅改文字')
    facts['positioning'] = context.get('positioning',{'kind':'unknown'})
    facts['hash'] = key({k:v for k,v in facts.items() if k!='hash'})
    draft = read(inside(draft_path, ROOT/'private-context'))
    validate_draft(draft,facts)
    template = template_config(draft,facts,read(cover_config or INSTALL_ROOT/'pipeline/cover_config.json'))
    ratio = '3x4' if info.get('main_cover_3x4') else '4x3'
    render_template(package/source_name,None,template,ratio,font_path)
    dest = ROOT/'outputs'/component(task_id,'task_id')/'publish-package'
    if dest.parent.exists():
        raise FileExistsError('修订任务目录已存在')
    dest.mkdir(parents=True)
    for src in package.iterdir():
        if src.is_dir(): shutil.copytree(src,dest/src.name)
        elif src.name not in ('cover-3x4.jpg','cover-4x3.jpg','publish.json','publish-preview.md',
                             'title.txt','caption.txt'):
            copy_new(src,dest/src.name)
    atomic(dest/'publish.json',dict(info,status='cover_failed',reused_from=str(package),
                                    error='文字修订未完成；保留现场'))
    boxes = render_template(dest/source_name,dest/f'cover-{ratio}.jpg',template,ratio,font_path)
    info.update(title=draft['title_candidates'][draft['selected_title']],
                title_candidates=draft['title_candidates'],caption=draft['caption'],
                hashtags=draft['hashtags'],task_record=context['task_record'],
                task_context=context,reused_from=str(package))
    info['cover']['text'] = template['lines']
    text_new(dest/'title.txt',info['title'])
    text_new(dest/'caption.txt',info['caption'])
    atomic(dest/'logs/facts.json',facts)
    atomic(dest/'logs/draft.json',draft)
    old = read(package/'logs/cover-layout.json')
    atomic(dest/'logs/cover-layout.json',dict(old,config=template,layout=boxes,facts_hash=facts['hash'],
           positioning=facts['positioning'],evidence=draft['template_cover']['evidence'],
           text_layout_hash=key([template,file_hash(font_path)]),
           cover_hashes={ratio:file_hash(dest/f'cover-{ratio}.jpg')}))
    info['delivery_files'] = delivery_manifest(dest,info)
    atomic(dest/'publish.json',info)
    atomic(dest/'publish-preview.md',preview(dest,info),plain=True)
    return dest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','preflight','finish','retext'])
    p.add_argument('--source',type=Path)
    p.add_argument('--task-id')
    p.add_argument('--package',type=Path)
    p.add_argument('--draft',type=Path)
    p.add_argument('--font',type=Path)
    p.add_argument('--cover-mode',choices=['source_frame','user_image'],default=DEFAULT_COVER_MODE)
    p.add_argument('--cover-ratio',choices=['3x4','4x3'],default='3x4')
    p.add_argument('--user-image',type=Path)
    p.add_argument('--image-review',type=Path)
    p.add_argument('--cover-config',type=Path)
    p.add_argument('--config',type=Path,default=ROOT/'config.json')
    p.add_argument('--task-record',type=Path)
    a=p.parse_args()
    if a.action=='prepare':
        if not a.source or not a.task_id: p.error('需要--source和--task-id')
        print(prepare(a.source,a.task_id,a.config,a.task_record))
    else:
        if not a.package or not a.draft: p.error('需要--package和--draft')
        if a.action == 'retext':
            if not a.task_id: p.error('retext需要新--task-id')
            print(retext(a.package,a.draft,a.font or local_config().get('font_path'),a.task_id,
                         a.cover_config,a.task_record))
        elif a.action == 'preflight':
            report = preflight(a.package,a.draft,a.font,cover_mode=a.cover_mode,
                               cover_config=a.cover_config,task_record=a.task_record,
                               cover_ratio=a.cover_ratio,user_image=a.user_image,
                               image_review_path=a.image_review)
            print(json.dumps(report,ensure_ascii=False,indent=2))
            raise SystemExit(0 if report['status']=='passed' else 1)
        else:
            print(json.dumps(finish(a.package,a.draft,a.font,cover_mode=a.cover_mode,
                                    cover_config=a.cover_config,task_record=a.task_record,
                                    cover_ratio=a.cover_ratio,user_image=a.user_image,
                                    image_review_path=a.image_review),ensure_ascii=False,indent=2))
