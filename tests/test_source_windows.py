import importlib
import json
import os
import array
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))


class SourceWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / 'workspace'
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {'ECLIP_WORKSPACE': str(self.workspace)})
        self.env.start()
        import runtime
        import selector_support
        import quality_support
        import run_records
        importlib.reload(runtime)
        importlib.reload(selector_support)
        importlib.reload(quality_support)
        importlib.reload(run_records)
        self.runtime = runtime
        self.support = selector_support
        source_folder = Path(self.temp.name) / 'source-root'
        source_folder.mkdir()
        self.source = source_folder / '原始 直播.mp4'
        self.source.write_bytes(b'original source bytes')
        self.reference = Path(self.temp.name) / 'reference.txt'
        self.reference.write_text('invented product', encoding='utf-8')
        self.card = self.workspace / 'outputs' / 'task-one' / 'run-state.json'
        self.card.parent.mkdir(parents=True)
        self.product = {'name': '虚构商品', 'code': 'local-test', 'reference':
                        {'path': str(self.reference), 'sha256': self.support.file_hash(self.reference)}}
        self.context = {'task_id': 'task-one', 'scope': 'production',
                        'profile': runtime.profile(), 'content_strategy': {'version': 'single-style-batch-v1'},
                        'product': self.product, 'source_video': str(self.source),
                        'source_sha256': self.support.file_hash(self.source),
                        'confirmed_windows': [], 'source_review': {'conclusion': 'unknown'},
                        'cross_device_state': 'local_records_checked'}
        self._write_card()
        import source_windows
        self.windows = importlib.reload(source_windows)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _write_card(self, status='NOT_UPLOADED_NOT_SUBMITTED', submitted=False):
        self.card.write_text(json.dumps({'task_context': self.context, 'status': status,
                                         'submitted': submitted, 'started_at': '2026-10-15T10:00:00+08:00'}),
                             encoding='utf-8')

    def _extraction(self):
        folder = self.workspace / 'private-context' / 'source-windows' / 'task-one' / 'window-example'
        folder.mkdir(parents=True)
        video = folder / 'window.mp4'
        video.write_bytes(b'new window bytes')
        audio = folder / 'audio-review.m4a'
        audio.write_bytes(b'window audio bytes')
        frames = []
        for i, at in enumerate((10.5, 15, 19.5), 1):
            image = folder / f'frame-{i}.png'
            image.write_bytes(f'frame {i}'.encode())
            frames.append({'path': str(image), 'sha256': self.support.file_hash(image),
                           'source_at': at, 'window_at': at-10})
        extraction = {'schema_version': 1, 'task_id': 'task-one', 'task_record': str(self.card),
                      'source': {'path': str(self.source), 'sha256': self.support.file_hash(self.source),
                                 'duration_seconds': 100},
                      'requested_range': [10, 20], 'source_span': [10, 20], 'offset_seconds': 10,
                      'frame_grid': {'aligned_start_seconds':10,'aligned_end_seconds':20,
                                     'expected_step_seconds':1/30,'first_pts':10,'last_pts':20,
                                     'video_start_pts':0,'requested_start_pts':10,'requested_end_pts':20,
                                     'frame_count':301,'max_step_error_seconds':0},
                      'source_timeline':{'video_start_seconds':0,'audio_start_seconds':0},
                      'window_audio_audit':{'first':0,'end':10,'frames':1},
                      'window': {'path': str(video), 'sha256': self.support.file_hash(video),
                                 'duration_seconds': 10},
                      'sample_frames': frames,
                      'audio_review': {'path': str(audio), 'sha256': self.support.file_hash(audio)},
                      'review_status':'unknown'}
        path = folder / 'extraction.json'
        path.write_text(json.dumps(extraction), encoding='utf-8')
        return path, extraction

    def _review(self, extraction_path, extraction, conclusion='matched'):
        frames = [{k: frame[k] for k in ('path', 'sha256')} |
                  {'at': frame['source_at'], 'finding': f'同一商品画面{i}'}
                  for i, frame in enumerate(extraction['sample_frames'], 1)]
        speech = dict(extraction['audio_review'], type='audio_listening', range=[10, 20],
                      finding='完整听到同一商品讲解')
        value = {'conclusion': conclusion, 'reviewer': '实际看听的运营',
                 'reviewed_at': '2026-10-15T11:00:00+08:00',
                 'windows': [{'extraction': str(extraction_path), 'visual': frames, 'speech': speech}]}
        path = self.workspace / 'private-context' / 'review-input.json'
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def test_source_must_be_original_outside_workspace(self):
        other = self.workspace / 'outputs' / 'old-clip.mp4'
        other.write_bytes(b'old clip')
        self.context['source_video'] = str(other)
        self.context['source_sha256'] = self.support.file_hash(other)
        self._write_card()
        with self.assertRaisesRegex(ValueError, '原始素材'):
            self.windows.original_context(self.card)

    def test_window_mapping_uses_actual_duration_and_rejects_overrun(self):
        path, extraction = self._extraction()
        entry = self.windows.import_entry(path)
        self.assertEqual(entry['offset_seconds'], 10)
        self.assertEqual(entry['mapping']['range'], [10, 20])
        extraction['window']['duration_seconds'] = 10.2
        path.write_text(json.dumps(extraction), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '实际时长'):
            self.windows.import_entry(path)
        extraction['window']['duration_seconds'] = 4
        extraction['source_span'] = [10, 14]
        path.write_text(json.dumps(extraction), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '实际时长'):
            self.windows.import_entry(path)

    def test_unknown_review_never_marks_original_matched(self):
        path, extraction = self._extraction()
        review = self._review(path, extraction, conclusion='unknown')
        before = self.card.read_bytes()
        with self.assertRaisesRegex(ValueError, 'matched'):
            self.windows.create_review(self.card, review)
        self.assertEqual(self.card.read_bytes(), before)

    def test_review_rejects_window_claiming_other_source_path(self):
        path, extraction = self._extraction()
        other = self.source.with_name('same-bytes.mp4')
        other.write_bytes(self.source.read_bytes())
        extraction['source']['path'] = str(other)
        path.write_text(json.dumps(extraction), encoding='utf-8')
        review = self._review(path, extraction)
        with self.assertRaisesRegex(ValueError, '当前原片'):
            self.windows.create_review(self.card, review)

    def test_review_requires_evidence_and_preserves_task_status(self):
        path, extraction = self._extraction()
        review = self._review(path, extraction)
        result = self.windows.create_review(self.card, review)
        card = json.loads(self.card.read_text(encoding='utf-8'))
        self.assertEqual(card['status'], 'NOT_UPLOADED_NOT_SUBMITTED')
        self.assertFalse(card['submitted'])
        self.assertEqual(card['task_context']['source_review']['conclusion'], 'matched')
        self.assertEqual(card['task_context']['confirmed_windows'][0]['range'], [10, 20])
        child = json.loads(Path(result['window_tasks'][0]['task_record']).read_text(encoding='utf-8'))
        self.assertEqual(child['task_context']['confirmed_windows'][0]['range'], [0, 10])
        self.assertEqual(child['task_context']['source_sha256'], extraction['window']['sha256'])

    def test_existing_adoption_rejects_review_rewrite(self):
        path, extraction = self._extraction()
        review = self._review(path, extraction)
        self.context['batch_ref'] = {'plan_path': 'existing'}
        self._write_card()
        before = self.card.read_bytes()
        with self.assertRaisesRegex(ValueError, '采用'):
            self.windows.create_review(self.card, review)
        self.assertEqual(self.card.read_bytes(), before)

    def test_review_rechecks_parent_after_child_creation(self):
        path, extraction = self._extraction()
        review = self._review(path, extraction)
        original_initialize = self.windows.initialize

        def competing_update(child_path, *, task_context):
            outcome = original_initialize(child_path, task_context=task_context)
            parent = json.loads(self.card.read_text(encoding='utf-8'))
            parent['next_action'] = '其他进程已经更新'
            self.card.write_text(json.dumps(parent, ensure_ascii=False), encoding='utf-8')
            return outcome

        with patch.object(self.windows, 'initialize', side_effect=competing_update):
            with self.assertRaisesRegex(ValueError, '并发'):
                self.windows.create_review(self.card, review)
        self.assertEqual(json.loads(self.card.read_text(encoding='utf-8'))['next_action'], '其他进程已经更新')

    def _fake_media(self):
        meta = {'duration': 100, 'fps': 30, 'width': 1920, 'height': 1080,
                'video_start': 0, 'video_index': 0, 'audio_index': 1,
                'audio': {'start': 0, 'duration': 100, 'sample_rate': 48000}}

        class FakeMedia:
            def __init__(self, config, log):
                self.log = log

            def probe(self, path):
                return dict(meta, duration=10, audio=dict(meta['audio'], duration=10)) if Path(path).name == 'window.mp4' else meta

            def look(self, source, duration, directory):
                paths = []
                for index in range(1, 4):
                    path = directory / f'frame-{index}.png'
                    path.write_bytes(f'frame {index}'.encode())
                    paths.append(str(path))
                return paths

            def command(self, tool, args):
                if tool == 'ffprobe':
                    if 'a:0' in args:
                        return json.dumps({'frames': [{'best_effort_timestamp_time': f'{10+i/50:.6f}',
                                                       'nb_samples': 960} for i in range(500)]})
                    return json.dumps({'frames': [{'best_effort_timestamp_time': f'{10+i/30:.6f}'}
                                                  for i in range(301)]})
                return ''

            def write_media(self, args, destination):
                destination.write_bytes(b'generated ' + Path(destination).name.encode())

        return FakeMedia

    def test_inspect_records_unknown_samples_without_touching_original(self):
        before = self.source.read_bytes()
        with patch.object(self.windows, 'MediaAdapter', self._fake_media()):
            report = self.windows.inspect(self.card)
        self.assertEqual(report['status'], 'needs_actual_product_review')
        self.assertEqual(len(report['sample_frames']), 3)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue(Path(report['inspection_path']).is_file())

    def test_extract_writes_new_window_and_exact_mapping_evidence(self):
        import transcribe_adapter
        before = self.source.read_bytes()
        with patch.object(self.windows, 'MediaAdapter', self._fake_media()), \
                patch.object(transcribe_adapter, 'audio_audit', return_value={'frames': 1, 'first': 0, 'end': 10}):
            result = self.windows.extract(self.card, 10, 20)
        entry = self.windows.import_entry(result['extraction'])
        self.assertEqual(entry['offset_seconds'], 10)
        self.assertEqual(entry['mapping']['range'], [10, 20])
        self.assertEqual(entry['mapping']['source_sha256'], self.context['source_sha256'])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertTrue(Path(result['window']['path']).is_file())

    def test_extract_rejects_non_initial_task_state(self):
        self._write_card(status='completed', submitted=False)
        with self.assertRaisesRegex(ValueError, '状态'):
            self.windows.extract(self.card, 10, 20)

    def test_nonzero_ts_pts_aligns_window_without_decoding_full_source_audio(self):
        import transcribe_adapter
        source = self.source
        raw_meta = {'duration':12849.420444,'fps':22,'width':720,'height':1270,
                    'video_start':1.49,'video_index':0,'audio_index':1,
                    'audio':{'start':1.452,'duration':12849.42,'sample_rate':48000}}
        class FakeMedia:
            def __init__(self, config, log): self.log=log
            def probe(self, path):
                if Path(path).name == 'window.mp4':
                    return dict(raw_meta,duration=60,video_start=0,
                                audio=dict(raw_meta['audio'],start=0,duration=60))
                return raw_meta
            def command(self, tool, args):
                if tool == 'ffprobe':
                    if 'a:0' in args:
                        return json.dumps({'frames': [{'best_effort_timestamp_time': round(1501.47+i/50, 6),
                                                       'nb_samples': 960} for i in range(3001)]})
                    return json.dumps({'frames':[{'best_effort_timestamp_time':round(1501.47+i/22,6)}
                                                  for i in range(1321)]})
                return ''
            def write_media(self, args, destination):
                destination.write_bytes(b'generated media')
            def look(self, path, duration, folder):
                paths=[]
                for i in range(3):
                    frame=folder/f'frame-{i}.png';frame.write_bytes(f'frame{i}'.encode());paths.append(str(frame))
                return paths
        def audit(path, meta):
            self.assertEqual(Path(path).name,'window.mp4')
            return {'first':0,'end':60,'frames':1}
        with patch.object(self.windows,'MediaAdapter',FakeMedia), \
             patch.object(transcribe_adapter,'audio_audit',side_effect=audit):
            result=self.windows.extract(self.card,1500,1560)
        record=self.support.read(result['extraction'])
        self.assertEqual(record['requested_range'],[1500,1560])
        self.assertAlmostEqual(record['source_span'][0],1499.98,places=3)
        self.assertEqual(record['source_timeline']['video_start_seconds'],1.49)
        self.assertEqual(record['source_timeline']['audio_start_seconds'],1.452)
        self.assertAlmostEqual(result['import_mapping']['offset_seconds'],1499.98,places=3)

    def test_preview_transcribes_only_verified_short_window_and_leaves_review_unknown(self):
        import transcribe_adapter
        path, extraction = self._extraction()
        extraction['review_status'] = 'unknown'
        path.write_text(json.dumps(extraction), encoding='utf-8')
        before = self.card.read_bytes()
        transcript = {'status':'complete','source':{'sha256':extraction['window']['sha256'],
                      'duration_seconds':10},'asr':{'config_hash':'local-model-config'},
                      'segments':[{'id':'s000001','start':1,'end':4,'text':'粉色连衣裙'}]}
        cache=self.workspace/'private-context/transcript.json'
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(transcript,ensure_ascii=False),encoding='utf-8')
        class FakeMedia:
            def __init__(self, config, log): pass
            def probe(self, window):
                return {'duration':10,'video_start':0,'audio':{'start':0},'fps':30}
        with patch.object(self.windows,'MediaAdapter',FakeMedia), \
             patch.object(transcribe_adapter,'transcribe',return_value=(
                 transcript,cache,{'cache_hit':False})) as asr:
            result=self.windows.transcribe_preview(path)
        self.assertEqual(asr.call_args.args[0],Path(extraction['window']['path']))
        preview=self.support.read(result['preview_path'])
        self.assertEqual(preview['status'],'unreviewed_asr_preview')
        self.assertEqual(preview['source_span'],[10,20])
        self.assertEqual(preview['segments'][0]['source_range'],[11,14])
        self.assertEqual(preview['same_product'],'unknown')
        self.assertEqual(preview['audio_listening'],'not_performed')
        self.assertEqual(self.card.read_bytes(),before)
        with self.assertRaisesRegex(ValueError,'matched'):
            self.windows.create_review(self.card,self._review(path,extraction,conclusion='unknown'))

    def test_preview_rejects_window_over_120_seconds_before_asr(self):
        import transcribe_adapter
        path, extraction = self._extraction()
        extraction['source']['duration_seconds']=200
        extraction['requested_range']=[10,131]
        extraction['source_span']=[10,131]
        extraction['window']['duration_seconds']=121
        extraction['review_status']='unknown'
        path.write_text(json.dumps(extraction),encoding='utf-8')
        with patch.object(transcribe_adapter,'transcribe') as asr:
            with self.assertRaisesRegex(ValueError,'120'):
                self.windows.transcribe_preview(path)
        asr.assert_not_called()

    def _aligned_boundary_preview(self):
        path, item = self._extraction()
        # Actual 22 fps TS failure: a 120-second request becomes 120.005 seconds.
        item['source']['duration_seconds'] = 13174.990444
        item['requested_range'] = [6910, 7030]
        item['offset_seconds'] = 6910.012000000001
        item['source_span'] = [item['offset_seconds'], item['offset_seconds']+120.005]
        item['window']['duration_seconds'] = 120.005
        item['frame_grid'] = {
            'first_pts':6911.502,'last_pts':7031.507,'frame_count':2641,
            'aligned_start_seconds':6910.012000000001,'aligned_end_seconds':7030.017,
            'requested_start_pts':6911.49,'requested_end_pts':7031.49,'video_start_pts':1.49,
            'expected_step_seconds':1/22,'max_step_error_seconds':.0015454545459347574}
        item['source_timeline'] = {'video_start_seconds':1.49,'audio_start_seconds':1.642}
        path.write_text(json.dumps(item), encoding='utf-8')
        return path, item

    def test_preview_accepts_120_second_request_with_verified_frame_rounding(self):
        import transcribe_adapter
        path, item = self._aligned_boundary_preview()
        original = path.read_bytes()
        transcript = {'source':{'sha256':item['window']['sha256'],'duration_seconds':120.005},
                      'asr':{'config_hash':'test-model'},
                      'segments':[{'id':'s1','start':119,'end':120.005,'text':'保留实际末句'}]}
        cache = self.workspace/'private-context/boundary-transcript.json'
        cache.write_text(json.dumps(transcript), encoding='utf-8')
        class Media:
            def __init__(self, config, log): pass
            def probe(self, window):
                return {'duration':120.005,'video_start':0,'audio':{'start':0},'fps':22}
        with patch.object(self.windows,'MediaAdapter',Media), \
             patch.object(transcribe_adapter,'transcribe',return_value=(transcript,cache,{})) as asr:
            result = self.windows.transcribe_preview(path)
        asr.assert_called_once()
        preview = self.support.read(result['preview_path'])
        self.assertEqual(preview['source_span'], item['source_span'])
        self.assertEqual(preview['segments'][0]['window_range'], [119,120.005])
        self.assertEqual(preview['segments'][0]['source_range'][1], item['source_span'][1])
        self.assertFalse(preview['transcript_verified'])
        self.assertEqual(path.read_bytes(), original)

    def test_preview_rejects_unproven_boundary_allowance_before_asr(self):
        import copy
        import transcribe_adapter
        path, original = self._aligned_boundary_preview()
        cases = ['request_over_limit','duration_too_long','missing_end','invalid_step',
                 'misaligned_end','changed_pts','non_continuous_grid']
        for case in cases:
            item = copy.deepcopy(original)
            if case == 'request_over_limit':
                item['requested_range'][1] = 7030.01
            elif case == 'duration_too_long':
                item['window']['duration_seconds'] = 120.07
                item['source_span'][1] = item['offset_seconds']+120.07
            elif case == 'missing_end':
                del item['frame_grid']['aligned_end_seconds']
            elif case == 'invalid_step':
                item['frame_grid']['expected_step_seconds'] = 5
            elif case == 'misaligned_end':
                item['frame_grid']['aligned_end_seconds'] = 7030.06
                item['frame_grid']['last_pts'] = 7031.55
            elif case == 'changed_pts':
                item['frame_grid']['last_pts'] += .01
            elif case == 'non_continuous_grid':
                item['frame_grid']['max_step_error_seconds'] = .003
            path.write_text(json.dumps(item), encoding='utf-8')
            with self.subTest(case=case), patch.object(transcribe_adapter,'transcribe') as asr:
                with self.assertRaises(ValueError):
                    self.windows.transcribe_preview(path)
                asr.assert_not_called()

    def test_preview_boundary_probe_must_still_match_extraction_duration(self):
        import transcribe_adapter
        path, _ = self._aligned_boundary_preview()
        class Media:
            def __init__(self, config, log): pass
            def probe(self, window):
                return {'duration':120.007,'video_start':0,'audio':{'start':0},'fps':22}
        with patch.object(self.windows,'MediaAdapter',Media), \
             patch.object(transcribe_adapter,'transcribe') as asr:
            with self.assertRaisesRegex(ValueError,'实际时长与抽取证据'):
                self.windows.transcribe_preview(path)
        asr.assert_not_called()

    @unittest.skipUnless(os.environ.get('ECLIP_TEST_FFMPEG') and os.environ.get('ECLIP_TEST_FFPROBE'),
                         'set ECLIP_TEST_FFMPEG and ECLIP_TEST_FFPROBE for actual media regression')
    def test_delayed_audio_source_maps_later_window_without_shifting_markers(self):
        from media_adapter import MediaAdapter
        cfg = {'ffmpeg_path': os.environ['ECLIP_TEST_FFMPEG'],
               'ffprobe_path': os.environ['ECLIP_TEST_FFPROBE']}
        (self.workspace / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
        media = MediaAdapter(cfg, self.workspace / 'fixture-media.log')
        self.source = self.source.with_suffix('.mkv')
        # Real source: video turns blue at PTS 2; delayed audio starts at PTS .5,
        # with a tone beginning at PTS 2. Both markers must remain at window t=1.
        media.write_media(['-f', 'lavfi', '-i', 'color=red:size=64x96:rate=25:duration=5',
                           '-f', 'lavfi', '-i',
                           'aevalsrc=if(gte(t\\,1.5)\\,0.4*sin(2*PI*440*t)\\,0):s=48000:d=4.5',
                           '-filter_complex',
                           "[0:v]drawbox=color=blue:t=fill:enable='gte(t,2)'[v];[1:a]asetpts=PTS+0.5/TB[a]",
                           '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-c:a', 'pcm_s16le',
                           str(self.source)], self.source)
        self.context['source_video'] = str(self.source)
        self.context['source_sha256'] = self.support.file_hash(self.source)
        self._write_card()
        before_card = self.card.read_bytes()
        source_meta = media.probe(self.source)
        self.assertGreater(source_meta['audio']['start'] - source_meta['video_start'], .4)
        result = self.windows.extract(self.card, 1, 3)
        record = self.support.read(result['extraction'])
        self.assertEqual(record['source_span'], [1, 3])
        self.assertEqual(record['review_status'], 'unknown')
        self.assertEqual(self.card.read_bytes(), before_card)
        self.assertEqual(self.support.file_hash(self.source), self.context['source_sha256'])
        self.assertLessEqual(record['source_audio_audit']['first_pts'], 1)
        self.assertGreaterEqual(record['source_audio_audit']['last_end_pts'], 3)
        window = Path(result['window']['path'])
        wav = window.parent / 'marker.wav'
        media.write_media(['-i', str(window), '-map', '0:a:0', '-ac', '1', '-ar', '48000',
                           '-c:a', 'pcm_s16le', str(wav)], wav)
        import wave
        with wave.open(str(wav), 'rb') as stream:
            samples = array.array('h', stream.readframes(stream.getnframes()))
        chunk = 480  # Ten-millisecond RMS blocks.
        rms = [sum(x*x for x in samples[i:i+chunk])/chunk for i in range(0, len(samples)-chunk, chunk)]
        tone_start = next(i for i, power in enumerate(rms) if power > 1000000) / 100
        self.assertAlmostEqual(tone_start, 1, delta=.04)
        from PIL import Image
        marker_frames = []
        for at, name in ((.96, 'before-marker.png'), (1.04, 'after-marker.png')):
            frame = window.parent / name
            media.write_media(self.windows.ffmpeg_subset.look(window, at, frame), frame)
            with Image.open(frame) as image:
                marker_frames.append(image.convert('RGB').getpixel((100, 100)))
        red, blue = marker_frames
        self.assertGreater(red[0], red[2]+150)
        self.assertGreater(blue[2], blue[0]+150)
        # The same source's initial missing half-second is not safe to pad silently.
        with self.assertRaisesRegex(ValueError, '原片音频.*覆盖'):
            self.windows._verify_audio_span(media, self.source, 0, 1, 48000)

    def test_selected_original_audio_gap_is_rejected_before_padding(self):
        class GapMedia:
            def command(self, tool, args):
                return json.dumps({'frames': [
                    {'best_effort_timestamp_time': 1, 'nb_samples': 4800},
                    {'best_effort_timestamp_time': 1.2, 'nb_samples': 4800}]})
        with self.assertRaisesRegex(ValueError, '原片音频.*不连续'):
            self.windows._verify_audio_span(GapMedia(), self.source, 1, 1.3, 48000)

    @unittest.skipUnless(os.environ.get('ECLIP_TEST_FFMPEG') and os.environ.get('ECLIP_TEST_FFPROBE'),
                         'set ECLIP_TEST_FFMPEG and ECLIP_TEST_FFPROBE for actual media regression')
    def test_aac_priming_at_zero_is_read_without_seek_dropping_first_frame(self):
        from media_adapter import MediaAdapter
        cfg = {'ffmpeg_path': os.environ['ECLIP_TEST_FFMPEG'],
               'ffprobe_path': os.environ['ECLIP_TEST_FFPROBE']}
        media = MediaAdapter(cfg, self.workspace / 'aac-fixture.log')
        source = self.source.with_name('aac-fixture.mp4')
        media.write_media(['-f', 'lavfi', '-i', 'color=red:size=64x96:rate=25:duration=2',
                           '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                           '-c:v', 'libx264', '-c:a', 'aac', str(source)], source)
        result = self.windows._verify_audio_span(media, source, 0, 1, 48000)
        self.assertEqual(result['first_pts'], 0)
        self.assertGreaterEqual(result['last_end_pts'], 1)

    def test_variable_frame_rate_remains_explicitly_unsupported(self):
        class VariableMedia:
            def command(self, tool, args):
                return json.dumps({'frames': [{'best_effort_timestamp_time': t}
                                              for t in (0, .04, .08, .16, .2)]})
        with self.assertRaisesRegex(ValueError, '非恒定帧率'):
            self.windows._verify_frame_grid(VariableMedia(), self.source, 0, .2, 25)

    def _terminal_media(self, duration_key='duration_time', duration='0.033333', gap=False):
        frames = [{'best_effort_timestamp_time': f'{7+i/30:.6f}'} for i in range(60)]
        if duration_key:
            frames[-1][duration_key] = duration
        if gap:
            del frames[30]
        class Media:
            def command(self, tool, args):
                return json.dumps({'frames': frames})
        return Media()

    def test_terminal_frame_duration_preserves_full_window_and_actual_frame_count(self):
        for field in ('duration_time', 'pkt_duration_time'):
            with self.subTest(field=field):
                grid = self.windows._verify_frame_grid(
                    self._terminal_media(field), self.source, 0, 2, 30, 7)
                self.assertEqual(grid['frame_count'], 60)
                self.assertAlmostEqual(grid['last_pts'], 8.966667)
                self.assertAlmostEqual(grid['aligned_end_seconds'], 2)
                self.assertAlmostEqual(grid['terminal_frame_duration_seconds'], 1/30, places=6)
                item = {'requested_range': [0, 2], 'window': {'duration_seconds': 2},
                        'frame_grid': grid}
                self.assertEqual(self.windows._preview_window_duration(item), 2)

    def test_terminal_boundary_requires_duration_and_regular_actual_frames(self):
        for value in (None, 0, -1, True, 'NaN', 'Infinity', 'bad', '.1'):
            with self.subTest(duration=value), self.assertRaises(ValueError):
                self.windows._verify_frame_grid(
                    self._terminal_media(duration=value), self.source, 0, 2, 30, 7)
        with self.assertRaises(ValueError):
            self.windows._verify_frame_grid(self._terminal_media(None), self.source, 0, 2, 30, 7)
        with self.assertRaisesRegex(ValueError, '非恒定帧率'):
            self.windows._verify_frame_grid(self._terminal_media(gap=True), self.source, 0, 2, 30, 7)

    def test_interior_boundary_does_not_extend_an_existing_frame_start(self):
        grid = self.windows._verify_frame_grid(self._terminal_media(), self.source, 0, 1, 30, 7)
        self.assertEqual(grid['aligned_end_seconds'], 1)
        self.assertEqual(grid['last_pts'], 8)
        self.assertNotIn('terminal_frame_duration_seconds', grid)

    def test_preview_rejects_invalid_or_inconsistent_terminal_evidence(self):
        _, original = self._extraction()
        for duration in (None, 0, -1, True, float('nan'), float('inf'), .1, 1/30):
            with self.subTest(duration=duration):
                # Adding an endpoint duration to an old start-point record is inconsistent.
                original['frame_grid']['terminal_frame_duration_seconds'] = duration
                with self.assertRaises(ValueError):
                    self.windows._preview_window_duration(original)

    @unittest.skipUnless(os.environ.get('ECLIP_TEST_FFMPEG') and os.environ.get('ECLIP_TEST_FFPROBE'),
                         'set ECLIP_TEST_FFMPEG and ECLIP_TEST_FFPROBE for actual media regression')
    def test_real_full_source_window_keeps_last_frame_without_changing_review(self):
        from media_adapter import MediaAdapter
        from PIL import Image
        cfg = {'ffmpeg_path': os.environ['ECLIP_TEST_FFMPEG'],
               'ffprobe_path': os.environ['ECLIP_TEST_FFPROBE']}
        (self.workspace / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
        media = MediaAdapter(cfg, self.workspace / 'terminal-fixture.log')
        self.source = self.source.with_suffix('.mkv')
        media.write_media(['-f', 'lavfi', '-i', 'color=red:size=64x96:rate=30:duration=2',
                           '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                           '-vf', "drawbox=color=blue:t=fill:enable='gte(n,59)'",
                           '-c:v', 'libx264', '-c:a', 'pcm_s16le', str(self.source)], self.source)
        self.context['source_video'] = str(self.source)
        self.context['source_sha256'] = self.support.file_hash(self.source)
        self._write_card()
        before = self.card.read_bytes()
        result = self.windows.extract(self.card, 0, 2)
        record = self.support.read(result['extraction'])
        self.assertAlmostEqual(record['source_span'][1], 2, delta=.001)
        self.assertEqual(record['frame_grid']['frame_count'], 60)
        self.assertAlmostEqual(record['source_audio_audit']['requested_end_pts'], 2, places=3)
        self.assertAlmostEqual(self.windows._preview_window_duration(record), 2, delta=.001)
        self.assertEqual(record['review_status'], 'unknown')
        self.assertEqual(self.card.read_bytes(), before)
        self.assertEqual(self.support.file_hash(self.source), self.context['source_sha256'])
        window = Path(result['window']['path'])
        probe = json.loads(media.command('ffprobe', ['-v', 'error', '-select_streams', 'v:0',
                           '-count_frames', '-show_entries', 'stream=nb_read_frames',
                           '-of', 'json', str(window)]))
        self.assertEqual(int(probe['streams'][0]['nb_read_frames']), 60)
        last = window.parent / 'last-frame.png'
        media.write_media(['-i', str(window), '-vf', 'select=eq(n\\,59)', '-frames:v', '1', str(last)], last)
        with Image.open(last) as image:
            red, _, blue = image.convert('RGB').getpixel((20, 20))
        self.assertGreater(blue, red+150)

    @unittest.skipUnless(os.environ.get('ECLIP_TEST_FFMPEG') and os.environ.get('ECLIP_TEST_FFPROBE'),
                         'set ECLIP_TEST_FFMPEG and ECLIP_TEST_FFPROBE for actual media regression')
    def test_real_terminal_frame_without_full_audio_is_not_silently_padded(self):
        from media_adapter import MediaAdapter
        cfg = {'ffmpeg_path': os.environ['ECLIP_TEST_FFMPEG'],
               'ffprobe_path': os.environ['ECLIP_TEST_FFPROBE']}
        (self.workspace / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
        media = MediaAdapter(cfg, self.workspace / 'short-audio-fixture.log')
        self.source = self.source.with_suffix('.mkv')
        media.write_media(['-f', 'lavfi', '-i', 'color=red:size=64x96:rate=30:duration=2',
                           '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1.97',
                           '-c:v', 'libx264', '-c:a', 'pcm_s16le', str(self.source)], self.source)
        self.context['source_video'] = str(self.source)
        self.context['source_sha256'] = self.support.file_hash(self.source)
        self._write_card()
        before = self.card.read_bytes()
        with self.assertRaisesRegex(ValueError, '原片音频.*覆盖'):
            self.windows.extract(self.card, 0, 2)
        self.assertEqual(self.card.read_bytes(), before)
        self.assertFalse(list(self.workspace.rglob('window.mp4')))


if __name__ == '__main__':
    unittest.main()
