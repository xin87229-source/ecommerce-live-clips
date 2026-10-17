"""Regression tests for full-source inventory, local delivery, and audio QA."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import batch_plan
import quality_support
import run_records
from selector_support import file_hash, key

TMP = Path(tempfile.gettempdir())


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


class FinalReviewFixes(unittest.TestCase):
    def test_passed_audio_review_requires_whole_video(self):
        with tempfile.TemporaryDirectory(dir=TMP) as name:
            proof = Path(name) / 'listening.txt'
            proof.write_text('试听记录', encoding='utf-8')
            review = {'status': 'passed', 'video_sha256': 'a' * 64,
                      'coverage': [0, 1], 'actor': 'reviewer',
                      'at': '2026-10-15T11:00:00+08:00', 'limitations': '无',
                      'evidence': {'type': 'audio_listening', 'path': str(proof),
                                   'sha256': file_hash(proof)}}
            with self.assertRaisesRegex(ValueError, '局部证据'):
                quality_support.quality_evidence({'audio_review': review}, 'a' * 64, 60)
            review['coverage'] = [0, 60]
            self.assertEqual(quality_support.quality_evidence(
                {'audio_review': review}, 'a' * 64, 60)['audio_review']['status'], 'passed')

    def _plan(self, root):
        source = root / 'private-context/source.mp4'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'original source identity')
        sha = file_hash(source)
        job = root / 'private-context/selector-v02a/job'
        transcript = {'schema_version': 1, 'status': 'complete',
                      'source': {'path': str(source), 'sha256': sha, 'duration_seconds': 100},
                      'coverage_scope': 'reviewed_windows_only', 'covered_ranges': [[20, 30]],
                      'segments': [{'id': 's000001', 'start': 21, 'end': 25, 'text': '真实口播'}]}
        transcript['segments_hash'] = key(transcript['segments'])
        tpath = save(job / 'transcript.json', transcript)
        save(job / 'manifest.json', {'source_video': str(source),
                                     'transcript_cache': str(tpath), 'transcript_hash': file_hash(tpath)})
        save(job / 'candidate-clips.json', {'source_video': str(source), 'candidates': [
            {'id': 'c1', 'start_seconds': 21, 'end_seconds': 25, 'kind': 'speech',
             'category': '商品讲解', 'reason': '实际候选', 'text': '真实口播',
             'start_segment_id': 's000001', 'end_segment_id': 's000001'}]})
        note = root / 'private-context/window-review.json'
        save(note, {'reviewer': 'human'})
        full = root / 'private-context/full-review.json'
        save(full, {'schema_version': 1, 'conclusion': 'full_source_reviewed',
                    'source_sha256': sha, 'duration_seconds': 100,
                    'searched_ranges': [[0, 100]], 'confirmed_ranges': [[20, 30]],
                    'pending_ranges': [], 'reviewer': 'human',
                    'reviewed_at': '2026-10-15T11:00:00+08:00',
                    'method': 'continuous_playback', 'finding': '其他时段未发现同款'})
        plan = {'version': batch_plan.VERSION, 'batch_id': 'b1', 'job_path': str(job),
                'source': {'path': str(source), 'sha256': sha, 'duration_seconds': 100},
                'product': {'name': '真实商品', 'code': 'local-1'},
                'coverage': {'scope': 'full_source', 'source_sha256': sha,
                             'evidence': {'path': str(note), 'sha256': file_hash(note)},
                             'full_review': {'path': str(full), 'sha256': file_hash(full)},
                             'searched_ranges': [[0, 100]], 'confirmed_ranges': [[20, 30]],
                             'pending_ranges': []}, 'items': [],
                'exclusions': {'c1': {'usable': False, 'reason': '未选用'}},
                'why_not_more': '仅有一段有效内容'}
        return save(root / 'private-context/plan.json', plan), full

    def test_full_human_inventory_with_window_only_asr(self):
        with tempfile.TemporaryDirectory(dir=TMP) as name:
            root = Path(name)
            path, full = self._plan(root)
            with patch.object(batch_plan, 'ROOT', root):
                result = batch_plan.validate_plan(path)
                self.assertTrue(result['coverage_complete'])
                self.assertEqual(result['searched_seconds'], 100)
                self.assertEqual(result['transcribed_seconds'], 10)
                batch_plan._executable(result, {'scope': 'production'})
                plan = json.loads(path.read_text(encoding='utf-8'))
                del plan['coverage']['full_review']
                save(path, plan)
                with self.assertRaisesRegex(ValueError, '全场盘点'):
                    batch_plan.validate_plan(path)
                plan['coverage']['full_review'] = {'path': str(full), 'sha256': file_hash(full)}
                plan['coverage']['confirmed_ranges'] = [[22, 30]]
                save(path, plan)
                with self.assertRaisesRegex(ValueError, '全场盘点|确认'):
                    batch_plan.validate_plan(path)

    def test_local_close_uses_package_and_qa_without_publish_result(self):
        with tempfile.TemporaryDirectory(dir=TMP) as name:
            root = Path(name)
            plan = save(root / 'private-context/plan.json', {
                'version': batch_plan.VERSION, 'batch_id': 'b1',
                'items': [{'item_id': 'i1', 'task_id': 'task-1'}]})
            card = save(root / 'outputs/task-1/run-state.json', {
                'task_context': {'task_id': 'task-1', 'batch_ref': {'plan_path': str(plan),
                                                                  'item_id': 'i1'}},
                'status': 'NOT_UPLOADED_NOT_SUBMITTED', 'submitted': False})
            package = root / 'outputs/task-1/publish-package'
            video = package / 'video.mp4'
            video.parent.mkdir(parents=True)
            video.write_bytes(b'local video')
            for asset in ('title.txt', 'caption.txt', 'cover-3x4.jpg', 'cover-source.jpg'):
                (package / asset).write_bytes(asset.encode())
            manifest = {name: file_hash(package / name) for name in
                        ('video.mp4', 'title.txt', 'caption.txt', 'cover-3x4.jpg', 'cover-source.jpg')}
            save(package / 'publish.json', {
                'status': 'ready_for_local_delivery', 'task_context': {'task_id': 'task-1'},
                'video': {'path': 'video.mp4', 'sha256': file_hash(video)},
                'delivery_files': manifest})
            refs = save(root / 'private-context/refs.json', {'items': [
                {'item_id': 'i1', 'run_state': str(card), 'package': str(package)}]})
            out = root / 'outputs/local-close'
            with patch.object(run_records, 'ROOT', root), \
                 patch.object(run_records.quality_support, 'check_package',
                              return_value={'delivery_status': 'ready_for_local_delivery',
                                            'video_sha256': file_hash(video),
                                            'quality_evidence': {'technical': {'status': 'passed'},
                                                                 'continuous_visual': {'status': 'passed'},
                                                                 'audio_review': {'status': 'passed'}},
                                            'evidence_gate': 'passed'}):
                result = run_records.local_close(plan, refs, out,
                                                  at='2026-10-15T12:00:00+08:00')
            self.assertEqual(result['items'][0]['status'], 'ready_for_local_delivery')
            self.assertFalse(result['items'][0]['submitted'])
            self.assertNotIn('published', result['items'][0])
            self.assertTrue((out / 'batch-summary.json').is_file())
            self.assertTrue((out / 'report.md').is_file())
            with patch.object(run_records, 'ROOT', root):
                with self.assertRaises(FileExistsError):
                    run_records.local_close(plan, refs, out)
            with patch.object(run_records, 'ROOT', root), \
                 patch.object(run_records.quality_support, 'check_package',
                              return_value={'delivery_status': 'ready_for_local_delivery',
                                            'video_sha256': file_hash(video),
                                            'quality_evidence': {'technical': {'status': 'passed'},
                                                                 'continuous_visual': {'status': 'passed'},
                                                                 'audio_review': {'status': 'not_reviewed'}},
                                            'evidence_gate': 'passed'}):
                stale = run_records.local_close(plan, refs, root / 'outputs/close-stale')
            self.assertEqual(stale['items'][0]['status'], 'needs_repair')
            self.assertIsNone(stale['items'][0]['video'])

    def test_local_close_keeps_blocked_item_without_package(self):
        with tempfile.TemporaryDirectory(dir=TMP) as name:
            root = Path(name)
            plan = save(root / 'private-context/plan.json', {
                'version': batch_plan.VERSION, 'batch_id': 'b1',
                'items': [{'item_id': 'i1', 'task_id': 'task-1'}]})
            card = save(root / 'outputs/task-1/run-state.json', {
                'task_context': {'task_id': 'task-1', 'batch_ref': {'plan_path': str(plan),
                                                                  'item_id': 'i1'}},
                'status': 'BLOCKED', 'submitted': False,
                'facts': {'reason': 'Cannot review audio'},
                'next_action': 'Arrange actual listening'})
            refs = save(root / 'private-context/refs.json', {'items': [
                {'item_id': 'i1', 'run_state': str(card)}]})
            out = root / 'outputs/close'
            with patch.object(run_records, 'ROOT', root):
                summary = run_records.local_close(plan, refs, out)
            item = summary['items'][0]
            self.assertEqual(item['status'], 'BLOCKED')
            self.assertIsNone(item['video'])
            self.assertIn('Cannot review audio', '\n'.join(item['issues']))
            report = (out / 'report.md').read_text(encoding='utf-8')
            self.assertIn('Cannot review audio', report)
            self.assertNotIn('ready_for_local_delivery', report)

    def test_local_close_isolates_package_failure_and_links_good_assets(self):
        with tempfile.TemporaryDirectory(dir=TMP) as name:
            root = Path(name)
            items = [{'item_id': 'i1', 'task_id': 'task-1'},
                     {'item_id': 'i2', 'task_id': 'task-2'}]
            plan = save(root / 'private-context/plan.json', {
                'version': batch_plan.VERSION, 'batch_id': 'b1', 'items': items})
            rows = []
            for item in items:
                task, iid = item['task_id'], item['item_id']
                context = {'task_id': task, 'batch_ref': {'plan_path': str(plan), 'item_id': iid}}
                card = save(root / f'outputs/{task}/run-state.json', {
                    'task_context': context, 'status': 'MEDIA_READY', 'submitted': False})
                package = root / f'outputs/{task}/publish-package'
                package.mkdir(parents=True)
                for asset in ('video.mp4', 'title.txt', 'caption.txt', 'cover-3x4.jpg', 'cover-source.jpg'):
                    (package / asset).write_bytes((task + asset).encode())
                manifest = {p.name: file_hash(p) for p in package.iterdir() if p.is_file()}
                save(package / 'publish.json', {
                    'status': 'ready_for_local_delivery', 'task_context': context,
                    'video': {'path': 'video.mp4', 'sha256': manifest['video.mp4']},
                    'delivery_files': manifest})
                rows.append({'item_id': iid, 'run_state': str(card), 'package': str(package)})
            refs = save(root / 'private-context/refs.json', {'items': rows})
            def gate(package, card):
                if 'task-2' in str(package):
                    raise ValueError('最终封面哈希不匹配')
                return {'delivery_status': 'ready_for_local_delivery',
                        'video_sha256': file_hash(Path(package) / 'video.mp4'),
                        'quality_evidence': {'technical': {'status': 'passed'},
                                             'continuous_visual': {'status': 'passed'},
                                             'audio_review': {'status': 'passed'}},
                        'evidence_gate': 'passed'}
            out = root / 'outputs/close'
            with patch.object(run_records, 'ROOT', root), \
                 patch.object(run_records.quality_support, 'check_package', side_effect=gate):
                summary = run_records.local_close(plan, refs, out)
            self.assertEqual([i['status'] for i in summary['items']],
                             ['ready_for_local_delivery', 'needs_repair'])
            self.assertIn('封面哈希', '\n'.join(summary['items'][1]['issues']))
            self.assertIsNone(summary['items'][1]['video'])
            report = (out / 'report.md').read_text(encoding='utf-8')
            self.assertIn('[观看成片](', report)
            self.assertIn('cover-3x4.jpg', report)
            self.assertIn('最终封面哈希不匹配', report)

    def test_local_close_rejects_package_identity_conflict_globally(self):
        with tempfile.TemporaryDirectory(dir=TMP) as name:
            root = Path(name)
            plan = save(root / 'private-context/plan.json', {
                'version': batch_plan.VERSION, 'batch_id': 'b1',
                'items': [{'item_id': 'i1', 'task_id': 'task-1'}]})
            card = save(root / 'outputs/task-1/run-state.json', {
                'task_context': {'task_id': 'task-1', 'batch_ref': {'plan_path': str(plan),
                                                                  'item_id': 'i1'}},
                'status': 'MEDIA_READY', 'submitted': False})
            package = root / 'outputs/task-1/publish-package'
            save(package / 'publish.json', {'status': 'awaiting_content_review',
                                            'task_context': {'task_id': 'other-task'}})
            refs = save(root / 'private-context/refs.json', {'items': [
                {'item_id': 'i1', 'run_state': str(card), 'package': str(package)}]})
            with patch.object(run_records, 'ROOT', root):
                with self.assertRaisesRegex(ValueError, '任务身份'):
                    run_records.local_close(plan, refs, root / 'outputs/close')


if __name__ == '__main__':
    unittest.main()
