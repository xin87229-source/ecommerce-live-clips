"""Small shared gates for existing production records. No media/model/browser calls.

These checks validate evidence provenance and declared conclusions, not garment recognition.
"""
import copy
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path

from selector_support import ROOT, read, key, file_hash
from runtime import profile

VERSION = 'quality-v1'
CONTENT_STRATEGY_VERSION = 'grade-content-v1'
BATCH_STRATEGY_VERSION = 'single-style-batch-v1'
GOALS = {'purchase_understanding', 'audience_follow', 'undetermined'}
PROTECTED = {'SUBMITTING', 'SUBMIT_UNKNOWN', 'SUBMITTED_PENDING_REVIEW',
             'SUBMITTED_AWAITING_REVIEW', 'SCHEDULED_VERIFIED', 'PUBLISHED_VERIFIED'}
QA_KINDS = ('technical', 'continuous_visual', 'audio_review', 'user_confirmation')


def required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('缺少' + label)
    return value


def normalize_content_strategy(value=None):
    """New batches are ungraded; preserve legacy snapshots without rewriting them."""
    if value is None:
        return {'version': CONTENT_STRATEGY_VERSION, 'grade': 'unknown',
                'source_type': None, 'source_ref': None}
    if isinstance(value, dict) and value.get('version') == BATCH_STRATEGY_VERSION:
        if set(value) != {'version'}:
            raise ValueError('单款批次策略不接受ABC等级或等级来源字段')
        return {'version': BATCH_STRATEGY_VERSION}
    if not isinstance(value, dict) or value.get('version') != CONTENT_STRATEGY_VERSION:
        raise ValueError('内容策略必须是grade-content-v1对象')
    grade, source_type, source_ref = (value.get(k) for k in ('grade', 'source_type', 'source_ref'))
    if grade not in ('A', 'B', 'C', 'unknown'):
        raise ValueError('内容策略等级仅允许A/B/C/unknown')
    if source_type not in (None, 'schedule', 'user'):
        raise ValueError('等级来源仅允许schedule/user或未知')
    if grade != 'unknown' and source_type is None:
        raise ValueError('明确等级必须有schedule/user来源')
    if source_type is not None:
        source_ref = required(source_ref, '等级来源引用').strip()
    elif source_ref is not None:
        raise ValueError('等级来源引用须同时有schedule/user来源')
    return {'version': CONTENT_STRATEGY_VERSION, 'grade': grade,
            'source_type': source_type, 'source_ref': source_ref}


def batch_execution_gate(context, stage='render'):
    """Analysis may run before adoption; all new-batch media writes require it."""
    if normalize_content_strategy(context.get('content_strategy'))['version'] != BATCH_STRATEGY_VERSION:
        evidence_file(context.get('legacy_resume') or {})
        return None
    ref = context.get('batch_ref')
    if not isinstance(ref, dict) or not ref.get('plan_path') or not ref.get('item_id'):
        raise ValueError('单款批次制作／发布须绑定明确采用的批次方案与条目')
    from batch_plan import execution_gate
    receipt = execution_gate(context, stage=stage)
    rendered_fingerprint = context.get('batch_adoption_fingerprint')
    if rendered_fingerprint and rendered_fingerprint != receipt['adoption_fingerprint']:
        raise ValueError('成片采用版本已失效，请按当前采用方案重做受影响条目')
    return receipt


def timestamp(value):
    t = datetime.fromisoformat(required(value, '带时区时间').replace('Z', '+00:00'))
    if t.utcoffset() is None:
        raise ValueError('时间必须带时区')
    return t


def span(value):
    if (not isinstance(value, list) or len(value) != 2
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in value)
            or not 0 <= value[0] < value[1]):
        raise ValueError('证据区间无效')
    return value


def evidence_file(item):
    path = Path(required(item.get('path'), '证据文件')).resolve(strict=True)
    if file_hash(path) != item.get('sha256'):
        raise ValueError('证据文件哈希不匹配')
    return path


def record_identity(record):
    """Normalize existing record shapes; conflicting identities fail closed."""
    nested = record.get('task_context') or {}
    result = {}
    for field in ('task_id', 'account_key', 'video_sha256'):
        outer = record.get(field)
        inner = nested.get(field)
        if outer and inner and outer != inner:
            raise ValueError('提交记录顶层与任务身份冲突')
        result[field] = outer or inner or (record.get('job_id') if field == 'task_id' else None)
    return result


def revision_previous(context):
    """A verified refused attempt may have one new revision, never a blind retry."""
    revision = context.get('originality_revision')
    if revision is None:
        return None
    if not isinstance(revision, dict) or type(revision.get('attempt')) is not int or revision['attempt'] != 1:
        raise ValueError('原创性返工仅允许第一次修订')
    path = evidence_file(revision.get('previous_result') or {})
    if path.name != 'publish-result.json' or not path.is_relative_to((ROOT/'outputs').resolve()):
        raise ValueError('返工须引用本地保留的原发布结果')
    old = read(path)
    identity = record_identity(old)
    if (identity['task_id'] != context.get('task_id') or not identity['account_key']
            or identity['account_key'] != context.get('account_key')
            or not isinstance(identity['video_sha256'], str) or len(identity['video_sha256']) != 64
            or old.get('status') != 'REJECTED_ORIGINALITY'
            or type(old.get('submitted')) is not bool
            or old.get('published') is not False or old.get('pending_review') is not False
            or old.get('originality_revision') or (old.get('task_context') or {}).get('originality_revision')):
        raise ValueError('原结果不是同任务账号首次明确原创性拒绝，不能返工')
    for field in ('rejection_evidence', 'content_change_evidence', 'previous_plan'):
        evidence_file(revision.get(field) or {})
    timestamp(revision.get('verified_at'))
    return old


def no_resubmit(context, records, video_sha256=None):
    """Read-only local guard; not a cross-device lock or a browser publisher."""
    if context.get('cross_device_state') != 'local_records_checked':
        raise ValueError('发布历史／跨设备状态未核对')
    previous = revision_previous(context)
    previous_identity = record_identity(previous) if previous else None
    if previous and video_sha256 == previous_identity['video_sha256']:
        raise ValueError('原创性修订不能重发旧视频')
    for r in records:
        identity = record_identity(r)
        protected = r.get('status') in PROTECTED | {'REJECTED_ORIGINALITY'} or r.get('submitted') is True
        same_task = identity['task_id'] == context.get('task_id')
        same_video = (video_sha256 and identity['video_sha256'] == video_sha256
                      and (not identity['account_key'] or not context.get('account_key')
                           or identity['account_key'] == context['account_key']))
        if protected and (same_task or same_video):
            if (previous and identity == previous_identity and r.get('status') == 'REJECTED_ORIGINALITY'
                    and not r.get('originality_revision')
                    and not (r.get('task_context') or {}).get('originality_revision')
                    and r.get('published') is False and r.get('pending_review') is False):
                continue
            raise ValueError('已提交／提交未知内容受保护；改时间或编号不能绕过')


def local_records(root=None):
    # ponytail: bounded to project outputs, serial operator only; no shared device lock.
    for name in ('publish-result.json', 'run-state.json'):
        for p in ((root or ROOT) / 'outputs').glob('**/' + name):
            yield read(p)  # Corrupt records fail closed; do not silently ignore them.


def context_from(record_path, fallback=None):
    record_path = record_path or (fallback or {}).get('task_record')
    if not record_path:
        raise ValueError('旧记录可读，但新执行需在run-state.json补task_context并传--task-record')
    path = Path(record_path).resolve(strict=True)
    if not any(path.is_relative_to((ROOT / d).resolve()) for d in ('outputs', 'private-context')):
        raise ValueError('任务记录只能保存在本地outputs或private-context')
    record = read(path)
    context = copy.deepcopy(record.get('task_context', {}))
    context['task_record'] = str(path)
    no_resubmit(context, [record, *local_records()], context.get('video_sha256'))
    return context


def validate_task(context, source, duration=None):
    required(context.get('task_id'), 'task_id')
    if context.get('scope') == 'full_publish':
        raise ValueError('full_publish在本地交付版中禁用')
    if context.get('profile') != profile():
        raise ValueError('任务预设与当前配置不符；需重新审查并采用受影响内容')
    context['content_strategy'] = normalize_content_strategy(context.get('content_strategy'))
    if context['content_strategy']['version'] != BATCH_STRATEGY_VERSION:
        if not context.get('legacy_resume'):
            raise ValueError('新执行必须明确批次策略；旧任务须有用户明确恢复证据legacy_resume')
        evidence_file(context['legacy_resume'])
    if context.get('scope') not in {'production', 'test'}:
        raise ValueError('咨询／只读／开发范围不允许启动制作')
    if context.get('content_goal', 'undetermined') not in GOALS:
        raise ValueError('未知内容目标')
    if context.get('material_date') is not None:
        required(context['material_date'], '素材日期（与发布日期分开）')
    if context.get('cart_required') is not None and type(context.get('cart_required')) is not bool:
        raise ValueError('挂车要求必须布尔或未知')
    product = context.get('product', {})
    required(product.get('name'), '目标商品')
    required(product.get('code'), '稳定本地商品标识')
    for k in ('id', 'code'):
        if product.get(k) is not None and not isinstance(product[k], str):
            raise ValueError('商品标识必须字符串')
    evidence_file(product.get('reference', {}))
    source = Path(source).resolve(strict=True)
    if (Path(context.get('source_video', '')).resolve() != source
            or context.get('source_sha256') != file_hash(source)):
        raise ValueError('任务卡与当前源文件不对应')
    windows = context.get('confirmed_windows', [])
    review = context.get('source_review', {})
    if (not windows or review.get('conclusion') != 'matched'
            or review.get('source_sha256') != context['source_sha256']
            or review.get('identity_hash') != key(product)
            or review.get('windows_hash') != key(windows)):
        raise ValueError('同款窗口未确认／证据与源、区间或参考不对应')
    required(review.get('reviewer'), '同款审核来源')
    timestamp(review.get('reviewed_at'))
    for window in windows:
        a, b = span(window['range'])
        frames = window.get('visual', [])
        times = []
        for frame in frames:
            evidence_file(frame)
            t = frame.get('at')
            if type(t) not in (int, float) or not a <= t <= b:
                raise ValueError('代表帧不在当前区间')
            times.append(t)
        if len(set(times)) < 3 or min(times) > a + (b-a)*.2 or max(times) < b - (b-a)*.2:
            raise ValueError('单帧不能确认整段；需首中末代表画面')
        speech = window.get('speech', {})
        if speech.get('type') not in {'audio_listening', 'verified_transcript'}:
            raise ValueError('缺少短原声或已核验转写依据')
        evidence_file(speech)
        sa, sb = span(speech.get('range'))
        if not a <= sa < sb <= b:
            raise ValueError('原声核验不在同款窗口')
        required(speech.get('finding'), '原声核验结论')
    if duration is not None:
        # prepare transcribes the entire supplied file: require contiguous reviewed coverage.
        end = 0.0
        for a, b in sorted(span(w['range']) for w in windows):
            if a > end + .001 or b > duration + .001:
                raise ValueError('待长转写文件含未确认窗口；先制作准确窗口工作副本')
            end = max(end, b)
        if abs(end-duration) > .001:
            raise ValueError('同款核验未覆盖待转写文件；禁止先全场ASR')
    return context


def requires_adoption_binding(result):
    return (result.get('version') in ('0.2b', '0.2c')
            or result.get('pipeline_version') in ('0.3a', '0.1.0')
            or result.get('cover_method') in ('ai_template', 'source_frame', 'user_image'))


def task_gate(record_path, source, fallback=None, duration=None, execution_stage=None):
    context = validate_task(context_from(record_path, fallback), source, duration)
    if fallback is not None:
        old_strategy = normalize_content_strategy((fallback.get('task_context') or {}).get('content_strategy'))
        if context['content_strategy'] != old_strategy:
            raise ValueError('内容策略等级或来源已改变，请重新prepare；完整转写缓存仍可复用')
    if fallback and fallback.get('task_context'):
        old = fallback['task_context']
        # The current revision carries the exception; an old media snapshot never grants one.
        no_resubmit(dict(old, cross_device_state=context['cross_device_state'], account_key=context.get('account_key'),
                        originality_revision=context.get('originality_revision')),
                    local_records(), old.get('video_sha256'))
        if old.get('originality_revision') and old['originality_revision'] != context.get('originality_revision'):
            raise ValueError('已有修订身份不得移除或改写')
        for field in ('source_sha256', 'confirmed_windows'):
            if context.get(field) != old.get(field):
                raise ValueError('上游任务身份或区间改变，需重新核验受影响阶段')
        old_product, product = old.get('product', {}), context.get('product', {})
        if product != old_product:
            if (old_product.get('id') is not None or not product.get('id')
                    or not old_product.get('code')
                    or {k:v for k,v in old_product.items() if k != 'id'} !=
                       {k:v for k,v in product.items() if k != 'id'}):
                raise ValueError('上游任务身份或区间改变，需重新核验受影响阶段')
            evidence_file(context.get('product_id_evidence') or {})
        # A rendered batch item cannot be relabelled as another item or legacy work.
        if old.get('batch_ref') and old.get('batch_ref') != context.get('batch_ref'):
            raise ValueError('批次条目引用已变化，需重新核验受影响阶段')
        if old.get('batch_adoption_fingerprint'):
            if context.get('batch_adoption_fingerprint') not in (None, old['batch_adoption_fingerprint']):
                raise ValueError('成片采用版本不能由新任务卡改写')
            context['batch_adoption_fingerprint'] = old['batch_adoption_fingerprint']
        elif old.get('batch_ref') and requires_adoption_binding(fallback):
            raise ValueError('批次成片缺少采用版本绑定，不能沿用旧成片')
    if execution_stage is not None:
        batch_execution_gate(context, execution_stage)
    return context


def content_review_path(reference, base):
    reference = required(reference, '可追溯字幕结果引用')
    path, _, fragment = reference.rpartition('#')
    review = Path(path)
    if fragment != 'content_decision' or review.name != 'review.json' or review.parent.name != 'logs':
        raise ValueError('字幕结果引用须为原字幕logs/review.json#content_decision')
    review = (Path(base)/review).resolve(strict=True)
    if not review.is_file():
        raise ValueError('字幕审核引用须指向真实review.json文件')
    return review


def current_media_quality(result_path, video, source_hashes=None):
    """Read current subtitle QA for these exact video bytes, without changing records."""
    result_path = Path(result_path).resolve(strict=True)
    if result_path.name != 'result.json' or not result_path.is_relative_to((ROOT/'outputs').resolve()):
        raise ValueError('字幕结果引用须指向本地outputs中的result.json')
    result = read(result_path)
    if result.get('version') != '0.2c' or result.get('status') != 'completed':
        raise ValueError('只接受成功的V0.2c最终成片')
    review = content_review_path(result.get('content_decision_ref'), result_path.parent)
    if review != (result_path.parent/'logs/review.json').resolve(strict=True):
        raise ValueError('字幕结果的审核引用与当前review不一致')
    source_video = (result_path.parent/Path(result['video'])).resolve(strict=True)
    sha = file_hash(video)
    if not source_video.is_relative_to(result_path.parent) or file_hash(source_video) != sha:
        raise ValueError('字幕结果与当前包视频不一致，不能复用其他版本QA')
    if source_hashes is not None:
        if (not isinstance(source_hashes, dict)
                or {str(Path(p).resolve()):h for p,h in source_hashes.items()}.get(str(source_video)) != sha):
            raise ValueError('字幕结果视频不在原包媒体清单或哈希不一致，不能改引旧QA副本')
    qa = quality_evidence(result.get('quality_evidence'), sha,
                          result['metadata']['duration'], result.get('audio_dependencies'))
    if qa['technical']['status'] != 'passed':
        raise ValueError('须补当前版本技术通过证据；不重做合格上游')
    if qa['continuous_visual']['status'] == 'failed':
        raise ValueError('已确认的连续画面问题须先处理；不要求用户中途观看确认')
    return qa


def package_quality(info, video):
    package = Path(video).resolve(strict=True).parent
    review = content_review_path(info.get('content_decision_ref'), package)
    sources = read(package/'logs/source-hashes.json')
    if not isinstance(sources, dict) or not sources:
        raise ValueError('缺少有效原包媒体清单，不能只凭同字节视频补写QA来源')
    return current_media_quality(review.parent.parent/'result.json', video, sources)


def cover_source_evidence(package, info):
    source = info.get('cover_source') or {}
    name = source.get('path')
    if not isinstance(name, str) or not name:
        raise ValueError('交付封面缺少实际来源文件')
    package = Path(package).resolve(strict=True)
    path = (package / name).resolve(strict=True)
    if not path.is_relative_to(package) or not path.is_file():
        raise ValueError('封面来源必须在当前交付包内')
    if file_hash(path) != source.get('sha256'):
        raise ValueError('封面来源文件哈希不匹配')
    return path


def subtitle_files_evidence(package, info):
    """Verify the exact sidecar subtitles shipped with a local release package."""
    package = Path(package).resolve(strict=True)
    subtitles = info.get('subtitles')
    current = info.get('pipeline_version') == '0.1.0' or subtitles is not None
    if not current:
        return {}
    if not isinstance(subtitles, dict) or not isinstance(subtitles.get('srt'), dict) \
            or set(subtitles) - {'srt', 'ass'}:
        raise ValueError('新版交付包须有字幕SRT及可选ASS来源记录')
    files = {}
    for kind, name in (('srt', 'subtitle.srt'), ('ass', 'subtitle.ass')):
        ref = subtitles.get(kind)
        path = (package / name).resolve()
        if ref is None:
            if path.exists():
                raise ValueError('包内存在未记录的字幕文件')
            continue
        if (not isinstance(ref, dict) or ref.get('path') != name
                or not path.is_relative_to(package) or not path.is_file()):
            raise ValueError('字幕路径须为包内固定文件且真实存在')
        sha = file_hash(path)
        if sha != ref.get('sha256'):
            raise ValueError('字幕文件哈希不匹配')
        files[name] = sha
    return files


def delivery_files_evidence(package, info):
    """Bind the final cover and copy to the current package and layout draft."""
    package = Path(package).resolve(strict=True)
    layout = read(package / 'logs/cover-layout.json')
    draft = read(package / 'logs/draft.json')
    hashes = layout.get('cover_hashes') or {}
    files = {}
    for ratio in ('3x4', '4x3'):
        name = info.get('main_cover_' + ratio)
        if not name:
            continue
        if name != f'cover-{ratio}.jpg' or (info.get('cover') or {}).get('path') != name:
            raise ValueError('最终封面路径与交付包不一致')
        path = (package / name).resolve(strict=True)
        if not path.is_relative_to(package) or not path.is_file() or file_hash(path) != hashes.get(ratio):
            raise ValueError('最终封面哈希不匹配')
        files[name] = file_hash(path)
    if not files:
        raise ValueError('缺少最终封面及布局哈希')
    if (info.get('cover') or {}).get('text') != layout.get('config', {}).get('lines'):
        raise ValueError('最终封面文案与布局记录不一致')
    if layout.get('config', {}).get('lines') != draft.get('template_cover', {}).get('lines'):
        raise ValueError('最终封面文案与当前草稿不一致')
    selected = draft.get('selected_title')
    titles = draft.get('title_candidates') or []
    if type(selected) is not int or not 0 <= selected < len(titles):
        raise ValueError('标题草稿缺少有效选定项')
    for name, field, expected in (('title.txt', 'title', titles[selected]),
                                  ('caption.txt', 'caption', draft.get('caption'))):
        path = (package / name).resolve(strict=True)
        if not path.is_relative_to(package) or not path.is_file():
            raise ValueError('交付' + field + '文件无效')
        if not isinstance(expected, str) or info.get(field) != expected or path.read_text(encoding='utf-8') != expected + '\n':
            raise ValueError('交付' + ('标题' if field == 'title' else '正文') + '与当前草稿或文件不一致')
        files[name] = file_hash(path)
    files.update(subtitle_files_evidence(package, info))
    manifest = info.get('delivery_files')
    if (info.get('pipeline_version') == '0.1.0' or info.get('subtitles') is not None) and manifest is None:
        raise ValueError('新版交付包缺少完整交付文件哈希清单')
    if manifest is not None:
        video = (package / 'video.mp4').resolve(strict=True)
        source_name = (info.get('cover_source') or {}).get('path')
        source = (package / source_name).resolve(strict=True) if isinstance(source_name, str) and source_name else None
        if not video.is_relative_to(package) or source is None or not source.is_relative_to(package):
            raise ValueError('交付成品文件哈希清单路径无效')
        files.update({'video.mp4': file_hash(video), source_name: file_hash(source)})
        if not isinstance(manifest, dict) or any(manifest.get(name) != sha for name, sha in files.items()):
            raise ValueError('交付成品文件哈希清单不匹配')
    return files


def check_package(package, record_path):
    """Read-only handoff gate; a pass does not authorize or execute publication."""
    package = Path(package).resolve(strict=True)
    if not package.is_relative_to((ROOT / 'outputs').resolve()):
        raise ValueError('只检查本地outputs中的待发布素材包')
    info = read(package / 'publish.json')
    if info.get('status') not in {'ready_for_local_delivery', 'awaiting_content_review'}:
        raise ValueError('只检查本地交付包或明确待内容审核的草稿')
    if not info.get('task_context'):
        raise ValueError('旧包缺任务身份快照，须先补可追溯证据，不能默认匹配')
    current = context_from(record_path, info)
    context = task_gate(current['task_record'], current['source_video'], info, execution_stage='render')
    video = (package / 'video.mp4').resolve(strict=True)
    if not video.is_relative_to(package):
        raise ValueError('包视频不得指向素材包外')
    sha = file_hash(video)
    if sha != info.get('video', {}).get('sha256'):
        raise ValueError('素材包视频哈希已改变，不复用旧通过结论')
    cover_source_evidence(package, info)
    delivery_files_evidence(package, info)
    package_record = dict(info, task_id=info['task_context'].get('task_id'), video_sha256=sha)
    no_resubmit(context, [package_record, *local_records()], sha)
    qa = package_quality(info, video)
    return {'task_id': context['task_id'], 'package': str(package),
            'content_strategy': context['content_strategy'], 'video_sha256': sha,
            'quality_evidence': qa,
            'delivery_status': info['status'], 'evidence_gate': 'passed',
            'note': '仅检查本地交付包与当前任务证据一致；待审核状态不等于内容验收通过'}


def selection_binding(selected):
    binding = {'source_sha256': selected['original_sha256'], 'speed': selected['speed'],
               'segments': [{k: s[k] for k in ('id', 'clip_id', 'start', 'end', 'text', 'final_start', 'final_end')}
                            for s in selected['segments']]}
    if 'clips' in selected:
        binding['clips'] = [{k: c[k] for k in ('clip_id', 'kind', 'start', 'end',
                                              'final_start', 'final_end', 'segment_ids')}
                            for c in selected['clips']]
    if 'render_timeline' in selected or 'edit_plan' in selected:
        timeline = selected.get('render_timeline')
        if (not isinstance(timeline, dict) or not isinstance(selected.get('edit_plan'), dict)
                or timeline.get('edit_plan_hash') != key(selected['edit_plan'])):
            raise ValueError('编辑选段须保留匹配的编辑计划和时间轴')
        binding['edit_plan_hash'] = timeline['edit_plan_hash']
        binding['render_timeline_hash'] = key(timeline)
    return binding


def content_decision(decision, binding, candidates_hash):
    if not decision or decision.get('selection_hash') != key(binding) or decision.get('candidates_hash') != candidates_hash:
        raise ValueError('缺少当前选段绑定的内容决策；先prepare并补审核，不重转写')
    if decision.get('content_goal') not in GOALS:
        raise ValueError('内容目标缺失')
    required(decision.get('viewer_question'), '帮助观众判断什么')
    opening = decision.get('opening', {})
    first_clip = (binding.get('clips') or [None])[0]
    if first_clip and first_clip['kind'] == 'visual':
        if (opening.get('kind') != 'visual' or opening.get('clip_id') != first_clip['clip_id']
                or opening.get('range') != [first_clip['start'], first_clip['end']]
                or opening.get('quote') is not None or opening.get('segment_id') is not None):
            raise ValueError('开头须绑定实际展示段，不能把后续口播冒称真实开头')
    else:
        first = binding['segments'][0]
        if opening.get('segment_id') != first['id'] or opening.get('quote') != first['text'] or opening.get('range') != [first['start'], first['end']]:
            raise ValueError('开头不是当前真实原句与区间')
    required(opening.get('visual'), '开头画面说明（未知应明确写未审）')
    order = ([c['clip_id'] for c in binding['clips']] if 'clips' in binding
             else list(dict.fromkeys(s['clip_id'] for s in binding['segments'])))
    roles = decision.get('roles', [])
    if [r.get('clip_id') for r in roles] != order or any(not r.get('role') for r in roles):
        raise ValueError('缺少当前各段作用与顺序')
    for field in ('repetition', 'context_dependency', 'ending', 'conclusion'):
        required(decision.get(field), field)
    for issue in decision.get('issues', []):
        if issue.get('severity') not in ('hard', 'soft'):
            raise ValueError('内容问题需区分hard/soft')
        required(issue.get('reason'), '问题依据')
        if issue['severity'] == 'hard' and (issue.get('resolved') is not True or not issue.get('evidence')):
            raise ValueError('硬内容问题未解决，禁止字幕烧录')
    if decision['conclusion'] not in ('pass', 'pass_with_soft_notes'):
        raise ValueError('内容结论不允许继续')
    return decision


def subtitle_risks(review, selected):
    """New render requires typed risks; reading old files is unaffected."""
    by_id = {s['id']:s for s in selected['segments']}
    for cue in review['cues']:
        for risk in cue.get('risks', []):
            if not isinstance(risk, dict) or risk.get('level') not in ('ordinary', 'correctable', 'blocking'):
                raise ValueError('旧风险可读，新执行须补ordinary/correctable/blocking分级')
            required(risk.get('reason'), '字幕风险原因')
            if risk.get('fact_field') or risk['level'] == 'blocking':
                if risk.get('resolved') is not True or not risk.get('evidence'):
                    raise ValueError('事实或严重表达问题尚无可靠依据')
                evidence_file(risk['evidence'])
            elif risk['level'] == 'correctable' and not risk.get('resolved'):
                raise ValueError('有证据识别错误须完成局部修正')
        for c in cue.get('corrections', []):
            e = c.get('evidence', {})
            if e.get('type') not in ('audio_listening', 'verified_transcript'):
                raise ValueError('商品页不能证明原声；纠错需要原声／核验转写依据')
            evidence_file(e)
            if e.get('source_sha256') != selected['original_sha256'] or e.get('segment_ids') != cue['segment_ids']:
                raise ValueError('纠错证据来源或区间不对应')
            ids = cue['segment_ids']
            if (not ids or any(sid not in by_id for sid in ids)
                    or e.get('range') != [by_id[ids[0]]['start'], by_id[ids[-1]]['end']]):
                raise ValueError('纠错证据须绑定当前完整cue的精确源区间；旧证据不得倒填')
            span(e['range'])


def audio_dependencies(selected, audio_hash):
    return dict(selection_binding(selected), transcript_sha256=selected['transcript_sha256'],
                audio_sha256=audio_hash)


def quality_evidence(records, video_sha256, duration, audio_deps=None):
    result = {}
    allowed = {'technical': {'media_check'}, 'continuous_visual': {'continuous_playback'},
               'audio_review': {'audio_listening'}, 'user_confirmation': {'user_message'}}
    for kind in QA_KINDS:
        r = copy.deepcopy((records or {}).get(kind))
        if not r:
            result[kind] = {'status': 'not_reviewed', 'video_sha256': video_sha256,
                            'coverage': None, 'evidence': None, 'actor': None, 'at': None,
                            'limitations': '未记录，不默认通过'}
            continue
        if r.get('status') not in ('passed', 'failed', 'not_reviewed', 'unknown'):
            raise ValueError('验收状态无效')
        same = r.get('video_sha256') == video_sha256
        reusable_audio = (kind == 'audio_review' and audio_deps is not None
                          and r.get('audio_dependencies') == audio_deps
                          and r.get('reused_from') and r.get('reuse_reason'))
        if not same and not reusable_audio:
            result[kind] = {'status': 'not_reviewed', 'video_sha256': video_sha256,
                            'coverage': None, 'evidence': None, 'actor': None, 'at': None,
                            'limitations': '版本变化，旧证据不覆盖新版', 'previous': r}
            continue
        if r['status'] in ('passed', 'failed'):
            required(r.get('actor'), '验收执行者')
            timestamp(r.get('at'))
            required(r.get('limitations'), '验收限制（无额外限制也须明确记录）')
            e = r.get('evidence', {})
            if e.get('type') not in allowed[kind]:
                raise ValueError('证据类型不能冒充' + kind)
            evidence_file(e)
            a, b = span(r.get('coverage'))
            if b > duration + .001:
                raise ValueError('验收覆盖超出成片')
            if kind in ('technical', 'continuous_visual', 'audio_review') and r['status'] == 'passed' and (a != 0 or abs(b-duration) > .001):
                raise ValueError('局部证据不能冒充完整技术／连续画面／音频验收')
        result[kind] = r
    return result


def cover_heading(positioning=None):
    p = positioning or {'kind': 'unknown'}
    if p.get('kind') not in ('new', 'returning', 'unknown'):
        raise ValueError('业务定位无效')
    if p['kind'] != 'unknown':
        if p.get('source_type') not in ('schedule', 'user', 'business_confirmation'):
            raise ValueError('不能用素材或发布日期推断上新／返场')
        required(p.get('source_ref'), '业务定位依据')
    return {'new': '今日上新', 'returning': '经典返场', 'unknown': '款式实看'}[p['kind']]


if __name__ == '__main__':
    import argparse
    import json
    from selector_support import atomic
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['check-task', 'check-package', 'audit'])
    p.add_argument('--task-record',type=Path,required=True)
    p.add_argument('--package',type=Path)
    p.add_argument('--result',type=Path)
    p.add_argument('--evidence',type=Path)
    a = p.parse_args()
    if a.action == 'check-package':
        if not a.package: p.error('check-package需要--package')
        print(json.dumps(check_package(a.package, a.task_record),ensure_ascii=False))
        raise SystemExit(0)
    c = context_from(a.task_record)
    validate_task(c,c['source_video'])
    if a.action == 'check-task':
        print(json.dumps({'task_id':c['task_id'],'scope':c['scope'],'evidence_gate':'passed',
                          'note':'仅检查证据与停止逻辑，不代表程序识别了服装'},ensure_ascii=False))
    else:
        if not a.result or not a.evidence: p.error('audit需要--result和--evidence')
        result_path = a.result.resolve(strict=True)
        if not result_path.is_relative_to((ROOT/'outputs').resolve()):
            raise ValueError('仅更新本地未提交产物的结果记录')
        r = read(result_path)
        task_gate(c['task_record'], c['source_video'], r)
        selected = read(result_path.parent/'logs/selected.json')
        if selected.get('original_sha256') != c['source_sha256']:
            raise ValueError('QA任务卡不是该产物的源，不能交叉补验')
        video = Path(r['video'])
        sha = file_hash(video)
        no_resubmit(c,[r,*local_records()],sha)
        previous = r.get('quality_evidence')
        supplied = read(a.evidence)
        updated = quality_evidence({**(previous or {}),**supplied},sha,r['metadata']['duration'],r.get('audio_dependencies'))
        r.setdefault('quality_evidence_history',[]).append(previous)
        r['quality_evidence'] = updated
        atomic(result_path,r)
        print('仅更新该版本QA证据；未运行媒体、模型或发布')
