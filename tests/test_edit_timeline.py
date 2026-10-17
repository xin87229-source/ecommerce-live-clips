"""Formal edit mappings bind original media and actual boundary feedback, read-only."""
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))


class EditTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {'ECLIP_WORKSPACE': str(self.workspace)})
        self.env.start()
        for name in ('runtime', 'selector_support', 'quality_support', 'source_windows'):
            importlib.reload(importlib.import_module(name))
        self.support = importlib.import_module('selector_support')
        original = self.root / 'source.mp4'
        original.write_bytes(b'synthetic original; no merchant content')
        self.source = {'path': str(original), 'sha256': self.support.file_hash(original),
                       'duration_seconds': 100}
        self.extraction = self.workspace / 'private-context/source-windows/source/window-one/extraction.json'
        window = self.extraction.parent / 'window.mp4'
        window.parent.mkdir(parents=True)
        window.write_bytes(b'synthetic zero origin window')
        self.extracted = {'schema_version': 1, 'source': dict(self.source),
            'window': {'path': str(window), 'sha256': self.support.file_hash(window), 'duration_seconds': 10},
            'requested_range': [10, 20], 'source_span': [10, 20], 'offset_seconds': 10,
            'frame_grid': {'expected_step_seconds': 1/30, 'aligned_start_seconds': 10}}
        self.save(self.extraction, self.extracted)
        self.transcript = {'schema_version': 1, 'status': 'complete', 'source': dict(self.source),
            'segments': [dict(id='s000001', start=11, end=12, text='这段原声'),
                         dict(id='s000002', start=12, end=14, text='这是完整原句') ]}
        self.candidates = {'c1': dict(id='c1', kind='speech', start_seconds=11, end_seconds=12,
                                    start_segment_id='s000001', end_segment_id='s000001', text='这段原声'),
                           'c2': dict(id='c2', kind='speech', start_seconds=12, end_seconds=14,
                                    start_segment_id='s000002', end_segment_id='s000002', text='这是完整原句')}
        self.order = ['c1', 'c2']
        self.plan = {'schema_version': 1, 'clips': [
            dict(candidate_id='c1', extraction=self.ref(self.extraction), window_range=[1, 2],
                 speed=1.3, purpose='先说原声'),
            dict(candidate_id='c2', extraction=self.ref(self.extraction), window_range=[2, 4],
                 purpose='保留完整收句')], 'end_hold_seconds': .6}

    def tearDown(self):
        self.env.stop()
        assert self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        self.temp.cleanup()

    def save(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def ref(self, path):
        return {'path': str(path), 'sha256': self.support.file_hash(path)}

    def api(self):
        try:
            return importlib.reload(importlib.import_module('edit_timeline'))
        except ModuleNotFoundError:
            self.fail('edit_timeline.build is not implemented')

    def build(self, plan=None, **overrides):
        values = dict(edit_plan=self.plan if plan is None else plan, order=self.order,
                      candidates_by_id=self.candidates, source=self.source, confirmed_ranges=[[10, 20]],
                      default_speed=1, transcript=self.transcript)
        values.update(overrides)
        return self.api().build(**values)

    def add_boundary(self):
        self.plan['clips'][1]['window_range'] = [2.25, 3.75]
        feedback = self.workspace / 'private-context/feedback.txt'
        feedback.write_text('合成测试听审：实际保留原话为完整原句。', encoding='utf-8')
        entry = dict(source_sha256=self.source['sha256'], segment_ids=['s000002'],
                     range=[12.25, 13.75], text='完整原句', confirmation={
                         'status': 'confirmed', 'type': 'user_message', 'actor': 'synthetic reviewer',
                         'at': '2026-10-16T00:00:00+08:00', 'quote': '实际保留原话为完整原句',
                         **self.ref(feedback)})
        proof = self.workspace / 'private-context/boundary-proof.json'
        self.save(proof, {'boundary_transcripts': [entry]})
        self.plan['clips'][1]['boundary_transcripts'] = [dict(segment_id='s000002',
            source_range=[12.25, 13.75], text='完整原句', evidence={
                'type': 'audio_listening', **self.ref(proof), 'source_sha256': self.source['sha256'],
                'segment_ids': ['s000002'], 'range': [12.25, 13.75]})]
        return proof, feedback, entry

    def test_variable_speed_mapping_is_read_only_and_uses_frame_offsets(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        supplied = copy.deepcopy((self.plan, self.candidates, self.transcript))
        record = self.build()
        self.assertEqual(record['raw_duration'], 3)
        self.assertEqual(record['frame_count'], 102)
        self.assertAlmostEqual(record['duration_seconds'], 3.4)
        self.assertEqual(record['segments'][0]['frame_range'], [0, 24])
        self.assertEqual(record['mapped_clips'][1]['final_start'], .8)
        self.assertEqual(record['mapped_clips'][1]['final_end'], 2.8)
        self.assertAlmostEqual(record['mapped_segments'][0]['final_end'], 1/1.3)
        self.assertEqual(record['mapped_segments'][1]['text'], '这是完整原句')
        self.assertNotIn('boundary_evidence', record['mapped_segments'][1])
        self.assertNotIn('approvals', record)
        self.assertEqual(record['edit_plan_hash'], self.support.key(self.plan))
        self.assertEqual(record['transcript_content_hash'], self.support.key(self.transcript))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertEqual(supplied, (self.plan, self.candidates, self.transcript))

    def test_visual_override_keeps_audio_and_maps_only_consumed_picture(self):
        self.plan['clips'][0]['visual'] = dict(extraction=self.ref(self.extraction), window_range=[5, 7])
        self.plan['clips'][0]['crop'] = dict(x=0, y=2, width=720, height=1280)
        record = self.build()
        segment = record['segments'][0]
        self.assertEqual(segment['audio']['source_range'], [11, 12])
        self.assertEqual(segment['visual']['requested_window_range'], [5, 7])
        self.assertAlmostEqual(segment['visual']['source_range'][1], 16.04)
        self.assertTrue(segment['non_synchronous_visual'])
        self.assertEqual(record['mapped_segments'][0]['text'], '这段原声')
        self.assertEqual(segment['crop']['y'], 2)

    def test_partial_asr_segment_requires_exact_real_feedback_and_keeps_original(self):
        proof, feedback, _ = self.add_boundary()
        record = self.build()
        mapped = record['mapped_segments'][1]
        self.assertEqual([mapped['start'], mapped['end']], [12.25, 13.75])
        self.assertEqual([mapped['asr_start'], mapped['asr_end']], [12, 14])
        self.assertEqual(mapped['asr_text'], '这是完整原句')
        self.assertEqual(mapped['text'], '完整原句')
        self.assertEqual(mapped['final_start'], .8)
        self.assertEqual(mapped['final_end'], 2.3)
        self.assertIn(str(proof), record['inputs'])
        self.assertIn(str(feedback), record['inputs'])
        self.assertIn('boundary_evidence', mapped)

    def test_partial_missing_duplicate_and_unneeded_boundary_entries_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan['clips'][1]['window_range'] = [2.25, 3.75]
        with self.assertRaises(ValueError):
            self.build(plan)
        self.add_boundary()
        duplicate = copy.deepcopy(self.plan)
        duplicate['clips'][1]['boundary_transcripts'] *= 2
        with self.assertRaises(ValueError):
            self.build(duplicate)
        unneeded = copy.deepcopy(self.plan)
        unneeded['clips'][1]['window_range'] = [2, 4]
        with self.assertRaises(ValueError):
            self.build(unneeded)

    def test_boundary_confirmation_cannot_be_empty_mismatched_or_fabricated(self):
        proof, _, good = self.add_boundary()
        changes = [dict(source_sha256='0'*64), dict(segment_ids=['s000001']), dict(range=[12, 14]),
                   dict(text='未在反馈出现的文字')]
        for field, value in [('status', 'pending'), ('type', 'model_guess'), ('actor', ''),
                             ('at', '2026-10-16T00:00:00'), ('quote', ''), ('quote', '伪造原话'),
                             ('sha256', '0'*64)]:
            changes.append({'confirmation': {**good['confirmation'], field: value}})
        for change in changes:
            with self.subTest(change=change):
                self.save(proof, {'boundary_transcripts': [{**good, **change}]})
                self.plan['clips'][1]['boundary_transcripts'][0]['evidence'].update(self.ref(proof))
                with self.assertRaises(ValueError):
                    self.build()
        self.save(proof, {'boundary_transcripts': [good, good]})
        self.plan['clips'][1]['boundary_transcripts'][0]['evidence'].update(self.ref(proof))
        with self.assertRaises(ValueError):
            self.build()

    def test_original_and_extraction_hashes_and_transcript_identity_are_required(self):
        bad = copy.deepcopy(self.plan)
        bad['clips'][0]['extraction']['sha256'] = '0'*64
        with self.assertRaises(ValueError):
            self.build(bad)
        transcript = copy.deepcopy(self.transcript)
        transcript['source']['sha256'] = '0'*64
        with self.assertRaises(ValueError):
            self.build(transcript=transcript)
        extracted = copy.deepcopy(self.extracted)
        extracted['source']['duration_seconds'] = 101
        self.save(self.extraction, extracted)
        for clip in self.plan['clips']:
            clip['extraction'] = self.ref(self.extraction)
        with self.assertRaises(ValueError):
            self.build()

    def test_order_candidate_bounds_and_confirmed_picture_are_enforced(self):
        for order in (['c2', 'c1'], ['c1', 'c1'], ['c1']):
            with self.subTest(order=order), self.assertRaises(ValueError):
                self.build(order=order)
        bad = copy.deepcopy(self.plan)
        bad['clips'][0]['window_range'] = [0, 2]
        with self.assertRaises(ValueError):
            self.build(bad)
        self.plan['clips'][0]['visual'] = dict(extraction=self.ref(self.extraction), window_range=[5, 7])
        with self.assertRaises(ValueError):
            self.build(confirmed_ranges=[[11, 14]])
        with self.assertRaises(ValueError):
            self.build(confirmed_ranges=[])

    def test_invalid_geometry_speed_hold_and_total_frames_are_rejected(self):
        for field, value in [('speed', True), ('speed', 1.51), ('speed', float('nan')),
                             ('window_range', [-1, 2]), ('window_range', [1, 11]),
                             ('purpose', ''), ('crop', dict(x=1, y=0, width=10, height=10))]:
            bad = copy.deepcopy(self.plan)
            bad['clips'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.build(bad)
        for hold in (True, -.1, 2.1, float('inf')):
            bad = copy.deepcopy(self.plan)
            bad['end_hold_seconds'] = hold
            with self.subTest(hold=hold), self.assertRaises(ValueError):
                self.build(bad)
        bad = copy.deepcopy(self.plan)
        bad['clips'][0]['visual'] = dict(extraction=self.ref(self.extraction), window_range=[5, 5.5])
        with self.assertRaises(ValueError):
            self.build(bad)

    def test_actual_window_span_not_requested_end_controls_source_mapping(self):
        self.extracted.update(requested_range=[10, 20.02], source_span=[10, 19.98])
        self.extracted['window']['duration_seconds'] = 9.98
        self.save(self.extraction, self.extracted)
        for clip in self.plan['clips']:
            clip['extraction'] = self.ref(self.extraction)
        self.plan['clips'][0]['visual'] = dict(extraction=self.ref(self.extraction), window_range=[9, 10])
        with self.assertRaises(ValueError):
            self.build()

    def test_existing_input_verifier_detects_changed_feedback(self):
        _, feedback, _ = self.add_boundary()
        record = self.build()
        verifier = importlib.reload(importlib.import_module('review_cut'))
        verifier.verify_inputs(record)
        feedback.write_text('changed feedback', encoding='utf-8')
        with self.assertRaises(ValueError):
            verifier.verify_inputs(record)

    def test_different_window_picture_requires_same_original_and_binds_every_file(self):
        second = self.workspace / 'private-context/source-windows/source/window-two/extraction.json'
        picture = second.parent / 'window.mp4'
        picture.parent.mkdir()
        picture.write_bytes(b'separate picture window')
        record = copy.deepcopy(self.extracted)
        record.update(requested_range=[30, 40], source_span=[30, 40], offset_seconds=30,
                      frame_grid={'expected_step_seconds': 1/30, 'aligned_start_seconds': 30},
                      window={'path': str(picture), 'sha256': self.support.file_hash(picture), 'duration_seconds': 10})
        self.save(second, record)
        self.plan['clips'][0]['visual'] = dict(extraction=self.ref(second), window_range=[1, 3])
        mapped = self.build(confirmed_ranges=[[10, 20], [30, 40]])
        self.assertAlmostEqual(mapped['segments'][0]['visual']['source_range'][0], 31)
        expected = {self.source['path'], str(self.extraction), self.extracted['window']['path'],
                    str(second), str(picture)}
        self.assertEqual(set(mapped['inputs']), expected)
        self.assertEqual(mapped, self.build(confirmed_ranges=[[10, 20], [30, 40]]))
        different_source = self.root / 'other.mp4'
        different_source.write_bytes(Path(self.source['path']).read_bytes())
        record['source']['path'] = str(different_source)
        self.save(second, record)
        self.plan['clips'][0]['visual']['extraction'] = self.ref(second)
        with self.assertRaises(ValueError):
            self.build(confirmed_ranges=[[10, 20], [30, 40]])

    def test_total_duration_uses_rounded_frames_and_hold(self):
        self.extracted.update(requested_range=[10, 90], source_span=[10, 90])
        self.extracted['window']['duration_seconds'] = 80
        self.save(self.extraction, self.extracted)
        self.transcript['segments'] = [dict(id='s000001', start=11, end=81, text='合成完整长句')]
        self.candidates = {'c1': dict(id='c1', kind='speech', start_seconds=11, end_seconds=81,
                                    start_segment_id='s000001', end_segment_id='s000001', text='合成完整长句')}
        self.order = ['c1']
        self.plan = dict(schema_version=1, clips=[dict(candidate_id='c1', extraction=self.ref(self.extraction),
                         window_range=[1, 71], purpose='合成边界长度')])
        mapped = self.build(confirmed_ranges=[[10, 90]])
        self.assertEqual(mapped['frame_count'], 2100)
        self.plan['end_hold_seconds'] = 1/30
        with self.assertRaises(ValueError):
            self.build(confirmed_ranges=[[10, 90]])

    def test_false_boundary_time_is_not_accepted_as_number_one(self):
        self.extracted.update(offset_seconds=0, requested_range=[0, 10], source_span=[0, 10],
                              frame_grid={'expected_step_seconds': 1/30, 'aligned_start_seconds': 0})
        self.save(self.extraction, self.extracted)
        self.transcript['segments'] = [dict(id='s000001', start=0, end=2, text='完整原句')]
        self.candidates = {'c1': dict(id='c1', kind='speech', start_seconds=0, end_seconds=2,
                                    start_segment_id='s000001', end_segment_id='s000001', text='完整原句')}
        feedback = self.workspace / 'private-context/feedback.txt'
        feedback.write_text('合成反馈：保留原句', encoding='utf-8')
        proof = self.workspace / 'private-context/proof.json'
        evidence_range = [True, 1.5]  # Python bool equality must not masquerade as second 1.
        entry = dict(source_sha256=self.source['sha256'], segment_ids=['s000001'],
                     range=evidence_range, text='保留原句', confirmation={
                         'status': 'confirmed', 'type': 'user_message', 'actor': 'synthetic reviewer',
                         'at': '2026-10-16T00:00:00+08:00', 'quote': '保留原句', **self.ref(feedback)})
        self.save(proof, {'boundary_transcripts': [entry]})
        self.order = ['c1']
        self.plan = dict(schema_version=1, clips=[dict(candidate_id='c1', extraction=self.ref(self.extraction),
                         window_range=[1, 1.5], purpose='合成部分原话', boundary_transcripts=[dict(
                             segment_id='s000001', source_range=evidence_range, text='保留原句',
                             evidence={'type': 'audio_listening', **self.ref(proof),
                                       'source_sha256': self.source['sha256'], 'segment_ids': ['s000001'],
                                       'range': evidence_range})])])
        with self.assertRaises(ValueError):
            self.build(confirmed_ranges=[[0, 10]])

    def test_unsupported_edit_fields_and_unbound_boundary_reference_are_rejected(self):
        bad = copy.deepcopy(self.plan)
        bad['clips'][0]['audio_gain'] = 4
        with self.assertRaises(ValueError):
            self.build(bad)
        proof, feedback, good = self.add_boundary()
        outside = self.root / 'outside-feedback.txt'
        outside.write_bytes(feedback.read_bytes())
        good['confirmation'].update(self.ref(outside))
        self.save(proof, {'boundary_transcripts': [good]})
        self.plan['clips'][1]['boundary_transcripts'][0]['evidence'].update(self.ref(proof))
        with self.assertRaises(ValueError):
            self.build()


if __name__ == '__main__':
    unittest.main()
