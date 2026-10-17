"""Generate a private installation sample; never create business review evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time


INSTALL = Path(__file__).resolve().parents[1]


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description='四秒本地技术样例：图案与测试音，不是直播业务验收')
    parser.add_argument('--workspace', required=True, type=Path, help='已初始化的独立工作区绝对路径')
    parser.add_argument('--name', required=True, help='唯一名称；1至64个字母、数字、短横线或下划线')
    parser.add_argument('--load-model', action='store_true', help='额外初始化已配置本地模型；不转写、不下载')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args.name):
        parser.error('name须为1至64个字母、数字、短横线或下划线')
    if not args.workspace.is_absolute():
        parser.error('workspace必须是绝对路径')
    workspace = args.workspace.resolve()
    if workspace.is_relative_to(INSTALL) or INSTALL.is_relative_to(workspace):
        parser.error('工作区与项目安装目录必须相互独立')
    if not (workspace / 'config.json').is_file():
        parser.error('先用cli.py init建立独立工作区')
    output = workspace / 'outputs' / 'install-smoke' / args.name
    if not output.resolve().is_relative_to(workspace):
        parser.error('输出路径越出工作区，停止执行')
    if output.exists() or output.is_symlink():
        parser.error('输出目录已存在，禁止覆盖；请换一个新name')

    # Workspace must be selected before any pipeline module is imported.
    os.environ['ECLIP_WORKSPACE'] = str(workspace)
    sys.path.insert(0, str(INSTALL / 'pipeline'))
    output.mkdir(parents=True, exist_ok=False)
    report = {
        'kind': 'installation_technical_smoke', 'status': 'running',
        'at': datetime.now(timezone.utc).isoformat(),
        'source': 'FFmpeg-generated test pattern and 440 Hz test tone; no merchant media',
        'business_validation': False, 'asr_accuracy': 'not_checked',
        'same_product_review': 'not_applicable', 'transcript_verified': False,
        'continuous_visual': 'not_reviewed', 'audio_listening': 'not_reviewed',
        'model_check': {'status': 'not_checked'},
        'limitations': ['技术测试，不是直播原话', '不验证同款、原声转写或女装业务闭环',
                        '无任务卡、采用、人工审核或平台状态写入'],
    }
    started = time.perf_counter()
    try:
        from runtime import config
        from media_adapter import MediaAdapter
        from subtitle import font_choice, subtitle_files, filter_path

        settings = config()
        if not settings.get('font_path'):
            raise ValueError('工作区config.json须显式配置本地font_path')
        style = json.loads((INSTALL / 'pipeline' / 'subtitle_style.json').read_text('utf-8'))
        family, font = font_choice(style['font_preferences'], settings['font_path'])
        fonts = output / 'fonts'
        fonts.mkdir()
        shutil.copyfile(font, fonts / font.name)
        report['font'] = {'family': family, 'sha256': sha256(font),
                          'note': '字体副本只留在私有工作区；不随源码候选分发'}
        media = MediaAdapter(settings, output / 'media.log')
        raw = output / 'pattern.mp4'
        media.write_media([
            '-f', 'lavfi', '-i', 'testsrc2=size=1080x1920:rate=30:duration=4',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4',
            '-map', '0:v:0', '-map', '1:a:0', '-t', '4',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-ar', '48000', '-b:a', '128k', '-movflags', '+faststart',
            str(raw)], raw, delivery=True)

        # Fixed test wording is deliberately unrelated to any ASR or merchant speech.
        style = dict(style, font_size=72, position_y_percent=50, fade_ms=0, outline=5)
        cues = [{'start': 0, 'end': 4, 'lines': ['技术测试', '不是直播原话'], 'highlights': []}]
        ass, srt, _ = subtitle_files(cues, style, family)
        (output / 'subtitle.ass').write_text(ass, encoding='utf-8')
        (output / 'subtitle.srt').write_text(srt, encoding='utf-8')
        video = output / 'video.mp4'
        vf = 'ass=filename=' + filter_path(output / 'subtitle.ass') + ':fontsdir=' + filter_path(fonts)
        media.write_media([
            '-i', str(raw), '-map', '0:v:0', '-map', '0:a:0', '-vf', vf,
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
            '-c:a', 'copy', '-movflags', '+faststart', str(video)], video, delivery=True)
        metadata, checks = media.check(video, 4, [1080, 1920], delivery=True)
        if metadata['frame_timing']['frame_count'] != 120:
            raise ValueError('四秒技术样例必须恰好为120帧')
        cover = output / 'cover.png'
        media.write_media(['-ss', '2', '-i', str(video), '-map', '0:v:0',
                           '-frames:v', '1', '-update', '1', str(cover)], cover)
        report.update(metadata=metadata, technical_checks=checks,
                      cover={'file': cover.name, 'source': video.name, 'at_seconds': 2,
                             'note': '从生成的测试视频提取真实帧；不是商品封面'})
        if args.load_model:
            report['model_check'] = {'status': 'running'}
            os.environ['HF_HUB_OFFLINE'] = '1'
            os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
            os.environ['HF_HUB_DISABLE_XET'] = '1'
            from transcribe_adapter import local_model_path
            from faster_whisper import WhisperModel
            model_started = time.perf_counter()
            model = WhisperModel(str(local_model_path()), device='cpu', compute_type='int8',
                                 cpu_threads=6, local_files_only=True)
            report['model_check'] = {'status': 'passed', 'device': 'cpu', 'compute_type': 'int8',
                                     'elapsed_seconds': time.perf_counter() - model_started,
                                     'note': '仅初始化本地模型；没有中文语音转写或准确性验收'}
            del model
        report['status'] = 'technical_passed'
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        if report['model_check']['status'] == 'running':
            report['model_check'] = {'status': 'failed', 'error': str(error)}
    report['elapsed_seconds'] = time.perf_counter() - started
    report['files'] = {p.relative_to(output).as_posix(): sha256(p)
                       for p in sorted(output.rglob('*')) if p.is_file()}
    with (output / 'report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'status': report['status'], 'output': str(output),
                      'report': str(output / 'report.json'), 'error': report.get('error')},
                     ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'technical_passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
