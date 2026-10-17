"""Local traceability in an existing snapshot JSON/workbook workflow; never fetches data."""
import argparse
import copy
import json
import math
import re
from datetime import timezone, timedelta
from pathlib import Path

from selector_support import ROOT, read, atomic, key, file_hash
from quality_support import timestamp, required, GOALS

WORK_FIELDS = ('task_id','work_id','account_key','product_name','product_id','product_code',
               'cart_required','video_sha256','title','duration','segment_count','speed','content_goal',
               'opening_type','content_decision_ref','cover_heading','cover_hashes','quality_version',
               'code_version','platform_status','published_at','published_evidence','run_metrics_ref')
SNAPSHOT_FIELDS = ('task_id','work_id','captured_at','source','statistical_scope','paid_traffic',
                   'observation_window','metrics','missing_notes')
OPENINGS = {'audience','detail','wearing_problem','scene','other','unclassified'}
UNITS = {'plays':'次','viewers':'人','avg_watch_seconds':'秒','completion_rate':'%',
         'attributed_follows':'人','product_clicks':'次','orders_created':'单',
         'orders_paid':'单','orders_settled':'单','amount_paid':'元','amount_settled':'元',
         'amount_after_refunds':'元'}


def identifier(value):
    if value is not None and (not isinstance(value,str) or not value.strip()
                              or re.fullmatch(r'[+-]?\d+(?:\.\d+)?[eE][+-]?\d+',value)):
        raise ValueError('ID必须是未失真的字符串，不能从数字或科学计数法恢复')


def validate_work(work):
    w = {**dict.fromkeys(WORK_FIELDS), **copy.deepcopy(work)}
    required(w['task_id'], 'task_id')
    for field in ('task_id','work_id','account_key','product_id','product_code'):
        identifier(w[field])
    if w['cart_required'] is not None and type(w['cart_required']) is not bool:
        raise ValueError('cart_required须布尔或未知')
    if w['content_goal'] not in GOALS | {None} or w['opening_type'] not in OPENINGS | {None}:
        raise ValueError('描述标签无效，不分配实验组')
    if w.get('published_at'):
        timestamp(w['published_at'])
        if w['platform_status'] != 'PUBLISHED_VERIFIED':
            raise ValueError('审核／定时状态不能有已核验实际公开时间')
        e = w.get('published_evidence') or {}
        if e.get('type') != 'platform_actual_publish_time' or not e.get('source'):
            raise ValueError('提交、计划或首次观察时间不能代替实际公开时间')
    return w


def metric(raw, cart_required):
    m = copy.deepcopy(raw)
    for field in ('raw_name','unit','source','period','scope'):
        required(m.get(field), field)
    if m.get('state') not in ('measured','missing','not_provided','not_applicable','unmapped'):
        raise ValueError('指标需明确实测／缺失／未提供／不适用／未映射')
    name = m.get('name')
    if name not in UNITS:
        m['state'] = 'unmapped'
        m['reason'] = m.get('reason') or '后台原字段保留；没有确认标准映射'
    elif UNITS[name] != m['unit']:
        m['state'] = 'unmapped'
        m['reason'] = '单位未映射，不静默转换'
    if cart_required is False and name in {'product_clicks','orders_created','orders_paid','orders_settled','amount_paid','amount_settled','amount_after_refunds'}:
        m.update(state='not_applicable',reason='本条明确不挂车；原始值保留，不评价为0')
    if m['state'] == 'measured':
        value = m.get('value')
        if type(value) not in (int,float) or not math.isfinite(value) or value < 0:
            raise ValueError('实测值须非负有限数字；0与未知分开')
        if 'raw_value' not in m:
            raise ValueError('必须保留平台原值')
    else:
        m['value'] = None
        required(m.get('reason'), '缺失／不适用／未映射原因')
    return m


def validate_snapshot(snapshot, work):
    s = copy.deepcopy(snapshot)
    if s.get('task_id') != work['task_id'] or s.get('work_id') not in (None,work['work_id']):
        raise ValueError('快照与作品身份不对应')
    identifier(s.get('work_id'))
    captured = timestamp(s.get('captured_at'))
    for field in ('source','statistical_scope'):
        required(s.get(field),field)
    if s.get('paid_traffic') not in ('yes','no','unknown'):
        raise ValueError('投流情况必须有／无／未知')
    metrics = [metric(m,work['cart_required']) for m in s.get('metrics',[])]
    keys = [(m.get('name'),m['raw_name'],m['source'],m['period'],m['scope']) for m in metrics]
    if len(keys) != len(set(keys)):
        raise ValueError('同一快照含重复指标，不能覆盖或相加')
    s['metrics'] = metrics
    s['hours_since_public'] = None
    s['within_target_window'] = None
    s['captured_at_shanghai'] = captured.astimezone(timezone(timedelta(hours=8))).isoformat()
    if work['published_at'] and work['platform_status'] == 'PUBLISHED_VERIFIED':
        hours = (captured-timestamp(work['published_at'])).total_seconds()/3600
        if hours < 0: raise ValueError('采集早于实际公开')
        s['hours_since_public'] = hours
        window = s.get('observation_window')
        if window:
            target,tolerance = window.get('target_hours'),window.get('tolerance_hours')
            if target not in (24,72) or type(tolerance) not in (int,float) or not math.isfinite(tolerance) or tolerance < 0:
                raise ValueError('内部观察点只用24/72小时；容差须明确非负')
            s['within_target_window'] = abs(hours-target) <= tolerance
    s['follows_per_1000_plays'] = None
    s['custom_metric_note'] = '内部自定义指标；缺作品归因关注或播放次数'
    plays = [m for m in metrics if m.get('name')=='plays' and m['state']=='measured' and m['scope']=='work']
    follows = [m for m in metrics if m.get('name')=='attributed_follows' and m['state']=='measured' and m['scope']=='work']
    if len(plays)==len(follows)==1:
        p,f = plays[0],follows[0]
        if p['source'] != f['source'] or p['period'] != f['period']:
            s['custom_metric_note'] = '统计来源／窗口不兼容，未计算'
        elif p['value']==0:
            s['custom_metric_note'] = '播放次数为0，未计算'
        else:
            s['follows_per_1000_plays'] = f['value']/p['value']*1000
            s['custom_metric_note'] = '内部自定义指标：作品归因关注÷播放次数×1000；无行业合格线'
    s['snapshot_key'] = key([work['task_id'],captured.astimezone(timezone.utc).isoformat(),s['source'],s['statistical_scope']])
    return s


def merge(store, incoming):
    """Preserve the existing source-data structure; one optional traceability extension."""
    result = copy.deepcopy(store)
    trace = result.setdefault('traceability',{'version':1,'works':[],'snapshots':[]})
    works = {w['task_id']:validate_work(w) for w in trace['works']}
    for row in incoming.get('works',[]):
        w = validate_work(row)
        if w['task_id'] in works and works[w['task_id']] != w:
            raise ValueError('同task_id档案冲突；需显式人工核对，导入不覆盖历史')
        if w['work_id'] and any(x['work_id']==w['work_id'] and x['task_id']!=w['task_id'] for x in works.values()):
            raise ValueError('同作品ID不能建立第二条逻辑作品')
        works[w['task_id']] = w
    snapshots = {s['snapshot_key']:s for s in trace['snapshots']}
    added = duplicates = 0
    for raw in incoming.get('snapshots',[]):
        if raw.get('task_id') not in works: raise ValueError('快照缺对应作品档案')
        s = validate_snapshot(raw,works[raw['task_id']])
        old = snapshots.get(s['snapshot_key'])
        if old is not None:
            if old != s: raise ValueError('同采集身份数据冲突；保留原快照，不能静默覆盖')
            duplicates += 1
        else:
            snapshots[s['snapshot_key']] = s
            added += 1
    trace.update(works=list(works.values()),snapshots=list(snapshots.values()))
    return result, {'added_snapshots':added,'duplicate_snapshots':duplicates,'works':len(works)}


def local_output(path):
    p = Path(path).resolve()
    if not any(p.is_relative_to((ROOT/d).resolve()) for d in ('outputs','private-context','data')):
        raise ValueError('真实档案／快照仅存被忽略本地目录')
    return p


def archive(package, publish_result=None):
    package = local_output(package)
    p = read(package/'publish.json')
    r = read(publish_result) if publish_result else {}
    context = p.get('task_context',{})
    actual_sha = file_hash(package/'video.mp4')
    if p.get('video',{}).get('sha256') not in (None,actual_sha):
        raise ValueError('发布包视频已改变，不能沿用旧生产版本')
    if r and (r.get('video_sha256') != actual_sha or
              (r.get('account_key') and context.get('account_key') and r['account_key'] != context['account_key'])):
        raise ValueError('发布记录与当前包视频／账号不对应，不能关联作品数据')
    material = context.get('product',{})
    work = dict.fromkeys(WORK_FIELDS)
    work.update(task_id=context.get('task_id') or package.parent.name,work_id=r.get('work_id'),
                account_key=r.get('account_key') or context.get('account_key'),
                product_name=p.get('product',{}).get('name'),product_id=r.get('product_id') or material.get('id'),
                product_code=material.get('code'),cart_required=r.get('cart_required',context.get('cart_required')),
                video_sha256=actual_sha,title=p.get('title'),duration=p.get('video',{}).get('duration'),
                content_goal=context.get('content_goal','undetermined'),opening_type='unclassified',
                content_decision_ref=p.get('content_decision_ref'),cover_heading=((p.get('cover') or {}).get('text') or [None])[0],
                platform_status=r.get('status','UNKNOWN'),published_at=r.get('published_at'),
                published_evidence=r.get('published_evidence'))
    if (package/'logs/cover-layout.json').exists():
        work['cover_hashes'] = read(package/'logs/cover-layout.json').get('cover_hashes')
    for field in ('segment_count','speed','quality_version','code_version','run_metrics_ref'):
        work[field] = p.get('production',{}).get(field)
    return validate_work(work)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['template','archive','import'])
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--input',type=Path)
    p.add_argument('--store',type=Path)
    p.add_argument('--package',type=Path)
    p.add_argument('--publish-result',type=Path)
    a = p.parse_args()
    out = local_output(a.output)
    if out.exists(): raise FileExistsError('使用新输出文件，保留原数据与工作副本')
    if a.action == 'template':
        data = {'works':[], 'snapshots':[], 'fields':{'work':list(WORK_FIELDS),'snapshot':list(SNAPSHOT_FIELDS)},
                'note':'空记录；字段规范见docs/quality-workflow.md。不自动回填旧作品。'}
    elif a.action == 'archive':
        if not a.package: p.error('archive需要--package')
        data = {'works':[archive(a.package,a.publish_result)],'snapshots':[]}
    else:
        if not a.input: p.error('import需要--input')
        data,report = merge(read(a.store) if a.store else {},read(a.input))
        print(json.dumps(report,ensure_ascii=False))
    atomic(out,data)
    print(out)
