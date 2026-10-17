"""Machine word timing only paginates display; it never confirms or edits speech."""
import copy
import importlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import rehearsal
import transcribe_adapter
import test_rehearsal as fixture


class WordPageTests(unittest.TestCase):
    def cue(self):
        return dict(id='s000001', clip_id='clip-1', text='甲乙丙丁戊己庚辛',
                    start=10, end=14, window_range=[2, 8], source_range=[102, 108],
                    extraction='original.json', word_timing='machine_alignment_not_verified',
                    words=[dict(word=char, start=2+i*.75, end=2+(i+1)*.75, probability=.8)
                           for i, char in enumerate('甲乙丙丁戊己庚辛')])

    @staticmethod
    def fit(text):
        if len(text) > 4:
            raise ValueError('two-line overflow')
        return [text]

    def pages(self, cue, terms=()):
        api = getattr(rehearsal, 'timed_pages', None)
        self.assertTrue(callable(api), 'long captions need measured pages at actual machine word times')
        return api(cue, self.fit, terms)

    def test_pages_preserve_text_raw_evidence_and_speed_offset_mapping(self):
        cue = self.cue()
        before = copy.deepcopy(cue)
        pages = self.pages(cue)
        self.assertEqual(cue, before)
        self.assertEqual(''.join(p['text'] for p in pages), cue['text'])
        self.assertEqual([p['word_span'] for p in pages], [[0, 4], [4, 8]])
        self.assertEqual([p['window_range'] for p in pages], [[2, 5], [5, 8]])
        self.assertEqual([p['source_range'] for p in pages], [[102, 105], [105, 108]])
        self.assertEqual([(p['start'], p['end']) for p in pages], [(10, 12), (12, 14)])
        for page in pages:
            self.assertEqual(page['source_cues'], [before])
            self.assertEqual(page['word_timing'], 'machine_alignment_not_verified')
        groups = rehearsal.readable_cues(pages, self.fit)
        self.assertEqual([p['source_cues'] for p in groups], [[before], [before]])

    def test_highlight_phrase_is_atomic_across_pages(self):
        cue = self.cue()
        pages = self.pages(cue, ['丁戊'])
        self.assertTrue(any('丁戊' in p['text'] for p in pages))
        self.assertEqual(''.join(p['text'] for p in pages), cue['text'])
        self.assertNotIn(4, [p['word_span'][1] for p in pages[:-1]])

    def test_missing_legacy_words_and_invalid_timing_cannot_be_invented(self):
        for mutate in (
            lambda c: c.pop('words'),
            lambda c: c.update(words=[]),
            lambda c: c['words'][0].update(word='更改'),
            lambda c: c['words'][0].update(start=True),
            lambda c: c['words'][0].update(end=float('nan')),
            lambda c: c['words'][0].update(start=2.75),
            lambda c: c['words'][1].update(start=2.5),
            lambda c: c['words'][-1].update(end=8.01),
            lambda c: c['words'][0].update(probability=2),
        ):
            cue = self.cue()
            mutate(cue)
            with self.subTest(cue=cue), self.assertRaises(ValueError):
                self.pages(cue)

    def test_word_gaps_are_preserved_and_no_page_is_extended_to_fill_them(self):
        cue = self.cue()
        cue['words'][3]['end'] = 4.8
        cue['words'][4]['start'] = 5.2
        pages = self.pages(cue)
        self.assertAlmostEqual(pages[0]['end'], 10 + (4.8-2)/1.5)
        self.assertAlmostEqual(pages[1]['start'], 10 + (5.2-2)/1.5)

    def test_machine_word_mapping_at_one_point_three_speed(self):
        cue = self.cue()
        cue['end'] = 10 + 6/1.3
        pages = self.pages(cue)
        self.assertAlmostEqual(pages[0]['end'], 10 + 3/1.3)
        self.assertAlmostEqual(pages[1]['start'], 10 + 3/1.3)
        self.assertEqual(pages[0]['source_range'], [102, 105])

    def test_merged_short_cues_keep_words_only_in_each_original_evidence(self):
        first, second = self.cue(), self.cue()
        first.update(text='甲', start=0, end=.5, window_range=[0, .5], source_range=[100, 100.5],
                     words=[dict(word='甲', start=0, end=.5, probability=.8)])
        second.update(id='s000002', text='乙', start=.5, end=1, window_range=[.5, 1], source_range=[100.5, 101],
                      words=[dict(word='乙', start=.5, end=1, probability=.8)])
        group = rehearsal.readable_cues([first, second], self.fit)[0]
        self.assertEqual(group['text'], '甲 乙')
        self.assertEqual(group['source_cues'], [first, second])
        self.assertNotIn('words', group, 'a merged display must not label only its first segment as all words')
        self.assertNotIn('word_timing', group)

    def test_single_oversized_word_or_highlight_remains_explicit_failure(self):
        cue = self.cue()
        cue['words'] = [dict(word=cue['text'], start=2, end=8, probability=.8)]
        with self.assertRaises(ValueError):
            self.pages(cue)
        with self.assertRaises(ValueError):
            self.pages(self.cue(), ['乙丙丁戊己'])

    def test_subtitle_pipeline_uses_pages_and_preserves_highlight_and_raw_text(self):
        class WideFont:
            size = 48
            def getlength(self, value):
                return len(value)*250
        cue = self.cue()
        style = json.loads(rehearsal.subtitle.STYLE.read_text('utf-8'))
        record = dict(cues=[cue], highlight_terms=['丁戊'], cut={'duration_seconds': 14.6})
        try:
            ass, srt, pages = rehearsal.subtitles(record, style, 'synthetic-font', WideFont())
        except ValueError as error:
            self.fail('valid machine word timing should paginate the full caption: ' + str(error))
        self.assertGreater(len(pages), 1)
        self.assertEqual(''.join(p['text'] for p in pages), cue['text'])
        self.assertEqual(sum(p['highlight_occurrences'] for p in pages), 1)
        self.assertTrue(any('丁戊' in line for p in pages for line in p['lines']))
        self.assertTrue(all(p['source_cues'] == [cue] for p in pages))
        self.assertEqual(ass.count('Dialogue: 0,'), len(pages))


class BoundWordTests(unittest.TestCase):
    save = fixture.RehearsalTests.save
    bind_asr = fixture.RehearsalTests.bind_asr
    setUp = fixture.RehearsalTests.setUp
    tearDown = fixture.RehearsalTests.tearDown
    api = fixture.RehearsalTests.api

    def test_words_come_from_hashed_cache_not_mutable_preview_fields(self):
        transcript = self.support.read(self.transcript)
        words = [dict(word=self.machine_text, start=1, end=4, probability=.8)]
        transcript['asr']['word_timestamps'] = True
        transcript['segments'][0]['words'] = words
        self.save(self.transcript, transcript)
        preview = self.support.read(self.preview)
        preview['transcript_cache']['sha256'] = self.support.file_hash(self.transcript)
        preview['segments'][0]['words'] = [dict(word='伪造', start=0, end=5)]
        self.save(self.preview, preview)
        record = self.api().plan(self.rehearsal)
        self.assertEqual(record['cues'][0].get('words'), words)
        self.assertEqual(record['cues'][0]['word_timing'], 'machine_alignment_not_verified')
        self.assertFalse(record['transcript_verified'])
        self.assertTrue(all(v == 'not_reviewed' for v in record['approvals'].values()))
        transcript['segments'][0]['words'][0]['end'] = 3
        self.save(self.transcript, transcript)
        with self.assertRaises(ValueError):
            self.api().plan(self.rehearsal)

    def test_legacy_short_cue_is_unchanged_without_words(self):
        record = self.api().plan(self.rehearsal)
        self.assertNotIn('words', record['cues'][0])
        self.assertEqual(record['cues'][0]['text'], self.machine_text)


class TranscribeWordTests(unittest.TestCase):
    def test_new_config_writes_words_and_separate_cache_from_legacy(self):
        import numpy as np
        importlib.reload(transcribe_adapter)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.mp4'
            source.write_bytes(b'synthetic-test-not-merchant-media')
            model = root / 'model'
            model.mkdir()
            for name in ('model.bin', 'tokenizer.json'):
                (model / name).write_bytes(b'fixture')
            calls = []
            class Model:
                def __init__(self, *args, **kwargs):
                    pass
                def transcribe(self, path, **options):
                    calls.append(options)
                    words = [SimpleNamespace(word='完整原文', start=np.float64(.25), end=np.float64(1.75),
                                             probability=np.float64(.9))]
                    segment = SimpleNamespace(start=np.float64(.25), end=np.float64(1.75), text='完整原文',
                                              avg_logprob=-.1, no_speech_prob=.1,
                                              words=words if options['word_timestamps'] else None)
                    return iter([segment]), SimpleNamespace(duration_after_vad=2)
            with patch.object(transcribe_adapter, 'PRIVATE', root), \
                 patch.object(transcribe_adapter, 'local_model_path', return_value=model), \
                 patch.object(transcribe_adapter, 'audio_audit', return_value={'synthetic': True}), \
                 patch.dict(sys.modules, {'faster_whisper': SimpleNamespace(WhisperModel=Model)}):
                with patch.dict(transcribe_adapter.OPTIONS, word_timestamps=False):
                    try:
                        _, legacy, _ = transcribe_adapter.transcribe(source, {'duration': 2})
                    except ValueError as error:
                        self.fail('finite real backend scalars must be normalized before validation: ' + str(error))
                old = legacy.read_bytes()
                result, current, _ = transcribe_adapter.transcribe(source, {'duration': 2})
                self.assertTrue(calls[-1]['word_timestamps'])
                self.assertNotEqual(current, legacy)
                self.assertEqual(legacy.read_bytes(), old)
                self.assertEqual(result['segments'][0]['words'],
                                 [dict(word='完整原文', start=.25, end=1.75, probability=.9)])
                self.assertIs(type(result['segments'][0]['start']), float)
                self.assertIs(type(result['segments'][0]['end']), float)
                cached, cached_path, metrics = transcribe_adapter.transcribe(source, {'duration': 2})
                self.assertTrue(metrics['cache_hit'])
                self.assertEqual(current, cached_path)
                self.assertEqual(cached['segments'], result['segments'])

    def test_model_numbers_do_not_relax_invalid_time_validation(self):
        for value in (True, float('nan'), float('inf'), '0.25'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                transcribe_adapter.bounded_asr_end(value, 1, 2)
        with self.assertRaises(ValueError):
            transcribe_adapter.bounded_asr_end(1.5, 1, 2)


if __name__ == '__main__':
    unittest.main()
