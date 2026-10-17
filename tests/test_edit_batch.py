"""Editorial timing is adopted and executed, never silently dropped."""
import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import batch_plan
import combine
from selector_support import key
import test_final_review_fixes as fixtures


class EditBatchTests(unittest.TestCase):
    def plan(self, root):
        path, _ = fixtures.FinalReviewFixes()._plan(root)
        plan = json.loads(path.read_text('utf-8'))
        plan['items'] = [{'item_id': 'item-1', 'task_id': 'clip-1', 'order': ['c1'],
                          'speed': 1, 'speed_reason': 'synthetic pacing',
                          'mainline': 'synthetic unit', 'completeness': 'synthetic complete statement',
                          'roles': {'c1': 'main'},
                          'edit_plan': {'schema_version': 1, 'clips': [], 'end_hold_seconds': .6}}]
        plan['exclusions'] = {}
        fixtures.save(path, plan)
        return path, plan

    def test_batch_uses_retained_audio_and_frame_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, plan = self.plan(root)
            timeline = {'raw_duration': 2.5, 'duration_seconds': 2.6, 'frame_count': 78}
            build = Mock(return_value=timeline)
            with patch.object(batch_plan, 'ROOT', root), patch.dict(sys.modules,
                    {'edit_timeline': types.SimpleNamespace(build=build)}):
                result = batch_plan.validate_plan(path)
            item = result['items']['item-1']
            self.assertEqual(item['raw_duration'], 2.5)
            self.assertEqual(item['expected_duration'], 2.6)
            self.assertEqual(item['render_timeline'], timeline)
            self.assertEqual(build.call_count, 1)
            self.assertEqual(build.call_args.args[0], plan['items'][0]['edit_plan'])

    def test_invalid_or_overlong_edit_plan_cannot_be_adopted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path, _ = self.plan(root)
            before = path.read_bytes()
            for build in (Mock(side_effect=ValueError('synthetic unconfirmed boundary')),
                          Mock(return_value={'raw_duration': 69, 'duration_seconds': 70.6})):
                with self.subTest(build=build), patch.object(batch_plan, 'ROOT', root), patch.dict(sys.modules,
                        {'edit_timeline': types.SimpleNamespace(build=build)}):
                    with self.assertRaises(ValueError):
                        batch_plan.validate_plan(path)
            self.assertEqual(path.read_bytes(), before)

    def test_edit_plan_change_revokes_existing_adoption_identity(self):
        item = {'item_id': 'i', 'task_id': 't', 'order': ['c'], 'speed': 1,
                'edit_plan': {'schema_version': 1, 'clips': [], 'end_hold_seconds': .6}}
        plan = {'batch_id': 'b', 'source': {'sha256': 's'}, 'product': {'name': 'synthetic'},
                'coverage': {}}
        first = batch_plan._fingerprint(plan, item, {'c': {'id': 'c'}})
        changed = copy.deepcopy(item)
        changed['edit_plan']['end_hold_seconds'] = .7
        self.assertNotEqual(first, batch_plan._fingerprint(plan, changed, {'c': {'id': 'c'}}))

    def test_render_must_match_adopted_edit_plan_and_cannot_omit_it(self):
        edit = {'schema_version': 1, 'clips': [], 'end_hold_seconds': .6}
        item = {'item_id': 'i', 'task_id': 't', 'order': ['c'], 'speed': 1, 'edit_plan': edit,
                'adoption': {'fingerprint': 'fp', 'user_evidence': {}, 'adopted_at': '2026-10-16T00:00:00+00:00'}}
        source = {'path': str(Path('synthetic-source.mp4').resolve()), 'sha256': 's'}
        plan = {'source': source, 'product': {'name': 'synthetic'},
                'coverage': {'confirmed_ranges': [[0, 10]]}, 'items': [item]}
        result = {'plan': plan, 'plan_path': 'synthetic-plan',
                  'items': {'i': {'adoption_fingerprint': 'fp', 'render_timeline': {'kind': 'synthetic'}}}}
        context = {'batch_ref': {'plan_path': 'synthetic-plan', 'item_id': 'i'}, 'task_id': 't',
                   'source_video': source['path'], 'source_sha256': 's', 'product': plan['product'],
                   'confirmed_windows': [{'range': [0, 10]}]}
        with patch.object(batch_plan, 'validate_plan', return_value=result), \
                patch.object(batch_plan, '_executable'), patch.object(batch_plan, '_protected'), \
                patch.object(batch_plan, '_revision_binding'), patch.object(batch_plan, 'evidence'):
            self.assertEqual(batch_plan.execution_gate(context, edit_plan=edit)['render_timeline'], {'kind': 'synthetic'})
            with self.assertRaisesRegex(ValueError, '编辑'):
                batch_plan.execution_gate(context, edit_plan=None)
            with self.assertRaisesRegex(ValueError, '编辑'):
                batch_plan.execution_gate(context, edit_plan={**edit, 'end_hold_seconds': .7})
            # Read-only quality callers can still check the adopted item without passing render arguments.
            self.assertEqual(batch_plan.execution_gate(context)['adoption_fingerprint'], 'fp')

    def test_candidate_envelope_limit_defers_to_validated_edit_duration(self):
        candidates = [{'id': 'c', 'kind': 'speech', 'start_seconds': 0, 'end_seconds': 100}]
        annotation = {'order': ['c'], 'speed': 1, 'speed_reason': 'synthetic',
                      'content_complete_reason': 'synthetic', 'edit_plan': {'schema_version': 1}}
        # The execution gate, not this preselection helper, validates and caps the retained timeline.
        self.assertEqual(combine.choose_explicit(candidates, annotation)['order'], ['c'])


class EditBatchVisualReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path, full = fixtures.FinalReviewFixes()._plan(self.root)
        self.plan = json.loads(self.path.read_text('utf-8'))
        job = Path(self.plan['job_path'])
        transcript = json.loads((job/'transcript.json').read_text('utf-8'))
        transcript['covered_ranges'] = [[20, 40]]
        transcript['segments'].append(dict(id='s000002', start=25, end=27, text='合成第二句'))
        transcript['segments_hash'] = key(transcript['segments'])
        fixtures.save(job/'transcript.json', transcript)
        manifest = json.loads((job/'manifest.json').read_text('utf-8'))
        manifest['transcript_hash'] = fixtures.file_hash(job/'transcript.json')
        fixtures.save(job/'manifest.json', manifest)
        data = json.loads((job/'candidate-clips.json').read_text('utf-8'))
        data['candidates'].append(dict(id='c2', kind='speech', start_seconds=25, end_seconds=27,
            start_segment_id='s000002', end_segment_id='s000002', text='合成第二句',
            category='合成分类', reason='合成测试'))
        fixtures.save(job/'candidate-clips.json', data)
        self.candidates = {c['id']: c for c in data['candidates']}
        self.plan['coverage']['confirmed_ranges'] = [[20, 40]]
        full_review = json.loads(full.read_text('utf-8'))
        full_review['confirmed_ranges'] = [[20, 40]]
        fixtures.save(full, full_review)
        self.plan['coverage']['full_review']['sha256'] = fixtures.file_hash(full)
        extraction = self.root/'private-context/source-windows/synthetic/window-one/extraction.json'
        window = extraction.parent/'window.mp4'
        window.parent.mkdir(parents=True)
        window.write_bytes(b'synthetic media identity only')
        fixtures.save(extraction, dict(schema_version=1, source=self.plan['source'],
            window=dict(path=str(window), sha256=fixtures.file_hash(window), duration_seconds=20),
            requested_range=[20,40], source_span=[20,40], offset_seconds=20,
            frame_grid=dict(expected_step_seconds=1/30, aligned_start_seconds=20)))
        self.reference = dict(path=str(extraction), sha256=fixtures.file_hash(extraction))
        self.plan['items'] = [dict(item_id=f'i{i}', task_id=f't{i}', order=[f'c{i}'], speed=1,
            speed_reason='合成节奏', mainline='合成主线', completeness='合成完整句', roles={f'c{i}':'main'},
            edit_plan=dict(schema_version=1, clips=[dict(candidate_id=f'c{i}', extraction=self.reference,
                window_range=span, purpose='合成原声')])) for i, span in [(1,[1,5]),(2,[5,7])]]
        self.plan.update(exclusions={}, reuse=[])
        import edit_timeline
        import source_windows
        for module, name, value in [(batch_plan, 'ROOT', self.root), (edit_timeline, 'ROOT', self.root),
                                    (source_windows, 'WINDOWS', self.root/'private-context/source-windows')]:
            change = patch.object(module, name, value)
            change.start()
            self.addCleanup(change.stop)

    def picture(self, item, span):
        self.plan['items'][item]['edit_plan']['clips'][0]['visual'] = dict(
            extraction=self.reference, window_range=span)

    def validate(self):
        self.plan['pair_reviews'] = [dict(items=['i1','i2'], mainline='合成主线比较', speech='合成口播比较',
            visual='合成画面比较', basis='合成测试，非业务审核',
            conclusion='disclosed_overlap' if self.plan['reuse'] else 'distinct',
            content_hash=batch_plan.pair_content_hash(self.plan, ['i1','i2'], self.candidates))]
        fixtures.save(self.path, self.plan)
        return batch_plan.validate_plan(self.path)

    def test_replacement_picture_cannot_borrow_other_main_audio_or_picture(self):
        for first, second in [([5,9],[10,12]), ([10,14],[1,3]), ([10,14],[11,13])]:
            with self.subTest(first=first, second=second):
                self.picture(0, first)
                self.picture(1, second)
                with self.assertRaisesRegex(ValueError, '挪用|重叠'):
                    self.validate()

    def test_replacement_picture_cannot_borrow_unedited_other_item(self):
        self.picture(0, [5,9])
        self.plan['items'][1].pop('edit_plan')
        with self.assertRaisesRegex(ValueError, '挪用|重叠'):
            self.validate()

    def test_different_auxiliary_candidates_do_not_authorize_picture_reuse(self):
        self.picture(0, [10,14])
        self.picture(1, [11,13])
        for item in self.plan['items']:
            item['roles'] = {cid:'auxiliary' for cid in item['order']}
        with self.assertRaisesRegex(ValueError, '挪用|重叠'):
            self.validate()

    def test_consumed_picture_ranges_can_touch_without_false_overlap(self):
        self.picture(0, [10,20])  # Only [10,14] is consumed by this four-second segment.
        self.picture(1, [14,16])
        result = self.validate()
        self.assertEqual(result['items']['i1']['render_timeline']['segments'][0]['visual']['source_range'], [30,34])

    def test_same_auxiliary_candidate_still_requires_exact_reuse_disclosure(self):
        second = self.plan['items'][1]
        second.update(order=['c1'], roles={'c1':'auxiliary'})
        second['edit_plan'] = copy.deepcopy(self.plan['items'][0]['edit_plan'])
        self.plan['items'][0]['roles'] = {'c1':'auxiliary'}
        self.plan['exclusions'] = {'c2':'合成未选用'}
        self.picture(0, [10,14])
        self.picture(1, [11,15])
        disclosure = dict(items=['i1','i2'], candidate_ids=['c1'], reason='合成明确披露同一辅助素材')
        self.plan['reuse'] = [disclosure]
        self.assertEqual(len(self.validate()['items']), 2)
        for values in ([], [{**disclosure, 'candidate_ids':['c1','c2']}]):
            with self.subTest(reuse=values):
                self.plan['reuse'] = values
                with self.assertRaisesRegex(ValueError, '声明'):
                    self.validate()

    def test_report_shows_actual_segment_speeds_and_hold(self):
        self.plan['items'][0]['edit_plan']['clips'][0]['speed'] = 1.25
        self.plan['items'][0]['edit_plan']['end_hold_seconds'] = .6
        self.plan['items'][1]['edit_plan']['clips'][0]['speed'] = 1.5
        self.validate()
        content = batch_plan.report(self.path)
        self.assertIn('逐段倍速：c1 1.25倍', content)
        self.assertIn('逐段倍速：c2 1.5倍', content)
        self.assertIn('结尾定格0.600秒', content)


if __name__ == '__main__':
    unittest.main()
