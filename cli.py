"""Local operator entry point. Workspace is selected before pipeline imports."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

INSTALL = Path(__file__).resolve().parent
STAGES = {'suggest': 'suggest', 'batch': 'batch_plan', 'import': 'batch_import',
          'combine': 'combine', 'subtitle': 'subtitle', 'review': 'subtitle_review',
          'package': 'publish_package', 'quality': 'quality_support', 'records': 'run_records',
          'window': 'source_windows', 'workflow': 'workflow', 'review-card': 'review_card',
          'review-cut': 'review_cut', 'auto': 'auto_workflow', 'rehearsal': 'rehearsal',
          'delivery': 'reviewed_delivery'}


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def save_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')


def independent(source, workspace):
    if source == workspace or source.is_relative_to(workspace) or workspace.is_relative_to(source):
        raise ValueError('原素材目录和工作目录必须彼此独立，不能互相包含')


def initialize(args, runtime):
    path = runtime.ROOT / 'config.json'
    if path.exists():
        raise ValueError('工作目录已初始化；请明确编辑配置，不覆盖现有文件')
    settings = {'profile': args.profile, 'max_duration_seconds': args.max_duration}
    for name in ('ffmpeg', 'ffprobe', 'font', 'model', 'source_root'):
        value = getattr(args, name)
        if value:
            resolved = Path(value).expanduser().resolve(strict=True)
            settings[{'source_root': 'source_root'}.get(name, name + '_path')] = str(resolved)
    if settings.get('source_root'):
        independent(Path(settings['source_root']), runtime.ROOT)
    if not 0 < args.max_duration <= 70:
        raise ValueError('成片上限须大于0且不超过70秒')
    save_new(path, settings)
    return {'workspace': str(runtime.ROOT), 'config': str(path), 'profile': runtime.profile()}


def doctor(runtime):
    settings = runtime.config()
    checks = []
    for name in ('ffmpeg', 'ffprobe'):
        executable = settings.get(name + '_path') or shutil.which(name)
        item = {'name': name, 'ok': False, 'path': executable}
        if executable and Path(executable).is_file():
            try:
                check = subprocess.run([executable, '-version'], capture_output=True, text=True,
                                       errors='replace', timeout=15,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                item.update(ok=check.returncode == 0,
                            version=check.stdout.splitlines()[0] if check.stdout else None)
            except (OSError, subprocess.TimeoutExpired) as error:
                item['error'] = str(error)
        checks.append(item)
    for module in ('PIL', 'faster_whisper'):
        checks.append({'name': module, 'ok': importlib.util.find_spec(module) is not None})
    model = Path(settings['model_path']) if settings.get('model_path') else None
    checks.append({'name': 'local_model_files', 'ok': bool(model and all(
        (model / name).is_file() for name in ('model.bin', 'config.json', 'tokenizer.json'))),
                   'path': str(model) if model else None,
                   'note': '文件存在不等于模型已加载；不会自动下载'})
    font = Path(settings['font_path']) if settings.get('font_path') else None
    checks.append({'name': 'font_file', 'ok': bool(font and font.is_file()),
                   'note': '首次使用应明确配置可显示中文的字体；渲染时还需验证'})
    checks.append({'name': 'initialized', 'ok': (runtime.ROOT / 'config.json').is_file()})
    runtime.profile()
    return {'ready': all(check['ok'] for check in checks), 'checks': checks,
            'workspace': str(runtime.ROOT), 'read_only': True,
            'not_checked': ['模型真实转写', '成片连续观看', '原声试听', '商家验收']}


def new_task(args, runtime):
    if not (runtime.ROOT / 'config.json').is_file():
        raise ValueError('先执行init建立独立工作目录')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args.task_id):
        raise ValueError('task-id须为1至64个字母、数字、短横线或下划线')
    source = Path(args.source).expanduser().resolve(strict=True)
    reference = Path(args.reference).expanduser().resolve(strict=True)
    if not source.is_file() or not reference.is_file():
        raise ValueError('原片和商品参考必须为文件')
    independent(source.parent, runtime.ROOT)
    configured = runtime.config().get('source_root')
    if configured and not source.is_relative_to(Path(configured).resolve()):
        raise ValueError('原片不在当前配置的source_root中')
    destination = runtime.ROOT / 'outputs' / args.task_id
    if destination.exists():
        raise ValueError('任务目录已存在；请读取原任务接续，不初始化覆盖')
    from quality_support import local_records, no_resubmit
    records = list(local_records())
    # Unknown history is not equivalent to a clean history.
    if any(not isinstance(record, dict) or not isinstance(record.get('status'), str)
           for record in records):
        raise ValueError('本地历史格式不完整，状态UNKNOWN，须先核实')
    identity = hashlib.sha256((args.product_name + '\n' + digest(reference)).encode()).hexdigest()[:16]
    context = {'task_id': args.task_id, 'scope': args.scope,
               'content_strategy': {'version': 'single-style-batch-v1'},
               'content_goal': 'undetermined', 'profile': runtime.profile(),
               'product': {'name': args.product_name, 'code': args.sku or 'local-' + identity,
                           'id': None, 'reference': {'path': str(reference), 'sha256': digest(reference)}},
               'source_video': str(source), 'source_sha256': digest(source),
               'confirmed_windows': [], 'source_review': {'conclusion': 'unknown'},
               'cross_device_state': 'local_records_checked',
               'history_check': {'scope': 'this_workspace_only', 'records': len(records),
                                 'checked_at': datetime.now(timezone.utc).isoformat()}}
    if args.material_date:
        context['material_date'] = args.material_date
    no_resubmit(context, records)
    from run_records import initialize as init_card
    path = destination / 'run-state.json'
    init_card(path, task_context=context)
    return {'task_record': str(path), 'next_action': '检查原片并记录同款窗口与真实证据；新建不代表审核通过'}


def status(runtime):
    from run_records import submission_state
    tasks = []
    for path in sorted((runtime.ROOT / 'outputs').glob('**/run-state.json')):
        item = {'path': str(path), 'status': 'UNKNOWN', 'submitted': None}
        try:
            card = json.loads(path.read_text('utf-8-sig'))
            if not isinstance(card, dict):
                raise ValueError('任务卡必须为对象')
            item['recorded_state'] = {k: card[k] for k in ('status', 'submitted') if k in card}
            item.update(task_id=card.get('task_context', {}).get('task_id'),
                        next_action=card.get('next_action'))
            item.update(submission_state(card))
        except (OSError, ValueError, TypeError, AttributeError) as error:
            item.update(status='UNKNOWN', error=str(error))
        tasks.append(item)
    return {'workspace': str(runtime.ROOT), 'tasks': tasks}


def main():
    parser = argparse.ArgumentParser(description='通用电商直播切片：本地制作与交付')
    parser.add_argument('--workspace', help='独立工作目录的绝对路径')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init', help='建立配置，不覆盖已有配置')
    init.add_argument('--profile', choices=('basic', 'apparel'), default='basic')
    init.add_argument('--max-duration', type=float, default=70)
    for name in ('ffmpeg', 'ffprobe', 'font', 'model', 'source-root'):
        init.add_argument('--' + name)
    sub.add_parser('doctor', help='只读检查本地工具和配置')
    sub.add_parser('status', help='只读查看本地任务，缺失或非法状态显示UNKNOWN，矛盾显示CONFLICT')
    task = sub.add_parser('new-task', help='创建待核验的原片任务')
    for name in ('source', 'product-name', 'reference', 'task-id'):
        task.add_argument('--' + name, required=True)
    task.add_argument('--sku')
    task.add_argument('--material-date')
    task.add_argument('--scope', choices=('production', 'test'), default='production')
    for stage in STAGES:
        sub.add_parser(stage, add_help=False)
    args, remaining = parser.parse_known_args()
    if args.workspace:
        workspace = Path(args.workspace).expanduser()
        if not workspace.is_absolute():
            parser.error('--workspace必须是绝对路径')
        os.environ['ECLIP_WORKSPACE'] = str(workspace.resolve())
    sys.path.insert(0, str(INSTALL / 'pipeline'))
    try:
        import runtime
        if args.command in STAGES:
            command = [sys.executable, '-B', str(INSTALL / 'pipeline' / (STAGES[args.command] + '.py'))]
            command.extend(remaining)
            return subprocess.run(command, env=os.environ.copy()).returncode
        if remaining:
            parser.error('无法识别的参数: ' + ' '.join(remaining))
        if args.command == 'init':
            emit(initialize(args, runtime))
        elif args.command == 'new-task':
            emit(new_task(args, runtime))
        elif args.command == 'doctor':
            report = doctor(runtime)
            emit(report)
            return 0 if report['ready'] else 1
        elif args.command == 'status':
            report = status(runtime)
            emit(report)
            return 1 if any(t['status'] in {'UNKNOWN', 'CONFLICT', 'SUBMITTING', 'SUBMIT_UNKNOWN'}
                            for t in report['tasks']) else 0
        return 0
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        emit({'status': 'ERROR', 'error': str(error)})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
