"""Portable subtitle resources, font selection, and source-bound caption guards."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


INSTALL_ROOT = Path(__file__).resolve().parents[1]
PIPELINE = INSTALL_ROOT / 'pipeline'
sys.path.insert(0, str(PIPELINE))

import subtitle as sub


class SubtitlePortableTests(unittest.TestCase):
    def test_optional_emphasis_changes_size_weight_color_and_resets_only_two_matches(self):
        style = dict(sub.read(sub.STYLE), font_size=48, highlight_font_size=68,
                     highlight_bold=True, highlight_color='#FFD75A', max_highlights=2)
        cues = [dict(start=0, end=3, lines=['领口细节领口细节领口细节'], highlights=['领口细节'])]
        ass, srt, _ = sub.subtitle_files(cues, style, 'Test Font')
        self.assertEqual(ass.count(r'\fs68'), 2)
        self.assertEqual(ass.count(r'\b1'), 2)
        self.assertEqual(ass.count(r'\fs48'), 2)
        self.assertEqual(ass.count(r'\b0'), 2)
        self.assertIn('&H005AD7FF', ass)
        self.assertIn('领口细节领口细节领口细节', srt)
        self.assertNotIn(r'\fs', srt)
        legacy_style = {k: v for k, v in sub.read(sub.STYLE).items() if k not in ('highlight_font_size', 'highlight_bold')}
        legacy, _, _ = sub.subtitle_files(cues, legacy_style, 'Test Font')
        self.assertNotIn(r'\fs', legacy)
        self.assertNotIn(r'\b1', legacy)

    def test_optional_emphasis_style_rejects_invalid_size_and_weight(self):
        style = sub.read(sub.STYLE)
        for changes in ({'highlight_font_size': 20}, {'highlight_font_size': 120},
                        {'highlight_font_size': True}, {'highlight_bold': 'yes'}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    sub.validate_style(dict(style, **changes))

    def test_installed_formal_style_uses_large_bold_gold_emphasis(self):
        style = sub.read(sub.STYLE)
        self.assertEqual(style['font_size'], 66)
        self.assertEqual(style.get('highlight_font_size'), 92)
        self.assertIs(style.get('highlight_bold'), True)
        self.assertEqual(style['highlight_color'], '#FFD75A')
        self.assertEqual(sub.validate_style(style), style)
        cue = dict(start=0, end=2, lines=['看这处领口细节'], highlights=['领口细节'])
        ass, srt, _ = sub.subtitle_files([cue], style, 'Test Font')
        self.assertIn(r'\fs92\b1', ass)
        self.assertIn(r'\fs66\b0', ass)
        self.assertIn('看这处领口细节', srt)

    @unittest.skipUnless(os.environ.get('ECLIP_TEST_FONT'), 'requires explicit Chinese font')
    def test_formal_width_check_accounts_for_large_bold_words(self):
        from PIL import ImageFont
        style = sub.read(sub.STYLE)
        font = ImageFont.truetype(os.environ['ECLIP_TEST_FONT'], style['font_size'])
        large = ImageFont.truetype(os.environ['ECLIP_TEST_FONT'], style.get('highlight_font_size', style['font_size']))
        line = '甲' * 9 + '领口细节'
        self.assertLess(font.getlength(line), 950)
        self.assertGreater(sub.emphasis_width(line, ['领口细节'], 1, font, large, True), 950)

    def test_overlapping_terms_count_as_one_visible_highlight(self):
        self.assertEqual(sub.highlight_spans('领口细节和领口细节', ['领口', '领口细节'], 2),
                         [(0, 4, '领口细节'), (5, 9, '领口细节')])

    def test_installed_resources_are_separate_from_workspace_outputs(self):
        workspace = Path(os.environ.get('ECLIP_WORKSPACE') or INSTALL_ROOT / 'workspace').resolve()
        self.assertEqual(sub.STYLE, INSTALL_ROOT / 'pipeline/subtitle_style.json')
        self.assertEqual(sub.DICTIONARY, INSTALL_ROOT / 'pipeline/subtitle_dictionary.json')
        self.assertEqual(sub.PRIVATE, workspace / 'private-context/subtitle-v02c')
        self.assertNotEqual(sub.ROOT, INSTALL_ROOT)
        self.assertEqual(sub.read(sub.DICTIONARY)['replacements'], {})
        self.assertFalse((workspace / 'pipeline/subtitle_style.json').exists())
        with tempfile.TemporaryDirectory() as folder:
            shadow = Path(folder) / 'subtitle_style.json'
            shadow.write_text(sub.STYLE.read_text(encoding='utf-8'), encoding='utf-8')
            with self.assertRaises(ValueError):
                sub.installed_style(shadow)

    def test_basic_style_is_neutral_and_apparel_words_are_separate(self):
        style = sub.read(sub.STYLE)
        self.assertFalse(set(style['keywords']) & {'显瘦', '腰线', '显白', '垂感', '云肩', '钉珠'})
        basic = sub.style_for_profile(style, 'basic')
        apparel = sub.style_for_profile(style, 'apparel')
        self.assertEqual(basic['keywords'], style['keywords'])
        self.assertIn('显瘦', apparel['keywords'])
        self.assertEqual(style['keywords'], sub.read(sub.STYLE)['keywords'])

    def test_explicit_font_path_has_priority_and_missing_path_fails(self):
        font = Path('C:/Windows/Fonts/simsun.ttc')
        if not font.is_file():
            self.skipTest('Windows Chinese font unavailable')
        family, selected = sub.font_choice(['No Such Font'], font)
        self.assertTrue(family)
        self.assertEqual(selected.resolve(), font.resolve())
        with self.assertRaises(ValueError):
            sub.font_choice(['Microsoft YaHei'], font.with_name('missing-font.ttf'))
        with self.assertRaises(ValueError):
            sub.font_choice(['Microsoft YaHei'], 'relative-font.ttf')

    def test_preflight_reads_font_path_from_local_config(self):
        font = Path('C:/Windows/Fonts/simsun.ttc')
        if not font.is_file():
            self.skipTest('Windows Chinese font unavailable')
        selected = {'original_sha256': 'f' * 64, 'duration': 2, 'speed': 1,
                    'original': 'synthetic-source.mp4', 'video': 'synthetic-composition.mp4',
                    'segments': [dict(id='s1', clip_id='c1', start=10, end=12,
                                      final_start=0, final_end=2, text='这款好用')]}
        review = {'selected_hash': sub.key(selected),
                  'cues': [dict(segment_ids=['s1'], lines=['这款好用'])]}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            composition = root / 'outputs' / 'clip'
            composition.mkdir(parents=True)
            (composition / 'result.json').write_text('{}', encoding='utf-8')
            (composition / 'candidate-clips.json').write_text('{"candidates":[]}', encoding='utf-8')
            review_path = root / 'private-context' / 'review.json'
            review_path.parent.mkdir()
            review_path.write_text(json.dumps(review, ensure_ascii=False), encoding='utf-8')
            with patch.object(sub, 'ROOT', root), \
                 patch.object(sub, 'runtime_config', return_value={'font_path': str(font)}), \
                 patch.object(sub, 'extract', return_value=selected), \
                 patch.object(sub, 'task_gate', return_value={}), \
                 patch.object(sub, 'probe_input', return_value={'width': 1080, 'height': 1920,
                                                                'audio': {'codec': 'aac'}}), \
                 patch.object(sub, 'content_decision'), \
                 patch.object(sub, 'subtitle_risks'):
                checks, inputs = sub.render_inputs(composition, review_path, None, sub.STYLE, None)
            self.assertEqual(checks.report()['status'], 'passed', checks.report())
            self.assertEqual(inputs['font_info'][1], font.resolve())

    def test_reordered_segments_keep_source_and_final_time(self):
        selected = {
            'original_sha256': 'f' * 64, 'duration': 4.0,
            'segments': [
                dict(id='b', clip_id='c2', start=60, end=63, final_start=0, final_end=2,
                     text='商品好用'),
                dict(id='a', clip_id='c1', start=20, end=23, final_start=2, final_end=4,
                     text='价格99元'),
            ],
        }
        review = {'selected_hash': sub.key(selected), 'clip_positions': {'c2': 15},
                  'cues': [dict(segment_ids=['b'], lines=['商品好用']),
                           dict(segment_ids=['a'], lines=['价格99元'])]}
        style = sub.read(sub.STYLE)
        cues, audit = sub.cues_from_review(selected, review, style, sub.read(sub.DICTIONARY))
        self.assertEqual([(c['start'], c['end']) for c in cues], [(0, 2), (2, 4)])
        self.assertEqual([a['range'] for a in audit], [[60, 63], [20, 23]])
        self.assertEqual(cues[0]['position_y_percent'], 15)
        self.assertNotIn('position_y_percent', cues[1])
        ass, srt, _ = sub.subtitle_files(cues, style, 'Test Font')
        self.assertIn('00:00:02,000 --> 00:00:04,000', srt)
        self.assertIn(r'\pos(540,288)', ass)

    def test_unsupported_rewrite_or_unknown_number_is_rejected(self):
        selected = {'original_sha256': 'f' * 64, 'duration': 2,
                    'segments': [dict(id='s1', clip_id='c1', start=10, end=12,
                                      final_start=0, final_end=2, text='价格99元')]}
        review = {'selected_hash': sub.key(selected),
                  'cues': [dict(segment_ids=['s1'], lines=['价格9元'],
                                corrections=[{'from': '99', 'to': '9', 'reason': '猜测'}])]}
        with self.assertRaises(ValueError):
            sub.cues_from_review(selected, review, sub.read(sub.STYLE), sub.read(sub.DICTIONARY))
        review['cues'][0].pop('corrections')
        with self.assertRaises(ValueError):
            sub.cues_from_review(selected, review, sub.read(sub.STYLE), sub.read(sub.DICTIONARY))
        review['cues'][0]['lines'] = ['价格99元']
        review['clip_positions'] = {'c1': 20}
        cues, _ = sub.cues_from_review(selected, review, sub.read(sub.STYLE), sub.read(sub.DICTIONARY))
        self.assertEqual(cues[0]['lines'], ['价格99元'])
        review['cues'][0]['position_y_percent'] = 25
        with self.assertRaises(ValueError):
            sub.cues_from_review(selected, review, sub.read(sub.STYLE), sub.read(sub.DICTIONARY))

    def test_semantic_grouping_cannot_cross_cut_or_exceed_display_limit(self):
        selected = {'original_sha256': 'f' * 64, 'duration': 3,
                    'segments': [
                        dict(id='s1', clip_id='c1', start=10, end=11.5,
                             final_start=0, final_end=1.5, text='这款好用'),
                        dict(id='s2', clip_id='c1', start=11.5, end=13,
                             final_start=1.5, final_end=3, text='质量稳定')]}
        review = {'selected_hash': sub.key(selected),
                  'cues': [dict(segment_ids=['s1', 's2'], lines=['这款好用', '质量稳定'])]}
        style = sub.read(sub.STYLE)
        cues, _ = sub.cues_from_review(selected, review, style, sub.read(sub.DICTIONARY))
        self.assertEqual((cues[0]['start'], cues[0]['end']), (0, 3))
        selected['segments'][1]['clip_id'] = 'c2'
        review['selected_hash'] = sub.key(selected)
        with self.assertRaises(ValueError):
            sub.cues_from_review(selected, review, style, sub.read(sub.DICTIONARY))
        selected['segments'][1]['clip_id'] = 'c1'
        selected['segments'][1]['final_end'] = 3.5
        selected['duration'] = 3.5
        review['selected_hash'] = sub.key(selected)
        with self.assertRaises(ValueError):
            sub.cues_from_review(selected, review, style, sub.read(sub.DICTIONARY))


if __name__ == '__main__':
    unittest.main()
