"""Bounded offline second opinion; synthetic fixtures are not hearing evidence."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import audio_recheck as ar


class RecheckTests(unittest.TestCase):
    def test_ranges_reject_prompt_injection_bad_bounds_and_duplicates(self):
        good = [{'id': 'Q1', 'window_range': [2, 8]}]
        self.assertEqual(ar.requests(good, 20), good)
        for bad in ([], good*2, [{'id': '../escape', 'window_range': [1, 2]}],
                    [{'id': 'Q1', 'window_range': [0, 21]}],
                    [{'id': 'Q1', 'window_range': [False, 2]}],
                    [{'id': 'Q1', 'window_range': [1, float('nan')]}],
                    [{'id': 'Q1', 'window_range': [1, 2], 'expected': '猜测'}]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ar.requests(bad, 30)

    def test_no_config_never_downloads_or_calls_model(self):
        with patch.object(ar.subprocess, 'run') as process, self.assertRaises(ValueError):
            ar.resources({})
        process.assert_not_called()

    def test_resources_pin_all_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'model'; path.write_bytes(b'not an official model')
            with self.assertRaisesRegex(ValueError, '版本不符'):
                ar.resources({'audio_recheck': {k:str(path) for k in ar.PINNED}})
            with self.assertRaisesRegex(ValueError, '绝对路径'):
                ar.resources({'audio_recheck': {k:'https://example.invalid/model' for k in ar.PINNED}})

    def test_infer_receives_audio_without_expected_answer_or_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            paths=dict(executable='local.exe', encoder='encoder.gguf', decoder='decoder.gguf')
            completed=subprocess.CompletedProcess([], 0, '绒毛\n')
            with patch.object(ar.subprocess, 'run', return_value=completed) as process:
                self.assertEqual(ar.infer(paths, folder/'audio.wav', folder, 'Q1'), '绒毛')
            args, kwargs=process.call_args
            self.assertIn(str(folder/'audio.wav'), args[0])
            self.assertNotIn('绒毛', args[0]); self.assertFalse(kwargs['shell'])
            self.assertEqual(kwargs['timeout'], 300)

    def test_model_failure_and_timeout_do_not_retry_cloud(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=dict(executable='local.exe', encoder='encoder', decoder='decoder')
            for outcome in (subprocess.CompletedProcess([], 1, ''), subprocess.TimeoutExpired('local',300)):
                with patch.object(ar.subprocess, 'run') as process:
                    if isinstance(outcome, Exception):process.side_effect=outcome
                    else:process.return_value=outcome
                    with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                        ar.infer(paths, Path(tmp)/'a.wav', Path(tmp), 'Q1')
                    self.assertEqual(process.call_count, 1)

    def test_invalid_model_output_rejected(self):
        for raw in ('', '  ', '\ufffd', 'x'*1501, '\x00'):
            with self.assertRaises(ValueError):ar.parse_output(raw)

    def fixture(self, root):
        (root/'private-context').mkdir()
        (root/'private-context/audio-recheck-resources.json').write_text('{}')
        spec=root/'spec.json';spec.write_text('{}')
        source=root/'source.mp4';source.write_bytes(b'synthetic')
        sha=ar.file_hash(source)
        asr={'inputs': {str(source):sha}, 'item': {'source':{'path':str(source),'sha256':sha},
             'window':{'path':str(source)}, 'offset_seconds':100},
             'preview':{'segments':[{'id':'s1','window_range':[0,1], 'text':'机器原文'}]}}
        return spec, asr, [{'id':'Q1','window_range':[0,1]}]

    def test_run_preserves_sources_records_failure_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); fixture=self.fixture(root); before=fixture[0].read_bytes()
            with patch.object(ar,'ROOT',root), patch.object(ar,'checked_plan',return_value=fixture), \
                 patch.object(ar,'resources',return_value={}), patch.object(ar,'config',return_value={}), \
                 patch.object(ar,'MediaAdapter',side_effect=RuntimeError('local media failed')):
                with self.assertRaises(RuntimeError):ar.run(fixture[0],'run1')
                report=ar.read(root/'private-context/audio-rechecks/run1/report.json')
                self.assertEqual(report['status'],'failed')
                self.assertFalse(report['human_confirmation'])
                with self.assertRaises(FileExistsError):ar.run(fixture[0],'run1')
                self.assertEqual(fixture[0].read_bytes(),before)

    def test_success_is_second_opinion_not_approval_and_detects_changed_input(self):
        class Media:
            def __init__(self,*args):pass
            def write_media(self,args,path):
                with wave.open(str(path),'wb') as w:
                    w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0'*32000)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); fixture=self.fixture(root)
            def infer(paths,audio,folder,name):
                (folder/(name+'.stdout.txt')).write_text('第二路原文',encoding='utf-8')
                return '第二路原文'
            with patch.object(ar,'ROOT',root), patch.object(ar,'checked_plan',return_value=fixture), \
                 patch.object(ar,'resources',return_value={}), patch.object(ar,'config',return_value={}), \
                 patch.object(ar,'MediaAdapter',Media), patch.object(ar,'infer',side_effect=infer):
                result=ar.run(fixture[0],'run1');report=ar.read(result['report'])
                self.assertEqual(report['status'],'machine_recheck_complete')
                self.assertFalse(report['transcript_verified']); self.assertFalse(report['human_confirmation'])
                self.assertEqual(report['results'][0]['source_range'],[100,101])
                self.assertIsNone(report['results'][0]['word_timestamps'])
                ar.verify_inputs(report['artifacts'])
                def local(value, base):
                    path=Path(value).resolve(strict=True)
                    if not path.is_relative_to(Path(base).resolve()):raise ValueError('outside')
                    return path
                with patch.object(ar,'local_file',side_effect=local):
                    self.assertTrue(ar.check(result['report'])['not_human_approval'])
                    tampered=copy.deepcopy(report);tampered['results'][0]['second_opinion']='伪造文字'
                    ar.atomic(result['report'],tampered)
                    with self.assertRaisesRegex(ValueError,'改写'):ar.check(result['report'])
                    ar.atomic(result['report'],report)
                fixture[0].write_text('changed')
                with self.assertRaises(ValueError):ar.verify_inputs(report['inputs'])


if __name__ == '__main__':unittest.main()
