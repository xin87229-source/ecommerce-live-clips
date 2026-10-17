"""Pinned faster-whisper, CPU baseline, complete content-addressed local cache."""
import importlib.metadata
import json
import math
import os
import time
from pathlib import Path

from selector_support import PRIVATE, atomic, file_hash, key, read, validate_transcript
from runtime import config

COMMIT = "ed9a06cd89a93e47838f564998a6c09b655d7f43"
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
TAIL_TOLERANCE_SECONDS = 2.0
OPTIONS = {"language": "zh", "task": "transcribe", "word_timestamps": False,
           "vad_filter": True, "vad_parameters": {"min_silence_duration_ms": 2000, "speech_pad_ms": 400},
           "beam_size": 5, "condition_on_previous_text": True, "without_timestamps": False}


def local_model_path():
    raw = config().get('model_path')
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('config.json须显式提供本地model_path；模型安装是独立步骤')
    path = Path(raw).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('model_path须为已存在的本地绝对目录')
    path = path.resolve()
    if not (path / 'tokenizer.json').is_file() or not (path / 'model.bin').is_file():
        raise ValueError('model_path缺少tokenizer.json或model.bin')
    return path


def bounded_asr_end(start, end, duration):
    if (not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end, duration))
            or duration <= 0 or not 0 <= start <= end):
        raise ValueError("转写时间无效")
    if end > duration + TAIL_TOLERANCE_SECONDS:
        raise ValueError(f"转写时间超出素材: start={start}, end={end}, duration={duration}")
    return min(end, duration)


def audio_audit(source, meta):
    import av
    if abs(meta["audio"]["start"]) > 0.05 or abs(meta["video_start"]) > 0.05:
        raise ValueError("V0.2a首版只接受接近零起点的音视频，拒绝猜测时间偏移")
    previous, first, count, max_gap = None, None, 0, 0.0
    with av.open(str(source)) as container:
        for frame in container.decode(audio=0):
            if frame.pts is None:
                raise ValueError("音轨缺少时间戳，不能确认原时间轴")
            start = float(frame.pts * frame.time_base)
            if first is None:
                first = start
            if previous is not None:
                gap = abs(start - previous)
                max_gap = max(max_gap, gap)
                if gap > 0.1:
                    raise ValueError("音轨时间不连续，不能直接映射；原片保持不动")
            previous = start + frame.samples / frame.sample_rate
            count += 1
    if first is None or abs(first) > 0.05 or abs(previous-meta["audio"]["duration"]) > 0.15:
        raise ValueError("音轨解码覆盖范围异常")
    return {"frames": count, "first": first, "end": previous, "max_gap_seconds": max_gap}


def transcribe(source, meta):
    started = time.perf_counter()
    model_path = local_model_path()
    before = source.stat()
    sha = file_hash(source)
    packages = {name: importlib.metadata.version(name) for name in
                ("faster-whisper", "ctranslate2", "av", "onnxruntime", "tokenizers")}
    asr = {"model_path": str(model_path),
           "model_files": {name: file_hash(model_path / name) for name in ('tokenizer.json', 'model.bin')},
           "packages": packages,
           "device": "cpu", "compute_type": "int8", "cpu_threads": 6, **OPTIONS,
           "tail_policy": "clamp-or-drop-v1", "tail_tolerance_seconds": TAIL_TOLERANCE_SECONDS}
    asr["config_hash"] = key(asr)
    directory = PRIVATE / "transcripts" / key([sha, asr["config_hash"]])
    cache = directory / "transcript.json"
    if cache.exists():
        t = read(cache)
        validate_transcript(t)
        if t["source"]["sha256"] != sha or t["asr"]["config_hash"] != asr["config_hash"]:
            raise ValueError("转写缓存身份错误")
        if (before.st_size, before.st_mtime_ns) != (source.stat().st_size, source.stat().st_mtime_ns):
            raise ValueError("读取缓存期间素材变化")
        return t, cache, {"cache_hit": True, "elapsed_seconds": time.perf_counter()-started}
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "transcribing.lock"
    # A failed run retains evidence, but releases its own lock in finally.
    with lock.open("x", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    try:
        audit = audio_audit(source, meta)
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        from faster_whisper import WhisperModel
        model_started = time.perf_counter()
        model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=6, local_files_only=True)
        model_seconds = time.perf_counter()-model_started
        asr_started = time.perf_counter()
        iterable, info = model.transcribe(str(source), **OPTIONS)
        segments = []
        adjustments = []
        previous_start = 0
        for segment in iterable:
            try:
                end = bounded_asr_end(segment.start, segment.end, meta["duration"])
                if segment.start < previous_start:
                    raise ValueError("转写时间倒序")
                previous_start = segment.start
            except ValueError as exc:
                atomic(directory / f"failed-{time.time_ns()}.json", {
                    "status": "failed", "error": str(exc), "source_sha256": sha,
                    "asr_config_hash": asr["config_hash"], "duration_seconds": meta["duration"],
                    "failed_segment": {"start": segment.start, "end": segment.end, "text": segment.text},
                    "segments": segments, "tail_adjustments": adjustments,
                    "elapsed_seconds": time.perf_counter()-asr_started})
                raise
            if segment.end > meta["duration"] or segment.start >= meta["duration"]:
                adjustments.append({"start": segment.start, "end": segment.end, "text": segment.text,
                                    "overrun_seconds": segment.end-meta["duration"],
                                    "action": "drop" if segment.start >= end else "clamp",
                                    "normalized_end": end})
            if segment.start >= meta["duration"]:
                continue  # No media exists for this tolerated trailing segment.
            segments.append({"id": f"s{len(segments)+1:06d}", "start": segment.start,
                             "end": end, "text": segment.text,
                             "avg_logprob": segment.avg_logprob, "no_speech_prob": segment.no_speech_prob})
            if len(segments) % 100 == 0:
                atomic(directory / "progress.json", {"status": "processing", "segments": len(segments),
                       "last_end": segment.end, "elapsed_seconds": time.perf_counter()-asr_started})
                print(f"ASR进度：{len(segments)} segments，原片{segment.end:.1f}秒", flush=True)
        asr_seconds = time.perf_counter()-asr_started
        if (before.st_size, before.st_mtime_ns) != (source.stat().st_size, source.stat().st_mtime_ns) or file_hash(source) != sha:
            raise ValueError("素材处理期间发生变化，拒绝缓存")
        t = {"schema_version": 1, "status": "complete",
             "source": {"path": str(source), "sha256": sha, "size": before.st_size,
                        "mtime_ns": before.st_mtime_ns, "duration_seconds": meta["duration"],
                        "audio_offset_seconds": 0}, "asr": asr,
             "audio_audit": audit, "tail_adjustments": adjustments,
             "metrics": {"transcribe_seconds": asr_seconds,
                 "model_load_seconds": model_seconds, "duration_after_vad": info.duration_after_vad},
             "segments": segments, "segments_hash": key(segments)}
        validate_transcript(t)
        atomic(cache, t)
        return t, cache, {"cache_hit": False, "elapsed_seconds": time.perf_counter()-started}
    finally:
        lock.unlink()
