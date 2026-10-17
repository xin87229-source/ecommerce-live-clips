import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.apparel_strategy import evaluate_shots, strategy_for


class ApparelStrategyTests(unittest.TestCase):
    def test_categories_and_aliases(self):
        for alias, expected in [('上衣', 'tops'), ('裤子', 'trousers'), ('连衣裙', 'dress'),
                                ('半身裙', 'skirt'), ('外套', 'outerwear'), ('套装', 'set'), ('其他', 'other')]:
            result = strategy_for(alias)
            self.assertEqual(result['category'], expected)
            self.assertTrue(result['focus_shots'])
            self.assertTrue(result['structure_choices'])
            self.assertTrue(result['avoid'])

    def test_unknown_category_preserves_input(self):
        result = strategy_for('陌生品类')
        self.assertEqual(result['category'], 'other')
        self.assertEqual(result['requested_category'], '陌生品类')

    def test_ambiguous_skirt_word_does_not_guess_dress_type(self):
        for word in ['裙子', '裙装', '长裙', '短裙', '开衫']:
            self.assertEqual(strategy_for(word)['category'], 'other')

    def test_explicit_length_skirt_is_unambiguous(self):
        for word in ['长款半身裙', '短款半身裙']:
            self.assertEqual(strategy_for(word)['category'], 'skirt')

    def test_style_preserves_unknown_words(self):
        result = strategy_for('dress', '我的特殊风格')
        self.assertEqual(result['style']['name'], '我的特殊风格')
        self.assertFalse(result['style']['known'])

    def test_no_observations_means_no_matches(self):
        result = evaluate_shots('dress', [])
        self.assertEqual(result['matched'], [])
        self.assertEqual(result['missing'], strategy_for('dress')['focus_shots'])

    def test_evidence_required_and_unrelated_tags_not_counted(self):
        tag = strategy_for('dress')['focus_shots'][0]
        result = evaluate_shots('dress', [{'tag': tag}, {'tag': tag, 'evidence': '原片 00:12 正面全身'},
                                          {'tag': '丝绸', 'evidence': '商品名称含丝绸'}])
        self.assertEqual(result['matched'], [tag])
        self.assertEqual(len(result['evidence']), 1)
        self.assertEqual(len(result['ignored']), 2)

    def test_results_are_independent(self):
        result = strategy_for('dress')
        result['focus_shots'].clear()
        self.assertTrue(strategy_for('dress')['focus_shots'])

    def test_invalid_observation_container_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_shots('dress', '正面全身')

    def test_read_only_cli(self):
        run = subprocess.run([sys.executable, str(ROOT / 'pipeline/apparel_strategy.py'),
                              '--category', '裤子', '--style', '通勤'], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['category'], 'trousers')


if __name__ == '__main__':
    unittest.main()
