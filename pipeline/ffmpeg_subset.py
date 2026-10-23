"""Reviewed, adapted subset of kajisho5/ffmpeg-skill; see THIRD_PARTY_NOTICES.md.

Only probe/cut/fit/check/look primitives. No upstream CLI, global state or batch.
"""
import math
from fractions import Fraction

UPSTREAM_COMMIT = "c3a4a5f0ef6629018308a7eaeb5a1f592e5defc1"
DELIVERY_FPS = 30


def frame_timing(raw):
    """Check decoded presentation timestamps, not just nominal frame rate."""
    stream = raw['streams'][0]
    tick = Fraction(stream['time_base'])
    expected = Fraction(1, DELIVERY_FPS)
    if (not 0 < tick < expected or
            any(Fraction(stream[name]) != DELIVERY_FPS for name in ('avg_frame_rate', 'r_frame_rate'))):
        raise ValueError('成片必须为30/1 fps CFR；需重新导出，不可仅修改帧率标签')
    frames = raw.get('frames', [])
    if len(frames) < 2 or any(type(f.get('pts')) is not int for f in frames):
        raise ValueError('无法确认逐帧时间戳')
    pts = [f['pts'] * tick for f in frames]
    intervals = [b - a for a, b in zip(pts, pts[1:])]
    if any(d <= 0 or abs(d - expected) > tick for d in intervals):
        raise ValueError('逐帧验收失败：时间戳不单调或间隔偏离1/30秒')
    return {'rule_version': 'cfr30-v1', 'target_fps': DELIVERY_FPS,
            'actual_fps': str(Fraction(stream['avg_frame_rate'])),
            'frame_count': len(frames), 'anomaly_count': 0,
            'time_base': str(tick), 'tolerance_seconds': float(tick),
            'min_interval_seconds': float(min(intervals)),
            'max_interval_seconds': float(max(intervals)), 'status': 'passed'}


def probe(raw):
    """Adapted from probe.py's _common.probe; discard private media tags."""
    streams = raw.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if len(videos) != 1 or len(audios) != 1:
        raise ValueError("需要一条视频流与一条已确认的原声音轨")
    v, a = videos[0], audios[0]
    rotation = float(v.get("tags", {}).get("rotate", 0))
    for side in v.get("side_data_list", []):
        rotation = float(side.get("rotation", rotation))
        if "dv_profile" in side or "DOVI" in side.get("side_data_type", ""):
            raise ValueError("V0.1不处理Dolby Vision，请提供SDR素材")
    if rotation % 90:
        raise ValueError("无法可靠处理非直角旋转")
    w, h = int(v["width"]), int(v["height"])
    if int(rotation) % 180:
        w, h = h, w
    sar = v.get("sample_aspect_ratio", "1:1")
    if sar not in ("1:1", "N/A", "0:1"):
        raise ValueError("V0.1不处理非方形像素，请提供方形像素素材")
    if v.get("color_transfer") in ("smpte2084", "arib-std-b67") or v.get("color_primaries") == "bt2020":
        raise ValueError("V0.1不自动转换HDR，请提供SDR素材")
    fps = float(Fraction(v.get("avg_frame_rate") or v.get("r_frame_rate", "0/1")))
    duration = float(raw.get("format", {}).get("duration", v.get("duration", 0)))
    if not all(math.isfinite(x) and x > 0 for x in (fps, duration, w, h)):
        raise ValueError("无法确认时长、分辨率或帧率")
    return {"duration": duration, "width": w, "height": h, "fps": fps,
            "codec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"),
            "rotation": rotation, "video_index": v["index"], "audio_index": a["index"],
            "audio": {"codec": a.get("codec_name"), "channels": int(a.get("channels", 0)),
                      "sample_rate": int(a.get("sample_rate", 0)),
                      "duration": float(a.get("duration", duration)),
                      "start": float(a.get("start_time", 0))},
            "video_start": float(v.get("start_time", 0)),
            "size_bytes": int(raw.get("format", {}).get("size", 0))}


def fit(meta, mode, anchor, size, *, exact_size=False):
    """Keep V0.1 vertical dimensions unless the caller requests an exact canvas."""
    if meta["width"] * 16 == meta["height"] * 9:
        if not exact_size or [meta["width"], meta["height"]] == size:
            return "", [meta["width"], meta["height"]]
        return f"scale={size[0]}:{size[1]},setsar=1", size
    if mode == "keep":
        raise ValueError("keep要求源视频已是9:16")
    w, h = size
    if mode == "crop":
        x, y = anchor
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase:force_divisible_by=2,"
              f"crop={w}:{h}:(in_w-out_w)*{x:g}:(in_h-out_h)*{y:g}")
    else:
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    return vf + ",setsar=1", size


def cut(source, segments, meta, vf, destination, *, preserve_terminal_frame=False):
    """Adapt cut.py accurate seek; concat decoded segments in a single encode.

    No stream-copy keyframe snap, temporary parts or concat-path escaping.
    """
    if any(not math.isfinite(t) or t < 0 for t in (meta['video_start'], meta['audio']['start'])):
        raise ValueError('原素材须有有限且非负的音画起点')
    args, filters, video_labels, audio_labels = ['-copyts'], [], [], []
    elapsed = 0.0
    for i, (start, end) in enumerate(segments):
        absolute_start, absolute_end = meta['video_start'] + start, meta['video_start'] + end
        seek = max(0, absolute_start - 10)
        # Decode before the cut, then trim on original PTS; TS input seeking can land late.
        args += ["-protocol_whitelist", "file,pipe", "-seek_timestamp", "1", "-ss", f"{seek:.6f}",
                 "-t", f"{absolute_end - seek:.6f}", "-i", str(source)]
        filters += [f"[{i}:{meta['video_index']}]trim=start={absolute_start:.6f}:end={absolute_end:.6f},"
                    f"setpts=PTS-{absolute_start:.6f}/TB+{elapsed:.6f}/TB[v{i}]",
                    f"[{i}:{meta['audio_index']}]atrim=start={absolute_start:.6f}:end={absolute_end:.6f},"
                    f"asetpts=PTS-{absolute_start:.6f}/TB,aresample=first_pts=0[a{i}]"]
        video_labels.append(f"[v{i}]")
        audio_labels.append(f"[a{i}]")
        elapsed += end - start
    # Joint AV concat pads audio to rounded video frames, then a duration cap cuts real tail samples.
    # Place video on the selected timeline and join audio independently, without stretching either.
    filters.append("".join(video_labels) + f"interleave=nb_inputs={len(segments)}:duration=longest[video]")
    filters.append("".join(audio_labels) + f"concat=n={len(segments)}:v=0:a=1[audio]")
    # Convert samples before canvas padding, including the already-vertical/no-resize path.
    # A probed terminal source frame has no following frame to advance the
    # interleave EOF timestamp. Keep it only when the caller proved its span.
    eof = ':eof_action=pass' if preserve_terminal_frame else ''
    filters.append("[video]scale=in_range=auto:out_range=tv," + ((vf + ",") if vf else "")
                   + f"fps={meta['fps']:.6f}:start_time=0{eof},format=yuv420p[out]")
    return args + ["-filter_complex", ";".join(filters), "-map", "[out]", "-map", "[audio]",
                   "-map_metadata", "-1", "-map_chapters", "-1", "-c:v", "libx264",
                   "-preset", "fast", "-crf", "18", "-color_range", "tv", "-c:a", "aac", "-b:a", "192k",
                   "-ar", "48000", "-movflags", "+faststart", str(destination)]


def check(meta, expected, size):
    """Adapt check.py's duration/aspect/codec/audio rows with local strict gates."""
    tolerance = max(0.12, 2 / meta["fps"])
    a = meta["audio"]
    checks = {
        "duration": abs(meta["duration"] - expected) <= tolerance,
        "resolution": [meta["width"], meta["height"]] == size,
        "aspect": meta["width"] * 16 == meta["height"] * 9,
        "codec": meta["codec"] == "h264" and meta["pix_fmt"] == "yuv420p",
        "audio": a["codec"] == "aac" and a["channels"] > 0 and a["sample_rate"] == 48000,
        "av_timing": abs(a["duration"] - expected) <= tolerance
                     and abs(a["start"] - meta["video_start"]) <= tolerance,
        "size": 1024 < meta["size_bytes"] < 4 * 1024**3,
    }
    if not all(checks.values()):
        raise ValueError("基础验收失败：" + ", ".join(k for k, ok in checks.items() if not ok))
    return checks


def look(source, seconds, destination):
    """Adapt look.py --at/--no-timecode; no font dependency on Windows."""
    return ["-ss", f"{seconds:.6f}", "-i", str(source), "-vf", "scale=360:-2",
            "-frames:v", "1", "-update", "1", str(destination)]
