"""Fixed local media interface. All external media execution stays here."""
import json
import shutil
import subprocess
from pathlib import Path

import ffmpeg_subset as subset


class MediaAdapter:
    def __init__(self, config, log):
        self.log = log
        self.tools = {}
        for name in ("ffmpeg", "ffprobe"):
            configured = config.get(name + "_path", "")
            found = configured or shutil.which(name)
            if not found or not Path(found).is_file():
                raise ValueError(f"{name}不可用，请检查本地配置或PATH")
            self.tools[name] = str(Path(found).resolve())
            self.command(name, ["-version"], timeout=15)

    def command(self, tool, args, timeout=1800):
        # Local logs only; callers pass fixed commands, never task-provided flags.
        command = [self.tools[tool], *args]
        with self.log.open("a", encoding="utf-8") as log:
            log.write(json.dumps(command, ensure_ascii=False) + "\n")
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=log, encoding="utf-8", errors="replace",
                                    timeout=timeout, shell=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log.write(result.stdout + "\n")
        if result.returncode:
            raise RuntimeError(f"{tool}执行失败（退出码{result.returncode}），详见本地技术日志")
        return result.stdout

    def probe(self, source):
        entries = ("format=duration,size:stream=index,codec_type,codec_name,width,height,"
                   "avg_frame_rate,r_frame_rate,pix_fmt,sample_aspect_ratio,color_transfer,"
                   "color_primaries,channels,sample_rate,start_time,duration:"
                   "stream_disposition=attached_pic:stream_tags=rotate:stream_side_data")
        raw = self.command("ffprobe", ["-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries", entries,
                                      "-of", "json", str(source)], timeout=60)
        return subset.probe(json.loads(raw))

    def write_media(self, args, destination, *, delivery=False):
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("输出文件已经存在，禁止覆盖")
        if delivery:
            if str(args[-1]) != str(destination):
                raise ValueError('成片输出路径必须为最后一个参数')
            args = [*args[:-1], '-r', str(subset.DELIVERY_FPS), '-fps_mode', 'cfr', args[-1]]
        self.command("ffmpeg", ["-hide_banner", "-loglevel", "error", "-nostdin", "-n", *args])
        if not destination.is_file() or destination.stat().st_size == 0:
            raise RuntimeError("未生成有效输出文件")

    def render(self, source, segments, meta, mode, anchor, size, destination, *, limit_duration=False, exact_size=False):
        vf, dimensions = subset.fit(meta, mode, anchor, size, exact_size=exact_size)
        args = subset.cut(source, segments, meta, vf, destination)
        if limit_duration:
            # Concat/frame rounding may extend the video past the selected audio envelope.
            duration = sum(end - start for start, end in segments)
            args = [*args[:-1], '-t', f'{duration:.6f}', args[-1]]
        self.write_media(args, destination)
        return {"fit": mode if vf else "skipped-already-vertical", "size": dimensions}

    def check_frame_timing(self, source):
        raw = self.command('ffprobe', ['-v', 'error', '-protocol_whitelist', 'file,pipe',
                           '-select_streams', 'v:0', '-show_entries',
                           'stream=time_base,avg_frame_rate,r_frame_rate:frame=pts',
                           '-of', 'json', str(source)])
        return subset.frame_timing(json.loads(raw))

    def check(self, destination, expected, size, *, delivery=False):
        meta = self.probe(destination)
        checks = subset.check(meta, expected, size)
        if delivery:
            meta['frame_timing'] = self.check_frame_timing(destination)
            checks['frame_timing_30fps'] = True
        self.command("ffmpeg", ["-hide_banner", "-loglevel", "error", "-nostdin", "-xerror",
                                "-i", str(destination), "-map", "0:v:0", "-map", "0:a:0",
                                "-f", "null", "-"])
        checks["full_decode"] = True
        return meta, checks

    def look(self, source, duration, directory):
        images = []
        for i, fraction in enumerate((0.1, 0.5, 0.9), 1):
            destination = directory / f"frame-{i}.png"
            self.write_media(subset.look(source, duration * fraction, destination), destination)
            images.append(str(destination))
        return images
