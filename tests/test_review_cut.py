"""Review-only cuts preserve source provenance without manufacturing approvals."""
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))


class ReviewCutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {'ECLIP_WORKSPACE': str(self.workspace)})
        self.env.start()
        for name in ('runtime', 'selector_support', 'quality_support', 'run_records', 'source_windows'):
            importlib.reload(importlib.import_module(name))
        self.runtime = importlib.import_module('runtime')
        self.windows = importlib.import_module('source_windows')
        self.support = importlib.import_module('selector_support')
        self.source = self.root / 'original/source.mp4'
        self.source.parent.mkdir()
        self.source.write_bytes(b'synthetic original source; not real merchant media')
        self.reference = self.root / 'reference.txt'
        self.reference.write_text('synthetic reference', encoding='utf-8')
        self.card = self.workspace / 'outputs/source/run-state.json'
        self.context = {'task_id': 'source', 'scope': 'test', 'profile': self.runtime.profile(),
                        'source_video': str(self.source), 'source_sha256': self.support.file_hash(self.source),
                        'content_strategy': {'version': 'single-style-batch-v1'},
                        'product': {'reference': {'path': str(self.reference),
                                                 'sha256': self.support.file_hash(self.reference)}},
                        'confirmed_windows': [], 'source_review': {'conclusion': 'unknown'},
                        'cross_device_state': 'local_records_checked'}
        self.save(self.card, {'status': 'NOT_UPLOADED_NOT_SUBMITTED', 'submitted': False,
                             'task_context': self.context})
        self.extraction = self.workspace / 'private-context/source-windows/source/window-unit/extraction.json'
        window = self.extraction.parent / 'window.mp4'
        window.parent.mkdir(parents=True)
        window.write_bytes(b'synthetic source window')
        self.extracted = {'schema_version': 1, 'task_id': 'source', 'task_record': str(self.card),
                          'source': {'path': str(self.source), 'sha256': self.support.file_hash(self.source),
                                     'duration_seconds': 100},
                          'window': {'path': str(window), 'sha256': self.support.file_hash(window),
                                     'duration_seconds': 10},
                          'requested_range': [10, 20], 'source_span': [10, 20], 'offset_seconds': 10,
                          'frame_grid': {'expected_step_seconds': 1 / 30, 'aligned_start_seconds': 10},
                          'review_status': 'unknown'}
        self.save(self.extraction, self.extracted)
        self.spec_path = self.workspace / 'private-context/cut.json'
        self.spec = {'schema_version': 1, 'task_record': str(self.card),
                     'clips': [{'extraction': str(self.extraction), 'window_range': [1, 2],
                                'speed': 1.3, 'purpose': '原声开头', 'questions': ['原句待核词']}],
                     'end_hold_seconds': .6}
        self.save(self.spec_path, self.spec)

    def tearDown(self):
        self.env.stop()
        assert self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        self.temp.cleanup()

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def api(self):
        try:
            return importlib.reload(importlib.import_module('review_cut'))
        except ModuleNotFoundError:
            self.fail('review-cut stage is not implemented yet')

    def test_plan_keeps_unknown_review_and_maps_rounded_segments(self):
        before = self.card.read_bytes()
        plan = self.api().plan(self.spec_path)
        self.assertEqual(plan['status'], 'unreviewed_review_cut')
        self.assertEqual(plan['frame_count'], 42)
        self.assertEqual(plan['duration_seconds'], 1.4)
        segment = plan['segments'][0]
        self.assertEqual(segment['video_range'], [0, .8])
        self.assertEqual(segment['audio']['source_range'], [11, 12])
        self.assertEqual(segment['visual']['source_range'], [11, 12])
        self.assertFalse(segment['non_synchronous_visual'])
        self.assertGreater(segment['audio_padding_seconds'], 0)
        self.assertLess(segment['audio_padding_seconds'], 1 / 30)
        self.assertEqual(segment['questions'], ['原句待核词'])
        self.assertTrue(all(v == 'not_reviewed' for v in plan['approvals'].values()))
        self.assertEqual(self.card.read_bytes(), before)
        self.assertFalse((self.workspace / 'outputs/review-cuts').exists())

    def test_visual_override_has_separate_source_map_and_duration_check(self):
        self.spec['clips'][0]['visual'] = {'extraction': str(self.extraction), 'window_range': [5, 7]}
        self.save(self.spec_path, self.spec)
        segment = self.api().plan(self.spec_path)['segments'][0]
        self.assertTrue(segment['non_synchronous_visual'])
        self.assertEqual(segment['audio']['source_range'], [11, 12])
        self.assertAlmostEqual(segment['visual']['source_range'][0], 15)
        self.assertAlmostEqual(segment['visual']['source_range'][1], 16.04)
        self.spec['clips'][0]['visual']['window_range'] = [5, 5.5]
        self.save(self.spec_path, self.spec)
        with self.assertRaisesRegex(ValueError, '画面.*覆盖'):
            self.api().plan(self.spec_path)

    def test_invalid_ranges_speeds_holds_crops_and_total_length_are_rejected(self):
        cases = []
        for field, value in [('speed', True), ('speed', 1.6), ('speed', float('nan')),
                             ('window_range', [-1, 2]), ('window_range', [1, 11]),
                             ('purpose', ''), ('crop', {'x': -2, 'y': 0, 'width': 10, 'height': 10})]:
            spec = copy.deepcopy(self.spec)
            spec['clips'][0][field] = value
            cases.append(spec)
        spec = copy.deepcopy(self.spec)
        spec['end_hold_seconds'] = 3
        cases.append(spec)
        spec = copy.deepcopy(self.spec)
        spec['clips'] *= 100
        cases.append(spec)
        for spec in cases:
            with self.subTest(spec=spec):
                self.save(self.spec_path, spec)
                with self.assertRaises(ValueError):
                    self.api().plan(self.spec_path)
        self.assertFalse((self.workspace / 'outputs/review-cuts').exists())

    def test_extraction_must_belong_to_original_task_and_keep_hashes(self):
        cases = [dict(self.extracted, task_record=str(self.card.with_name('other.json'))),
                 dict(self.extracted, task_id='other'),
                 dict(self.extracted, source={**self.extracted['source'], 'sha256': '0' * 64}),
                 dict(self.extracted, window={**self.extracted['window'], 'sha256': '0' * 64})]
        for extracted in cases:
            with self.subTest(extracted=extracted):
                self.save(self.extraction, extracted)
                with self.assertRaises(ValueError):
                    self.api().plan(self.spec_path)

    def test_protected_or_adopted_task_cannot_create_review_cut(self):
        for fields in ({'submitted': True}, {'submitted': None}, {'status': 'SUBMIT_UNKNOWN'},
                       {'task_context': {**self.context, 'batch_ref': {'item_id': 'adopted'}}}):
            with self.subTest(fields=fields):
                self.save(self.card, {'status': 'NOT_UPLOADED_NOT_SUBMITTED', 'submitted': False,
                                     'task_context': self.context, **fields})
                with self.assertRaises(ValueError):
                    self.api().plan(self.spec_path)

    def test_spec_outside_private_context_and_existing_output_are_rejected(self):
        outside = self.root / 'outside.json'
        self.save(outside, self.spec)
        with self.assertRaises(ValueError):
            self.api().plan(outside)
        output = self.workspace / 'outputs/review-cuts/existing'
        output.mkdir(parents=True)
        sentinel = output / 'preserve.txt'
        sentinel.write_bytes(b'preserve existing output')
        with self.assertRaises(FileExistsError):
            self.api().run(self.spec_path, 'existing')
        self.assertEqual(sentinel.read_bytes(), b'preserve existing output')

    def test_inputs_changed_after_planning_invalidate_preview(self):
        api = self.api()
        planned = api.plan(self.spec_path)
        self.spec['clips'][0]['purpose'] = '输入已被修改'
        self.save(self.spec_path, self.spec)
        with self.assertRaisesRegex(ValueError, '发生变化'):
            api.verify_inputs(planned)

    def test_media_failure_keeps_failed_technical_record_and_unreviewed_approvals(self):
        api = self.api()
        with patch.object(api, 'MediaAdapter', side_effect=RuntimeError('synthetic media error')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic media error'):
                api.run(self.spec_path, 'failed-media')
        output = self.workspace / 'outputs/review-cuts/failed-media'
        preview = json.loads((output / 'preview.json').read_text('utf-8'))
        technical = json.loads((output / 'technical.json').read_text('utf-8'))
        self.assertEqual(preview['render_status'], 'failed')
        self.assertEqual(technical['status'], 'failed')
        self.assertNotIn('video', preview)
        self.assertTrue(all(v == 'not_reviewed' for v in preview['approvals'].values()))

    def test_cli_routes_review_cut_help(self):
        cli = Path(__file__).resolve().parents[1] / 'cli.py'
        result = subprocess.run([sys.executable, '-B', str(cli), '--workspace', str(self.workspace),
                                 'review-cut', '--help'], capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--spec', result.stdout)

    @unittest.skipUnless(os.environ.get('ECLIP_TEST_FFMPEG') and os.environ.get('ECLIP_TEST_FFPROBE'),
                         'real media smoke requires explicitly configured local FFmpeg tools')
    def test_real_media_has_exact_frames_silent_hold_and_no_approval_mutation(self):
        ffmpeg, ffprobe = os.environ['ECLIP_TEST_FFMPEG'], os.environ['ECLIP_TEST_FFPROBE']
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                        '-f', 'lavfi', '-i', 'testsrc2=size=96x160:rate=30:duration=6',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=6',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(self.source)],
                       check=True, capture_output=True)
        self.context['source_sha256'] = self.support.file_hash(self.source)
        self.save(self.card, {'status': 'NOT_UPLOADED_NOT_SUBMITTED', 'submitted': False,
                             'task_context': self.context})
        self.save(self.workspace / 'config.json', {'ffmpeg_path': ffmpeg, 'ffprobe_path': ffprobe})
        extraction = self.windows.extract(self.card, 0, 5)['extraction']
        self.spec['clips'][0]['extraction'] = extraction
        self.spec['clips'][0]['visual'] = {'extraction': extraction, 'window_range': [3, 5]}
        self.spec['clips'][0]['crop'] = {'x': 0, 'y': 0, 'width': 96, 'height': 160}
        self.spec['clips'].append({'extraction': extraction, 'window_range': [2, 3],
                                   'speed': 1.3, 'purpose': '第二段同期画面'})
        self.save(self.spec_path, self.spec)
        before = self.card.read_bytes()
        result = self.api().run(self.spec_path, 'real-smoke')
        output = Path(result['output'])
        preview = json.loads((output / 'preview.json').read_text('utf-8'))
        technical = json.loads((output / 'technical.json').read_text('utf-8'))
        self.assertEqual(preview['frame_count'], 66)
        self.assertEqual(technical['metadata']['frame_timing']['frame_count'], 66)
        self.assertEqual(preview['segments'][1]['video_range'], [.8, 1.6])
        self.assertEqual(technical['metadata']['width'], 1080)
        self.assertEqual(technical['metadata']['height'], 1920)
        self.assertTrue(all(v == 'not_reviewed' for v in preview['approvals'].values()))
        self.assertEqual(self.card.read_bytes(), before)
        self.assertEqual(len(list(output.glob('frame-*.png'))), 3)
        self.assertFalse((output / 'result.json').exists())
        pcm = subprocess.run([ffmpeg, '-v', 'error', '-ss', '1.8', '-i', str(output / 'preview.mp4'),
                              '-t', '0.3', '-vn', '-ac', '1', '-f', 's16le', '-'],
                             check=True, capture_output=True).stdout
        import array
        samples = array.array('h', pcm)
        self.assertTrue(samples)
        self.assertLessEqual(max(abs(n) for n in samples), 4, 'hold must contain silence')
        pitched = subprocess.run([ffmpeg, '-v', 'error', '-ss', '0.1', '-i', str(output / 'preview.mp4'),
                                  '-t', '0.4', '-vn', '-ac', '1', '-f', 's16le', '-'],
                                 check=True, capture_output=True).stdout
        tone = array.array('h', pitched)
        crossings = sum(a <= 0 < b for a, b in zip(tone, tone[1:]))
        self.assertAlmostEqual(crossings / (len(tone) / 48000), 440, delta=5,
                               msg='1.3x audio must preserve the original tone pitch')
        content_frames = preview['frame_count'] - preview['end_hold']['frame_count']
        self.assertEqual(content_frames, 48)
        decoded = subprocess.run([ffmpeg, '-v', 'error', '-i', str(output / 'preview.mp4'),
                                  '-an', '-vf', 'scale=16:16,format=rgb24', '-f', 'rawvideo', '-'],
                                 check=True, capture_output=True).stdout
        self.assertEqual(len(decoded), preview['frame_count'] * 768)
        frames = [decoded[i:i + 768] for i in range(0, len(decoded), 768)]

        def assert_encoded_hold(candidate):
            reference = candidate[content_frames - 1]
            for frame in candidate[content_frames:]:
                errors = [abs(a - b) for a, b in zip(reference, frame)]
                # This 16x16 testsrc2 fixture has small CRF18 quantization changes, not motion.
                self.assertLessEqual(sum(errors) / len(errors), .1)
                self.assertLessEqual(max(errors), 4)

        assert_encoded_hold(frames)
        for label, tail in (
                ('black tail', [bytes(768)] * 18),
                ('wrong frozen frame', [frames[0]] * 18),
                ('continued motion', frames[:18])):
            with self.subTest(encoded_negative=label), self.assertRaises(AssertionError):
                assert_encoded_hold(frames[:content_frames] + tail)

        api = self.api()
        media = api.MediaAdapter({'ffmpeg_path': ffmpeg, 'ffprobe_path': ffprobe}, output / 'lossless.log')
        write_media = media.write_media

        def write_lossless(args, destination, **kwargs):
            args = list(args)
            args[args.index('-crf') + 1] = '0'
            return write_media(args, destination, **kwargs)

        baseline = None
        for hold_frames in (18, 0, 1):
            with self.subTest(lossless_hold_frames=hold_frames):
                record = copy.deepcopy(preview)
                record['end_hold'].update(frame_count=hold_frames, seconds=hold_frames / 30,
                                          video_range=[content_frames / 30, (content_frames + hold_frames) / 30])
                record['frame_count'] = content_frames + hold_frames
                record['duration_seconds'] = record['frame_count'] / 30
                video = output / f'lossless-hold-{hold_frames}.mp4'
                # Keep the production filter chain; exact frame equality belongs before lossy coding.
                with patch.object(media, 'write_media', side_effect=write_lossless):
                    api.render(media, record, video)
                digest = subprocess.run([ffmpeg, '-v', 'error', '-i', str(video), '-map', '0:v:0',
                                         '-f', 'framemd5', '-'], check=True, capture_output=True, text=True).stdout
                hashes = [row.rsplit(',', 1)[1].strip() for row in digest.splitlines() if not row.startswith('#')]
                self.assertEqual(len(hashes), record['frame_count'])
                if baseline is None:
                    baseline = hashes[:content_frames]
                    self.assertGreater(len(set(baseline)), 1, 'fixture must contain actual motion')
                self.assertEqual(hashes[:content_frames], baseline, 'hold must not change content frames')
                self.assertEqual(hashes[content_frames - 1:], [baseline[-1]] * (hold_frames + 1),
                                 'every full-resolution hold frame must clone the final content frame')


if __name__ == '__main__':
    unittest.main()
