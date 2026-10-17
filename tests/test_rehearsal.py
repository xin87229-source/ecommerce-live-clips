"""Draft rehearsal preserves unknown content states and uses real source media."""
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_review_cut as cut_fixture


class RehearsalTests(unittest.TestCase):
    save = cut_fixture.ReviewCutTests.save

    def tearDown(self):
        try:
            cut_fixture.ReviewCutTests.tearDown(self)
        finally:
            # Runtime selection is process-global. Restore module constants too,
            # so later suites never inherit this test's deleted workspace.
            for name in ('runtime', 'selector_support', 'quality_support', 'run_records',
                         'source_windows', 'review_cut', 'review_card', 'subtitle', 'rehearsal'):
                if name in sys.modules:
                    importlib.reload(sys.modules[name])

    def setUp(self):
        cut_fixture.ReviewCutTests.setUp(self)
        self.spec['clips'][0].update(window_range=[1, 4], speed=1.3)
        self.save(self.spec_path, self.spec)
        self.preview = self.extraction.parent / 'asr-preview-test.json'
        self.transcript = self.workspace / 'private-context/transcript.json'
        self.machine_text = '这是机器错词原文'
        self.bind_asr(self.extraction, self.machine_text)
        self.rehearsal = self.workspace / 'private-context/rehearsal.json'
        self.request = {'schema_version': 1, 'cut_spec': str(self.spec_path),
                        'asr_previews': [str(self.preview)],
                        'selection': {'selected': '测试候选', 'reason': '具有完整展示',
                                      'alternatives': ['另款：展示不完整'], 'uncertainties': ['同款待确认']},
                        'title': '真实画面待审样片', 'caption': '查看这条原声展示，具体原话与商品事实仍需确认。',
                        'hashtags': ['#女装', '#原声展示'], 'cover_lines': ['原声展示'], 'cover_time': 1}
        self.save(self.rehearsal, self.request)

    def bind_asr(self, extraction, text):
        item = self.support.read(extraction)
        window = item['window']
        offset = item['offset_seconds']
        transcript = {'schema_version': 1, 'status': 'complete', 'source': window,
                      'asr': {'config_hash': 'synthetic-fixture-not-real-ASR'},
                      'segments': [{'id': 's000001', 'start': 1, 'end': 4, 'text': text}]}
        self.save(self.transcript, transcript)
        preview = {'schema_version': 1, 'status': 'unreviewed_asr_preview',
                   'extraction': {'path': str(extraction), 'sha256': self.support.file_hash(extraction)},
                   'source_sha256': item['source']['sha256'], 'window_sha256': window['sha256'],
                   'source_span': item['source_span'],
                   'transcript_cache': {'path': str(self.transcript), 'sha256': self.support.file_hash(self.transcript)},
                   'asr_config_hash': transcript['asr']['config_hash'],
                   'segments': [{'id': 's000001', 'window_range': [1, 4],
                                 'source_range': [offset + 1, offset + 4], 'text': text}],
                   'same_product': 'unknown', 'audio_listening': 'not_performed', 'transcript_verified': False}
        self.save(self.preview, preview)

    def api(self):
        for name in ('review_cut', 'review_card', 'subtitle'):
            importlib.reload(importlib.import_module(name))
        try:
            return importlib.reload(importlib.import_module('rehearsal'))
        except ModuleNotFoundError:
            self.fail('rehearsal build/check is not implemented')

    def test_plan_maps_exact_machine_text_without_claiming_content_review(self):
        original = self.card.read_bytes()
        planned = self.api().plan(self.rehearsal)
        self.assertEqual(planned['status'], 'unreviewed_rehearsal')
        self.assertFalse(planned['submitted'])
        self.assertFalse(planned['transcript_verified'])
        self.assertEqual(planned['cues'][0]['text'], self.machine_text)
        self.assertEqual(planned['cues'][0]['start'], 0)
        self.assertAlmostEqual(planned['cues'][0]['end'], 3 / 1.3)
        self.assertEqual(planned['cues'][0]['source_range'], [11, 14])
        self.assertTrue(all(v == 'not_reviewed' for v in planned['approvals'].values()))
        self.assertEqual(self.card.read_bytes(), original)
        self.assertFalse((self.workspace / 'outputs/reviews/rehearsals').exists())

    def test_partial_asr_segment_is_rejected_with_full_boundary(self):
        self.spec['clips'][0]['window_range'] = [1.2, 4]
        self.save(self.spec_path, self.spec)
        with self.assertRaisesRegex(ValueError, r'完整.*1.*4'):
            self.api().plan(self.rehearsal)

    def test_optional_highlight_terms_bind_only_to_selected_original_words(self):
        self.request['highlight_terms'] = ['机器', '原文']
        self.save(self.rehearsal, self.request)
        record = self.api().plan(self.rehearsal)
        self.assertEqual(record['highlight_terms'], ['机器', '原文'])
        self.assertEqual(record['cues'][0]['text'], self.machine_text)
        self.assertFalse(record['transcript_verified'])
        for terms in (['未说过的词'], ['机器', '机器'], [''], ['原文', 1], '机器'):
            with self.subTest(terms=terms):
                self.save(self.rehearsal, dict(self.request, highlight_terms=terms))
                with self.assertRaises(ValueError):
                    self.api().plan(self.rehearsal)

    @unittest.skipUnless(os.environ.get('ECLIP_TEST_FONT'), 'requires explicit Chinese font')
    def test_emphasized_word_uses_larger_measured_width_and_is_not_split(self):
        from PIL import ImageFont
        api = self.api()
        font = ImageFont.truetype(os.environ['ECLIP_TEST_FONT'], 48)
        large = ImageFont.truetype(os.environ['ECLIP_TEST_FONT'], 68)
        raw = '甲' * 16 + '领口细节' + '乙' * 2
        lines = api.wrap(raw, font, highlights=['领口细节'], highlight_font=large, bold=True)
        self.assertEqual(''.join(lines), raw)
        self.assertEqual(len(lines), 2)
        self.assertIn('领口细节', lines[1])
        for line in lines:
            self.assertLessEqual(api.subtitle.emphasis_width(line, ['领口细节'], 2, font, large, True), 950)
        plain = api.wrap('甲' * 20, font, border=10)
        self.assertEqual(len(plain), 2)
        self.assertEqual(''.join(plain), '甲' * 20)

    def test_text_changes_fake_approval_and_wrong_source_cannot_reuse_asr(self):
        data = self.support.read(self.preview)
        for changed in (dict(data, transcript_verified=True),
                        dict(data, source_sha256='0' * 64),
                        dict(data, segments=[{**data['segments'][0], 'text': '被悄悄更正'}])):
            self.save(self.preview, changed)
            with self.assertRaises(ValueError):
                self.api().plan(self.rehearsal)

    def test_no_injected_subtitle_or_publication_fields_are_accepted(self):
        for field, value in [('subtitle_text', '自行改词'), ('submitted', True),
                             ('account', 'account'), ('approvals', {'same_product': 'passed'})]:
            with self.subTest(field=field):
                self.save(self.rehearsal, dict(self.request, **{field: value}))
                with self.assertRaises(ValueError):
                    self.api().plan(self.rehearsal)

    def test_cover_cannot_be_from_silent_tail_or_unknown_cut(self):
        for at in (2.5, -1, float('nan'), True):
            self.save(self.rehearsal, dict(self.request, cover_time=at))
            with self.assertRaises(ValueError):
                self.api().plan(self.rehearsal)

    def test_existing_output_is_preserved(self):
        folder = self.workspace / 'outputs/reviews/rehearsals/existing'
        folder.mkdir(parents=True)
        (folder / 'keep.txt').write_text('preserve', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.api().build(self.rehearsal, 'existing')
        self.assertEqual((folder / 'keep.txt').read_text('utf-8'), 'preserve')

    def test_cli_routes_rehearsal_help(self):
        cli = Path(__file__).resolve().parents[1] / 'cli.py'
        result = subprocess.run([sys.executable, '-B', str(cli), '--workspace', str(self.workspace),
                                 'rehearsal', '--help'], capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('build', result.stdout)
        self.assertIn('check', result.stdout)

    def test_page_shows_selection_timeline_and_separate_quality_records(self):
        api = self.api()
        page = api.page(api.plan(self.rehearsal), ['review-1/index.html'])
        for value in ('00:11.00', '00:14.00', '1.3', '原声开头', 'quality.json', 'quality.md', '同款'):
            self.assertIn(value, page)

    def test_literal_ass_sequences_do_not_become_commands_or_line_breaks(self):
        from PIL import ImageFont
        api = self.api()
        record = api.plan(self.rehearsal)
        raw = r'原文{测试}\N保持'
        record['cues'][0]['text'] = raw
        style = dict(api.subtitle.installed_style(api.subtitle.STYLE), font_size=48, max_highlights=0)
        ass, srt, _ = api.subtitles(record, style, 'test', ImageFont.load_default())
        self.assertIn(raw, srt)
        self.assertNotIn(r'\N', ass, 'literal machine text must not inject an ASS line break')
        self.assertNotIn(r'{测试}', ass, 'literal braces must not become an override block')

    @unittest.skipUnless(all(os.environ.get(k) for k in
                            ('ECLIP_TEST_FFMPEG', 'ECLIP_TEST_FFPROBE', 'ECLIP_TEST_FONT')),
                         'real media rehearsal needs explicitly configured FFmpeg and Chinese font')
    def test_real_media_complete_draft_and_tamper_detection(self):
        ffmpeg, ffprobe, font = (os.environ[k] for k in
                               ('ECLIP_TEST_FFMPEG', 'ECLIP_TEST_FFPROBE', 'ECLIP_TEST_FONT'))
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                        '-f', 'lavfi', '-i', 'testsrc2=size=96x160:rate=30:duration=6',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=6',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(self.source)],
                       check=True, capture_output=True)
        self.context['source_sha256'] = self.support.file_hash(self.source)
        self.save(self.card, {'status': 'NOT_UPLOADED_NOT_SUBMITTED', 'submitted': False,
                             'task_context': self.context})
        self.save(self.workspace / 'config.json', {'ffmpeg_path': ffmpeg, 'ffprobe_path': ffprobe,
                                                  'font_path': font})
        extraction = Path(self.windows.extract(self.card, 0, 5)['extraction'])
        self.preview = extraction.parent / 'asr-preview-test.json'
        self.bind_asr(extraction, self.machine_text)
        self.spec['clips'][0]['extraction'] = str(extraction)
        self.save(self.spec_path, self.spec)
        self.request['asr_previews'] = [str(self.preview)]
        self.request['highlight_terms'] = ['机器', '原文']
        self.save(self.rehearsal, self.request)
        before = {p: self.support.file_hash(p) for p in (self.source, self.card, extraction, self.preview)}
        api = self.api()
        report = api.build(self.rehearsal, 'media-smoke')
        package = Path(report['package'])
        for name in ('draft-video.mp4', 'draft-subtitle.srt', 'draft-subtitle.ass', 'cover-3x4.jpg',
                     'cover-4x3.jpg', 'title.txt', 'caption.txt', 'index.html', 'simulation.json', 'quality.json', 'quality.md'):
            self.assertTrue((package / name).is_file(), name)
        self.assertEqual(report['status'], 'SIMULATED_NOT_SUBMITTED')
        self.assertFalse(report['platform_ui_verified'])
        self.assertFalse(report['uploaded'])
        self.assertFalse(report['submitted'])
        self.assertEqual(api.check(package)['status'], 'SIMULATED_NOT_SUBMITTED')
        self.assertIn(self.machine_text, (package / 'draft-subtitle.srt').read_text('utf-8'))
        self.assertIn('待审演练', (package / 'draft-subtitle.ass').read_text('utf-8'))
        quality = self.support.read(package / 'quality.json')['subtitle']
        self.assertEqual(quality['font_size'], 48)
        self.assertEqual(quality['highlight_font_size'], 68)
        self.assertTrue(quality['highlight_bold'])
        self.assertEqual(quality['highlight_occurrences'], 2)
        self.assertEqual(before, {p: self.support.file_hash(p) for p in before})
        self.assertFalse((package / 'publish.json').exists())
        self.assertFalse((package / 'result.json').exists())
        card_data = self.support.read(package / 'review-1/review-card.json')
        self.assertIn('原句待核词', card_data['questions'][0]['question'])
        simulation = self.support.read(package / 'simulation.json')
        wrong = copy.deepcopy(simulation)
        wrong['checks']['human_content_review'] = 'passed'
        self.save(package / 'simulation.json', wrong)
        with self.assertRaisesRegex(ValueError, '矛盾'):
            api.check(package)
        self.save(package / 'simulation.json', simulation)
        source_bytes = self.source.read_bytes()
        self.source.write_bytes(source_bytes + b'changed source')
        with self.assertRaisesRegex(ValueError, '变化'):
            api.check(package)
        self.source.write_bytes(source_bytes)
        self.assertEqual(api.check(package)['status'], 'SIMULATED_NOT_SUBMITTED')
        (package / 'title.txt').write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '变化'):
            api.check(package)


if __name__ == '__main__':
    unittest.main()
