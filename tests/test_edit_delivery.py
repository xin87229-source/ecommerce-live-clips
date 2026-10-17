"""Synthetic timeline/byte fixtures: no merchant media or human review assertions."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import batch_plan
import publish_package as delivery
import quality_support as quality
import runtime
import subtitle as sub


class EditDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.composition = self.root / 'outputs/composition'
        self.composition.mkdir(parents=True)
        (self.root / 'private-context').mkdir()
        self.original = self.root / 'original.mp4'
        self.original.write_bytes(b'synthetic original; not playable')
        self.window = self.root / 'private-context/window.mp4'
        self.window.write_bytes(b'synthetic window; not playable')
        self.video = self.composition / 'final.mp4'
        self.video.write_bytes(b'synthetic assembled audio/video bytes; not playable')
        self.transcript_path = self.root / 'private-context/transcript.json'
        self.transcript = {'schema_version': 1, 'status': 'complete',
            'source': {'path': str(self.original), 'sha256': sub.file_hash(self.original), 'duration_seconds': 20},
            'asr': {'config_hash': 'synthetic-only'}, 'segments': [
                {'id': 's000001', 'start': 1, 'end': 3.61, 'text': '商品展示'},
                {'id': 's000002', 'start': 5, 'end': 8, 'text': '材质说明'}]}
        self.save(self.transcript_path, self.transcript)
        self.edit_plan = {'schema_version': 1, 'end_hold_seconds': .6, 'clips': [
            {'clip_id': 'c1', 'speed': 1.3}, {'clip_id': 'c2', 'speed': 1.5}]}
        self.timeline = {'schema_version': 1, 'kind': 'edit_timeline',
            'edit_plan_hash': sub.key(self.edit_plan), 'transcript_content_hash': sub.key(self.transcript),
            'source': self.transcript['source'], 'fps': 30, 'size': [1080, 1920],
            'raw_duration': 5.61, 'frame_count': 139, 'duration_seconds': 139 / 30,
            'inputs': {str(self.original): sub.file_hash(self.original), str(self.window): sub.file_hash(self.window)},
            'segments': [], 'mapped_segments': [], 'mapped_clips': [],
            'end_hold': {'requested_seconds': .6, 'seconds': .6, 'frame_count': 18,
                         'video_range': [121 / 30, 139 / 30], 'audio': 'silence'}}
        for index, (clip_id, speed, frame_range) in enumerate([('c1', 1.3, [0, 61]), ('c2', 1.5, [61, 121])]):
            original = self.transcript['segments'][index]
            source_range = [original['start'], original['end']]
            visual_range = [10, 13] if index == 1 else source_range
            audio = {'window': {'path': str(self.window), 'sha256': sub.file_hash(self.window)},
                     'source_range': source_range, 'window_range': source_range,
                     'source_sha256': sub.file_hash(self.original)}
            visual = dict(audio, source_range=visual_range, window_range=visual_range)
            self.timeline['segments'].append({'id': clip_id, 'speed': speed, 'audio': audio, 'visual': visual,
                'crop': {'x': 0, 'y': 0, 'width': 300, 'height': 500} if index else None,
                'non_synchronous_visual': bool(index), 'frame_range': frame_range,
                'frame_count': frame_range[1] - frame_range[0],
                'video_range': [frame_range[0] / 30, frame_range[1] / 30]})
            mapped = dict(original, clip_id=clip_id, speed=speed, final_start=frame_range[0] / 30,
                final_end=frame_range[0] / 30 + (original['end'] - original['start']) / speed,
                asr_start=original['start'], asr_end=original['end'], asr_text=original['text'])
            self.timeline['mapped_segments'].append(mapped)
            self.timeline['mapped_clips'].append(dict(clip_id=clip_id, kind='speech', start=original['start'],
                end=original['end'], final_start=frame_range[0] / 30, final_end=frame_range[1] / 30,
                segment_ids=[original['id']], speed=speed, frame_range=frame_range,
                audio_source_range=source_range, visual_source_range=visual_range,
                crop=self.timeline['segments'][-1]['crop'], non_synchronous_visual=bool(index)))
        self.candidates = {'transcript_cache': str(self.transcript_path), 'candidates': [
            {'id': f'c{i}', 'start_segment_id': s['id'], 'end_segment_id': s['id'],
             'start_seconds': s['start'], 'end_seconds': s['end']}
            for i, s in enumerate(self.transcript['segments'], 1)]}
        self.result = {'version': '0.2b', 'status': 'completed', 'final_video': str(self.video),
            'video_sha256': sub.file_hash(self.video), 'source_sha256_after': sub.file_hash(self.original),
            'material': {'source_video': str(self.original), 'date': None, 'product_name': '合成测试商品'},
            'best_combination': {'order': ['c1', 'c2'], 'speed': 1.3, 'raw_duration': 5.61,
                                 'edit_plan': self.edit_plan}, 'metadata': {'duration': 139 / 30},
            'task_context': {'content_strategy': {'version': 'single-style-batch-v1'},
                             'batch_adoption_fingerprint': 'synthetic-adoption'},
            'render_timeline': self.timeline}
        self.save(self.composition / 'candidate-clips.json', self.candidates)
        self.save(self.composition / 'result.json', self.result)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(sub, 'ROOT', self.root))
        self.receipt = {'adoption_fingerprint': 'synthetic-adoption', 'render_timeline': copy.deepcopy(self.timeline)}
        self.gate = self.stack.enter_context(patch.object(batch_plan, 'execution_gate', return_value=self.receipt))

    @staticmethod
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def test_extract_uses_frame_timeline_and_does_not_caption_silent_tail(self):
        selected = sub.extract(self.composition)
        self.assertEqual(self.gate.call_args.kwargs['edit_plan'], self.edit_plan)
        self.assertEqual(selected['segments'], self.timeline['mapped_segments'])
        self.assertEqual(selected['clips'], self.timeline['mapped_clips'])
        self.assertEqual(selected['render_timeline'], self.timeline)
        self.assertEqual(selected['edit_plan'], self.edit_plan)
        review = {'selected_hash': sub.key(selected), 'cues': [
            {'segment_ids': [s['id']], 'lines': [s['text']]} for s in selected['segments']]}
        cues, _ = sub.cues_from_review(selected, review, sub.read(sub.STYLE), sub.read(sub.DICTIONARY))
        self.assertAlmostEqual(cues[1]['start'], 61 / 30)
        self.assertAlmostEqual(cues[-1]['end'], 121 / 30)
        self.assertLess(cues[-1]['end'], selected['duration'])
        self.assertEqual(len(cues), 2)

    def test_stale_timeline_input_or_rendered_media_is_rejected(self):
        for kind in ('timeline', 'window', 'video', 'missing_hash', 'missing_plan'):
            with self.subTest(kind=kind):
                result = copy.deepcopy(self.result)
                before = self.window.read_bytes(), self.video.read_bytes()
                if kind == 'timeline': result['render_timeline']['segments'][1]['speed'] = 1.4
                if kind == 'window': self.window.write_bytes(b'changed synthetic window')
                if kind == 'video': self.video.write_bytes(b'changed synthetic video')
                if kind == 'missing_hash': result.pop('video_sha256')
                if kind == 'missing_plan': result['best_combination'].pop('edit_plan')
                self.save(self.composition / 'result.json', result)
                with self.assertRaises(ValueError): sub.extract(self.composition)
                self.window.write_bytes(before[0]); self.video.write_bytes(before[1])
        self.save(self.composition / 'result.json', self.result)

    def test_selection_identity_changes_for_every_edit_dependency(self):
        selected = {'original_sha256': sub.file_hash(self.original), 'speed': 1.3,
                    'segments': self.timeline['mapped_segments'], 'clips': self.timeline['mapped_clips'],
                    'render_timeline': self.timeline, 'edit_plan': self.edit_plan}
        before = quality.selection_binding(selected)
        mutations = [lambda t: t['segments'][1].update(crop=None),
                     lambda t: t['segments'][1].update(speed=1.4),
                     lambda t: t['segments'][1]['audio'].update(source_range=[5.1, 8]),
                     lambda t: t['segments'][1]['visual'].update(source_range=[11, 14]),
                     lambda t: t['end_hold'].update(seconds=.5),
                     lambda t: t['mapped_segments'][0].update(boundary_evidence={'sha256': 'synthetic-new-evidence'})]
        for change in mutations:
            changed = copy.deepcopy(selected)
            change(changed['render_timeline'])
            self.assertNotEqual(before, quality.selection_binding(changed))

    def test_missing_or_changed_adoption_receipt_rejects_existing_edit(self):
        for field, value in (('render_timeline', None), ('adoption_fingerprint', None),
                             ('adoption_fingerprint', 'synthetic-new-adoption')):
            with self.subTest(field=field, value=value):
                receipt = copy.deepcopy(self.receipt)
                if value is None:
                    receipt.pop(field)
                else:
                    receipt[field] = value
                self.gate.return_value = receipt
                with self.assertRaises(ValueError): sub.extract(self.composition)
        self.gate.return_value = self.receipt

    def test_package_records_actual_speeds_hold_and_edit_identity(self):
        (self.root / 'VERSION').write_text('9.8.7-synthetic\n', encoding='utf-8')
        selected = sub.extract(self.composition)
        final = self.root / 'outputs/subtitled'
        (final / 'logs').mkdir(parents=True)
        video = final / 'final.mp4'
        video.write_bytes(b'synthetic subtitled final; not playable')
        (final / 'subtitle.srt').write_text('synthetic subtitle', encoding='utf-8')
        source_result = {'version': '0.2c', 'status': 'completed', 'video': str(video),
                         'metadata': {'duration': 139 / 30}, 'render_timeline': self.timeline,
                         'edit_plan': self.edit_plan,
                         'source_hashes_after': {k: selected[k] for k in ('video_sha256', 'original_sha256')},
                         'quality_version': 'synthetic'}
        self.save(final / 'result.json', source_result)
        self.save(final / 'logs/selected.json', selected)
        self.save(final / 'logs/cues.json', [{'start': 0, 'lines': ['商品展示']}])
        class SyntheticMedia:
            def __init__(self, *args): pass
            def check_frame_timing(self, *args): return {'status': 'passed', 'synthetic_only': True}
            def probe(self, *args): return {'width': 1080, 'height': 1920, 'duration': 139 / 30}
            def write_media(self, args, path): Image.new('RGB', (360, 640), 'blue').save(path)
        with patch.object(delivery, 'ROOT', self.root), patch.object(runtime, 'ROOT', self.root), \
                patch.object(delivery, 'INSTALL_ROOT', self.root), \
                patch.object(delivery, 'task_gate', return_value=dict(self.result['task_context'],
                    task_record='synthetic-task', batch_ref={'plan_path': 'synthetic-plan', 'item_id': 'i1'})), \
                patch.object(delivery, 'no_resubmit'), patch.object(delivery, 'local_records', return_value=[]), \
                patch.object(delivery, 'current_media_quality', return_value={}), \
                patch.object(delivery, 'production_hashes', return_value={}), \
                patch.object(delivery, 'MediaAdapter', SyntheticMedia):
            package = delivery.prepare(final, 'delivery-edit', None)
            info = sub.read(package / 'publish.json')
            self.assertEqual(info['production']['code_version'], '9.8.7-synthetic')
            self.assertEqual(info['production']['release_version'], '9.8.7-synthetic')
            self.assertIsNone(info['production']['git_revision'])
            self.assertEqual(info['production']['speed_mode'], 'per_segment')
            self.assertIsNone(info['production']['speed'])
            self.assertEqual([s['speed'] for s in info['production']['segment_speeds']], [1.3, 1.5])
            self.assertEqual(info['production']['end_hold_seconds'], .6)
            self.assertEqual(info['production']['edit_plan_hash'], self.timeline['edit_plan_hash'])
            self.assertEqual(info['production']['render_timeline_hash'], sub.key(self.timeline))
            changed = copy.deepcopy(selected)
            changed['render_timeline']['end_hold']['seconds'] = 1
            self.save(final / 'logs/selected.json', changed)
            with self.assertRaises(ValueError): delivery.prepare(final, 'delivery-stale-edit', None)
            self.assertFalse((self.root / 'outputs/delivery-stale-edit').exists())
            # Removing new fields cannot downgrade an edited composition to the legacy path.
            stripped = sub.extract(self.composition)
            stripped.pop('render_timeline')
            stripped.pop('edit_plan')
            source_result.pop('render_timeline')
            self.save(final / 'logs/selected.json', stripped)
            self.save(final / 'result.json', source_result)
            with self.assertRaises(ValueError): delivery.prepare(final, 'delivery-downgrade', None)
            self.assertFalse((self.root / 'outputs/delivery-downgrade').exists())
            # The adopted-plan receipt remains edited even if every output marker is stripped.
            all_stripped = copy.deepcopy(self.result)
            all_stripped.pop('render_timeline')
            all_stripped['best_combination'].pop('edit_plan')
            self.save(self.composition / 'result.json', all_stripped)
            source_result.pop('edit_plan')
            self.save(final / 'result.json', source_result)
            with self.assertRaises(ValueError): delivery.prepare(final, 'delivery-all-markers-removed', None)
            self.assertFalse((self.root / 'outputs/delivery-all-markers-removed').exists())
            # Removing the composition's batch context must not hide the task's adopted timeline.
            all_stripped['task_context'] = {}
            all_stripped['metadata']['duration'] = 5.61 / 1.3
            self.save(self.composition / 'result.json', all_stripped)
            self.save(final / 'logs/selected.json', sub.extract(self.composition))
            with self.assertRaises(ValueError): delivery.prepare(final, 'delivery-context-removed', None)
            self.assertFalse((self.root / 'outputs/delivery-context-removed').exists())

    def test_release_identity_reads_current_version_without_git(self):
        version = self.root / 'VERSION'
        version.write_text('9.8.7-synthetic\n', encoding='utf-8')
        with patch.object(delivery, 'INSTALL_ROOT', self.root):
            self.assertEqual(delivery.release_identity(),
                             {'release_version': '9.8.7-synthetic', 'git_revision': None})
            version.write_text('9.8.8-synthetic\n', encoding='utf-8')
            self.assertEqual(delivery.release_identity()['release_version'], '9.8.8-synthetic')

    def test_edit_markers_cannot_be_removed_to_downgrade_approved_timeline(self):
        stripped = copy.deepcopy(self.result)
        stripped.pop('render_timeline')
        stripped['best_combination'].pop('edit_plan')
        stripped['metadata']['duration'] = 5.61 / 1.3
        self.save(self.composition / 'result.json', stripped)
        with self.assertRaisesRegex(ValueError, '编辑'):
            sub.extract(self.composition)

    def test_old_composition_without_edit_plan_keeps_uniform_mapping(self):
        result = copy.deepcopy(self.result)
        result.pop('render_timeline')
        result['best_combination'].pop('edit_plan')
        result['metadata']['duration'] = 5.61 / 1.3
        self.save(self.composition / 'result.json', result)
        self.receipt.pop('render_timeline')
        selected = sub.extract(self.composition)
        self.assertNotIn('render_timeline', selected)
        self.assertNotIn('edit_plan', self.gate.call_args.kwargs)
        self.assertAlmostEqual(selected['segments'][1]['final_start'], 2.61 / 1.3)


if __name__ == '__main__':
    unittest.main()
