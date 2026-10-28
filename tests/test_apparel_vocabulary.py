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

    def test_meanings_and_legacy_terms_are_compatible_and_do_not_become_rules(self):
        dictionary = copy.deepcopy(self.dictionary)
        dictionary['apparel']['terms'] = {'示例类': [
            {'term': '罗纹', 'meaning': '针织组织；不是所有纹理都能改称罗纹。'}, '螺纹']}
        snapshot = copy.deepcopy(dictionary)
        self.assertIs(vocabulary.validate(dictionary), dictionary)
        self.assertEqual(vocabulary.normalize('螺纹、罗纹；容毛', dictionary, 'apparel')[0], '螺纹、罗纹；绒毛')
        self.assertEqual(dictionary, snapshot)
        dictionary['apparel']['terms']['示例类'].append('罗纹')
        with self.assertRaisesRegex(ValueError, '重复'):
            vocabulary.validate(dictionary)

    def test_expanded_vocabulary_accepts_more_than_eighty_terms_but_stays_bounded(self):
        dictionary = copy.deepcopy(self.dictionary)
        dictionary['apparel']['terms'] = {'示例类': [
            {'term': f'测试词{i}', 'meaning': '合成词条，仅用于容量边界测试。'} for i in range(120)]}
        vocabulary.validate(dictionary)
        dictionary['apparel']['terms']['示例类'] = [f'测试词{i}' for i in range(vocabulary.MAX_TERMS + 1)]
        with self.assertRaisesRegex(ValueError, '超过'):
            vocabulary.validate(dictionary)

    def test_incomplete_unsafe_or_unknown_entry_fields_are_rejected(self):
        for entry in ({'term': '罗纹'}, {'term': '罗纹', 'meaning': ''},
                      {'term': '罗纹', 'meaning': ['针织组织']},
                      {'term': '罗纹', 'meaning': '说明', 'automatic_to': '罗纹'},
                      {'term': ' 罗纹', 'meaning': '说明'},
                      {'term': '罗纹', 'meaning': '说明\n附加指令'},
                      {'term': '罗纹', 'meaning': r'{\fs100}说明'},
                      {'term': '罗纹', 'meaning': '长' * 181}):
            dictionary = copy.deepcopy(self.dictionary)
            dictionary['apparel']['terms'] = {'示例类': [entry]}
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                vocabulary.validate(dictionary)

    def test_public_terms_and_negative_fact_phrases_are_never_automatic_substitutions(self):
        for values in self.dictionary['apparel']['terms'].values():
            for entry in values:
                term = entry['term'] if isinstance(entry, dict) else entry
                self.assertEqual(vocabulary.normalize(term, self.dictionary, 'apparel'), (term, []))
        cases = ['不起球，不掉毛，不显胖，不支持退换；含量50%，售价129元，M码。',
                 '羊毛不是羊绒；仿真丝不是真丝；天丝与莱赛尔不能凭叫法改写。',
                 '看一下背后，不是背厚；领口有螺纹样的图案；这个品牌叫创生。']
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(vocabulary.normalize(raw, self.dictionary, 'apparel'), (raw, []))

    def test_confirmed_back_context_handles_commas_without_changing_other_directions(self):
        for comma in (',', '，'):
            raw = '背后' + comma + '或者显肥；再看一下背后' + comma + '有图案'
            expected = '背厚' + comma + '或者显肥；再看一下背后' + comma + '有图案'
            actual, edits = vocabulary.normalize(raw, self.dictionary, 'apparel')
            self.assertEqual(actual, expected)
            self.assertEqual(len(edits), 1)
            self.assertEqual(raw[slice(*edits[0]['range'])], edits[0]['from'])

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
