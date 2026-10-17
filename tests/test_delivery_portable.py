"""Portable delivery checks using synthetic media, never merchant assets."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import cover_layout
import publish_package as delivery
import runtime
from quality_support import delivery_files_evidence


FONT = Path('C:/Windows/Fonts/msyh.ttc')


class DeliveryPortableTests(unittest.TestCase):
    def test_prepare_complete_without_git_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)/'workspace'
            root.mkdir()
            source = root/'outputs/final'
            comp = root/'outputs/composition'
            (source/'logs').mkdir(parents=True)
            comp.mkdir()
            video = source/'final.mp4'
            video.write_bytes(b'synthetic-final-video')
            original = root/'original.mp4'
            original.write_bytes(b'synthetic-original')
            for name in ('subtitle.srt','subtitle.ass'):
                (source/name).write_text('synthetic subtitle',encoding='utf-8')
            selected = {'clips':[{'id':'clip-001'}],'speed':1.0,'composition':str(comp),
                        'video':str(video),'original':str(original)}
            for path,data in [(source/'result.json',{'version':'0.2c','status':'completed',
                                 'video':str(video),'metadata':{'duration':10},'quality_version':'test'}),
                              (source/'logs/selected.json',selected),
                              (source/'logs/cues.json',[{'start':i,'lines':['蓝色杯子']} for i in range(1,8)]),
                              (comp/'result.json',{'material':{'date':None,'product_name':'蓝色杯子'}})]:
                path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
            class FakeMedia:
                def __init__(self,*args): pass
                def check_frame_timing(self,*args): return {'status':'passed'}
                def probe(self,*args): return {'width':1080,'height':1920,'duration':10}
                def write_media(self,args,path): Image.new('RGB',(360,640),'blue').save(path)
            local_config = root/'config.json'
            local_config.write_text('{}',encoding='utf-8')
            external_config = root.parent/'external-config.json'
            external_config.write_text('{}',encoding='utf-8')
            with self._gates(root),patch.object(delivery,'current_media_quality',return_value={}), \
                 patch.object(delivery,'MediaAdapter',FakeMedia),patch.object(delivery,'git_revision',return_value=None), \
                 patch.object(runtime,'ROOT',root):
                with self.assertRaisesRegex(ValueError,'当前工作目录'):
                    delivery.prepare(source,'delivery-test',external_config)
                self.assertFalse((root/'outputs/delivery-test').exists())
                package = delivery.prepare(source,'delivery-test',local_config)
                info = json.loads((package/'publish.json').read_text(encoding='utf-8'))
                self.assertEqual(info['status'],'awaiting_package_content')
                self.assertEqual(info['production']['release_version'],
                                 (delivery.INSTALL_ROOT / 'VERSION').read_text(encoding='utf-8-sig').strip())
                self.assertIsNone(info['production']['git_revision'])
                self.assertTrue((package/'frames/contact-sheet.jpg').is_file())
                for kind in ('srt','ass'):
                    name = 'subtitle.' + kind
                    self.assertTrue((package/name).is_file(), name + ' must travel with the package')
                    self.assertEqual((package/name).read_bytes(),(source/name).read_bytes())
                    self.assertEqual(info['subtitles'][kind],
                                     {'path':name,'sha256':delivery.file_hash(source/name)})
                    self.assertIn(f']({name})',(package/'publish-preview.md').read_text(encoding='utf-8'))
                (source/'subtitle.ass').unlink()
                srt_only = delivery.prepare(source,'delivery-srt-only',local_config)
                srt_info = json.loads((srt_only/'publish.json').read_text(encoding='utf-8'))
                self.assertEqual(set(srt_info['subtitles']),{'srt'})
                self.assertFalse((srt_only/'subtitle.ass').exists())
                (source/'subtitle.srt').unlink()
                with self.assertRaisesRegex(ValueError,'SRT|subtitle.srt'):
                    delivery.prepare(source,'delivery-missing-srt',local_config)
                self.assertFalse((root/'outputs/delivery-missing-srt').exists())

    def _package_fixture(self, root):
        root = Path(root)
        package = root/'outputs/task/publish-package'
        (package/'logs').mkdir(parents=True)
        (package/'frames').mkdir()
        (root/'private-context').mkdir()
        video = package/'video.mp4'
        video.write_bytes(b'synthetic-video-no-actual-media')
        subtitles = {}
        for kind in ('srt','ass'):
            name = 'subtitle.' + kind
            (package/name).write_text('synthetic ' + kind + ' subtitle',encoding='utf-8')
            subtitles[kind] = {'path':name,'sha256':delivery.file_hash(package/name)}
        frame = package/'frames/frame-01.jpg'
        Image.new('RGB',(360,640),'#2865a4').save(frame)
        frame_hash = delivery.file_hash(frame)
        facts = {'product':{'date':None,'name':'蓝色杯子'},
                 'video_sha256':delivery.file_hash(video),
                 'subtitles':[{'id':'cue-001','text':'这是蓝色杯子，拿起来看杯身'}],
                 'positioning':{'kind':'unknown'}}
        facts['hash'] = delivery.key(facts)
        draft = {'facts_hash':facts['hash'],
                 'title_candidates':['蓝色杯子杯身细节展示','看这只蓝色杯子的杯身','蓝色杯子拿起实拍展示'],
                 'selected_title':0,'selection_reason':'来自原声','caption':'这是蓝色杯子，主播拿起来展示杯身，具体细节请以视频实拍为准。',
                 'hashtags':['#杯子展示','#杯身细节','#实物展示'],
                 'cover_frame':1,'frame_review':{'sha256':frame_hash,'frame_time':1.5,
                    'reviewer':'test-visual-review','reviewed_at':'2026-10-15T10:00:00+08:00',
                    'conclusion':'matched','finding':'看过合成图，蓝色杯子完整显示'},
                 'cover_reason':'商品完整','evidence':[{'cue_id':'cue-001','quote':'蓝色杯子'}],
                 'template_cover':{'lines':['蓝色杯子','拿起来看杯身'],
                    'evidence':[{'line_index':0,'cue_id':'cue-001','quote':'蓝色杯子'}]}}
        draft_path = root/'private-context/draft.json'
        for path, value in [(package/'publish.json',{'pipeline_version':'0.1.0',
                     'status':'awaiting_package_content','product':facts['product'],
                     'subtitles':subtitles,
                     'publish':{'account':None,'product_link':None,'scheduled_time':None}}),
                     (package/'logs/facts.json',facts),
                     (package/'logs/frames.json',[{'id':1,'time':1.5,'path':str(frame),'sha256':frame_hash}]),
                     (package/'logs/source-hashes.json',{str(video):delivery.file_hash(video)}),
                     (draft_path,draft)]:
            path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        return package,draft_path,frame,draft

    def _gates(self, root):
        stack = ExitStack()
        context = {'task_record':str(Path(root)/'private-context/task.json'),
                   'source_video':str(Path(root)/'original.mp4')}
        stack.enter_context(patch.object(delivery,'ROOT',Path(root)))
        stack.enter_context(patch('quality_support.context_from',return_value=context))
        stack.enter_context(patch.object(delivery,'task_gate',return_value=context))
        stack.enter_context(patch.object(delivery,'no_resubmit'))
        stack.enter_context(patch.object(delivery,'local_records',return_value=[]))
        stack.enter_context(patch.object(delivery,'production_hashes',return_value={}))
        stack.enter_context(patch.object(delivery,'package_quality',return_value={
            'technical':{'status':'passed'},'continuous_visual':{'status':'not_reviewed'},
            'audio_review':{'status':'not_reviewed'}}))
        return stack

    def test_source_frame_finish_is_review_bound_and_does_not_claim_content_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            package,draft_path,frame,draft = self._package_fixture(tmp)
            with self._gates(tmp):
                report = delivery.preflight(package,draft_path,FONT)
                self.assertEqual(report['status'],'passed',report)
                result = delivery.finish(package,draft_path,FONT)
                self.assertEqual(result['status'],'awaiting_content_review')
                self.assertEqual(result['cover_method'],'source_frame')
                self.assertEqual(result['cover_source']['sha256'],delivery.file_hash(frame))
                self.assertEqual(result['delivery_files']['video.mp4'],delivery.file_hash(package/'video.mp4'))
                self.assertEqual(result['delivery_files']['cover-3x4.jpg'],delivery.file_hash(package/'cover-3x4.jpg'))
                self.assertEqual(result['delivery_files']['title.txt'],delivery.file_hash(package/'title.txt'))
                self.assertEqual(result['delivery_files']['caption.txt'],delivery.file_hash(package/'caption.txt'))
                for kind in ('srt','ass'):
                    name = 'subtitle.' + kind
                    self.assertEqual(result['delivery_files'].get(name),delivery.file_hash(package/name))
                self.assertEqual(delivery_files_evidence(package,result)['cover-3x4.jpg'],
                                 result['delivery_files']['cover-3x4.jpg'])
                self.assertTrue((package/'cover-3x4.jpg').is_file())
                self.assertFalse((package/'cover-4x3.jpg').exists())
                self.assertEqual((package/'video.mp4').read_bytes(),b'synthetic-video-no-actual-media')
                (package/'title.txt').write_text('tampered\n',encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'标题|哈希'):
                    delivery_files_evidence(package,result)
                with self.assertRaises(FileExistsError):
                    delivery.finish(package,draft_path,FONT)

    def test_retext_keeps_video_and_changes_only_copy_and_cover_in_new_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            package,draft_path,frame,draft = self._package_fixture(tmp)
            with self._gates(tmp):
                delivery.finish(package,draft_path,FONT)
                video_hash = delivery.file_hash(package/'video.mp4')
                subtitle_hashes = {name:delivery.file_hash(package/name)
                                   for name in ('subtitle.srt','subtitle.ass')}
                draft['title_candidates'][0] = '蓝色杯子拿起看杯身细节'
                draft['template_cover']['lines'] = ['蓝色杯子', '实拍杯身细节']
                draft_path.write_text(json.dumps(draft,ensure_ascii=False),encoding='utf-8')
                revised = delivery.retext(package,draft_path,FONT,'revision-01')
                self.assertEqual(delivery.file_hash(revised/'video.mp4'),video_hash)
                self.assertEqual(delivery.file_hash(package/'video.mp4'),video_hash)
                self.assertEqual((revised/'title.txt').read_text(encoding='utf-8').strip(),draft['title_candidates'][0])
                self.assertEqual(json.loads((revised/'publish.json').read_text(encoding='utf-8'))['status'],
                                 'awaiting_content_review')
                revised_info=json.loads((revised/'publish.json').read_text(encoding='utf-8'))
                for name,sha in subtitle_hashes.items():
                    self.assertEqual(delivery.file_hash(revised/name),sha)
                    self.assertEqual(delivery.file_hash(package/name),sha)
                    self.assertEqual(revised_info['delivery_files'].get(name),sha)
                    self.assertIn(f']({name})',(revised/'publish-preview.md').read_text(encoding='utf-8'))
                self.assertEqual(revised_info['delivery_files']['video.mp4'],video_hash)
                self.assertEqual(revised_info['delivery_files']['cover-3x4.jpg'],delivery.file_hash(revised/'cover-3x4.jpg'))
                self.assertEqual(revised_info['delivery_files']['title.txt'],delivery.file_hash(revised/'title.txt'))
                self.assertEqual(delivery_files_evidence(revised,revised_info)['caption.txt'],
                                 revised_info['delivery_files']['caption.txt'])
                self.assertFalse((revised/'cover-4x3.jpg').exists())

    def test_changed_subtitles_cannot_be_finished_or_resigned_by_retext(self):
        with tempfile.TemporaryDirectory() as tmp:
            package,draft_path,_,_ = self._package_fixture(tmp)
            original = (package/'subtitle.srt').read_bytes()
            with self._gates(tmp):
                (package/'subtitle.srt').write_bytes(b'changed synthetic subtitles')
                report = delivery.preflight(package,draft_path,FONT)
                self.assertEqual(report['status'],'failed')
                self.assertFalse((package/'title.txt').exists())
                (package/'subtitle.srt').write_bytes(original)
                delivery.finish(package,draft_path,FONT)
                (package/'subtitle.srt').write_bytes(b'changed synthetic subtitles')
                with self.assertRaisesRegex(ValueError,'字幕|哈希'):
                    delivery.retext(package,draft_path,FONT,'tampered-revision')
                self.assertFalse((Path(tmp)/'outputs/tampered-revision').exists())

    def test_user_image_requires_matching_visual_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            package,draft_path,frame,draft = self._package_fixture(tmp)
            external = Path(tmp)/'user-image.png'
            Image.new('RGB',(500,500),'#285ca4').save(external)
            review = dict(draft['frame_review'],sha256=delivery.file_hash(external))
            review.pop('frame_time')
            with self._gates(tmp):
                bad = delivery.preflight(package,draft_path,FONT,cover_mode='user_image',
                    cover_ratio='4x3',user_image=external,image_review_path=dict(review,sha256='wrong'))
                self.assertEqual(bad['status'],'failed')
                self.assertFalse((package/'cover-4x3.jpg').exists())
                good = delivery.preflight(package,draft_path,FONT,cover_mode='user_image',
                    cover_ratio='4x3',user_image=external,image_review_path=review)
                self.assertEqual(good['status'],'passed',good)
                result = delivery.finish(package,draft_path,FONT,cover_mode='user_image',
                    cover_ratio='4x3',user_image=external,image_review_path=review)
                self.assertEqual(result['cover_method'],'user_image')
                self.assertTrue((package/'cover-4x3.jpg').is_file())
                self.assertEqual(result['cover_source']['path'],'cover-source.png')

    def test_source_frame_without_actual_visual_review_cannot_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            package,draft_path,frame,draft = self._package_fixture(tmp)
            draft.pop('frame_review')
            draft_path.write_text(json.dumps(draft,ensure_ascii=False),encoding='utf-8')
            with self._gates(tmp):
                report = delivery.preflight(package,draft_path,FONT)
                self.assertEqual(report['status'],'failed')
                self.assertFalse((package/'cover-3x4.jpg').exists())
                with self.assertRaisesRegex(ValueError,'AI生成封面暂不支持'):
                    delivery.finish(package,draft_path,FONT,cover_mode='ai_template')

    def test_portrait_source_is_contained_and_text_stays_in_separate_zone(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/'source.png', Path(tmp)/'cover.jpg'
            picture = Image.new('RGB', (360, 640), '#1468c4')
            for x in range(360):
                for y in (0, 639): picture.putpixel((x, y), (240,32,32))
            for y in range(640):
                for x in (0, 359): picture.putpixel((x, y), (240,32,32))
            picture.save(source)
            config = json.loads((Path(__file__).resolve().parents[1]/'pipeline/cover_config.json').read_text(encoding='utf-8'))
            config['lines'] = ['真实原声', '蓝色商品展示']
            for ratio, size in [('3x4',(1080,1440)), ('4x3',(1440,1080))]:
                target = output.with_name(f'cover-{ratio}.jpg')
                result = cover_layout.render(source, target, config, ratio, FONT)
                with Image.open(target) as rendered:
                    self.assertEqual(rendered.size, size)
                    self.assertEqual(result['image_box'][2]-result['image_box'][0],
                                     round((result['image_box'][3]-result['image_box'][1])*360/640),
                                     'image aspect ratio must be preserved')
                    self.assertTrue(all(box[1] >= result['text_area'][1] and box[3] <= result['text_area'][3]
                                        for box in result['text_boxes']))
                    self.assertLess(rendered.getpixel((result['image_box'][0]+2,result['image_box'][1]+2))[0], 255)

    def test_no_git_revision_falls_back_to_release_version(self):
        with patch.object(delivery, 'git_revision', return_value=None):
            identity = delivery.release_identity()
        self.assertEqual(identity['release_version'],
                         (delivery.INSTALL_ROOT / 'VERSION').read_text(encoding='utf-8-sig').strip())
        self.assertIsNone(identity['git_revision'])

    def test_default_cover_and_preview_do_not_claim_ai_or_publish_review(self):
        self.assertEqual(delivery.DEFAULT_COVER_MODE, 'source_frame')
        sample = {'product':{'name':'测试商品'},'status':'awaiting_content_review',
                  'cover':{'path':'cover-3x4.jpg'},'main_cover_3x4':'cover-3x4.jpg',
                  'title':'真实展示','caption':'真实原声','hashtags':[],
                  'quality_evidence':{'continuous_visual':{'status':'not_reviewed'}}}
        page = delivery.preview(Path('.'), sample)
        self.assertIn('待审', page)
        self.assertNotIn('AI模板', page)
        self.assertNotIn('待人工发布', page)


if __name__ == '__main__': unittest.main()
