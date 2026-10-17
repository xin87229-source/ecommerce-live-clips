import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / 'cli.py'


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / '商家 工作区'
        materials = self.root / 'materials'
        materials.mkdir()
        self.source = materials / '原片.mp4'
        self.source.write_bytes(b'only for input-state tests; media not yet verified')
        self.reference = materials / '商品.png'
        self.reference.write_bytes(b'reference not yet visually reviewed')

    def tearDown(self):
        assert self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        self.temp.cleanup()

    def call(self, *args):
        return subprocess.run([sys.executable, str(CLI), '--workspace', str(self.workspace), *args],
                              capture_output=True, text=True, encoding='utf-8')

    def test_doctor_is_read_only_and_reports_missing_tools(self):
        result = self.call('doctor')
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report['ready'])
        self.assertFalse(self.workspace.exists())
        self.assertIn('checks', report)

    def test_task_initialization_never_invents_review_or_platform_id(self):
        self.assertEqual(self.call('init', '--profile', 'basic').returncode, 0)
        result = self.call('new-task', '--source', str(self.source), '--product-name', '测试商品',
                           '--reference', str(self.reference), '--task-id', 'first')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        card = json.loads((self.workspace / 'outputs/first/run-state.json').read_text('utf-8'))
        context = card['task_context']
        self.assertEqual(context['scope'], 'production')
        self.assertIsNone(context['product']['id'])
        self.assertTrue(context['product']['code'].startswith('local-'))
        self.assertEqual(context['source_review']['conclusion'], 'unknown')
        self.assertEqual(context['confirmed_windows'], [])
        self.assertEqual(context['source_sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertFalse(card.get('submitted', False))

    def test_repeated_init_and_task_leave_existing_bytes(self):
        self.assertEqual(self.call('init').returncode, 0)
        config = self.workspace / 'config.json'
        before = config.read_bytes()
        self.assertNotEqual(self.call('init', '--profile', 'apparel').returncode, 0)
        self.assertEqual(config.read_bytes(), before)
        args = ('new-task', '--source', str(self.source), '--product-name', '测试商品',
                '--reference', str(self.reference), '--task-id', 'first')
        self.assertEqual(self.call(*args).returncode, 0)
        state = self.workspace / 'outputs/first/run-state.json'
        before = state.read_bytes()
        self.assertNotEqual(self.call(*args).returncode, 0)
        self.assertEqual(state.read_bytes(), before)

    def test_source_inside_workspace_is_rejected(self):
        self.assertEqual(self.call('init').returncode, 0)
        source = self.workspace / 'input.mp4'
        source.write_bytes(b'media')
        result = self.call('new-task', '--source', str(source), '--product-name', '测试商品',
                           '--reference', str(self.reference), '--task-id', 'bad')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.workspace / 'outputs/bad').exists())

    def test_status_never_treats_corrupt_state_as_unsubmitted(self):
        self.workspace.mkdir()
        output = self.workspace / 'outputs/broken'
        output.mkdir(parents=True)
        (output / 'run-state.json').write_text('{broken', encoding='utf-8')
        result = self.call('status')
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report['tasks'][0]['status'], 'UNKNOWN')

    def status_for_card(self, card):
        path = self.workspace / 'outputs/state/run-state.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(card, ensure_ascii=False), encoding='utf-8')
        before = path.read_bytes()
        result = self.call('status')
        self.assertEqual(path.read_bytes(), before, 'status must leave the task card unchanged')
        report = json.loads(result.stdout)
        self.assertEqual(len(report['tasks']), 1)
        return result, report['tasks'][0]

    def test_status_reports_conflicting_submission_fields(self):
        for state, submitted in (
                ('NOT_UPLOADED_NOT_SUBMITTED', True), ('MEDIA_READY', True),
                ('SUBMITTED_PENDING_REVIEW', False), ('SUBMITTED_AWAITING_REVIEW', False),
                ('SCHEDULED_VERIFIED', False), ('PUBLISHED_VERIFIED', False),
                ('SUBMITTING', False), ('SUBMITTING', True),
                ('SUBMIT_UNKNOWN', False), ('SUBMIT_UNKNOWN', True)):
            with self.subTest(state=state, submitted=submitted):
                card = {'status': state, 'submitted': submitted}
                result, task = self.status_for_card(card)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(task['status'], 'CONFLICT')
                self.assertIsNone(task['submitted'])
                self.assertEqual(task['recorded_state'], card)
                self.assertTrue(task['error'])

    def test_status_reports_missing_or_invalid_submission_fields_as_unknown(self):
        cards = [{'status': 'NOT_UPLOADED_NOT_SUBMITTED'}, {'submitted': False},
                 {'status': 'SUBMIT_UNKNOWN'}, {'status': 'PUBLISHED_VERIFIED'}]
        cards += [{'status': 'NOT_UPLOADED_NOT_SUBMITTED', 'submitted': value}
                  for value in (None, 0, 1, 'false', 'true', [], {})]
        cards += [{'status': state, 'submitted': False}
                  for state in (None, 7, [], {}, '', ' ', 'UNRECOGNIZED_STATE')]
        cards += [{'status': state, 'submitted': None}
                  for state in ('PUBLISHED_VERIFIED', 'REJECTED_ORIGINALITY')]
        for card in cards:
            with self.subTest(card=card):
                result, task = self.status_for_card(card)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(task['status'], 'UNKNOWN')
                self.assertIn('submitted', task)
                self.assertIsNone(task['submitted'])
                self.assertEqual(task.get('recorded_state'), card)
                self.assertTrue(task['error'])

    def test_status_preserves_known_consistent_submission_states(self):
        pre_submission = (
            'NOT_UPLOADED_NOT_SUBMITTED', 'ENVIRONMENT_NOT_READY', 'READY_TO_UPLOAD',
            'READY_TO_SUBMIT', 'IN_PROGRESS', 'BLOCKED', 'FAILED', 'MEDIA_READY',
            'DRAFT_CONFLICT', 'PAGE_STATE_LOST', 'VIDEO_PREVIEW_FAILED', 'PRODUCT_BOUND_MISMATCH',
            'PRODUCT_BOUND_DUPLICATED', 'PRODUCT_BOUND_ERROR', 'NEEDS_LOGIN', 'NEEDS_CAPTCHA',
            'AWAITING_USER_CONFIRMATION', 'PRODUCT_QUOTA_BLOCKED',
            'running', 'completed', 'failed', 'blocked', 'reused', 'ready_for_manual_publish')
        cases = [(state, False) for state in pre_submission]
        cases += [(state, True) for state in (
            'SUBMITTED_PENDING_REVIEW', 'SUBMITTED_AWAITING_REVIEW',
            'SCHEDULED_VERIFIED', 'PUBLISHED_VERIFIED')]
        cases += [('REJECTED_ORIGINALITY', False), ('REJECTED_ORIGINALITY', True)]
        for state, submitted in cases:
            with self.subTest(state=state, submitted=submitted):
                card = {'status': state, 'submitted': submitted,
                        'task_context': {'task_id': 'state'}, 'next_action': '保留原建议'}
                result, task = self.status_for_card(card)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertEqual(task['status'], state)
                self.assertIs(task['submitted'], submitted)
                self.assertEqual(task['task_id'], 'state')
                self.assertEqual(task['next_action'], '保留原建议')

    def test_status_keeps_uncertain_states_explicit_and_returns_nonzero(self):
        for state in ('SUBMITTING', 'SUBMIT_UNKNOWN', 'UNKNOWN', 'CONFLICT'):
            with self.subTest(state=state):
                card = {'status': state, 'submitted': None}
                result, task = self.status_for_card(card)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(task['status'], state)
                self.assertIsNone(task['submitted'])
                self.assertEqual(task.get('recorded_state'), card)


if __name__ == '__main__':
    unittest.main()
