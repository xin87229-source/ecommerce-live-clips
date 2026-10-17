"""Small operator steps around the existing candidate and batch validators."""
import argparse
import copy
import json
from pathlib import Path

import batch_plan
import quality_support as quality
import run_records
from run import component, write_json
from runtime import ROOT
from selector_support import file_hash, read


def _parent(task_record):
    path = Path(task_record).resolve(strict=True)
    if path.name != 'run-state.json' or not path.is_relative_to((ROOT/'outputs').resolve()):
        raise ValueError('父任务卡须位于当前工作目录outputs')
    card = read(path)
    if card.get('status') != 'NOT_UPLOADED_NOT_SUBMITTED' or card.get('submitted') is not False:
        raise ValueError('已提交、未知或受保护的任务卡不能作为新批次父任务')
    context = card.get('task_context') or {}
    if context.get('batch_ref') or context.get('batch_adoption_fingerprint'):
        raise ValueError('已有采用的任务卡不能冒充批次父任务')
    checked = quality.task_gate(path, context['source_video'])
    return path, card, checked


def draft(job_path, task_record, batch_id, output):
    """Inventory verified candidates without inventing a plan or review findings."""
    job = batch_plan.local_path(job_path)
    parent, _, context = _parent(task_record)
    component(batch_id, 'batch_id')
    target = batch_plan.local_path(output, exists=False)
    if target.exists():
        raise FileExistsError('批次草稿已存在；先编辑现有草稿，不覆盖')
    manifest = read(job/'manifest.json')
    source = Path(context['source_video']).resolve(strict=True)
    if (Path(manifest['source_video']).resolve(strict=True) != source
            or file_hash(source) != context['source_sha256']):
        raise ValueError('候选job与父任务原片身份不匹配')
    confirmed = batch_plan.union_ranges(w['range'] for w in context['confirmed_windows'])
    transcript = read(manifest['transcript_cache'])
    duration = transcript['source']['duration_seconds']
    covered = (transcript.get('covered_ranges') if transcript.get('coverage_scope') == 'reviewed_windows_only'
               else [[0,duration]])
    searched = batch_plan.union_ranges([max(a,c),min(b,d)] for a,b in confirmed for c,d in covered
                                        if max(a,c) < min(b,d))
    coverage_scope = ('reviewed_windows_only' if context.get('source_window')
                      or transcript.get('coverage_scope') == 'reviewed_windows_only'
                      or searched != [[0,duration]] else 'full_source')
    plan = {'version':batch_plan.VERSION,'batch_id':batch_id,'job_path':str(job),
            'source':{'path':str(source),'sha256':context['source_sha256'],
                      'duration_seconds':duration},
            'product':copy.deepcopy(context['product']),
            'source_window':copy.deepcopy(context.get('source_window')),
            'coverage':{'source_sha256':context['source_sha256'], 'scope':coverage_scope,
                        'evidence':copy.deepcopy(context['source_review'].get('review_input')),
                        'searched_ranges':searched,'confirmed_ranges':searched,'pending_ranges':[]},
            'items':[],'exclusions':{},'reuse':[],'pair_reviews':[],'why_not_more':''}
    candidates, _ = batch_plan._candidates(plan)
    plan['exclusions'] = {cid:{'usable':None,'reason':''} for cid in candidates}
    plan['candidate_inventory'] = [{k:c.get(k) for k in ('id','kind','start_seconds','end_seconds',
                                    'category','reason','text')} for c in candidates.values()]
    plan['draft_help'] = {
        'status':'未完成，禁止直接采用；当前检索范围只取已确认同款窗口与已转写范围的交集，未证明整场盘点',
        'item_fields':{'item_id':'稳定条目ID','task_id':'新子任务ID','order':['真实候选ID，按实际顺序'],
                       'speed':1.0,'speed_reason':'按原声试听填写','mainline':'真实主线判断',
                       'completeness':'完整表达依据','roles':{'候选ID':'main或auxiliary'}},
        'finish_fields':'production须单独补全场人工盘点full_review证据，searched_ranges覆盖原片，已确认同款窗口均须转写；不要求整场ASR。test可保留reviewed_windows_only但只能称局部结果。补未用候选usable/reason、why_not_more；多条时逐对填写pair_reviews和真实比较依据。先batch validate再adopt。'}
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(target,plan)
    return {'plan_draft':str(target),'candidate_count':len(candidates),
            'candidate_ids':list(candidates),'task_record':str(parent),
            'status':'draft_requires_semantic_and_coverage_review'}


def bind(plan_path, item_id, parent_record):
    """After an existing adoption, bind one exact plan item to a new local card."""
    plan_path = batch_plan.local_path(plan_path)
    result = batch_plan.validate_plan(plan_path)
    parent, _, context = _parent(parent_record)
    batch_plan._executable(result, context)
    plan = result['plan']
    item = next((value for value in plan['items'] if value['item_id']==item_id),None)
    if item is None:
        raise ValueError('批次条目ID不存在')
    expected = result['items'][item_id]['adoption_fingerprint']
    if item.get('adoption',{}).get('fingerprint') != expected:
        raise ValueError('当前条目版本尚未采用；不能给子任务写旧指纹')
    if (context['task_id'] == item['task_id'] or context['source_sha256'] != plan['source']['sha256']
            or Path(context['source_video']).resolve() != Path(plan['source']['path']).resolve()
            or context['product'] != plan['product']
            or batch_plan.union_ranges(w['range'] for w in context['confirmed_windows'])
               != batch_plan.union_ranges(plan['coverage']['confirmed_ranges'])):
        raise ValueError('父任务的源、商品或同款窗口与已采用批次不符')
    child = copy.deepcopy(context)
    child['task_id'] = item['task_id']
    child['batch_ref'] = {'plan_path':str(plan_path),'item_id':item_id}
    child['batch_adoption_fingerprint'] = expected
    child['batch_parent'] = {'task_record':str(parent),'task_id':context['task_id']}
    gate = batch_plan.execution_gate(child, stage='render')
    if gate['adoption_fingerprint'] != expected:
        raise ValueError('采用指纹与当前条目实际执行门禁不一致')
    child_path = ROOT/'outputs'/component(item['task_id'],'task_id')/'run-state.json'
    if child_path.parent.exists():
        if child_path.is_file():
            old = read(child_path)
            if (old.get('task_context') == child and old.get('submitted') is False
                    and old.get('status') == 'NOT_UPLOADED_NOT_SUBMITTED'):
                return {'task_record':str(child_path),'batch_ref':child['batch_ref'],
                        'adoption_fingerprint':expected,'resumed':True}
        raise ValueError('旧任务卡或目录冲突；不覆盖已有任务状态')
    run_records.initialize(child_path,task_context=child)
    return {'task_record':str(child_path),'batch_ref':child['batch_ref'],
            'adoption_fingerprint':expected,'resumed':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    make=sub.add_parser('draft',help='从真实候选生成未完成、可编辑的批次草稿')
    make.add_argument('--job',required=True,type=Path)
    make.add_argument('--task-record',required=True,type=Path)
    make.add_argument('--batch-id',required=True)
    make.add_argument('--output',required=True,type=Path)
    use=sub.add_parser('bind',help='在batch adopt后为一条已采用方案创建独立子任务卡')
    use.add_argument('--plan',required=True,type=Path)
    use.add_argument('--item-id',required=True)
    use.add_argument('--task-record',required=True,type=Path,help='原片父任务卡')
    args=parser.parse_args()
    value=(draft(args.job,args.task_record,args.batch_id,args.output) if args.action=='draft'
           else bind(args.plan,args.item_id,args.task_record))
    print(json.dumps(value,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
