"""Machine captions remain traceable while avoiding needless flashing/orphan lines."""
import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import rehearsal


class UnitFont:
    def getlength(self, text):
        return len(text)


class ReadabilityTests(unittest.TestCase):
    def cue(self, identifier, start, end, text, clip='clip-1', extraction='original.json'):
        return dict(id=identifier, clip_id=clip, start=start, end=end, text=text,
                    extraction=extraction, window_range=[start, end], source_range=[100+start, 100+end])

    def test_overflow_balances_lines_instead_of_orphaning_final_two_characters(self):
        raw = '甲' * 18 + '姑娘们'
        lines = rehearsal.wrap(raw, UnitFont(), width=19)
        self.assertEqual(''.join(lines), raw)
        self.assertEqual(len(lines), 2)
        self.assertLessEqual(abs(len(lines[0])-len(lines[1])), 1)
        self.assertTrue(lines[1].endswith('姑娘们'))

    def test_groups_adjacent_short_cues_without_changing_originals(self):
        original = [self.cue('s1', 0, .75, '今天'), self.cue('s2', .75, 1.5, '去上班')]
        snapshot = copy.deepcopy(original)
        groups = rehearsal.readable_cues(original, lambda text: [text])
        self.assertEqual(original, snapshot)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['text'], '今天 去上班')
        self.assertEqual(groups[0]['source_cues'], snapshot)
        self.assertEqual((groups[0]['start'], groups[0]['end']), (0, 1.5))

    def test_does_not_merge_across_cut_source_gap_or_long_caption(self):
        first = self.cue('s1', 0, .75, '今天')
        candidates = [self.cue('s2', .75, 1.5, '上班', clip='clip-2'),
                      self.cue('s2', .75, 1.5, '上班', extraction='other.json'),
                      self.cue('s2', 1.0, 1.75, '上班'),
                      self.cue('s2', .75, 6, '上班')]
        for second in candidates:
            with self.subTest(second=second):
                self.assertEqual(len(rehearsal.readable_cues([first, second], lambda text: [text])), 2)
        def too_wide(text):
            if ' ' in text:
                raise ValueError('two lines exceeded')
            return [text]
        self.assertEqual(len(rehearsal.readable_cues([first, self.cue('s2', .75, 1.5, '上班')], too_wide)), 2)

    def test_last_short_cue_can_join_previous_without_extending_total_time(self):
        cues = [self.cue('s1', 0, 1.5, '日常搭配'), self.cue('s2', 1.5, 2.2, '也可以')]
        groups = rehearsal.readable_cues(cues, lambda text: [text])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['end'], 2.2)
        self.assertEqual([s['id'] for s in groups[0]['source_cues']], ['s1', 's2'])


if __name__ == '__main__':
    unittest.main()
