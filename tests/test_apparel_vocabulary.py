"""Conservative apparel display edits; no model or API calls."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

INSTALL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INSTALL_ROOT / 'pipeline'))
import apparel_vocabulary as vocabulary


class VocabularyTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = vocabulary.load()

    def test_four_rules_preserve_unrelated_words_numbers_and_spacing(self):
        raw = '嗯，塔兔 的容毛；让你创生。\t背后或者显肥  129元 M码 50% 背后走。创生世界'
        expected = '嗯，獭兔 的绒毛；让你穿上身。\t背厚或者显肥  129元 M码 50% 背后走。创生世界'
        snapshot = copy.deepcopy(self.dictionary)
        result, edits = vocabulary.normalize(raw, self.dictionary, 'apparel')
        self.assertEqual(result, expected)
        self.assertEqual(len(edits), 4)
        self.assertEqual(self.dictionary, snapshot)
        for edit in edits:
            self.assertEqual(raw[slice(*edit['range'])], edit['from'])
            self.assertTrue(edit['reason'])

    def test_repeated_occurrences_record_original_ranges_after_length_change(self):
        raw = '让你创生，容毛和容毛'
        result, edits = vocabulary.normalize(raw, self.dictionary, 'apparel')
        self.assertEqual(result, '让你穿上身，绒毛和绒毛')
        pile = [edit for edit in edits if edit['rule'] == 'pile-spelling']
        self.assertEqual([edit['occurrence'] for edit in pile], [1, 2])
        self.assertEqual([raw[slice(*edit['range'])] for edit in pile], ['容毛', '容毛'])

    def test_basic_and_legacy_dictionaries_do_not_change_text(self):
        raw = '塔兔 容毛 背后 创生'
        self.assertEqual(vocabulary.normalize(raw, self.dictionary, 'basic'), (raw, []))
        self.assertEqual(vocabulary.normalize(raw, {'replacements': {}}, 'apparel'), (raw, []))
        with patch.object(vocabulary, 'runtime_profile', return_value={'name': 'basic'}):
            self.assertEqual(vocabulary.normalize(raw), (raw, []))
        with self.assertRaises(ValueError):
            vocabulary.normalize(raw, profile_name='unknown')

    def test_normal_words_and_nonmatching_context_remain_unchanged(self):
        raw = '獭兔绒毛穿上身 背后有图案 创生世界 我不怕背厚'
        self.assertEqual(vocabulary.normalize(raw, self.dictionary, 'apparel'), (raw, []))

    def test_duplicate_rules_or_noop_are_rejected(self):
        rule = self.dictionary['apparel']['rules'][0]
        for extra in (rule, dict(rule, id='another-id'), dict(rule, id='noop', **{'from': '原文', 'to': '原文'})):
            dictionary = copy.deepcopy(self.dictionary)
            dictionary['apparel']['rules'].append(extra)
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                vocabulary.normalize('塔兔', dictionary, 'apparel')

    def test_numeric_and_ass_injection_rules_are_rejected(self):
        for old, new in [('价格九', '价格十'), ('129元', '199元'), ('价格', '129元'),
                         ('塔兔', r'{\fs100}獭兔'), ('塔兔', '獭兔\n卖点')]:
            dictionary = copy.deepcopy(self.dictionary)
            dictionary['apparel']['rules'][0].update({'from': old, 'to': new})
            with self.subTest(old=old, new=new), self.assertRaises(ValueError):
                vocabulary.normalize(old, dictionary, 'apparel')

    def test_overlapping_matches_reject_ambiguity_and_rules_do_not_cascade(self):
        dictionary = copy.deepcopy(self.dictionary)
        dictionary['apparel']['rules'].append(
            {'id': 'overlap', 'from': '你创生', 'to': '你穿上身', 'reason': '测试歧义'})
        with self.assertRaisesRegex(ValueError, '重叠'):
            vocabulary.normalize('让你创生', dictionary, 'apparel')
        dictionary['apparel']['rules'][-1] = {
            'id': 'cascade', 'from': '獭兔', 'to': '短毛', 'reason': '测试禁止连锁替换'}
        self.assertEqual(vocabulary.normalize('塔兔', dictionary, 'apparel')[0], '獭兔')

    def test_terms_are_bounded_unique_and_have_no_ass_controls(self):
        for change in ('duplicate', 'control', 'unknown'):
            dictionary = copy.deepcopy(self.dictionary)
            if change == 'duplicate':
                dictionary['apparel']['terms']['品类'].append('獭兔')
            elif change == 'control':
                dictionary['apparel']['terms']['品类'].append(r'\N')
            else:
                dictionary['apparel']['unexpected'] = True
            with self.subTest(change=change), self.assertRaises(ValueError):
                vocabulary.normalize('塔兔', dictionary, 'apparel')

if __name__ == '__main__':
    unittest.main()
