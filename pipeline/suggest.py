"""V0.2a: prepare -> one block at a time -> submit -> merge -> rank -> finish.

Codex supplies selection/ranking JSON locally; no external LLM API or V0.1 tasks.
"""
import argparse
import json
import sys
import time
import traceback
from pathlib import Path

from media_adapter import MediaAdapter
from quality_support import context_from, normalize_content_strategy, task_gate, validate_task
from selector_support import (ROOT, PRIVATE, PROMPT, atomic, chunks, dedupe, file_hash,
                              key, materialize, private_path, ranked, read, review)
from runtime import stage_config

EXTENSIONS = {".mp4", ".mov", ".mkv", ".flv", ".ts", ".webm", ".m4v", ".avi"}


def find_video(folder):
    folder = Path(folder).resolve(strict=True)
    videos = sorted(p.resolve() for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONS)
    if len(videos) != 1:
        raise ValueError(f"须有且仅有一条视频，找到{len(videos)}条：" + ", ".join(p.name for p in videos))
    if videos[0].is_relative_to(PRIVATE.resolve()) or PRIVATE.resolve().is_relative_to(folder):
        raise ValueError("原素材与产物目录必须隔离")
    return videos[0]


def prepare(folder, config, material=None, task_record=None):
    started = time.perf_counter()
    source = Path(material["source_video"]) if material else find_video(folder)
    context = task_gate(task_record, source)
    if material and material.get('product_name') and material['product_name'] != context['product']['name']:
        raise ValueError('入口目标商品与当前任务卡不符，禁止长转写')
    PRIVATE.mkdir(parents=True, exist_ok=True)
    media = MediaAdapter(stage_config(config), PRIVATE / "probe.log")
    meta = media.probe(source)
    validate_task(context, source, meta['duration'])
    from transcribe_adapter import transcribe
    t, transcript_path, metrics = transcribe(source, meta)
    identity = [t["source"]["sha256"], t["asr"]["config_hash"], file_hash(PROMPT), "chunks-480-45-v1",
                context['content_strategy']]
    if material:
        identity.append(material)
    job = PRIVATE / "jobs" / key(identity)
    job.mkdir(parents=True, exist_ok=True)
    blocks = chunks(t)
    for b in blocks:
        b['content_strategy'] = context['content_strategy']
        b['hash'] = key({k: v for k, v in b.items() if k != 'hash'})
    manifest = {"version": "0.2a", "source_video": str(source), "transcript_cache": str(transcript_path),
                "transcript_hash": file_hash(transcript_path), "prompt_hash": file_hash(PROMPT),
                "blocks": [{k: v for k, v in b.items() if k != "segments"} for b in blocks]}
    manifest.update(task_record=context['task_record'], task_context=context)
    if material:
        manifest.update(version="0.2b", material=material)
    for b in blocks:
        target = job / "chunks" / (b["id"] + ".json")
        if not target.exists() or read(target) != b:
            atomic(target, b)
    atomic(job / "manifest.json", manifest)
    runs = job / "runs"
    runs.mkdir(exist_ok=True)
    metrics.update(prepare_seconds=time.perf_counter()-started, segments=len(t["segments"]),
                   duration=meta["duration"], blocks=len(blocks), job=str(job))
    atomic(runs / f"{time.time_ns()}.json", metrics)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


def load_job(job, task_record=None):
    job = private_path(job)
    manifest = read(job / "manifest.json")
    current = normalize_content_strategy(context_from(task_record, manifest).get('content_strategy'))
    context = manifest.get('task_context') or {}
    manifest['content_strategy'] = normalize_content_strategy(context.get('content_strategy'))
    if context:
        context['content_strategy'] = manifest['content_strategy']
    if current != manifest['content_strategy']:
        raise ValueError('内容策略等级或来源已改变，请重新prepare；完整转写缓存仍可复用')
    if manifest["prompt_hash"] != file_hash(PROMPT):
        raise ValueError("Prompt已变更，请重新prepare；转写缓存仍可复用")
    if manifest["transcript_hash"] != file_hash(private_path(manifest["transcript_cache"])):
        raise ValueError("转写已改变，请重新prepare")
    return job, manifest


def load_block(job, manifest, name):
    entry = next((b for b in manifest["blocks"] if b["id"] == name), None)
    if entry is None:
        raise ValueError("未知块ID")
    b = read(job / "chunks" / (name + ".json"))
    if b["hash"] != entry["hash"] or key({k:v for k,v in b.items() if k != "hash"}) != b["hash"]:
        raise ValueError("分块缓存已改变")
    return b


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["prepare", "block", "submit", "merge", "finish"])
    p.add_argument("--source-dir", type=Path)
    p.add_argument("--task-record", type=Path, help="本地run-state.json，内含已核验task_context")
    p.add_argument("--date")
    p.add_argument("--product")
    p.add_argument("--material-config", type=Path, default=ROOT / "private-context/material-root.json")
    p.add_argument("--config", type=Path, default=ROOT / "config.json")
    p.add_argument("--job", type=Path)
    p.add_argument("--block")
    p.add_argument("--selection", type=Path)
    p.add_argument("--ranking", type=Path)
    p.add_argument("--top", type=int, help="仅旧策略可限Top数量；新策略始终保留全池")
    args = p.parse_args()
    if args.action == "prepare":
        if args.date or args.product:
            from material_locator import locate
            source = locate(read(args.material_config)["source_root"], args.date or "", args.product or "")
            prepare(None, args.config, {"date": args.date, "product_name": args.product, "source_video": str(source)}, args.task_record)
            return
        if args.source_dir is None:
            p.error("prepare需要--source-dir")
        prepare(args.source_dir, args.config, task_record=args.task_record)
        return
    if args.job is None:
        p.error("需要--job")
    job, manifest = load_job(args.job, args.task_record)
    if args.action in ("block", "submit"):
        if not args.block or (args.action == "submit" and args.selection is None):
            p.error("需要--block；submit还需要--selection")
        b = load_block(job, manifest, args.block)
        if args.action == "block":
            if not (job / "analysis-start.json").exists():
                atomic(job / "analysis-start.json", {"started_unix": time.time()})
            # Deliberately show only ONE block, not the complete transcript.
            print('content_strategy=' + json.dumps(manifest['content_strategy'], ensure_ascii=False))
            print(f"{b['id']} core=[{b['core_start']:.3f},{b['core_end']:.3f}) hash={b['hash']}")
            for s in b["segments"]:
                print(f"{s['id']} [{s['start']:.3f},{s['end']:.3f}] {s['text']}")
        else:
            selection = read(private_path(args.selection))
            if selection.get("block_hash") != b["hash"]:
                raise ValueError("选择与分块不匹配")
            candidates = materialize(b, selection["candidates"], bool(manifest.get("material")))
            atomic(job / "selections" / (b["id"] + ".json"), selection)
            print(f"已校验保存{len(candidates)}条候选")
    elif args.action == "merge":
        all_candidates = []
        for entry in manifest["blocks"]:
            b = load_block(job, manifest, entry["id"])
            selection = read(job / "selections" / (b["id"] + ".json"))
            if selection["block_hash"] != b["hash"]:
                raise ValueError("候选缓存失效")
            all_candidates.extend(materialize(b, selection["candidates"], bool(manifest.get("material"))))
        kept, removed = dedupe(all_candidates, preserve_all=(
            manifest['content_strategy'].get('version') == 'single-style-batch-v1'))
        data = {"first_pass_count": len(all_candidates), "deduped_count": len(kept),
                "removed": removed, "candidates": kept,
                "content_strategy": manifest['content_strategy']}
        data["hash"] = key(data)
        atomic(job / "ranking-input.json", data)
        print(f"首轮{len(all_candidates)}条，去重后{len(kept)}条；只读取ranking-input.json进行第二轮")
    else:
        if args.ranking is None:
            p.error("finish需要--ranking")
        batch = manifest['content_strategy'].get('version') == 'single-style-batch-v1'
        if batch and args.top is not None:
            raise ValueError('新策略保留全池，不接受--top裁剪；可产数量由批次方案决定')
        top = 5 if args.top is None else args.top
        if not batch and not 1 <= top <= 5:
            raise ValueError("Top数量限1至5")
        data = read(job / "ranking-input.json")
        if data["hash"] != key({k:v for k,v in data.items() if k != "hash"}):
            raise ValueError("排名输入被改动")
        if normalize_content_strategy(data.get('content_strategy')) != manifest['content_strategy']:
            raise ValueError('排名输入内容策略不匹配，请重新merge并排名')
        r = read(private_path(args.ranking))
        if r["input_hash"] != data["hash"]:
            raise ValueError("排名缓存失效")
        ordered = ranked(data["candidates"], r["order"])
        atomic(job / "ranking.json", r)
        selected = ordered if batch else ordered[:top]
        output = {"source_video": manifest["source_video"], "transcript_cache": manifest["transcript_cache"],
                  "content_strategy": manifest['content_strategy'],
                  "status": "pending_batch_adoption" if batch else "pending_human_review", "candidates": selected,
                  "counts": {"first_pass": data["first_pass_count"], "deduped": len(ordered),
                             "final": len(selected)}, "token_usage": None}
        if manifest.get("material"):
            output.update(material=manifest["material"])
            if not batch:
                output['status'] = "ready_for_combination"
        if (job / "analysis-start.json").exists():
            timing_path = job / "analysis-timing.json"
            if not timing_path.exists():
                atomic(timing_path, {"analysis_wall_seconds": time.time()-read(job / "analysis-start.json")["started_unix"],
                       "note": "逐块读取至首次finish的墙钟时间，包含人工/工具等待，不是模型纯推理耗时"})
            output["timing"] = read(timing_path)
        atomic(job / "candidate-clips.json", output)
        atomic(job / "candidate-review.md", review(output), plain=True)
        print(f"已输出{len(output['candidates'])}条待人工审核候选：{job / 'candidate-review.md'}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        PRIVATE.mkdir(parents=True, exist_ok=True)
        atomic(PRIVATE / f"failure-{time.time_ns()}.txt", traceback.format_exc(), plain=True)
        print(f"V0.2a停止：{error}")
        sys.exit(1)
