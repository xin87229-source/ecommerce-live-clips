"""V0.2a deterministic support; no model calls and no video writes."""
import hashlib
import json
import math
import os
import re
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

from runtime import INSTALL_ROOT, ROOT
PRIVATE = ROOT / "private-context/selector-v02a"
PROMPT = INSTALL_ROOT / "pipeline/prompts/candidates.md"
WEIGHTS = {"hook": 15, "audience": 15, "product": 25, "experience": 15,
           "complete": 20, "clarity": 10}
FLAGS = {"product_uncertain", "price_needs_confirmation", "stock_needs_confirmation",
         "transcript_uncertain", "possible_product_switch", "possible_compliance_risk",
         "pure_welcome", "pure_size_reading", "too_much_repetition"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def key(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic(path, value, plain=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value if plain else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(payload + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def private_path(path):
    result = Path(path).resolve()
    if not result.is_relative_to(PRIVATE.resolve()):
        raise ValueError("本版产物只能写入本地private-context/selector-v02a")
    return result


def clock(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def validate_transcript(t):
    if t.get("status") != "complete" or t.get("schema_version") != 1:
        raise ValueError("转写缓存未完成或版本不匹配")
    duration = t["source"]["duration_seconds"]
    if not math.isfinite(duration) or duration <= 0 or not isinstance(t["segments"], list):
        raise ValueError("转写时长或segments错误")
    previous = 0
    for i, s in enumerate(t["segments"], 1):
        if (s["id"] != f"s{i:06d}" or not isinstance(s["text"], str)
                or not all(type(s[k]) in (int, float) and math.isfinite(s[k]) for k in ("start", "end"))
                or not 0 <= s["start"] <= s["end"] <= duration
                or s["start"] < previous):
            raise ValueError("转写ID或绝对时间无效")
        previous = s["start"]
    expected = t.get("segments_hash")
    if expected and expected != key(t["segments"]):
        raise ValueError("转写缓存内容哈希不匹配")


def chunks(t, core=480, overlap=45):
    validate_transcript(t)
    segments, duration = t["segments"], t["source"]["duration_seconds"]
    result, start = [], 0.0
    while start < duration:
        # End the core at the next segment start, never split a text string.
        end = next((s["start"] for s in segments if s["start"] >= start + core), duration)
        end = min(end, duration)
        selected = [s for s in segments if start <= s["start"] < min(duration, end + overlap)]
        item = {"id": f"block-{len(result)+1:03d}", "core_start": start, "core_end": end,
                "segments": selected}
        item["hash"] = key(item)
        result.append(item)
        start = end
    return result


def short_text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("分类或原因缺失/过长")
    return value.strip()


def visual_span(block, proposal):
    """A visual unit requires affirmative full-span listening evidence, not ASR absence."""
    start, end = proposal['start_seconds'], proposal['end_seconds']
    if (not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end))
            or not block['core_start'] <= start < end <= block['core_end']):
        raise ValueError('展示单元须完整位于已确认窗口内')
    visual, speech = proposal['visual_review'], proposal['speech_review']
    for evidence in (visual, speech):
        if not isinstance(evidence, dict) or not evidence.get('path'):
            raise ValueError('展示单元缺少画面或完整原声核验证据')
        if file_hash(Path(evidence['path']).resolve(strict=True)) != evidence.get('sha256'):
            raise ValueError('展示单元核验证据哈希不匹配')
    at = visual.get('at')
    if type(at) not in (int, float) or not math.isfinite(at) or not start <= at <= end:
        raise ValueError('展示核验画面不在本单元内')
    audio_range = speech.get('range')
    if (not isinstance(audio_range, list) or len(audio_range) != 2
            or not all(type(v) in (int, float) and math.isfinite(v) for v in audio_range)
            or not 0 <= audio_range[0] <= start < end <= audio_range[1]
            or speech.get('conclusion') != 'no_intelligible_speech'
            or not isinstance(speech.get('finding'), str) or not speech['finding'].strip()):
        raise ValueError('展示单元须有覆盖全段且无应保留口播的实际听审结论')
    return start, end


def materialize(block, proposals, single_product=False):
    batch = block.get('content_strategy', {}).get('version') == 'single-style-batch-v1'
    if not isinstance(proposals, list) or (not batch and len(proposals) > 3):
        raise ValueError("每块仅允许0至3条候选")
    positions = {s["id"]: i for i, s in enumerate(block["segments"])}
    result = []
    for p in proposals:
        visual = batch and p.get('kind') == 'visual'
        if visual:
            expected = {'kind', 'start_seconds', 'end_seconds', 'scores', 'category', 'reason',
                        'risk_flags', 'visual_review', 'speech_review'}
            if set(p) != expected:
                raise ValueError('展示单元字段不完整或存在未允许字段')
            start, end = visual_span(block, p)
            span = []
        else:
            expected = {"start_segment_id", "end_segment_id", "scores", "category", "reason", "risk_flags"}
            if batch and p.get('kind') == 'speech':
                expected.add('kind')
            if set(p) != expected:
                raise ValueError("口播只接受真实segment ID、评分、类型、原因与风险；禁止自由时间/改写原话")
            a, b = positions[p["start_segment_id"]], positions[p["end_segment_id"]]
            if a > b:
                raise ValueError("候选ID顺序错误")
            span = block["segments"][a:b+1]
            start, end = span[0]["start"], span[-1]["end"]
        if (not block["core_start"] <= start < block["core_end"]
                or not all(type(x) in (int, float) and math.isfinite(x) for x in (start, end))
                or (end <= start if batch else not 20 <= end-start <= 45)):
            raise ValueError("候选须起于本块核心且时间有效；旧策略另限20至45秒")
        if any(right["start"] - left["end"] > 5 for left, right in zip(span, span[1:])):
            raise ValueError("候选中存在超过5秒冷场，需重新选择")
        scores, flags = p["scores"], p["risk_flags"]
        if (not isinstance(scores, dict) or set(scores) != set(WEIGHTS)
                or any(type(scores[k]) is not int or not 0 <= scores[k] <= cap for k, cap in WEIGHTS.items())):
            raise ValueError("六维评分无效")
        if not isinstance(flags, list) or any(f not in FLAGS for f in flags):
            raise ValueError("风险标记无效")
        flags = sorted(set(flags) - {"product_uncertain", "possible_product_switch"} if single_product and not batch
                       else set(flags) | {"product_uncertain"})
        result.append({"id": ('visual-' + key(p)[:24]) if visual else f"clip-{span[0]['id']}-{span[-1]['id']}",
                        "start_segment_id": None if visual else span[0]["id"],
                        "end_segment_id": None if visual else span[-1]["id"],
                       "start": clock(start), "end": clock(end), "start_seconds": start,
                       "end_seconds": end, "duration": round(end-start, 3), "scores": scores,
                       "score": sum(scores.values()), "category": short_text(p["category"], 40),
                       "reason": short_text(p["reason"], 100), "text": "".join(s["text"] for s in span),
                       "risk_flags": flags, "review_status": "pending", "product_status": "user_confirmed_single_source" if single_product else "needs_confirmation"})
        if batch:
            result[-1].update(kind='visual' if visual else 'speech', product_status='needs_confirmation')
            if visual:
                result[-1].update(visual_review=p['visual_review'], speech_review=p['speech_review'])
    if len({c["id"] for c in result}) != len(result):
        raise ValueError("同块重复候选")
    return result


def dedupe(candidates, preserve_all=False):
    kept, removed = [], []
    # ponytail: quadratic comparisons for at most dozens; index only if volume changes.
    for c in sorted(candidates, key=lambda x: (-x["score"], x["start_seconds"])):
        duplicate = None
        for old in kept:
            overlap = max(0, min(c["end_seconds"], old["end_seconds"]) - max(c["start_seconds"], old["start_seconds"]))
            # Use exact source intervals; a positive short unit may round to 0ms in its label.
            shorter = min(c['end_seconds']-c['start_seconds'], old['end_seconds']-old['start_seconds'])
            if shorter <= 0:
                raise ValueError('候选源时长必须为正数')
            if overlap / shorter >= 0.7:
                if preserve_all:
                    c = dict(c, risk_flags=sorted(set(c['risk_flags']) | {'too_much_repetition'}))
                else:
                    duplicate = old["id"]
                    break
            # Similar text is only a flag while product identity is unconfirmed.
            clean = lambda s: re.sub(r"\W", "", s)
            if c['text'] and old['text'] and SequenceMatcher(None, clean(c["text"]), clean(old["text"])).ratio() >= 0.9:
                c = dict(c, risk_flags=sorted(set(c["risk_flags"]) | {"too_much_repetition"}))
        if duplicate:
            removed.append({"id": c["id"], "duplicate_of": duplicate})
        else:
            kept.append(c)
    return kept, removed


def ranked(candidates, order):
    ids = [c["id"] for c in candidates]
    if not isinstance(order, list) or len(order) != len(ids) or set(order) != set(ids):
        raise ValueError("排名必须且只能包含全部已有候选ID，各一次")
    by_id = {c["id"]: c for c in candidates}
    return [by_id[i] for i in order]


def review(data):
    single = bool(data.get('material'))
    batch = data.get('content_strategy', {}).get('version') == 'single-style-batch-v1'
    notice = ("完整素材池，待形成批次方案并由用户明确采用；此处候选数量不等于可产视频数量。" if batch else
              "单款已由用户确认；组合成片与复盘交付后由用户查看，不等待观看反馈。" if single else
              "候选质量、商品归属和实际听看效果待用户确认。")
    lines = ["# 直播候选片段", "", notice, "原话为ASR原文，未人工校正。", ""]
    for i, c in enumerate(data["candidates"], 1):
        lines += [f"## {i}｜{c['score']}分", "", f"时间：{c['start']} → {c['end']}",
                  f"时长：{c['duration']}秒", f"类型：{c['category']}", "", f"推荐理由：{c['reason']}",
                  "", "主播原话（转写，未校正）：", "", c["text"], "",
                  "风险：" + ("" if single else "商品待确认；") + "、".join(c["risk_flags"]), "", f"候选ID：{c['id']}", "", "---", ""]
    return "\n".join(lines)
