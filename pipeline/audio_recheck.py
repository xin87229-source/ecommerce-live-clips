"""Local second opinion for short audio; never edits transcripts or approvals."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import wave

from media_adapter import MediaAdapter
from rehearsal import asr_input
from review_card import local_file, span
from runtime import ROOT, config
from selector_support import atomic, file_hash, read

# Exact official Windows CPU release and model revision documented in audio-recheck.md.
PINNED = {
    'executable': 'b06c099ca771d8ffa09f644cb67ac1bbf9c1341981dd9826f0d6b3a8f04885b3',
    'encoder': 'f92f91d01a24fbed6c863495b2ee8c6a6788144a02858b75743f0946668de8a2',
    'decoder': '819f385dc0e035dccc3d9e7edaf6b7b044b8ba7ace63cbcbf84c7e397eecbf27',
}


def resources(settings):
    selected = settings.get('audio_recheck')
    if not isinstance(selected, dict) or set(selected) != set(PINNED):
        raise ValueError('先单独安装并配置 audio_recheck 的 executable、encoder、decoder；不会下载或调用云服务')
    result = {}
    for name, expected in PINNED.items():
        raw = selected[name]
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise ValueError('复核资源必须为本地绝对路径')
        path = Path(raw).resolve(strict=True)
        if not path.is_file() or file_hash(path) != expected:
            raise ValueError('复核资源与已验证版本不符: ' + name)
        result[name] = str(path)
    return result


def requests(value, duration):
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise ValueError('每次只复核1至12个疑难短片段')
    ids = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {'id', 'window_range'}:
            raise ValueError('片段只接受id和window_range，不向模型提供预期答案')
        name = row['id']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}', name) or name in ids:
            raise ValueError('片段id须唯一且只含字母数字短横线下划线')
        ids.add(name)
        a, b = span(row['window_range'], duration)
        if b-a > 20:
            raise ValueError('每段最多20秒，避免长音频重复生成；由Skill选择完整上下文')
    return value


def checked_plan(spec_path):
    path = local_file(spec_path, ROOT / 'private-context')
    spec = read(path)
    if (not isinstance(spec, dict) or set(spec) != {'schema_version', 'asr_preview', 'ranges'}
            or type(spec['schema_version']) is not int or spec['schema_version'] != 1):
        raise ValueError('复核输入须为schema_version=1、asr_preview和ranges')
    asr = asr_input(spec['asr_preview'])
    rows = requests(spec['ranges'], asr['item']['window']['duration_seconds'])
    return path, asr, rows


def parse_output(raw):
    text = raw.strip()
    # This pinned release writes plain transcription to stdout, diagnostics to stderr.
    if not text or len(text) > 1500 or '\ufffd' in text or any(ord(c) < 32 and c not in '\n\r\t' for c in text):
        raise ValueError('复核模型未返回有效文字；保留失败记录，不伪造识别结果')
    return text


def infer(paths, audio, folder, name):
    command = [paths['executable'], '--enc', paths['encoder'], '-m', paths['decoder'],
               '-a', str(audio), '-n', '256']
    with (folder / (name + '.stderr.log')).open('w', encoding='utf-8') as log:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=log, encoding='utf-8', errors='strict', timeout=300,
                                shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    (folder / (name + '.stdout.txt')).write_text(result.stdout, encoding='utf-8')
    if result.returncode:
        raise RuntimeError('本地复核模型运行失败，详见本地日志；没有云端回退')
    return parse_output(result.stdout)


def verify_inputs(inputs):
    for name, sha in inputs.items():
        if file_hash(Path(name)) != sha:
            raise ValueError('复核输入或资源发生变化: ' + name)


def run(spec_path, run_id):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', run_id):
        raise ValueError('run-id格式无效')
    path, asr, rows = checked_plan(spec_path)
    settings = config()
    resource_file = (ROOT / 'private-context/audio-recheck-resources.json').resolve(strict=True)
    if not resource_file.is_relative_to((ROOT / 'private-context').resolve()):
        raise ValueError('复核资源配置须在当前私有目录')
    paths = resources({'audio_recheck': read(resource_file)})
    inputs = {str(path): file_hash(path), **asr['inputs'],
              **{value: PINNED[name] for name, value in paths.items()}}
    inputs[str(resource_file)] = file_hash(resource_file)
    if (ROOT / 'config.json').exists():
        inputs[str(ROOT / 'config.json')] = file_hash(ROOT / 'config.json')
    source = asr['item']['source']
    inputs[source['path']] = source['sha256']
    folder = (ROOT / 'private-context/audio-rechecks' / run_id).resolve()
    if not folder.is_relative_to((ROOT / 'private-context').resolve()):
        raise ValueError('复核输出越出本地私有目录')
    folder.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    report = {'schema_version': 1, 'status': 'running', 'inputs': inputs,
              'engine': 'fun-asr-nano-gguf-cpu-v0.2.1', 'network_calls': 0,
              'spec': str(path),
              'expected_answer_prompt': False, 'transcript_verified': False,
              'human_confirmation': False, 'audio_listening': 'not_performed',
              'results': []}
    output = folder / 'report.json'
    atomic(output, report)
    try:
        media = MediaAdapter(settings, folder / 'media.log')
        for row in rows:
            a, b = row['window_range']
            audio = folder / (row['id'] + '.wav')
            media.write_media(['-protocol_whitelist', 'file,pipe', '-i', asr['item']['window']['path'],
                               '-ss', f'{a:.6f}', '-t', f'{b-a:.6f}', '-map', '0:a:0',
                               '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(audio)], audio)
            with wave.open(str(audio)) as wav:
                duration = wav.getnframes()/wav.getframerate()
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 16000) or abs(duration-(b-a)) > .02:
                    raise ValueError('复核音频格式或覆盖范围异常')
            then = time.perf_counter()
            second = infer(paths, audio, folder, row['id'])
            offset = asr['item']['offset_seconds']
            first = [s for s in asr['preview']['segments'] if s['window_range'][0] < b and s['window_range'][1] > a]
            report['results'].append(dict(row, source_range=[offset+a, offset+b],
                audio={'path': str(audio), 'sha256': file_hash(audio)},
                primary_segments=first, second_opinion=second,
                elapsed_seconds=time.perf_counter()-then,
                decision='agent_comparison_required', word_timestamps=None))
            atomic(output, report)
        verify_inputs(inputs)
        report.update(status='machine_recheck_complete', elapsed_seconds=time.perf_counter()-started)
        report['artifacts'] = {str(p): file_hash(p) for p in folder.iterdir() if p.is_file() and p != output}
        atomic(output, report)
        return {'report': str(output), 'status': report['status'], 'results': report['results'],
                'next_action': '比较已有确认、词库和原声转写；未解决先选清楚完整替代句，不将机器复核写成人工确认'}
    except Exception as exc:
        report.update(status='failed', error=str(exc), elapsed_seconds=time.perf_counter()-started)
        atomic(output, report)
        raise


def check(path):
    path = local_file(path, ROOT / 'private-context/audio-rechecks')
    report = read(path)
    if (report.get('schema_version') != 1 or report.get('status') != 'machine_recheck_complete'
            or report.get('transcript_verified') is not False or report.get('human_confirmation') is not False
            or report.get('audio_listening') != 'not_performed' or not report.get('results')):
        raise ValueError('复核尚未完成或状态被改写')
    verify_inputs(report['inputs'])
    verify_inputs(report['artifacts'])
    _, asr, rows = checked_plan(report['spec'])
    if len(rows) != len(report['results']):
        raise ValueError('复核结果数量变化')
    for row, result in zip(rows, report['results']):
        a, b = row['window_range']
        offset = asr['item']['offset_seconds']
        original = [s for s in asr['preview']['segments'] if s['window_range'][0] < b and s['window_range'][1] > a]
        raw = local_file(path.parent / (row['id'] + '.stdout.txt'), path.parent)
        if (result.get('id') != row['id'] or result.get('window_range') != row['window_range']
                or result.get('source_range') != [offset+a, offset+b]
                or result.get('primary_segments') != original
                or result.get('second_opinion') != parse_output(raw.read_text('utf-8'))
                or report['artifacts'].get(str(raw)) != file_hash(raw)):
            raise ValueError('复核结果、原始输出或范围被改写')
    return {'status': 'machine_recheck_binding_valid', 'report': str(path), 'not_human_approval': True}


def main():
    parser = argparse.ArgumentParser(description='疑难短句本地第二识别，不上传、不收费、不自动审核')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('run')
    p.add_argument('--spec', required=True, type=Path)
    p.add_argument('--run-id', required=True)
    p = sub.add_parser('check')
    p.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run(args.spec, args.run_id) if args.command == 'run' else check(args.report)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'ERROR', 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
