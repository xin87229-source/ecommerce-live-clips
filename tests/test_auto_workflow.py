"""Resumption must execute work, preserve evidence, and reject stale reuse."""
import importlib
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
        self.card.write_text(json.dumps({'task_context': {'source_video': str(self.source)}}))
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
        self.assertEqual(subtitle_call.call_args.args[0], composition)
        self.assertEqual(prepare_call.call_args.args[0], subtitled)
        self.assertNotEqual(finish_call.call_args.args[0], prepared)
        new_package = finish_call.call_args.args[0]
        copied_frame = json.loads((new_package / 'logs/frames.json').read_text('utf-8'))[0]
        self.assertEqual(Path(copied_frame['path']).parent, new_package / 'frames')
        self.assertEqual(copied_frame['sha256'], api.digest(frame))
        self.assertEqual(combine_call.call_args.args[4], Path(spec['source_review']))
