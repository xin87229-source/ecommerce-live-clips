import json
import sys
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))


def test_runtime_config_and_profiles(tmp_path):
    import importlib
    import runtime
    importlib.reload(runtime)
    assert runtime.ROOT == tmp_path
    assert runtime.config() == {}
    assert runtime.profile()['name'] == 'basic'
    assert runtime.profile()['min_source_seconds'] == 0
    assert runtime.profile()['max_duration_seconds'] == 70
    assert runtime.profile()['size'] == [1080, 1920]
    (tmp_path / 'config.json').write_text(json.dumps({'profile': 'apparel', 'max_duration_seconds': 30}), encoding='utf-8')
    assert runtime.profile()['min_source_seconds'] == 20
    assert runtime.profile()['max_duration_seconds'] == 30
    (tmp_path / 'config.json').write_text(json.dumps({'profile': 'basic', 'max_duration_seconds': 71}), encoding='utf-8')
    with unittest.TestCase().assertRaisesRegex(ValueError, '70'):
        runtime.profile()


def test_install_resource_and_private_workspace(tmp_path):
    import importlib
    import runtime
    import selector_support
    importlib.reload(runtime)
    importlib.reload(selector_support)
    assert selector_support.ROOT == tmp_path
    assert selector_support.PRIVATE == tmp_path / 'private-context/selector-v02a'
    assert selector_support.PROMPT == runtime.INSTALL_ROOT / 'pipeline/prompts/candidates.md'


def test_basic_short_complete_clip_and_configured_limit(tmp_path):
    (tmp_path / 'config.json').write_text(json.dumps({'max_duration_seconds': 12}), encoding='utf-8')
    import combine
    candidates = [{'id': 'one', 'start_seconds': 0, 'end_seconds': 10, 'kind': 'speech'}]
    annotations = {'speed': 1, 'speed_reason': '原速清晰', 'content_complete_reason': '完整一句讲解', 'order': ['one']}
    assert combine.choose_explicit(candidates, annotations)['expected_duration'] == 10
    candidates[0]['end_seconds'] = 13
    with unittest.TestCase().assertRaisesRegex(ValueError, '12'):
        combine.choose_explicit(candidates, annotations)


def test_profile_change_invalidates_adoption_fingerprint(tmp_path):
    import batch_plan
    plan = {'source': {'sha256': 'a' * 64}, 'product': {'name': '商品', 'code': 'local-x'},
            'coverage': {'confirmed_ranges': [[0, 10]]}, 'batch_id': 'batch-x'}
    item = {'item_id': 'one', 'task_id': 'task-x', 'order': ['c'], 'speed': 1,
            'mainline': '完整讲解', 'completeness': '完整', 'roles': {'c': 'main'}}
    candidates = {'c': {'id': 'c', 'start_seconds': 0, 'end_seconds': 10}}
    first = batch_plan._fingerprint(plan, item, candidates)
    (tmp_path / 'config.json').write_text(json.dumps({'profile': 'apparel'}), encoding='utf-8')
    assert batch_plan._fingerprint(plan, item, candidates) != first


def test_transcription_requires_explicit_local_model(tmp_path):
    import transcribe_adapter
    with unittest.TestCase().assertRaisesRegex(ValueError, 'model_path'):
        transcribe_adapter.local_model_path()
    model = tmp_path / 'model'
    model.mkdir()
    (model / 'tokenizer.json').write_text('{}', encoding='utf-8')
    (model / 'model.bin').write_bytes(b'weights')
    (tmp_path / 'config.json').write_text(json.dumps({'model_path': str(model)}), encoding='utf-8')
    assert transcribe_adapter.local_model_path() == model


def test_full_publish_rejected_before_other_task_checks():
    import quality_support
    with unittest.TestCase().assertRaisesRegex(ValueError, 'full_publish'):
        quality_support.validate_task({'scope': 'full_publish', 'task_id': 'x'}, 'missing.mp4')


def test_basic_accepts_short_confirmed_source_without_platform_fields(tmp_path):
    import quality_support
    import selector_support
    from runtime import profile
    source = tmp_path / '原始 直播.mp4'
    source.write_bytes(b'not media, identity only')
    reference = tmp_path / '商品参考.txt'
    reference.write_text('虚构商品参考', encoding='utf-8')
    frame = tmp_path / '代表帧.txt'
    frame.write_text('人工已核对画面', encoding='utf-8')
    speech = tmp_path / '原声依据.txt'
    speech.write_text('人工已核对口播', encoding='utf-8')
    product = {'name': '虚构商品', 'code': 'local-test',
               'reference': {'path': str(reference), 'sha256': selector_support.file_hash(reference)}}
    windows = [{'range': [0, 8], 'visual': [
        {'path': str(frame), 'sha256': selector_support.file_hash(frame), 'at': t}
        for t in (0, 4, 8)],
        'speech': {'type': 'audio_listening', 'path': str(speech),
                   'sha256': selector_support.file_hash(speech), 'range': [0, 8], 'finding': '商品讲解完整'}}]
    context = {'task_id': 'test', 'scope': 'production', 'profile': profile(),
               'content_strategy': {'version': 'single-style-batch-v1'},
               'product': product, 'source_video': str(source),
               'source_sha256': selector_support.file_hash(source), 'confirmed_windows': windows,
               'source_review': {'conclusion': 'matched', 'source_sha256': selector_support.file_hash(source),
                                 'identity_hash': selector_support.key(product),
                                 'windows_hash': selector_support.key(windows),
                                 'reviewer': 'operator', 'reviewed_at': '2026-10-15T10:00:00+08:00'}}
    assert quality_support.validate_task(context, source)['product']['code'] == 'local-test'
    (tmp_path / 'config.json').write_text(json.dumps({'profile': 'apparel'}), encoding='utf-8')
    with unittest.TestCase().assertRaisesRegex(ValueError, '预设'):
        quality_support.validate_task(context, source)


def test_batch_source_threshold_is_profile_specific(tmp_path):
    import batch_plan
    result = {'coverage_complete': True, 'pending_seconds': 0, 'confirmed_source_seconds': 8,
              'plan': {'coverage': {'confirmed_ranges': [[0, 8]]}, 'items': [{'order': ['c']}]},
              'candidates': {'c': {'start_seconds': 0, 'end_seconds': 8}}}
    batch_plan._executable(result)
    (tmp_path / 'config.json').write_text(json.dumps({'profile': 'apparel'}), encoding='utf-8')
    with unittest.TestCase().assertRaisesRegex(ValueError, '20'):
        batch_plan._executable(result)


def test_partial_120_second_batch_only_executes_for_explicit_test_scope():
    import batch_plan
    source_duration = 3.5 * 60 * 60
    result = {'coverage_complete': False, 'pending_seconds': 0, 'confirmed_source_seconds': 120,
              'plan': {'source': {'duration_seconds': source_duration},
                       'coverage': {'scope': 'reviewed_windows_only',
                                    'searched_ranges': [[600, 720]],
                                    'confirmed_ranges': [[600, 720]]},
                       'items': [{'order': ['c']}]},
              'candidates': {'c': {'start_seconds': 620, 'end_seconds': 660}}}
    with unittest.TestCase().assertRaisesRegex(ValueError, '整场'):
        batch_plan._executable(result, {'scope': 'production'})
    batch_plan._executable(result, {'scope': 'test'})
    result['plan']['coverage'].pop('scope')
    with unittest.TestCase().assertRaisesRegex(ValueError, '整场'):
        batch_plan._executable(result, {'scope': 'test'})
    result['plan']['coverage']['scope'] = 'reviewed_windows_only'
    result['pending_seconds'] = 1
    with unittest.TestCase().assertRaisesRegex(ValueError, '待确认'):
        batch_plan._executable(result, {'scope': 'test'})


def test_batch_adopt_accepts_distinct_parent_task_but_keeps_source_binding(tmp_path):
    import batch_plan
    source = tmp_path / 'live.mp4'
    source.write_bytes(b'original')
    plan = {'source': {'path': str(source), 'sha256': 'a' * 64},
            'product': {'name': '商品', 'code': 'local-x'},
            'coverage': {'confirmed_ranges': [[600, 720]]}}
    item = {'task_id': 'clip-child'}
    parent = {'task_id': 'live-parent', 'source_video': str(source), 'source_sha256': 'a' * 64,
              'product': plan['product'], 'confirmed_windows': [{'range': [600, 720]}],
              'cross_device_state': 'local_records_checked'}
    batch_plan._protected(plan, item, parent, parent_adoption=True)
    parent['source_sha256'] = 'b' * 64
    with unittest.TestCase().assertRaisesRegex(ValueError, '父任务'):
        batch_plan._protected(plan, item, parent, parent_adoption=True)


def test_missing_task_card_does_not_reset_existing_output(tmp_path):
    import importlib
    import selector_support
    import run_records
    importlib.reload(selector_support)
    importlib.reload(run_records)
    folder = tmp_path / 'outputs' / 'task-one'
    folder.mkdir(parents=True)
    (folder / 'result.json').write_text('{"status":"completed"}', encoding='utf-8')
    with unittest.TestCase().assertRaisesRegex(ValueError, '接续'):
        run_records.initialize(folder / 'run-state.json', task_context={'task_id': 'task-one'})


def test_upload_event_is_disabled_in_local_delivery():
    import importlib
    import selector_support
    import run_records
    import runtime
    importlib.reload(selector_support)
    importlib.reload(run_records)
    with unittest.TestCase().assertRaisesRegex(ValueError, 'full_publish'):
        run_records.record_event(runtime.ROOT / 'outputs/missing.json', event_id='x', kind='upload', phase='started',
                                 stage='upload', evidence={})


def test_new_task_starts_with_local_work_action(tmp_path):
    import importlib
    import selector_support
    import run_records
    importlib.reload(selector_support)
    importlib.reload(run_records)
    card = run_records.initialize(tmp_path / 'outputs/task-two/run-state.json',
                                  task_context={'task_id': 'task-two'})
    assert '排期' not in card['next_action']
    assert card['submitted'] is False


def test_new_delivery_needs_adoption_binding():
    import quality_support
    assert quality_support.requires_adoption_binding({'pipeline_version': '0.1.0'})
    assert quality_support.requires_adoption_binding({'cover_method': 'source_frame'})
    assert quality_support.requires_adoption_binding({'cover_method': 'user_image'})


def test_cover_source_evidence_accepts_original_image_extension(tmp_path):
    import quality_support
    import selector_support
    image = tmp_path / 'cover-source.webp'
    image.write_bytes(b'example webp bytes')
    info = {'cover_method': 'user_image',
            'cover_source': {'path': image.name, 'sha256': selector_support.file_hash(image)}}
    assert quality_support.cover_source_evidence(tmp_path, info) == image
    info['cover_source']['sha256'] = '0' * 64
    with unittest.TestCase().assertRaisesRegex(ValueError, '哈希'):
        quality_support.cover_source_evidence(tmp_path, info)


def test_delivery_files_evidence_catches_final_cover_and_copy_changes(tmp_path):
    import quality_support
    import selector_support
    cover = tmp_path / 'cover-3x4.jpg'
    cover.write_bytes(b'final cover')
    (tmp_path / 'title.txt').write_text('实际标题\n', encoding='utf-8')
    (tmp_path / 'caption.txt').write_text('实际文案\n', encoding='utf-8')
    logs = tmp_path / 'logs'
    logs.mkdir()
    draft = {'title_candidates': ['实际标题', '其他标题'], 'selected_title': 0,
             'caption': '实际文案', 'hashtags': ['#示例'],
             'template_cover': {'lines': ['原话短语']}, 'facts_hash': 'fact-hash'}
    (logs / 'draft.json').write_text(json.dumps(draft, ensure_ascii=False), encoding='utf-8')
    layout = {'cover_hashes': {'3x4': selector_support.file_hash(cover)},
              'config': {'lines': ['原话短语']}, 'facts_hash': 'fact-hash'}
    (logs / 'cover-layout.json').write_text(json.dumps(layout, ensure_ascii=False), encoding='utf-8')
    info = {'title': '实际标题', 'title_candidates': draft['title_candidates'],
            'caption': '实际文案', 'hashtags': ['#示例'],
            'main_cover_3x4': cover.name,
            'cover': {'path': cover.name, 'source_path': 'cover-source.jpg', 'text': ['原话短语']},
            'cover_source': {'path': 'cover-source.jpg'}}
    quality_support.delivery_files_evidence(tmp_path, info)
    (tmp_path / 'video.mp4').write_bytes(b'current output')
    (tmp_path / 'cover-source.jpg').write_bytes(b'original frame')
    (tmp_path / 'subtitle.srt').write_text('1\n00:00:00,000 --> 00:00:01,000\n实际字幕\n', encoding='utf-8')
    info['pipeline_version'] = '0.1.0'
    info['subtitles'] = {'srt': {'path': 'subtitle.srt',
                                 'sha256': selector_support.file_hash(tmp_path / 'subtitle.srt')}}
    info['delivery_files'] = {name: selector_support.file_hash(tmp_path / name) for name in
                              ('video.mp4', 'cover-source.jpg', 'cover-3x4.jpg', 'title.txt', 'caption.txt')}
    with unittest.TestCase().assertRaisesRegex(ValueError, '字幕|清单'):
        quality_support.delivery_files_evidence(tmp_path, info)
    info['delivery_files']['subtitle.srt'] = selector_support.file_hash(tmp_path / 'subtitle.srt')
    quality_support.delivery_files_evidence(tmp_path, info)
    manifest = info.pop('delivery_files')
    with unittest.TestCase().assertRaisesRegex(ValueError, '清单'):
        quality_support.delivery_files_evidence(tmp_path, info)
    info['delivery_files'] = manifest
    cover.write_bytes(b'changed cover')
    with unittest.TestCase().assertRaisesRegex(ValueError, '封面'):
        quality_support.delivery_files_evidence(tmp_path, info)
    cover.write_bytes(b'final cover')
    (tmp_path / 'title.txt').write_text('篡改标题\n', encoding='utf-8')
    with unittest.TestCase().assertRaisesRegex(ValueError, '标题'):
        quality_support.delivery_files_evidence(tmp_path, info)


def test_subtitle_files_evidence_requires_exact_local_sources(tmp_path):
    import quality_support
    import selector_support
    info = {'pipeline_version': '0.1.0'}
    with unittest.TestCase().assertRaisesRegex(ValueError, '字幕'):
        quality_support.subtitle_files_evidence(tmp_path, info)
    info['subtitles'] = {'srt': None}
    with unittest.TestCase().assertRaisesRegex(ValueError, '字幕'):
        quality_support.subtitle_files_evidence(tmp_path, info)
    srt = tmp_path / 'subtitle.srt'
    srt.write_bytes(b'actual srt')
    info['subtitles'] = {'srt': {'path': 'subtitle.srt', 'sha256': selector_support.file_hash(srt)}}
    assert quality_support.subtitle_files_evidence(tmp_path, info) == {'subtitle.srt': selector_support.file_hash(srt)}
    ass = tmp_path / 'subtitle.ass'
    ass.write_bytes(b'actual ass')
    with unittest.TestCase().assertRaisesRegex(ValueError, '字幕'):
        quality_support.subtitle_files_evidence(tmp_path, info)
    info['subtitles']['ass'] = {'path': 'subtitle.ass', 'sha256': selector_support.file_hash(ass)}
    assert quality_support.subtitle_files_evidence(tmp_path, info)['subtitle.ass'] == selector_support.file_hash(ass)
    info['subtitles']['ass']['path'] = '../subtitle.ass'
    with unittest.TestCase().assertRaisesRegex(ValueError, '字幕'):
        quality_support.subtitle_files_evidence(tmp_path, info)


def test_stage_config_rejects_other_workspace_path(tmp_path):
    import runtime
    own = tmp_path / 'config.json'
    own.write_text(json.dumps({'profile': 'basic'}), encoding='utf-8')
    other = tmp_path / 'other' / 'config.json'
    other.parent.mkdir()
    other.write_text(json.dumps({'profile': 'apparel'}), encoding='utf-8')
    assert runtime.stage_config(own)['profile'] == 'basic'
    with unittest.TestCase().assertRaisesRegex(ValueError, '当前工作目录'):
        runtime.stage_config(other)


class RuntimeCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.env = patch.dict('os.environ', {'ECLIP_WORKSPACE': str(self.path)})
        self.env.start()
        import importlib
        if 'runtime' in sys.modules:
            importlib.reload(sys.modules['runtime'])

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_runtime_config_and_profiles(self):
        test_runtime_config_and_profiles(self.path)

    def test_install_resource_and_private_workspace(self):
        test_install_resource_and_private_workspace(self.path)

    def test_basic_short_complete_clip_and_configured_limit(self):
        test_basic_short_complete_clip_and_configured_limit(self.path)

    def test_profile_change_invalidates_adoption_fingerprint(self):
        test_profile_change_invalidates_adoption_fingerprint(self.path)

    def test_transcription_requires_explicit_local_model(self):
        test_transcription_requires_explicit_local_model(self.path)

    def test_full_publish_rejected_before_other_task_checks(self):
        test_full_publish_rejected_before_other_task_checks()

    def test_basic_accepts_short_confirmed_source_without_platform_fields(self):
        test_basic_accepts_short_confirmed_source_without_platform_fields(self.path)

    def test_batch_source_threshold_is_profile_specific(self):
        test_batch_source_threshold_is_profile_specific(self.path)

    def test_partial_120_second_batch_only_executes_for_explicit_test_scope(self):
        test_partial_120_second_batch_only_executes_for_explicit_test_scope()

    def test_batch_adopt_accepts_distinct_parent_task_but_keeps_source_binding(self):
        test_batch_adopt_accepts_distinct_parent_task_but_keeps_source_binding(self.path)

    def test_missing_task_card_does_not_reset_existing_output(self):
        test_missing_task_card_does_not_reset_existing_output(self.path)

    def test_upload_event_is_disabled_in_local_delivery(self):
        test_upload_event_is_disabled_in_local_delivery()

    def test_new_task_starts_with_local_work_action(self):
        test_new_task_starts_with_local_work_action(self.path)

    def test_new_delivery_needs_adoption_binding(self):
        test_new_delivery_needs_adoption_binding()

    def test_cover_source_evidence_accepts_original_image_extension(self):
        test_cover_source_evidence_accepts_original_image_extension(self.path)

    def test_delivery_files_evidence_catches_final_cover_and_copy_changes(self):
        test_delivery_files_evidence_catches_final_cover_and_copy_changes(self.path)

    def test_subtitle_files_evidence_requires_exact_local_sources(self):
        test_subtitle_files_evidence_requires_exact_local_sources(self.path)

    def test_stage_config_rejects_other_workspace_path(self):
        test_stage_config_rejects_other_workspace_path(self.path)
