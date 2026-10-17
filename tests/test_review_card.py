"""Review cards bind existing local media without manufacturing review evidence."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

CLI = Path(__file__).resolve().parents[1] / 'cli.py'


class ReviewCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.folder = self.workspace / 'private-context/source-windows/task-one/window-test'
        self.folder.mkdir(parents=True)
        self.source = self.root / 'original.mp4'
        self.source.write_bytes(b'synthetic original; not playable')
        self.window = self.folder / 'window.mp4'
        self.window.write_bytes(b'synthetic short window; not playable')
        self.poster = self.folder / 'frame-2.png'
        self.poster.write_bytes(b'synthetic source frame')
        self.card = self.workspace / 'outputs/task-one/run-state.json'
        self.save(self.card, {'status': 'NOT_UPLOADED_NOT_SUBMITTED', 'submitted': False,
                            'task_context': {'source_review': {'conclusion': 'unknown'}}})
        self.extraction = self.folder / 'extraction.json'
        self.extracted = {
            'schema_version': 1, 'task_id': 'task-one', 'task_record': str(self.card),
            'source': {'path': str(self.source), 'sha256': self.sha(self.source), 'duration_seconds': 100},
            'window': {'path': str(self.window), 'sha256': self.sha(self.window), 'duration_seconds': 10},
            'offset_seconds': 20, 'requested_range': [20, 30], 'source_span': [20, 30],
            'frame_grid': {'aligned_start_seconds': 20, 'expected_step_seconds': 1 / 30},
            'sample_frames': [{'path': str(self.poster), 'sha256': self.sha(self.poster),
                               'window_at': 5, 'source_at': 25}], 'review_status': 'unknown'}
        self.save(self.extraction, self.extracted)
        self.transcript = self.workspace / 'private-context/transcript.json'
        self.text = '<script>alert("ASR")</script> 原始错字'
        self.save(self.transcript, {
            'schema_version': 1, 'status': 'complete',
            'source': {'path': str(self.window), 'sha256': self.sha(self.window), 'duration_seconds': 10},
            'asr': {'config_hash': 'test-local-model'},
            'segments': [{'id': 's000001', 'start': 1, 'end': 4, 'text': self.text}]})
        self.preview = self.folder / 'asr-preview-test.json'
        self.preview_data = {
            'schema_version': 1, 'status': 'unreviewed_asr_preview',
            'extraction': {'path': str(self.extraction), 'sha256': self.sha(self.extraction)},
            'source_sha256': self.sha(self.source), 'window_sha256': self.sha(self.window),
            'source_span': [20, 30],
            'transcript_cache': {'path': str(self.transcript), 'sha256': self.sha(self.transcript)},
            'asr_config_hash': 'test-local-model',
            'segments': [{'id': 's000001', 'window_range': [1, 4], 'source_range': [21, 24],
                          'text': self.text}], 'same_product': 'unknown',
            'audio_listening': 'not_performed', 'transcript_verified': False}
        self.save(self.preview, self.preview_data)
        self.spec = self.workspace / 'private-context/review-spec.json'
        self.spec_data = {'extraction': str(self.extraction), 'asr_preview': str(self.preview),
                          'title': '</script><img src=x onerror=alert(1)>', 'poster': str(self.poster),
                          'proposed_segments': [{'label': '<b>拟片段</b>', 'window_range': [1, 5]}],
                          'questions': [{'id': 'Q1', 'window_range': [2, 4],
                                         'question': '<script>坏问题</script>'}]}
        self.save(self.spec, self.spec_data)
        self.output = self.workspace / 'outputs/reviews/card-one/index.html'

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def call(self, *arguments):
        arguments = arguments or ('--spec', str(self.spec), '--output', str(self.output))
        return subprocess.run([sys.executable, '-B', str(CLI), '--workspace', str(self.workspace),
                               'review-card', *arguments],
                              capture_output=True, text=True, encoding='utf-8',
                              env=dict(os.environ, PYTHONIOENCODING='utf-8'))

    def test_generates_bound_media_escaped_text_and_no_review_mutation(self):
        before = {p: p.read_bytes() for p in (self.card, self.extraction, self.preview, self.transcript)}
        result = self.call()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'unreviewed_review_card')
        self.assertEqual(self.sha(self.output.parent / 'review.mp4'), self.sha(self.window))
        self.assertEqual(self.sha(self.output.parent / 'poster.png'), self.sha(self.poster))
        self.assertEqual({p.name for p in self.output.parent.iterdir()},
                         {'index.html', 'review.mp4', 'poster.png', 'review-card.json'})
        page = self.output.read_text('utf-8')
        self.assertIn('&lt;script&gt;坏问题&lt;/script&gt;', page)
        self.assertIn('&lt;script&gt;alert(&quot;ASR&quot;)&lt;/script&gt;', page)
        self.assertNotIn('<img src=x', page)
        self.assertNotIn('<script>坏问题', page)
        self.assertNotIn(' autoplay', page)
        self.assertNotIn(' checked', page)
        self.assertIn('未听审', page)
        self.assertIn('不是最终成片', page)
        self.assertIn('原片 00:21.00', page)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        saved = self.output.read_bytes()
        self.assertNotEqual(self.call().returncode, 0)
        self.assertEqual(self.output.read_bytes(), saved)

    def test_rejects_stale_hash_wrong_mapping_or_changed_asr_before_writing(self):
        for field, value in [('source_sha256', '0' * 64), ('window_sha256', '0' * 64),
                             ('source_span', [21, 31]),
                             ('segments', [{'id': 's000001', 'window_range': [1, 4],
                                            'source_range': [21, 24], 'text': '被擅自修改'}])]:
            with self.subTest(field=field):
                self.save(self.preview, dict(self.preview_data, **{field: value}))
                self.assertNotEqual(self.call().returncode, 0)
                self.assertFalse(self.output.parent.exists())
        self.save(self.preview, self.preview_data)
        self.window.write_bytes(b'changed window')
        self.assertNotEqual(self.call().returncode, 0)
        self.assertFalse(self.output.parent.exists())
        self.window.write_bytes(b'synthetic short window; not playable')
        self.source.write_bytes(b'changed original')
        self.assertNotEqual(self.call().returncode, 0)
        self.assertFalse(self.output.parent.exists())

    def comparison(self):
        versions = []
        for number in (1, 2):
            path = self.workspace / f'outputs/draft-{number}/preview.mp4'
            path.parent.mkdir(parents=True)
            path.write_bytes(f'synthetic draft {number}; not playable'.encode())
            versions.append({'id': f'v{number}', 'label': f'第{number}版', 'path': str(path),
                             'sha256': self.sha(path), 'duration_seconds': 5,
                             'changes': ['原版参照' if number == 1 else '删重复句并调整结尾'],
                             'segments': [{'label': '开头', 'purpose': '展示商品形态',
                                           'window_range': [1, 5], 'video_range': [0, 3]}]})
        self.spec_data['versions'] = versions
        self.spec_data['questions'][0]['video_ranges'] = [
            {'version': 'v1', 'range': [1, 2]}, {'version': 'v2', 'range': [0, 1]}]
        self.save(self.spec, self.spec_data)
        return versions

    def test_comparison_copies_versions_and_binds_feedback_without_approving(self):
        versions = self.comparison()
        before = self.card.read_bytes()
        result = self.call()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.output.parent / 'review-card.json').is_file())
        manifest = json.loads((self.output.parent / 'review-card.json').read_text('utf-8'))
        self.assertEqual([v['id'] for v in manifest['versions']], ['v1', 'v2'])
        for index, version in enumerate(versions, 1):
            self.assertEqual(self.sha(self.output.parent / f'version-{index}.mp4'), version['sha256'])
        page = self.output.read_text('utf-8')
        self.assertIn('展示商品形态', page)
        self.assertIn('删重复句并调整结尾', page)
        self.assertIn('原速回听', page)
        self.assertIn('需要重新审核', page)
        self.assertIn('data-player="version-1"', page)
        raw = self.workspace / 'private-context/feedback.txt'
        raw_text = '  更喜欢第二版。\n结尾还不确定，没有完成整片听审。\n'
        raw.write_bytes(raw_text.encode('utf-8'))
        saved = self.workspace / 'private-context/feedback-v2.json'
        result = self.call('save-feedback', '--card', str(self.output), '--feedback-text', str(raw),
                           '--version', 'v2', '--range', '0', '5', '--output', str(saved))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(saved.read_text('utf-8'))
        self.assertEqual(record['raw_text'], raw_text)
        self.assertEqual(record['media']['sha256'], versions[1]['sha256'])
        self.assertEqual(record['version'], 'v2')
        self.assertEqual(record['range'], [0, 5])
        self.assertEqual(record['review_status'], 'pending')
        self.assertFalse(record['review_state_changed'])
        self.assertEqual(self.card.read_bytes(), before)
        self.assertNotEqual(self.call('save-feedback', '--card', str(self.output), '--feedback-text', str(raw),
                                     '--version', 'v2', '--range', '0', '5', '--output', str(saved)).returncode, 0)

    def test_changed_files_require_re_review_and_reject_new_feedback(self):
        versions = self.comparison()
        self.assertEqual(self.call().returncode, 0)
        self.assertTrue((self.output.parent / 'version-1.mp4').is_file())
        raw = self.workspace / 'private-context/feedback.txt'
        raw.write_text('偏好第二版', encoding='utf-8')
        saved = self.workspace / 'private-context/feedback-v2.json'
        for path in (Path(versions[1]['path']), self.output.parent / 'version-1.mp4',
                     self.spec, self.transcript, self.output):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b' changed')
                result = self.call('check', '--card', str(self.output))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('需要重新审核', result.stdout)
                result = self.call('save-feedback', '--card', str(self.output), '--feedback-text', str(raw),
                                   '--version', 'v2', '--range', '0', '5', '--output', str(saved))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(saved.exists())
                path.write_bytes(original)

    def test_rejects_comparison_stale_hash_missing_purpose_and_invalid_video_range(self):
        versions = self.comparison()
        cases = [dict(versions[1], sha256='0' * 64),
                 dict(versions[1], segments=[{'label': '开头', 'window_range': [1, 5], 'video_range': [0, 3]}]),
                 dict(versions[1], segments=[dict(versions[1]['segments'][0], video_range=[0, 6])]),
                 dict(versions[1], id='v1')]
        for version in cases:
            with self.subTest(version=version):
                self.save(self.spec, dict(self.spec_data, versions=[versions[0], version]))
                self.assertNotEqual(self.call().returncode, 0)
                self.assertFalse(self.output.parent.exists())

    def test_additional_window_maps_question_and_saved_feedback_without_asr(self):
        self.comparison()
        extra = self.folder.parent / 'window-extra/extraction.json'
        extra.parent.mkdir()
        media = extra.parent / 'window.mp4'
        media.write_bytes(b'another synthetic source window; not playable')
        data = dict(self.extracted, window={'path': str(media), 'sha256': self.sha(media),
                                          'duration_seconds': 8},
                    offset_seconds=40, requested_range=[40, 48], source_span=[40, 48],
                    frame_grid={'aligned_start_seconds': 40, 'expected_step_seconds': 1 / 30},
                    sample_frames=[])
        self.save(extra, data)
        self.spec_data['additional_extractions'] = [str(extra)]
        self.spec_data['questions'][0]['extraction'] = str(extra)
        self.spec_data['versions'][1]['segments'][0]['extraction'] = str(extra)
        self.save(self.spec, self.spec_data)
        result = self.call()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.output.parent / 'source-2.mp4').is_file())
        page = self.output.read_text('utf-8')
        self.assertIn('原片 00:42.00', page)
        self.assertIn('data-source="source-2.mp4"', page)
        raw = self.workspace / 'private-context/feedback.txt'
        raw.write_text('这个词听不清', encoding='utf-8')
        saved = self.workspace / 'private-context/feedback-q1.json'
        result = self.call('save-feedback', '--card', str(self.output), '--feedback-text', str(raw),
                           '--version', 'v2', '--range', '0', '1', '--question', 'Q1', '--output', str(saved))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(saved.read_text('utf-8'))
        self.assertEqual(record['question']['source_range'], [42, 44])
        self.assertEqual(record['question']['source_sha256'], self.sha(self.source))
        self.assertEqual(record['question']['window_sha256'], self.sha(media))
        self.assertEqual(record['review_status'], 'pending')

    def test_rejects_question_feedback_outside_bound_version_range(self):
        self.comparison()
        self.assertEqual(self.call().returncode, 0)
        raw = self.workspace / 'private-context/feedback.txt'
        raw.write_text('这个词听不清', encoding='utf-8')
        saved = self.workspace / 'private-context/feedback-q1.json'
        result = self.call('save-feedback', '--card', str(self.output), '--feedback-text', str(raw),
                           '--version', 'v2', '--range', '2', '3', '--question', 'Q1', '--output', str(saved))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('问题反馈区间', result.stdout)
        self.assertFalse(saved.exists())

    def test_rejects_unbound_extraction_and_outside_version_media(self):
        versions = self.comparison()
        outside = self.root / 'old-preview.mp4'
        outside.write_bytes(b'synthetic unbound preview')
        cases = [dict(self.spec_data, versions=[dict(versions[0], path=str(outside), sha256=self.sha(outside))]),
                 dict(self.spec_data, questions=[dict(self.spec_data['questions'][0], extraction=str(self.transcript))])]
        for index, spec in enumerate(cases):
            with self.subTest(index=index):
                self.save(self.spec, spec)
                self.assertNotEqual(self.call().returncode, 0)
                self.assertFalse(self.output.parent.exists())

    def test_empty_or_unknown_feedback_never_creates_record(self):
        self.comparison()
        self.assertEqual(self.call().returncode, 0)
        raw = self.workspace / 'private-context/feedback.txt'
        saved = self.workspace / 'private-context/feedback-v2.json'
        for text, version, times in [('  \n', 'v2', ('0', '5')), ('喜欢', 'absent', ('0', '5')),
                                     ('喜欢', 'v2', ('-1', '5')), ('喜欢', 'v2', ('0', '6'))]:
            with self.subTest(text=text, version=version, times=times):
                raw.write_bytes(text.encode('utf-8'))
                result = self.call('save-feedback', '--card', str(self.output), '--feedback-text', str(raw),
                                   '--version', version, '--range', *times, '--output', str(saved))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(saved.exists())

    def test_missing_page_or_changed_binding_record_requires_re_review(self):
        self.comparison()
        self.assertEqual(self.call().returncode, 0)
        manifest_path = self.output.parent / 'review-card.json'
        original = manifest_path.read_bytes()
        record = json.loads(original)
        record['versions'][1]['label'] = '改动后的版本说明'
        self.save(manifest_path, record)
        result = self.call('check', '--card', str(self.output))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('需要重新审核', result.stdout)
        manifest_path.write_bytes(original)
        self.output.unlink()
        result = self.call('check', '--card', str(self.output))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('需要重新审核', result.stdout)

    def test_rejects_outside_paths_overrun_and_duplicate_question_ids(self):
        cases = [dict(self.spec_data, extraction=str(self.root / 'extraction.json')),
                 dict(self.spec_data, proposed_segments=[{'label': '越界', 'window_range': [8, 11]}]),
                 dict(self.spec_data, questions=self.spec_data['questions'] * 2)]
        for spec in cases:
            with self.subTest(spec=spec):
                self.save(self.spec, spec)
                self.assertNotEqual(self.call().returncode, 0)
                self.assertFalse(self.output.parent.exists())
        self.save(self.spec, self.spec_data)
        self.output = self.root / 'outside/index.html'
        self.assertNotEqual(self.call().returncode, 0)
        self.assertFalse(self.output.parent.exists())


class StageForwardingTests(unittest.TestCase):
    def test_flags_and_positional_actions_reach_stages_in_original_order(self):
        spec = importlib.util.spec_from_file_location('review_card_cli_test', CLI)
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        for command, arguments in (
                ('review-card', ['--spec', 'spec.json', '--output', 'index.html']),
                ('window', ['extract', '--task-record', 'card.json', '--start', '1', '--end', '4']),
                ('combine', ['--input', 'input.json', '--output', 'output.mp4'])):
            with self.subTest(command=command), patch.object(sys, 'argv', [str(CLI), command, *arguments]), \
                    patch.object(cli.subprocess, 'run') as run:
                run.return_value.returncode = 0
                self.assertEqual(cli.main(), 0)
                self.assertEqual(run.call_args.args[0][3:], arguments)


if __name__ == '__main__':
    unittest.main()
