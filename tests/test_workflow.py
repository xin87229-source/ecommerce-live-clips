"""Real candidate draft shape and safe adoption-to-card binding."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import workflow
import batch_plan
import run_records
from selector_support import file_hash, key


class WorkflowTests(unittest.TestCase):
    def _save(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')

    def _fixture(self,root):
        root=Path(root)
        source=root.parent/'synthetic-original.mp4'
        source.write_bytes(b'synthetic original media')
        sha=file_hash(source)
        context={'task_id':'parent-01','scope':'test','product':{'name':'蓝杯','code':'SKU-BLUE'},
                 'source_video':str(source),'source_sha256':sha,'cross_device_state':'local_records_checked',
                 'confirmed_windows':[{'range':[0,10]}],
                 'source_review':{'conclusion':'matched','review_input':{'path':str(root/'private-context/review.json'),
                     'sha256':'review-hash'}}}
        card=root/'outputs/parent-01/run-state.json'
        self._save(card,{'task_context':context,'status':'NOT_UPLOADED_NOT_SUBMITTED','submitted':False})
        job=root/'private-context/selector-v02a/job-one'
        transcript=job/'transcript.json'
        data={'schema_version':1,'status':'complete','source':{'path':str(source),'sha256':sha,
              'duration_seconds':10},'segments':[{'id':'s000001','start':2,'end':7,'text':'这是蓝色杯子'}]}
        data['segments_hash']=key(data['segments'])
        self._save(transcript,data)
        self._save(job/'manifest.json',{'source_video':str(source),'transcript_cache':str(transcript),
                                      'transcript_hash':file_hash(transcript)})
        self._save(job/'candidate-clips.json',{'source_video':str(source),'candidates':[
            {'id':'c-001','kind':'speech','start_seconds':2,'end_seconds':7,'category':'商品展示',
             'reason':'实际候选','start_segment_id':'s000001','end_segment_id':'s000001',
             'text':'这是蓝色杯子'}]})
        return source,context,card,job

    def test_draft_uses_actual_candidate_and_leaves_semantics_unclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'workspace';root.mkdir()
            source,context,card,job=self._fixture(root)
            target=root/'private-context/batch-draft.json'
            with patch.object(workflow,'ROOT',root),patch.object(batch_plan,'ROOT',root), \
                 patch.object(workflow.quality,'task_gate',return_value=context):
                result=workflow.draft(job,card,'batch-one',target)
                with self.assertRaises(FileExistsError): workflow.draft(job,card,'batch-one',target)
            plan=json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(result['candidate_count'],1)
            self.assertEqual(plan['source']['sha256'],file_hash(source))
            self.assertEqual(plan['coverage']['searched_ranges'],[[0,10]])
            self.assertEqual(plan['items'],[])
            self.assertIsNone(plan['exclusions']['c-001']['usable'])
            self.assertEqual(plan['candidate_inventory'][0]['text'],'这是蓝色杯子')
            self.assertEqual(plan['why_not_more'],'')

    def test_imported_partial_transcript_does_not_claim_full_window_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'workspace';root.mkdir()
            _,context,card,job=self._fixture(root)
            transcript=job/'transcript.json'
            data=json.loads(transcript.read_text(encoding='utf-8'))
            data.update(coverage_scope='reviewed_windows_only',covered_ranges=[[0,7]])
            self._save(transcript,data)
            manifest=json.loads((job/'manifest.json').read_text(encoding='utf-8'))
            manifest['transcript_hash']=file_hash(transcript)
            self._save(job/'manifest.json',manifest)
            target=root/'private-context/partial-draft.json'
            with patch.object(workflow,'ROOT',root),patch.object(batch_plan,'ROOT',root), \
                 patch.object(workflow.quality,'task_gate',return_value=context):
                workflow.draft(job,card,'batch-partial',target)
            plan=json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(plan['coverage']['searched_ranges'],[[0,7]])
            self.assertEqual(plan['coverage']['confirmed_ranges'],[[0,7]])
            self.assertEqual(plan['coverage']['scope'],'reviewed_windows_only')
            with patch.object(batch_plan,'ROOT',root):
                batch_plan._candidates(plan)
                plan['coverage']['scope']='full_source'
                with self.assertRaisesRegex(ValueError,'全场盘点'):
                    batch_plan._candidates(plan)
                plan['coverage']['scope']='reviewed_windows_only'
                plan['coverage']['searched_ranges']=[[0,10]]
                with self.assertRaisesRegex(ValueError,'超出实际局部转写'):
                    batch_plan._candidates(plan)

    def test_extracted_window_child_does_not_claim_full_original_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'workspace';root.mkdir()
            _,context,card,job=self._fixture(root)
            context['source_window']={'parent_source_sha256':'p'*64,
                                      'offset_seconds':600,'source_span':[600,610]}
            self._save(card,{'task_context':context,'status':'NOT_UPLOADED_NOT_SUBMITTED','submitted':False})
            target=root/'private-context/window-draft.json'
            with patch.object(workflow,'ROOT',root),patch.object(batch_plan,'ROOT',root), \
                 patch.object(workflow.quality,'task_gate',return_value=context):
                workflow.draft(job,card,'batch-window',target)
            plan=json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(plan['coverage']['scope'],'reviewed_windows_only')

    def test_bind_writes_only_new_matching_child_card_and_rejects_stale_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'workspace';root.mkdir()
            source,context,parent,job=self._fixture(root)
            plan_path=root/'private-context/plan.json'
            plan={'batch_id':'batch-one','source':{'path':str(source),'sha256':file_hash(source),'duration_seconds':10},
                  'product':context['product'],'coverage':{'confirmed_ranges':[[0,10]]},
                  'items':[{'item_id':'item-01','task_id':'clip-01','order':['c-001'],'speed':1.0,
                            'adoption':{'fingerprint':'fingerprint-current'}}]}
            self._save(plan_path,plan)
            validated={'plan':plan,'items':{'item-01':{'adoption_fingerprint':'fingerprint-current'}},
                       'plan_path':str(plan_path)}
            with patch.object(workflow,'ROOT',root),patch.object(batch_plan,'ROOT',root), \
                 patch.object(run_records,'ROOT',root), \
                 patch.object(workflow.quality,'task_gate',return_value=context), \
                 patch.object(batch_plan,'validate_plan',return_value=validated), \
                 patch.object(batch_plan,'_executable'), \
                 patch.object(batch_plan,'execution_gate',return_value={'adoption_fingerprint':'fingerprint-current'}):
                validated['items']['item-01']['adoption_fingerprint']='stale'
                with self.assertRaisesRegex(ValueError,'旧指纹'):
                    workflow.bind(plan_path,'item-01',parent)
                validated['items']['item-01']['adoption_fingerprint']='fingerprint-current'
                plan['source']['sha256']='wrong-source'
                with self.assertRaisesRegex(ValueError,'源、商品'):
                    workflow.bind(plan_path,'item-01',parent)
                plan['source']['sha256']=file_hash(source)
                result=workflow.bind(plan_path,'item-01',parent)
                child=json.loads(Path(result['task_record']).read_text(encoding='utf-8'))
                self.assertEqual(child['task_context']['batch_ref'],{'plan_path':str(plan_path),'item_id':'item-01'})
                self.assertEqual(child['task_context']['batch_adoption_fingerprint'],'fingerprint-current')
                self.assertEqual(child['status'],'NOT_UPLOADED_NOT_SUBMITTED')
                self.assertEqual(json.loads(parent.read_text(encoding='utf-8'))['task_context'],context)
                self.assertTrue(workflow.bind(plan_path,'item-01',parent)['resumed'])
                child['task_context']['batch_adoption_fingerprint']='stale'
                self._save(Path(result['task_record']),child)
                with self.assertRaisesRegex(ValueError,'旧任务卡|冲突'):
                    workflow.bind(plan_path,'item-01',parent)

    def test_protected_parent_cannot_create_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'workspace';root.mkdir()
            source,context,parent,_=self._fixture(root)
            card=json.loads(parent.read_text(encoding='utf-8'))
            card.update(status='SUBMIT_UNKNOWN',submitted=None)
            self._save(parent,card)
            plan_path=root/'private-context/plan.json'
            plan={'source':{'path':str(source),'sha256':file_hash(source)},
                  'product':context['product'],'coverage':{'confirmed_ranges':[[0,10]]},
                  'items':[{'item_id':'item-01','task_id':'clip-01','adoption':{'fingerprint':'fp'}}]}
            self._save(plan_path,plan)
            validated={'plan':plan,'items':{'item-01':{'adoption_fingerprint':'fp'}}}
            with patch.object(workflow,'ROOT',root),patch.object(batch_plan,'ROOT',root), \
                 patch.object(batch_plan,'validate_plan',return_value=validated), \
                 patch.object(batch_plan,'_executable'):
                with self.assertRaisesRegex(ValueError,'受保护'):
                    workflow.bind(plan_path,'item-01',parent)
                self.assertFalse((root/'outputs/clip-01').exists())


if __name__=='__main__': unittest.main()
