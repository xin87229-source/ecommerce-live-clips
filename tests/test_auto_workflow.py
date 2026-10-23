"""Resumption must execute work, preserve evidence, and reject stale reuse."""
import importlib
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))


class AutoWorkflowTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / '工作区' / 'test-auto'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'ECLIP_WORKSPACE': str(self.root)})
        self.env.start()
        importlib.reload(importlib.import_module('runtime'))
        self.card = self.root / 'outputs/task/run-state.json'
        self.card.parent.mkdir(parents=True)
        self.source = self.root / 'source.bin'
        self.source.write_bytes(b'original')
        self.card.write_text(json.dumps({'status': 'NOT_UPLOADED_NOT_SUBMITTED', 'submitted': False,
                                        'task_context': {'source_video': str(self.source)}}))
        self.request = {'run_id': 'unit', 'task_record': str(self.card),
                        'windows': [{'start': 1, 'end': 3}], 'category': 'dress'}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def api(self):
        return importlib.reload(importlib.import_module('auto_workflow'))

    def test_runs_and_resumes_real_stage_calls_without_fake_approval(self):
        api = self.api()
        extraction = self.root / 'private-context/window/extraction.json'
        extraction.parent.mkdir(parents=True)
        extraction.write_text('{}')
        preview = extraction.parent / 'asr.json'
        preview.write_text('{}')
        log = extraction.parent / 'media.log'
        log.write_text('extract probe\n')
        def transcribe_action(_):
            with log.open('a') as stream:
                stream.write('ASR probe legitimately appended\n')
            return {'preview_path': str(preview)}
        with patch.object(api.source_windows, 'original_context', return_value=(self.card, {}, {'task_id': 'task'}, self.source)), patch.object(
                api.source_windows, 'extract', return_value={'extraction': str(extraction)}) as extract, patch.object(
                api.source_windows, 'transcribe_preview', side_effect=transcribe_action) as transcribe:
            report = api.run(self.request)
            self.assertEqual(report['status'], 'needs_agent_decision')
            self.assertEqual(api.run(self.request), report)
            extract.assert_called_once()
            transcribe.assert_called_once()
            with self.assertRaisesRegex(ValueError, '核心输入变化'):
                api.run(dict(self.request, category='tops'))
            extraction.write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError, '变化|哈希'):
                api.run(self.request)

    def test_concurrent_same_run_is_rejected_before_work(self):
        api = self.api()
        folder = api.folder_for('unit')
        folder.mkdir(parents=True)
        (folder / 'execution.lock').write_text('another process')
        with patch.object(api.source_windows, 'extract') as extract:
            with self.assertRaises(FileExistsError):
                api.run(self.request)
            extract.assert_not_called()

    def test_status_rejects_code_or_config_changes(self):
        api = self.api()
        path = api.folder_for('unit') / 'state.json'
        api.save(path, {'code_bindings': {}, 'bindings': {}, 'stages': {}, 'report': {}})
        with self.assertRaisesRegex(ValueError, '源码'):
            api.status('unit')
        bound = api.code_bindings()
        pipeline = Path(api.__file__).parent
        self.assertTrue(all(str(p) in bound for p in pipeline.glob('*.json')))
        self.assertTrue(all(str(p) in bound for p in (pipeline / 'prompts').glob('*') if p.is_file()))

    def test_custom_card_requires_current_window_and_exact_current_preview(self):
        api = self.api()
        extraction = str(self.root / 'extraction.json')
        cut = {'preview': str(self.source)}
        valid = {'extraction': extraction, 'versions': [{'path': str(self.source), 'sha256': api.digest(self.source)}]}
        api.validate_card_binding(valid, {extraction}, cut)
        with self.assertRaises(ValueError):
            api.validate_card_binding(dict(valid, extraction=str(self.root / 'other.json')), {extraction}, cut)
        with self.assertRaises(ValueError):
            api.validate_card_binding(dict(valid, versions=[]), {extraction}, cut)

    def test_production_cannot_cross_product_even_on_same_source(self):
        api = self.api()
        context = {'source_video': str(self.source), 'source_sha256': api.digest(self.source),
                   'product': {'name': 'A', 'code': 'A', 'reference': {'sha256': 'A'}}}
        api.validate_production_context(context, context)
        with self.assertRaises(ValueError):
            api.validate_production_context(context, dict(context, product={'name': 'B'}))

    def test_exact_extraction_recovery_rechecks_evidence_without_reextracting(self):
        api = self.api()
        base = self.root / 'private-context/source-windows'
        extraction = base / 'task/window-existing/extraction.json'
        extraction.parent.mkdir(parents=True)
        frame = extraction.parent / 'frame.jpg'
        frame.write_bytes(b'frame')
        context = {'task_id': 'task', 'source_sha256': api.digest(self.source)}
        extraction.write_text(json.dumps({'task_record': str(self.card), 'task_id': 'task',
            'requested_range': [1, 3], 'source': {'path': str(self.source), 'sha256': api.digest(self.source)},
            'sample_frames': [{'path': str(frame), 'sha256': api.digest(frame)}]}))
        with patch.object(api.source_windows, 'WINDOWS', base), patch.object(
                api.source_windows, 'original_context', return_value=(self.card, {}, context, self.source)), patch.object(
                api.source_windows, 'import_entry') as validate, patch.object(api.source_windows, 'extract') as extract:
            result = api.extract_or_resume(self.card, {'start': 1, 'end': 3})
            self.assertEqual(result['extraction'], str(extraction))
            validate.assert_called_once_with(extraction)
            extract.assert_not_called()
            frame.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, '哈希'):
                api.extract_or_resume(self.card, {'start': 1, 'end': 3})

    def test_rejects_changed_core_and_arbitrary_commands(self):
        api = self.api()
        for extra in ({'command': 'echo bad'}, {'run_id': '../escape'}, {'windows': []}):
            with self.assertRaises(ValueError):
                api.run(dict(self.request, **extra))

    def test_state_cannot_escape_through_symlink_or_task_path(self):
        api = self.api()
        with self.assertRaises(ValueError):
            api.run(dict(self.request, task_record=str(self.root.parent / 'other.json')))

    def test_automatic_card_preserves_original_and_preview_ranges(self):
        api = self.api()
        folder = self.root / 'private-context/auto-workflows/unit'
        record = self.root / 'preview.json'
        video = self.root / 'preview.mp4'
        video.write_bytes(b'synthetic preview')
        extraction = str(self.root / 'extraction.json')
        record.write_text(json.dumps({'segments': [{'purpose': '展示',
            'audio': {'extraction': {'path': extraction}, 'window_range': [1, 2]},
            'video_range': [0, .8], 'questions': ['原话未核验']}]}))
        result = api.automatic_card(folder, [{'extraction': extraction, 'asr_preview': 'asr.json'}],
                                    {'record': str(record), 'preview': str(video), 'duration_seconds': .8})
        card = json.loads(result.read_text('utf-8'))
        self.assertEqual(card['questions'][0]['window_range'], [1, 2])
        self.assertEqual(card['questions'][0]['video_ranges'][0]['range'], [0, .8])
        self.assertEqual(card['versions'][0]['sha256'], api.digest(video))

    def test_production_calls_existing_gates_and_finishes_unreviewed(self):
        api = self.api()
        import combine
        import subtitle
        import publish_package
        spec = {}
        for field in ('job', 'annotations', 'source_review', 'task_record', 'subtitle_review', 'draft'):
            path = self.root / (field + '.json')
            path.write_text('{}')
            spec[field] = str(path)
        composition = self.root / 'outputs/composition'
        subtitled = self.root / 'outputs/subtitle'
        prepared = self.root / 'outputs/prepared/publish-package'
        prepared.mkdir(parents=True)
        (prepared / 'publish.json').write_text('{}')
        (prepared / 'logs').mkdir()
        (prepared / 'frames').mkdir()
        frame = prepared / 'frames/frame-01.jpg'
        frame.write_bytes(b'synthetic copied frame')
        (prepared / 'logs/frames.json').write_text(json.dumps([
            {'id': 1, 'path': str(frame), 'sha256': api.digest(frame), 'time': .5}]))
        calls = []
        def stage(name, inputs, action, outputs):
            calls.append(name)
            return action('test')
        with patch.object(combine, 'execute', return_value={'final_video': str(composition / 'final/video.mp4')}) as combine_call, patch.object(
                subtitle, 'extract', return_value={'segments': []}), patch.object(
                subtitle, 'run', return_value={'video': str(subtitled / 'final/video.mp4')}) as subtitle_call, patch.object(
                publish_package, 'prepare', return_value=prepared) as prepare_call, patch.object(
                publish_package, 'finish', return_value={'status': 'awaiting_content_review'}) as finish_call:
            report = api.production(spec, stage, 'unit', {})
        self.assertEqual(calls, ['combine', 'subtitle', 'package-prepare', 'package-finish'])
        self.assertEqual(report['status'], 'completed_unreviewed')
        self.assertEqual(report['current_artifact']['kind'], 'formal_local_delivery_unreviewed')
        self.assertEqual(report['current_artifact']['video'], str(Path(report['delivery']['path']) / 'video.mp4'))
        self.assertEqual(subtitle_call.call_args.args[0], composition)
        self.assertEqual(prepare_call.call_args.args[0], subtitled)
        self.assertNotEqual(finish_call.call_args.args[0], prepared)
        new_package = finish_call.call_args.args[0]
        copied_frame = json.loads((new_package / 'logs/frames.json').read_text('utf-8'))[0]
        self.assertEqual(Path(copied_frame['path']).parent, new_package / 'frames')
        self.assertEqual(copied_frame['sha256'], api.digest(frame))
        self.assertEqual(combine_call.call_args.args[4], Path(spec['source_review']))

    def rehearsal_fixture(self):
        """Keep state/files real; replace only expensive media operations."""
        api = self.api()
        rehearsal = importlib.reload(importlib.import_module('rehearsal'))
        folder = self.root / 'private-context/window'
        folder.mkdir(parents=True)
        extraction, preview = folder / 'extraction.json', folder / 'asr.json'
        extraction.write_text('{}')
        preview.write_text('{}')
        cut = self.root / 'private-context/cut.json'
        cut.write_text(json.dumps({'task_record': str(self.card), 'clips': [
            {'extraction': str(extraction), 'window_range': [0, 1]}]}))
        spec = self.root / 'private-context/rehearsal.json'
        spec.write_text(json.dumps({'cut_spec': str(cut), 'asr_previews': [str(preview)]}))
        request = dict(self.request, rehearsal_spec=str(spec))
        calls = []

        def build(path, task_id):
            package = self.root / 'outputs/reviews/rehearsals' / task_id
            package.mkdir(parents=True, exist_ok=False)
            for name in ('index.html', 'draft-video.mp4', 'simulation.json'):
                (package / name).write_text('synthetic media boundary fixture')
            calls.append(package)
            return {'package': str(package), 'status': 'SIMULATED_NOT_SUBMITTED',
                    'uploaded': False, 'submitted': False, 'platform_ui_verified': False}

        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(api.source_windows, 'original_context', return_value=(self.card, {}, {'task_id': 'task'}, self.source)))
        stack.enter_context(patch.object(api.source_windows, 'extract', return_value={'extraction': str(extraction)}))
        stack.enter_context(patch.object(api.source_windows, 'transcribe_preview', return_value={'preview_path': str(preview)}))
        stack.enter_context(patch.object(rehearsal, 'build', side_effect=build))
        checked = stack.enter_context(patch.object(rehearsal, 'check', return_value={'status': 'SIMULATED_NOT_SUBMITTED'}))
        return api, rehearsal, request, spec, cut, preview, calls, checked

    def test_rehearsal_runs_resumes_and_exposes_pending_artifact(self):
        api, _, request, _, _, _, calls, checked = self.rehearsal_fixture()
        original = self.card.read_bytes()
        report = api.run(request)
        self.assertEqual(report['status'], 'needs_content_review')
        self.assertEqual(report['current_artifact']['kind'], 'unreviewed_rehearsal')
        self.assertTrue(Path(report['current_artifact']['index']).is_file())
        self.assertEqual(report['rehearsal']['status'], 'SIMULATED_NOT_SUBMITTED')
        self.assertFalse(report['submitted'])
        self.assertEqual(report['human_review'], 'not_reviewed')
        self.assertEqual(api.run(request), report)
        self.assertEqual(api.status('unit'), report)
        self.assertEqual(len(calls), 1)
        self.assertTrue(checked.called)
        self.assertEqual(self.card.read_bytes(), original)

    def test_rehearsal_missing_spec_fails_and_keeps_state_recoverable(self):
        api, _, request, spec, _, _, calls, _ = self.rehearsal_fixture()
        spec.unlink()
        with self.assertRaises(FileNotFoundError):
            api.run(request)
        self.assertFalse(calls)
        self.assertNotIn('rehearsal', api.read(api.folder_for('unit') / 'state.json')['stages'])

    def test_rehearsal_changed_inputs_or_removed_request_do_not_reuse(self):
        api, _, request, spec, cut, _, calls, _ = self.rehearsal_fixture()
        api.run(request)
        with self.assertRaisesRegex(ValueError, 'rehearsal_spec'):
            api.run(self.request)
        original = cut.read_bytes()
        cut.write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, '变化|哈希'):
            api.status('unit')
        cut.write_bytes(original)
        spec.write_text(spec.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, '变化|哈希'):
            api.run(request)
        self.assertEqual(len(calls), 1)

    def test_rehearsal_rejects_other_task_window_or_asr_before_build(self):
        api, _, request, spec, cut, preview, calls, _ = self.rehearsal_fixture()
        original_cut = json.loads(cut.read_text())
        original_spec = json.loads(spec.read_text())
        outside = self.root / 'private-context/other.json'
        outside.write_text('{}')
        for changed_cut, changed_spec in [
                (dict(original_cut, task_record=str(outside)), original_spec),
                (dict(original_cut, clips=[{'extraction': str(outside)}]), original_spec),
                (original_cut, dict(original_spec, asr_previews=[str(outside)]))]:
            cut.write_text(json.dumps(changed_cut))
            spec.write_text(json.dumps(changed_spec))
            with self.assertRaisesRegex(ValueError, '本次|当前'):
                api.run(request)
        self.assertFalse(calls)

    def test_rehearsal_same_name_collision_preserves_output_then_retries(self):
        api, _, request, _, _, _, calls, _ = self.rehearsal_fixture()
        # First run caches only extraction/ASR, leaving rehearsal unexecuted.
        api.run(self.request)
        old = self.root / 'outputs/reviews/rehearsals/unit-r-fixed000'
        old.mkdir(parents=True)
        (old / 'keep.txt').write_text('keep old output')
        class FixedId:
            hex = 'fixed000rest'
        with patch.object(api.uuid, 'uuid4', return_value=FixedId()):
            with self.assertRaises(FileExistsError):
                api.run(request)
        self.assertEqual((old / 'keep.txt').read_text(), 'keep old output')
        self.assertNotIn('rehearsal', api.read(api.folder_for('unit') / 'state.json')['stages'])
        report = api.run(request)
        self.assertEqual(report['status'], 'needs_content_review')
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(Path(report['current_artifact']['package']), old)

    def test_rehearsal_status_runs_existing_package_integrity_check(self):
        api, _, request, _, _, _, _, checked = self.rehearsal_fixture()
        api.run(request)
        checked.side_effect = ValueError('演练状态矛盾，不能标记发布或审核通过')
        with self.assertRaisesRegex(ValueError, '演练状态矛盾'):
            api.status('unit')

    def test_rehearsal_explicit_null_visual_keeps_default_source_picture(self):
        api, _, request, _, cut, _, calls, _ = self.rehearsal_fixture()
        provided = json.loads(cut.read_text())
        provided['clips'][0]['visual'] = None
        cut.write_text(json.dumps(provided))
        report = api.run(request)
        self.assertEqual(report['status'], 'needs_content_review')
        self.assertEqual(len(calls), 1)
        self.assertEqual(report['human_review'], 'not_reviewed')
        self.assertEqual(api.status('unit'), report)

    def delivery_fixture(self):
        api, _, request, _, _, _, calls, _ = self.rehearsal_fixture()
        draft = api.run(request)
        spec = self.root / 'private-context/delivery.json'
        spec.write_text(json.dumps({'rehearsal': draft['rehearsal']['package']}))
        import reviewed_delivery
        builds = []

        def build(path, task_id):
            package = self.root / 'outputs/deliveries' / task_id
            package.mkdir(parents=True)
            for name in ('index.html', 'video.mp4', 'delivery.json'):
                (package / name).write_text('synthetic stage fixture, not human evidence')
            builds.append(package)
            return {'package': str(package), 'status': 'LOCAL_DELIVERY_NOT_SUBMITTED',
                    'new_visual_review': 'not_reviewed',
                    'content_confirmation': 'prior_accepted_content_preserved', 'submitted': False}

        stack = ExitStack()
        self.addCleanup(stack.close)
        builder = stack.enter_context(patch.object(reviewed_delivery, 'build', side_effect=build))
        checker = stack.enter_context(patch.object(reviewed_delivery, 'check', side_effect=lambda package: {
            'package': str(package), 'status': 'LOCAL_DELIVERY_NOT_SUBMITTED',
            'new_visual_review': 'not_reviewed',
            'content_confirmation': 'prior_accepted_content_preserved', 'submitted': False}))
        return api, dict(request, delivery_spec=str(spec)), spec, calls, builds, builder, checker

    def test_accepted_delivery_resumes_without_rendering_or_claiming_new_visual_review(self):
        api, request, _, drafts, builds, builder, checker = self.delivery_fixture()
        original = self.card.read_bytes()
        report = api.run(request)
        self.assertEqual(report['status'], 'local_delivery_ready')
        self.assertEqual(report['current_artifact']['kind'], 'reviewed_local_delivery')
        self.assertEqual(report['current_artifact']['package'], str(builds[0]))
        self.assertEqual(report['current_artifact']['video'], str(builds[0] / 'video.mp4'))
        self.assertEqual(report['human_review'], 'prior_accepted_content_preserved')
        self.assertEqual(report['new_visual_review'], 'not_reviewed')
        self.assertFalse(report['submitted'])
        self.assertEqual(api.run(request), report)
        self.assertEqual(api.status('unit'), report)
        self.assertEqual(len(drafts), 1)
        builder.assert_called_once()
        checker.assert_called_with(str(builds[0]))
        self.assertEqual(self.card.read_bytes(), original)

    def test_completed_delivery_is_found_after_final_report_write_is_interrupted(self):
        api, request, _, _, builds, builder, _ = self.delivery_fixture()
        api.run(request)
        state_path = api.folder_for('unit') / 'state.json'
        state = api.read(state_path)
        state['report'] = {'run_id': 'unit', 'status': 'needs_content_review',
                           'human_review': 'not_reviewed', 'submitted': False}
        api.save(state_path, state)
        report = api.status('unit')
        self.assertEqual(report['status'], 'local_delivery_ready')
        self.assertEqual(report['current_artifact']['package'], str(builds[0]))
        self.assertEqual(report['human_review'], 'prior_accepted_content_preserved')
        builder.assert_called_once()

    def test_existing_delivery_does_not_hide_later_stage_failure(self):
        api, request, _, _, _, _, _ = self.delivery_fixture()
        api.run(request)
        cut = api.read(Path(request['rehearsal_spec']))['cut_spec']
        with patch.object(api.review_cut, 'plan'), patch.object(
                api.review_cut, 'run', side_effect=RuntimeError('new comparison interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'comparison interrupted'):
                api.run(dict(request, cut_spec=cut))
        report = api.status('unit')
        self.assertEqual(report['status'], 'ERROR')
        self.assertEqual(report['failed_stage'], 'cut')
        self.assertIn('comparison interrupted', report['error'])

    def test_delivery_requires_current_rehearsal_and_excludes_old_production_branch(self):
        api = self.api()
        for request in (dict(self.request, delivery_spec='not-read.json'),
                        dict(self.request, rehearsal_spec='not-read.json',
                             delivery_spec='not-read.json', production={})):
            with patch.object(api.source_windows, 'original_context') as source:
                with self.assertRaisesRegex(ValueError, 'delivery_spec|production'):
                    api.run(request)
                source.assert_not_called()

    def test_delivery_cannot_attach_another_accepted_rehearsal(self):
        api, request, spec, _, builds, builder, _ = self.delivery_fixture()
        other = self.root / 'outputs/reviews/rehearsals/another'
        other.mkdir()
        spec.write_text(json.dumps({'rehearsal': str(other)}))
        with self.assertRaisesRegex(ValueError, '本次.*演练'):
            api.run(request)
        builder.assert_not_called()
        self.assertFalse(builds)

    def test_delivery_rejects_missing_or_changed_completed_spec(self):
        api, request, spec, _, _, builder, _ = self.delivery_fixture()
        api.run(request)
        without = dict(request)
        del without['delivery_spec']
        with self.assertRaisesRegex(ValueError, 'delivery_spec'):
            api.run(without)
        spec.write_text(spec.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, '变化|哈希'):
            api.status('unit')
        builder.assert_called_once()

    def test_delivery_failure_preserves_attempt_and_retries_with_fresh_output(self):
        api, request, _, drafts, builds, builder, _ = self.delivery_fixture()
        successful_build = builder.side_effect

        def fail(path, task_id):
            result = successful_build(path, task_id)
            (Path(result['package']) / 'failure.json').write_text('render interrupted')
            raise RuntimeError('render interrupted')

        builder.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'render interrupted'):
            api.run(request)
        failure = api.status('unit')
        self.assertEqual(failure['status'], 'ERROR')
        self.assertEqual(failure['failed_stage'], 'delivery')
        self.assertNotIn('delivery', failure['completed_stages'])
        old = builds[0]
        before = {p.name: p.read_bytes() for p in old.iterdir()}
        builder.side_effect = successful_build
        result = api.run(request)
        self.assertEqual(result['status'], 'local_delivery_ready')
        self.assertNotEqual(Path(result['delivery']['package']), old)
        self.assertEqual(before, {p.name: p.read_bytes() for p in old.iterdir()})
        self.assertEqual(len(drafts), 1)

    def test_delivery_rejection_never_becomes_success_and_status_rechecks_nested_evidence(self):
        api, request, _, _, _, builder, checker = self.delivery_fixture()
        action = builder.side_effect
        builder.side_effect = ValueError('缺少真实完整确认')
        with self.assertRaisesRegex(ValueError, '完整确认'):
            api.run(request)
        self.assertEqual(api.status('unit')['status'], 'ERROR')
        builder.side_effect = action
        api.run(request)
        checker.side_effect = ValueError('原始反馈哈希变化')
        with self.assertRaisesRegex(ValueError, '反馈哈希'):
            api.status('unit')

    def test_rehearsal_non_object_visual_has_explicit_validation_error(self):
        api, _, request, _, cut, _, calls, _ = self.rehearsal_fixture()
        provided = json.loads(cut.read_text())
        for visual in ([], 'invalid', True):
            with self.subTest(visual=visual):
                provided['clips'][0]['visual'] = visual
                cut.write_text(json.dumps(provided))
                with self.assertRaisesRegex(ValueError, 'visual.*对象'):
                    api.run(request)
        self.assertFalse(calls)

    def test_existing_window_cannot_hide_unknown_conflicting_or_submitted_task(self):
        api, _, request, _, _, _, calls, _ = self.rehearsal_fixture()
        original = api.read(self.card)
        cases = [({'status': 'NOT_UPLOADED_NOT_SUBMITTED', 'submitted': True}, 'CONFLICT'),
                 ({'status': 'NOT_UPLOADED_NOT_SUBMITTED'}, 'UNKNOWN'),
                 ({'status': 'PUBLISHED_VERIFIED', 'submitted': True}, 'PUBLISHED_VERIFIED')]
        for index, (fields, message) in enumerate(cases):
            with self.subTest(fields=fields):
                changed = dict(task_context=original['task_context'], **fields)
                self.card.write_text(json.dumps(changed))
                before = self.card.read_bytes()
                with self.assertRaisesRegex(ValueError, message):
                    api.run(dict(request, run_id=f'bad-state-{index}'))
                self.assertEqual(self.card.read_bytes(), before)
        self.assertFalse(calls)

    def test_status_rechecks_task_semantics_even_when_bindings_match(self):
        api, _, request, _, _, _, _, _ = self.rehearsal_fixture()
        api.run(request)
        state_path = api.folder_for('unit') / 'state.json'
        state = api.read(state_path)
        card = api.read(self.card)
        card['submitted'] = True
        self.card.write_text(json.dumps(card))
        # Models an older run created from already-contradictory task state.
        state['bindings'][str(self.card)] = api.digest(self.card)
        api.save(state_path, state)
        with self.assertRaisesRegex(ValueError, 'CONFLICT'):
            api.status('unit')

    def test_failed_asr_reports_stage_and_resumes_without_reextracting(self):
        api, _, request, _, _, preview, _, _ = self.rehearsal_fixture()
        with patch.object(api.source_windows, 'transcribe_preview', side_effect=RuntimeError('model test failure')):
            with self.assertRaisesRegex(RuntimeError, 'model test failure'):
                api.run(self.request)
        report = api.status('unit')
        self.assertEqual(report.get('status'), 'ERROR')
        self.assertEqual(report['failed_stage'], 'asr-0')
        self.assertIn('model test failure', report['error'])
        self.assertIn('window-0', report['completed_stages'])
        with patch.object(api, 'extract_or_resume') as extract:
            resumed = api.run(self.request)
            extract.assert_not_called()
        self.assertEqual(resumed['status'], 'needs_agent_decision')
        self.assertEqual(len(api.read(api.folder_for('unit') / 'state.json')['failures']), 1)

    def test_empty_partial_report_has_explicit_recovery_status(self):
        api, _, _, _, _, _, _, _ = self.rehearsal_fixture()
        api.run(self.request)
        path = api.folder_for('unit') / 'state.json'
        state = api.read(path)
        state['report'] = {}
        api.save(path, state)
        report = api.status('unit')
        self.assertEqual(report.get('status'), 'needs_resume')
        self.assertTrue(report['completed_stages'])
        self.assertIn('next_action', report)
