"""Explicit human evidence is required to finish an accepted draft."""
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))


class ReviewedDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'ECLIP_WORKSPACE': str(self.root)})
        self.env.start()
        for name in ('runtime', 'selector_support', 'quality_support', 'review_cut', 'review_card', 'subtitle', 'rehearsal'):
            importlib.reload(importlib.import_module(name))
        self.support = importlib.import_module('selector_support')
        self.video = self.root / 'outputs/reviews/rehearsals/test/draft-video.mp4'
        self.video.parent.mkdir(parents=True)
        self.video.write_bytes(b'synthetic video fixture, not a media integration test')
        self.private = self.root / 'private-context'
        self.private.mkdir()
        self.raw = self.private / 'raw.txt'
        self.raw.write_text('已完整听看并核对原速片段；字幕和同款无误。', encoding='utf-8')
        self.overall = self.private / 'overall.txt'
        self.overall.write_text('可以接受', encoding='utf-8')
        self.record = {'video': self.ref(self.video), 'cut': {'duration_seconds': 56.0,
                       'source': {'sha256': 'a' * 64}}}
        self.feedback = {'review_status': 'pending', 'review_state_changed': False,
                         'version': 'v5', 'media': self.ref(self.video), 'range': [0, 56.0],
                         'question': None, 'raw_text_file': self.ref(self.raw),
                         'raw_text': self.raw.read_text('utf-8'),
                         'source_binding': {'source_sha256': 'a' * 64}}
        self.approval = {'actor': 'test user', 'at': '2026-10-20T12:00:00+08:00',
                         'confirmed_items': ['same_product', 'transcript', 'continuous_visual',
                                             'audio_listening', 'user_confirmation'],
                         'confirmation_quote': self.feedback['raw_text'],
                         'acceptance': {**self.ref(self.overall), 'quote': '可以接受'}}

    def tearDown(self):
        self.env.stop()
        # These modules hold workspace paths at import time; restore them before
        # another test class reuses the already-imported module objects.
        for name in ('runtime', 'selector_support', 'quality_support', 'review_cut', 'review_card', 'subtitle', 'rehearsal'):
            importlib.reload(importlib.import_module(name))
        self.temp.cleanup()

    def ref(self, path):
        return {'path': str(path), 'sha256': self.support.file_hash(path)}

    def api(self):
        module = Path(__file__).resolve().parents[1] / 'pipeline/reviewed_delivery.py'
        self.assertTrue(module.exists(), 'missing generic accepted-delivery implementation')
        return importlib.reload(importlib.import_module('reviewed_delivery'))

    def test_complete_explicit_confirmation_accepts_without_mutating_draft(self):
        before = copy.deepcopy(self.record)
        result = self.api().validate_acceptance(self.record, self.feedback, self.approval)
        self.assertEqual(result['scope'], 'accepted_video_and_retained_segments_only')
        self.assertEqual(result['actor'], 'test user')
        self.assertEqual(self.record, before)

    def test_overall_preference_does_not_fill_missing_content_reviews(self):
        for items in ([], ['user_confirmation'], ['same_product', 'transcript'],
                      self.approval['confirmed_items'] + ['user_confirmation']):
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.api().validate_acceptance(self.record, self.feedback,
                                               dict(self.approval, confirmed_items=items))

    def test_windows_multiline_feedback_preserves_original_line_endings(self):
        raw = '已完整听看。\r\n字幕和同款无误。\r\n'
        accepted = '已看完\r\n可以接受\r\n'
        for encoding in ('utf-8', 'utf-8-sig'):
            with self.subTest(encoding=encoding):
                self.raw.write_bytes(raw.encode(encoding))
                self.overall.write_bytes(accepted.encode(encoding))
                feedback = dict(self.feedback, raw_text_file=self.ref(self.raw), raw_text=raw)
                approval = dict(self.approval, confirmation_quote=raw.rstrip(),
                                acceptance={**self.ref(self.overall), 'quote': accepted.rstrip()})
                self.api().validate_acceptance(self.record, feedback, approval)
                self.assertEqual(self.raw.read_bytes().decode('utf-8-sig'), raw)

    def test_partial_or_question_only_feedback_is_not_full_acceptance(self):
        for change in ({'range': [2, 56]}, {'range': [0, 55.9]},
                       {'question': {'id': 'Q1'}}, {'review_state_changed': True},
                       {'source_binding': {'source_sha256': 'b' * 64}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.api().validate_acceptance(self.record, dict(self.feedback, **change), self.approval)

    def test_shell_decimal_roundtrip_is_tolerated_but_audio_truncation_is_not(self):
        self.record['cut']['duration_seconds'] = 56.03333333333333
        feedback = dict(self.feedback, range=[0.0, 56.0333333333333])
        self.api().validate_acceptance(self.record, feedback, self.approval)
        for span in ([0, 56.0332], [False, 56.03333333333333], [0, float('nan')]):
            with self.subTest(span=span), self.assertRaises(ValueError):
                self.api().validate_acceptance(self.record, dict(feedback, range=span), self.approval)

    def test_changed_video_or_raw_message_invalidates_confirmation(self):
        self.video.write_bytes(b'different video')
        with self.assertRaises(ValueError):
            self.api().validate_acceptance(self.record, self.feedback, self.approval)
        self.video.write_bytes(b'synthetic video fixture, not a media integration test')
        self.raw.write_text('只同意继续优化', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.api().validate_acceptance(self.record, self.feedback, self.approval)

    def test_quote_actor_and_timestamp_are_required(self):
        for change in ({'actor': ''}, {'at': 'yesterday'}, {'at': '2026-10-20T12:00:00'},
                       {'confirmation_quote': '用户未说过这句话'},
                       {'acceptance': {**self.ref(self.overall), 'quote': '全部商品都正确'}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.api().validate_acceptance(self.record, self.feedback, dict(self.approval, **change))

    def test_only_exact_draft_watermark_line_is_removed(self):
        api = self.api()
        watermark = api.watermark_line(56.0)
        body = '[Events]\nDialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,原话保留\n'
        self.assertEqual(api.delivery_ass(body + watermark, 56.0), body)
        for invalid in (body, body + watermark + watermark, body + watermark.replace('56.00', '55.00')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                api.delivery_ass(invalid, 56.0)

    def test_copy_quotes_must_match_accepted_subtitles(self):
        api = self.api()
        record = {**self.record, 'subtitle_cues': [{'text': '下面可以搭一条牛仔裤或者白裤子'}]}
        draft = {'title': '白色开衫的日常搭配', 'caption': '搭牛仔裤或白裤子，看看白色开衫的日常搭配。',
                 'hashtags': ['#女装', '#开衫穿搭', '#白色穿搭'], 'cover_lines': ['白色开衫', '日常搭配'],
                 'evidence': [{'cue': 1, 'quote': '牛仔裤或者白裤子'}],
                 'reviewer': 'test operator', 'reviewed_at': '2026-10-20T12:00:00+08:00',
                 'finding': '标题与文案只使用已确认搭配内容。'}
        self.assertEqual(api.validate_copy(record, draft), draft)
        draft['evidence'][0]['quote'] = '全网销量第一'
        with self.assertRaises(ValueError):
            api.validate_copy(record, draft)

    def test_existing_delivery_and_traversal_are_rejected(self):
        folder = self.root / 'outputs/deliveries/existing'
        folder.mkdir(parents=True)
        marker = folder / 'video.mp4'
        marker.write_bytes(b'keep existing')
        with self.assertRaises(FileExistsError):
            self.api().output_path('existing')
        self.assertEqual(marker.read_bytes(), b'keep existing')
        for name in ('../outside', 'a/b', ''):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.api().output_path(name)

    def delivery_fixture(self):
        folder = self.root / 'outputs/deliveries/check-unit'
        folder.mkdir(parents=True)
        for name in ('video.mp4', 'source-cut.mp4', 'subtitle.srt', 'subtitle.ass', 'cover-3x4.jpg',
                     'cover-4x3.jpg', 'title.txt', 'caption.txt', 'hashtags.txt', 'index.html', 'changes.md'):
            (folder / name).write_bytes(b'synthetic output')
        quality = {'new_video_visual_review': 'not_reviewed', 'audio_bitstream_identical': True,
                   'source_cut_byte_identical': True, 'subtitle_text_and_timing_unchanged': True,
                   'platform_ui_verified': False, 'uploaded': False, 'submitted': False}
        (folder / 'quality.json').write_text(json.dumps(quality), encoding='utf-8')
        result = {'schema_version': 1, 'kind': 'reviewed_local_delivery',
                  'status': 'LOCAL_DELIVERY_NOT_SUBMITTED', 'package': str(folder), 'is_draft': False,
                  'inputs': {}, 'files': {p.name: self.support.file_hash(p) for p in folder.iterdir()},
                  'video': self.ref(folder / 'video.mp4'), 'quality': quality,
                  'platform_ui_verified': False, 'uploaded': False, 'submitted': False}
        (folder / 'delivery.json').write_text(json.dumps(result), encoding='utf-8')
        return folder, result

    def test_manifest_rejects_extra_nested_receipt_and_missing_subtitles(self):
        folder, result = self.delivery_fixture()
        self.api().check(folder)
        nested = folder / 'unexpected/delivery.json'
        nested.parent.mkdir()
        nested.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.api().check(folder)

    def test_manifest_cannot_omit_required_deliverable_or_conflict_with_quality(self):
        folder, result = self.delivery_fixture()
        del result['files']['subtitle.srt']
        (folder / 'subtitle.srt').unlink()
        (folder / 'delivery.json').write_text(json.dumps(result), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.api().check(folder)


@unittest.skipUnless(all(os.environ.get(k) for k in
                         ('ECLIP_TEST_FFMPEG', 'ECLIP_TEST_FFPROBE', 'ECLIP_TEST_FONT')),
                     'requires explicit local FFmpeg and Chinese font; synthetic media only')
class ReviewedDeliveryMediaTests(unittest.TestCase):
    def test_cli_roundtrip_in_fresh_workspace_preserves_source_and_accepted_text(self):
        from test_rehearsal import RehearsalTests
        fixture = RehearsalTests()
        fixture.setUp()
        try:
            ffmpeg, ffprobe, font = (os.environ[k] for k in
                                    ('ECLIP_TEST_FFMPEG', 'ECLIP_TEST_FFPROBE', 'ECLIP_TEST_FONT'))
            subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                            '-f', 'lavfi', '-i', 'testsrc2=size=96x160:rate=30:duration=6',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=6',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(fixture.source)],
                           check=True, capture_output=True)
            fixture.context['source_sha256'] = fixture.support.file_hash(fixture.source)
            fixture.save(fixture.card, {'status': 'NOT_UPLOADED_NOT_SUBMITTED', 'submitted': False,
                                       'task_context': fixture.context})
            fixture.save(fixture.workspace / 'config.json', {'ffmpeg_path': ffmpeg, 'ffprobe_path': ffprobe,
                                                             'font_path': font})
            extraction = Path(fixture.windows.extract(fixture.card, 0, 5)['extraction'])
            fixture.preview = extraction.parent / 'asr-preview-test.json'
            fixture.bind_asr(extraction, fixture.machine_text)
            fixture.spec['clips'][0]['extraction'] = str(extraction)
            fixture.save(fixture.spec_path, fixture.spec)
            fixture.request['asr_previews'] = [str(fixture.preview)]
            fixture.request['highlight_terms'] = ['机器', '原文']
            fixture.save(fixture.rehearsal, fixture.request)
            fixture.api()
            auto = importlib.reload(importlib.import_module('auto_workflow'))
            auto_request = {'run_id': 'synthetic-auto', 'task_record': str(fixture.card),
                            'windows': [{'start': 0, 'end': 5}],
                            'rehearsal_spec': str(fixture.rehearsal)}
            # Only ASR is a synthetic fixture: this source is a sine tone, not
            # speech. Extraction reuse, rendering and state persistence are real.
            with patch.object(auto.source_windows, 'transcribe_preview',
                              return_value={'preview_path': str(fixture.preview)}):
                pending = auto.run(auto_request)
            self.assertEqual(pending['status'], 'needs_content_review')
            self.assertEqual(pending['human_review'], 'not_reviewed')
            package = Path(pending['rehearsal']['package'])
            record = fixture.support.read(package / 'rehearsal.json')
            original = {p: fixture.support.file_hash(p) for p in (fixture.source, fixture.card,
                        package / 'rehearsal.json', package / 'draft-video.mp4')}
            private = fixture.workspace / 'private-context'
            raw = private / 'synthetic-confirmation.txt'
            raw.write_text('合成测试夹具：声明完整内容确认。此记录不是实际用户或听审证据。', encoding='utf-8')
            overall = private / 'synthetic-acceptance.txt'
            overall.write_text('合成测试夹具接受，仅供自动化测试。', encoding='utf-8')
            feedback = private / 'synthetic-feedback.json'
            cli = Path(__file__).resolve().parents[1] / 'cli.py'

            def call(*args):
                completed = subprocess.run([sys.executable, '-B', str(cli), '--workspace',
                                            str(fixture.workspace), *map(str, args)],
                                           capture_output=True, text=True, encoding='utf-8',
                                           timeout=180, env={**os.environ, 'PYTHONUTF8': '1'})
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                return json.loads(completed.stdout)

            call('review-card', 'save-feedback', '--card', package / 'review-1/index.html',
                 '--feedback-text', raw, '--version', 'draft', '--range', 0,
                 record['cut']['duration_seconds'], '--output', feedback)
            spec = {'schema_version': 1, 'rehearsal': str(package), 'feedback': str(feedback),
                    'approval': {'actor': 'synthetic_test_fixture_not_a_real_user',
                                 'at': '2026-10-20T12:00:00+08:00',
                                 'confirmed_items': ['same_product', 'transcript', 'continuous_visual',
                                                     'audio_listening', 'user_confirmation'],
                                 'confirmation_quote': raw.read_text('utf-8'),
                                 'acceptance': {'path': str(overall), 'sha256': fixture.support.file_hash(overall),
                                                'quote': overall.read_text('utf-8')}},
                    'copy': {'title': '合成媒体技术测试', 'caption': '这里只验证技术流程，不是商家内容或真实用户验收。',
                             'hashtags': ['#合成测试', '#技术验证', '#非业务验收'],
                             'cover_lines': ['合成技术测试'],
                             'evidence': [{'cue': 1, 'quote': fixture.machine_text}],
                             'reviewer': 'synthetic_test_fixture', 'reviewed_at': '2026-10-20T12:00:00+08:00',
                             'finding': 'Synthetic color bars, sine tone and fixture transcript; no actual user.'}}
            spec_path = private / 'delivery-spec.json'
            fixture.save(spec_path, spec)
            auto_request['delivery_spec'] = str(spec_path)
            request_path = private / 'auto-request.json'
            fixture.save(request_path, auto_request)
            report = call('auto', 'run', request_path)
            self.assertEqual(report['status'], 'local_delivery_ready')
            self.assertEqual(report['human_review'], 'prior_accepted_content_preserved')
            self.assertEqual(report['new_visual_review'], 'not_reviewed')
            result = report['delivery']
            self.assertEqual(result['status'], 'LOCAL_DELIVERY_NOT_SUBMITTED')
            final = Path(result['package'])
            output_stamps = {p: (p.stat().st_mtime_ns, fixture.support.file_hash(p))
                             for p in final.rglob('*') if p.is_file()}
            self.assertEqual(call('auto', 'run', request_path), report)
            self.assertEqual(call('auto', 'status', 'synthetic-auto'), report)
            self.assertEqual(output_stamps, {p: (p.stat().st_mtime_ns, fixture.support.file_hash(p))
                                           for p in final.rglob('*') if p.is_file()})
            self.assertEqual(report['current_artifact']['video'], str(final / 'video.mp4'))
            self.assertEqual(call('delivery', 'check', '--package', final)['status'], result['status'])
            quality = fixture.support.read(final / 'quality.json')
            self.assertTrue(quality['source_cut_byte_identical'])
            self.assertTrue(quality['audio_bitstream_identical'])
            self.assertEqual((final / 'subtitle.srt').read_bytes(), (package / 'draft-subtitle.srt').read_bytes())
            self.assertNotIn('待审演练', (final / 'subtitle.ass').read_text('utf-8'))
            self.assertFalse(quality['uploaded'])
            self.assertFalse(quality['submitted'])
            self.assertEqual(original, {p: fixture.support.file_hash(p) for p in original})
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
