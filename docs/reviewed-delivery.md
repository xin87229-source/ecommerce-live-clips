# 从已审稿完成本地交付

适用：已经用通用 `rehearsal` 制作完整稿、用 `review-card` 对比，并收到真实完整内容确认。既有批次 `combine → subtitle → package` 流程继续保留；本入口复用同一原片剪辑与字幕能力，避免为已确认内容重复转写和选段。

运营不用写 JSON。直接说明实际看听范围、同款和字幕结论、是否接受；Codex 保存原文并整理成下面的交付输入。偏好某一版、播放成功或允许继续优化，都不能填成内容确认。

## 操作者执行

1. 将用户原话存进当前工作区 `private-context`，运行 `review-card check` 与 `review-card save-feedback`，绑定被审视频完整范围。只审核一个问题或局部片段时继续待审。
2. 根据真实回答分别记录 `same_product`、`transcript`、`continuous_visual`、`audio_listening`、`user_confirmation`；每项均已明确才可以交付。记录实际审核人、带时区时间、原文引用及用户接受的消息文件与哈希。程序验证证据绑定，不能替运营判断一句话的含义。
3. 查看已确认字幕和真实封面帧，写标题、正文、话题与封面文字。用字幕编号和原话引用说明依据，不加入未核实的材质、价格、库存、销量或效果承诺。
4. 运行公开入口。`$Python`、`$Install`、`$Workspace` 沿用安装文档中的实际绝对路径：

若演练由 `auto` 制作，优先按[统一入口](one-click.md)在原请求追加 `delivery_spec`，自动复用已完成阶段并返回正式交付。下列独立入口保留给既有演练任务；不要为接入 auto 伪造旧阶段记录。

```powershell
& $Python "$Install/cli.py" --workspace "$Workspace" delivery build --spec "$Workspace/private-context/delivery-spec.json" --task-id accepted-01
& $Python "$Install/cli.py" --workspace "$Workspace" delivery check --package "$Workspace/outputs/deliveries/accepted-01"
```

输入对象形状（由 Codex 填入真实值，不把示例当审批）：

```json
{
  "schema_version": 1,
  "rehearsal": "当前工作区完整演练包绝对路径",
  "feedback": "通用save-feedback生成的整片反馈JSON绝对路径",
  "approval": {
    "actor": "实际审核人",
    "at": "实际带时区时间",
    "confirmed_items": ["same_product", "transcript", "continuous_visual", "audio_listening", "user_confirmation"],
    "confirmation_quote": "真实反馈文件中的完整内容确认原话",
    "acceptance": {"path": "用户接受原文文件绝对路径", "sha256": "实际哈希", "quote": "实际接受原话"}
  },
  "copy": {
    "title": "有原话或商品证据的标题",
    "caption": "有证据的正文",
    "hashtags": ["#女装", "#穿搭", "#日常搭配"],
    "cover_lines": ["商品名称", "有证据的搭配主题"],
    "evidence": [{"cue": 1, "quote": "已确认字幕中的实际原话"}],
    "reviewer": "实际文案审核者",
    "reviewed_at": "实际带时区时间",
    "finding": "对照字幕和实物画面得到的具体判断"
  }
}
```

## 实际变化与验证边界

程序从原片窗口重新生成剪辑，不把旧成片当原素材。重建的无字幕剪辑须与已审稿对应剪辑逐字节一致；最终音频须与已审稿逐比特一致。SRT 不改动，ASS 只删除生成器的唯一待审水印行。缺少水印、重复水印或输入变化会拒绝执行。

新的双比例封面来自同一原片帧，去除待审标签；文案引用已确认字幕。`video.mp4`、`subtitle.srt/ass`、双封面、文案、来源、文件清单、分项质量及本地发帖预览放入新的 `outputs/deliveries/<任务号>`。失败现场保留，旧输出不覆盖。修复后用新的任务号重试；预检失败、尚未创建目录时可以使用原任务号。

内容确认只覆盖用户实际审核的那版及其保留片段，不扩大到完整长窗口、整场直播或其他商品。新视频去掉水印后文件哈希会改变：相同原声、文字与时间可以引用原确认，新画面和封面须另行实际检查。程序不会伪称用户看过新文件。发布状态始终为本地未提交；发帖预览不是平台后台验证。

`delivery check` 检查当前输入、来源及输出字节。更改任何依赖或交付文件后拒绝沿用旧检查；保留原证据并明确哪些内容发生变化。不能通过改写 manifest 重新给修改后的文件背书。
