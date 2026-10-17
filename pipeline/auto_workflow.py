"""Execute the local production loop; Codex supplies semantic decisions, never approvals."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import uuid

from runtime import ROOT
import source_windows
import review_cut
import review_card


def read(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def local(value, exists=True):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('统一入口路径必须为绝对路径')
    path = path.resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError('统一入口状态和任务必须位于当前工作区')
    if exists and not path.exists():
        raise FileNotFoundError(path)
    return path


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', value):
        raise ValueError('run_id须为1至48个字母、数字、短横线或下划线')
    return value


def save(path, value):
    path = local(path, False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    temporary.replace(path)


def refs(paths):
    result = {}
    for value in paths:
        path = local(value)
        files = sorted(path.rglob('*')) if path.is_dir() else [path]
        for item in files:
            item = local(item)
            if item.is_file():
                result[str(item)] = digest(item)
    if not result:
        raise ValueError('阶段没有真实产物，不能记录成功')
    return result


def verify(bindings):
    for value, expected in bindings.items():
        path = Path(value).resolve(strict=True)
        if not path.is_file() or digest(path) != expected:
            raise ValueError('输入或产物哈希变化，禁止复用旧结果；请使用新run_id: ' + value)


def folder_for(run_id):
    return local(ROOT / 'private-context' / 'auto-workflows' / identifier(run_id), False)


def code_bindings():
    install = Path(__file__).resolve().parent.parent
    files = sorted((install / 'pipeline').glob('*.py'))
    files += sorted((install / 'pipeline').glob('*.json'))
    files += sorted(p for p in (install / 'pipeline/prompts').rglob('*') if p.is_file())
    files += sorted((install / 'presets').glob('*.json'))
    files += [install / 'VERSION']
    return {str(path): digest(path) for path in files}


def status(run_id):
    state = read(folder_for(run_id) / 'state.json')
    if state.get('code_bindings') != code_bindings():
        raise ValueError('执行源码、策略或版本变化，请使用新run_id重新验证')
    verify(state['bindings'])
    for entry in state['stages'].values():
        verify(entry['inputs'])
        verify(entry['outputs'])
    return state['report']


def automatic_card(folder, windows, cut):
    record = read(cut['record'])
    segments, questions = [], []
    for index, part in enumerate(record['segments'], 1):
        audio = part['audio']
        entry = {'label': f'第{index}段', 'purpose': part['purpose'],
                 'extraction': audio['extraction']['path'], 'window_range': audio['window_range'],
                 'video_range': part['video_range']}
        segments.append(entry)
        questions.append({'id': f'Q{index}', 'extraction': entry['extraction'],
                          'window_range': entry['window_range'],
                          'question': '核对原话、剪口完整性、同款画面和实际听感；' + '；'.join(part.get('questions', [])),
                          'video_ranges': [{'version': 'preview', 'range': part['video_range']}]})
    spec = {'title': '自动制作待审片：原话、同款与最终听感待核验',
            'extraction': windows[0]['extraction'], 'asr_preview': windows[0]['asr_preview'],
            'additional_extractions': [w['extraction'] for w in windows[1:]],
            'proposed_segments': segments, 'questions': questions,
            'versions': [{'id': 'preview', 'label': '当前待审粗剪',
                          'path': cut['preview'], 'sha256': digest(cut['preview']),
                          'duration_seconds': cut['duration_seconds'],
                          'changes': ['按Codex提供的真实剪法生成；未烧录未经核词的字幕，尚未人工验收'],
                          'segments': segments}]}
    path = folder / 'review-spec.json'
    if path.exists():
        if read(path) != spec:
            raise ValueError('自动审核卡已有不同输入，请使用新run_id')
    else:
        save(path, spec)
    return path


def extraction_outputs(result):
    """Probe logs are append-only diagnostics, not immutable window evidence."""
    path = Path(result['extraction'])
    item = read(path)
    paths = [path]
    for entry in [item.get('window'), item.get('audio_review'), *item.get('sample_frames', [])]:
        if entry:
            paths.append(entry['path'])
    return paths


def extract_or_resume(task, window):
    """Recover only an exact immutable extraction; partial overlaps still fail."""
    card, _, context, source = source_windows.original_context(task)
    for path in sorted((source_windows.WINDOWS / context['task_id']).glob('window-*/extraction.json')):
        item = read(path)
        if item.get('requested_range') != [window['start'], window['end']]:
            continue
        if (item.get('task_record') != str(card) or item.get('task_id') != context['task_id']
                or Path(item['source']['path']).resolve() != source
                or item['source']['sha256'] != context['source_sha256']):
            raise ValueError('精确窗口的任务或原片绑定变化，不能恢复')
        source_windows.import_entry(path)
        for entry in [item.get('audio_review'), *item.get('sample_frames', [])]:
            if entry:
                evidence = local(entry['path'])
                if digest(evidence) != entry['sha256']:
                    raise ValueError('精确窗口审核媒体哈希变化，不能恢复')
        return {'extraction': str(path), 'recovered_exact_window': True}
    return source_windows.extract(task, window['start'], window['end'])


def validate_card_binding(spec, allowed, cut):
    sources = [spec['extraction'], *spec.get('additional_extractions', [])]
    if any(str(Path(value).resolve()) not in allowed for value in sources):
        raise ValueError('审核卡必须来自本次已抽取窗口')
    if not any(Path(version['path']).resolve() == Path(cut['preview']).resolve()
               and version.get('sha256') == digest(cut['preview']) for version in spec.get('versions', [])):
        raise ValueError('审核卡必须精确绑定本次待审片及哈希；可另保留前版比较')


def validate_production_context(parent, child):
    if (Path(child['source_video']).resolve() != Path(parent['source_video']).resolve()
            or child.get('source_sha256') != parent.get('source_sha256')
            or child.get('product') != parent.get('product')):
        raise ValueError('正式制作必须绑定同一原片、商品身份与参考图证据')


def run(request):
    if not isinstance(request, dict):
        raise ValueError('request必须为对象')
    folder = folder_for(request['run_id'])
    from run_records import locked
    with locked(folder / 'execution'):
        return _run(request)


def _run(request):
    if not isinstance(request, dict) or set(request) - {
            'run_id', 'task_record', 'windows', 'category', 'style', 'cut_spec', 'review_spec', 'production'}:
        raise ValueError('request含未知字段；不接受命令、shell或任意执行步骤')
    run_id = identifier(request['run_id'])
    task = local(request['task_record'])
    windows = request['windows']
    if not isinstance(windows, list) or not windows:
        raise ValueError('须提供明确原片窗口')
    for window in windows:
        if (not isinstance(window, dict) or set(window) != {'start', 'end'}
                or any(type(window[k]) not in (int, float) or not math.isfinite(window[k]) for k in window)
                or not 0 <= window['start'] < window['end'] or window['end'] - window['start'] > 120):
            raise ValueError('每个窗口须为明确start/end，不超过120秒')
    source_windows.original_context(task)
    context = read(task)['task_context']
    source = Path(context['source_video']).resolve(strict=True)
    core = {key: request.get(key, default) for key, default in
            [('task_record', None), ('windows', None), ('category', 'other'), ('style', 'auto')]}
    folder = folder_for(run_id)
    state_path = folder / 'state.json'
    if state_path.exists():
        state = read(state_path)
        if state['core'] != core:
            raise ValueError('核心输入变化，请使用新run_id，不复用旧结果')
        status(run_id)
    else:
        bindings = {str(task): digest(task), str(source): digest(source)}
        config = ROOT / 'config.json'
        if config.exists():
            bindings[str(config)] = digest(config)
        state = {'schema_version': 1, 'core': core, 'bindings': bindings,
                 'code_bindings': code_bindings(), 'stages': {}, 'report': {}}
        save(state_path, state)
    if 'cut' in state['stages'] and not request.get('cut_spec'):
        raise ValueError('已执行cut_spec不能从接续请求删除，请使用新run_id')
    if 'combine' in state['stages'] and not request.get('production'):
        raise ValueError('已执行production不能从接续请求删除，请使用新run_id')

    def stage(name, inputs, action, output_paths):
        input_refs = refs(inputs) if inputs else {}
        if name in state['stages']:
            old = state['stages'][name]
            if old['inputs'] != input_refs:
                raise ValueError('已执行阶段输入变化，请使用新run_id: ' + name)
            return old['result']
        # An interrupted stage is never recorded as complete; retry creates a
        # fresh attempt output and leaves the earlier attempt untouched.
        attempt = uuid.uuid4().hex[:8]
        result = action(attempt)
        outputs = refs(output_paths(result))
        verify(state['bindings'])
        state['stages'][name] = {'inputs': input_refs, 'outputs': outputs, 'result': result}
        save(state_path, state)
        return result

    extracted = []
    for index, window in enumerate(windows):
        item = stage(f'window-{index}', [],
                     lambda _, w=window: extract_or_resume(task, w),
                     extraction_outputs)
        asr = stage(f'asr-{index}', [item['extraction']],
                    lambda _: source_windows.transcribe_preview(Path(item['extraction'])),
                    lambda r: [r['preview_path'], read(r['preview_path']).get('transcript_cache', {}).get('path', r['preview_path'])])
        extracted.append({'extraction': item['extraction'], 'asr_preview': asr['preview_path']})
    from apparel_strategy import strategy_for
    report = {'run_id': run_id, 'status': 'needs_agent_decision', 'windows': extracted,
              'strategy': strategy_for(request.get('category', 'other'), request.get('style', 'auto')),
              'submitted': False, 'human_review': 'not_reviewed',
              'next_action': 'Codex查看真实帧与原速片段，选择剪法并填写cut_spec；运营无需填写JSON'}
    if request.get('cut_spec'):
        spec = local(request['cut_spec'])
        review_cut.plan(spec)
        allowed = {str(Path(item['extraction']).resolve()) for item in extracted}
        provided = read(spec)
        if Path(provided['task_record']).resolve() != task:
            raise ValueError('cut_spec必须绑定当前原片任务')
        for clip in provided['clips']:
            if (str(Path(clip['extraction']).resolve()) not in allowed or
                    ('visual' in clip and str(Path(clip['visual'].get('extraction', clip['extraction'])).resolve()) not in allowed)):
                raise ValueError('剪法必须来自本次真实抽取窗口')
        cut = stage('cut', [spec], lambda attempt: review_cut.run(spec, run_id + '-' + attempt),
                    lambda r: [r['output']])
        card_spec = local(request['review_spec']) if request.get('review_spec') else automatic_card(folder, extracted, cut)
        validate_card_binding(read(card_spec), allowed, cut)
        card = stage('card', [card_spec],
                     lambda attempt: review_card.generate(card_spec, ROOT / 'outputs/reviews' / (run_id + '-' + attempt) / 'index.html'),
                     lambda r: [Path(r['index']).parent])
        report.update(status='needs_content_review', cut=cut, review_card=card,
                      next_action='请实际听看审核卡；粗剪可播放，但原话、同款和最终内容仍待真实审核，不是正式成片交付')
    if request.get('production'):
        formal_task = request['production'].get('task_record') if isinstance(request['production'], dict) else None
        if formal_task:
            formal_context = read(local(formal_task))['task_context']
            validate_production_context(context, formal_context)
        report = production(request['production'], stage, run_id, report)
    state['report'] = report
    save(state_path, state)
    return report


def production(spec, stage, run_id, report):
    """Use existing adoption, transcript, and package gates without inventing evidence."""
    import combine
    import subtitle
    import publish_package
    if not isinstance(spec, dict) or set(spec) - {'job', 'annotations', 'source_review', 'task_record', 'subtitle_review', 'draft'}:
        raise ValueError('production只接受固定制作字段')
    required = ('job', 'annotations', 'source_review', 'task_record')
    if any(not spec.get(key) for key in required):
        report.update(status='needs_agent_decision', next_action='Codex先完成真实采用与来源证据，填写production的job/annotations/source_review/task_record')
        return report
    paths = {key: local(spec[key]) for key in required}
    config = ROOT / 'config.json'
    combined = stage('combine', list(paths.values()),
                     lambda attempt: combine.execute(paths['job'], paths['annotations'], run_id + '-c-' + attempt,
                                                     config, paths['source_review'], paths['task_record']),
                     lambda r: [Path(r['final_video']).parent.parent])
    composition = Path(combined['final_video']).parent.parent
    selected = subtitle.extract(composition)
    selected_path = folder_for(run_id) / 'selected.json'
    if not selected_path.exists():
        save(selected_path, selected)
    report.update(composition=str(composition), selected=str(selected_path), status='needs_agent_decision',
                  next_action='Codex依据真实核词证据填写production.subtitle_review，不以ASR代替核词')
    if not spec.get('subtitle_review'):
        return report
    review = local(spec['subtitle_review'])
    rendered = stage('subtitle', [review],
                     lambda attempt: subtitle.run(composition, review, run_id + '-s-' + attempt, config,
                                                  task_record=paths['task_record']),
                     lambda r: [Path(r['video']).parent.parent])
    package = stage('package-prepare', [],
                    lambda attempt: {'path': str(publish_package.prepare(Path(rendered['video']).parent.parent,
                                                                       run_id + '-p-' + attempt, config, paths['task_record']))},
                    lambda r: [r['path']])
    report.update(package=package['path'], next_action='Codex根据实际素材填写production.draft的封面和文案事实')
    if not spec.get('draft'):
        return report
    draft = local(spec['draft'])
    def finish(attempt):
        destination = local(ROOT / 'outputs' / (run_id + '-delivery-' + attempt) / 'publish-package', False)
        shutil.copytree(package['path'], destination)
        frames_path = destination / 'logs/frames.json'
        frames = read(frames_path)
        for frame in frames:
            original = Path(frame['path']).resolve(strict=True)
            old_frames = (Path(package['path']) / 'frames').resolve()
            if original.parent != old_frames or digest(original) != frame['sha256']:
                raise ValueError('准备包帧来源或哈希变化，不可复制审核证据')
            copied = destination / 'frames' / original.name
            if digest(copied) != frame['sha256']:
                raise ValueError('复制帧哈希变化，不可复用图片审核')
            frame['path'] = str(copied.resolve())
        save(frames_path, frames)
        info = publish_package.finish(destination, draft, task_record=paths['task_record'])
        return {'path': str(destination), 'info': info}
    finished = stage('package-finish', [draft], finish, lambda r: [r['path']])
    report.update(status='completed_unreviewed', delivery=finished,
                  next_action='正式本地交付已生成；请连续观看、实际试听并完成内容验收，未发布')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('run').add_argument('request', type=Path)
    sub.add_parser('status').add_argument('run_id')
    args = parser.parse_args()
    try:
        result = run(read(args.request)) if args.action == 'run' else status(args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as error:
        print(json.dumps({'status': 'ERROR', 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
