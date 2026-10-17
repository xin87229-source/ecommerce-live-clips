"""Real pixel values must be converted, not merely relabelled as limited range."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from media_adapter import MediaAdapter
import review_cut


@unittest.skipUnless(os.environ.get('ECLIP_TEST_FFMPEG') and os.environ.get('ECLIP_TEST_FFPROBE'),
                     'pixel regression requires explicitly configured local FFmpeg tools')
class ColorRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ffmpeg = os.environ['ECLIP_TEST_FFMPEG']
        self.ffprobe = os.environ['ECLIP_TEST_FFPROBE']
        self.media = MediaAdapter({'ffmpeg_path': self.ffmpeg, 'ffprobe_path': self.ffprobe},
                                  self.root / 'media.log')

    def tearDown(self):
        assert self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        self.temp.cleanup()

    def source(self, full):
        # Three flat bands have known Y/U/V bytes, independently of color-name conversion.
        values = ([0, 128, 255], [32, 128, 224], [224, 128, 32]) if full else (
                  [16, 126, 235], [44, 128, 212], [212, 128, 44])
        frame = b''
        for plane, (width, height) in zip(values, ((90, 160), (45, 80), (45, 80))):
            row = b''.join(bytes([value]) * (width // 3) for value in plane)
            frame += row * height
        raw = self.root / 'source.yuv'
        raw.write_bytes(frame)
        source = self.root / 'source.mp4'
        subprocess.run([self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                        '-stream_loop', '-1', '-f', 'rawvideo', '-pixel_format', 'yuv420p',
                        '-video_size', '90x160', '-framerate', '30',
                        '-color_range', 'pc' if full else 'tv', '-colorspace', 'bt709',
                        '-color_primaries', 'bt709', '-color_trc', 'bt709', '-i', str(raw),
                        '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '2',
                        '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p',
                        '-color_range', 'pc' if full else 'tv', '-colorspace', 'bt709',
                        '-color_primaries', 'bt709', '-color_trc', 'bt709',
                        '-c:a', 'aac', str(source)], check=True, capture_output=True)
        self.assert_pixels(source, values, full=full)
        return source

    def assert_pixels(self, path, expected, *, full=False):
        result = subprocess.run([self.ffprobe, '-v', 'error', '-select_streams', 'v:0',
                                 '-show_entries', 'stream=width,height,pix_fmt,color_range,color_space',
                                 '-of', 'json', str(path)], check=True, capture_output=True)
        stream = json.loads(result.stdout)['streams'][0]
        # Do not specify an output pixel format: that could silently convert the sampled bytes.
        raw = subprocess.run([self.ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:v:0',
                              '-frames:v', '1', '-c:v', 'rawvideo', '-f', 'rawvideo', '-'],
                             check=True, capture_output=True).stdout
        width, height = stream['width'], stream['height']
        self.assertEqual(len(raw), width * height * 3 // 2)
        offset = 0
        for label, values, (w, h) in zip(('Y', 'U', 'V'), expected,
                                        ((width, height), (width // 2, height // 2),
                                         (width // 2, height // 2))):
            for band, value in enumerate(values):
                sampled = raw[offset + (h // 4) * w + (2 * band + 1) * w // 6]
                self.assertAlmostEqual(sampled, value, delta=2,
                                       msg=f'{path.name} {label} band {band}: actual byte range')
            offset += w * h
        self.assertEqual(stream['pix_fmt'], 'yuvj420p' if full else 'yuv420p')
        self.assertEqual(stream['color_range'], 'pc' if full else 'tv')
        self.assertEqual(stream['color_space'], 'bt709', 'range conversion must not change matrix')

    def exercise(self, *, full, review, resize=False):
        source = self.source(full)
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        meta = self.media.probe(source)
        output = self.root / 'source-cut.mp4'
        if review:
            binding = {'window': {'path': str(source)}, 'window_range': [0, 1.3]}
            record = {'segments': [{'audio': binding, 'visual': binding, 'speed': 1.3,
                                     'frame_count': 30, 'crop': None}],
                      'end_hold': {'frame_count': 18}, 'frame_count': 48, 'duration_seconds': 1.6}
            review_cut.render(self.media, record, output)
            expected_duration, size = 1.6, [1080, 1920]
        else:
            result = self.media.render(source, [(0, .5), (1, 1.5)], meta, 'pad', [.5, .5],
                                       [180, 320] if resize else [90, 160], output,
                                       exact_size=resize)
            expected_duration, size = 1, result['size']
        limited = ([16, 126, 235], [44, 128, 212], [212, 128, 44])
        self.assert_pixels(output, limited)
        _, checks = self.media.check(output, expected_duration, size, delivery=True)
        self.assertTrue(all(checks.values()))
        # Rehearsal's ASS burn re-encodes this source-cut. Its metadata and samples must stay limited.
        ass = self.root / 'test.ass'
        ass.write_text('[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n'
                       '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, Alignment\n'
                       'Style: Default,Arial,32,&H00FFFFFF,2\n'
                       '[Events]\nFormat: Layer, Start, End, Style, Text\n'
                       'Dialogue: 0,0:00:00.00,0:00:02.00,Default,range test\n', encoding='utf-8')
        from subtitle import filter_path
        subtitled = self.root / 'subtitled.mp4'
        self.media.write_media(['-i', str(output), '-map', '0:v:0', '-map', '0:a:0',
                                '-vf', 'ass=filename=' + filter_path(ass),
                                '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                                '-pix_fmt', 'yuv420p', '-c:a', 'copy', str(subtitled)],
                               subtitled, delivery=True)
        self.assert_pixels(subtitled, limited)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)

    def test_review_cut_converts_full_range_samples_and_subtitle_output(self):
        self.exercise(full=True, review=True)

    def test_review_cut_keeps_limited_samples_without_double_compression(self):
        self.exercise(full=False, review=True)

    def test_standard_cut_converts_full_range_without_resize(self):
        self.exercise(full=True, review=False)

    def test_standard_cut_keeps_limited_samples_when_resizing(self):
        self.exercise(full=False, review=False, resize=True)


if __name__ == '__main__':
    unittest.main()
