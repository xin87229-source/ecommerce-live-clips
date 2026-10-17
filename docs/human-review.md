# 本地审片与反馈

听审卡把已有粗剪版本、原片窗口、未审核 ASR 和具体问题放在同一页。运营直接看视频、点击回听并用自然语言反馈；由 AI 按真实文件生成下面的输入和保存记录，运营无需手写 JSON。它不会确认同款、采用片段、自动解释反馈或改变任务状态。

先完成[安装与工作目录配置](quickstart.md#1-选工作目录并检查环境)。以下命令在 Windows PowerShell 中运行，沿用 `$Install`（完整源码）、`$Python`（本次独立虚拟环境的解释器）和 `$Workspace`（独立工作目录）；重新打开终端时先恢复这三个真实绝对路径。JSON 中的 `W` 同样由 Codex 替换为实际工作目录，不能照抄为相对路径。

## 先让剪辑判断可见

当使用者要评价选段、顺序或节奏时，先交付带标注的真实粗剪。仅发送一段原片并附核词问题，不足以让使用者评价剪辑效果。原片听审卡仍用于回听上下文；它不是成片预览。

- 对比同一组选段的两种顺序时，两版保持相同片段、语速、声音处理和标注样式，明确唯一变化是顺序。不要把这说成两套完全不同的内容方案。
- 粗剪须真正导出为可播放的视频；标注每段作用、原片位置、拟剪切位置与待核词，并保留原声和完整商品画面。时刻若来自剪辑计划，应注明实际导出有帧取整。
- 先请使用者判断哪版更想看完、哪里突兀，再针对保留的句子核词。删除的句子通常无需逐字听审；删除时损失的有用商品画面也应说明。
- 使用本次原片抽取和可追溯范围，不借用以前剪好的成片。未经听审的识别文字保持原文并显著标注“机器字幕，尚未核对”。
- 比较粗剪与正式成片分开保存。生成、播放、偏好某一版或技术检查通过，都不等于同款确认、转写准确或内容审核通过。

## 从原片生成独立待审粗剪

运营可以直接说“先保留完整口播，给我两个不同顺序的预览，把听不清的位置列出来”。Codex 根据真实原片和本次 `window extract` 记录生成输入 JSON；运营不需要编辑路径、哈希或时间数组。`review-cut` 在同款与核词尚未确认时提供可播放的选段预览，原任务须仍为 `NOT_UPLOADED_NOT_SUBMITTED`、`submitted=false`，且没有批次采用绑定。

```powershell
& $Python "$Install/cli.py" --workspace "$Workspace" review-cut --spec "$Workspace/private-context/cut-spec.json" --task-id cut-v1
```

`W` 须替换为独立工作目录的绝对路径。最小输入如下，区间和速度只示意结构，必须由 Codex 按本次窗口实际内容填写：

```json
{
  "schema_version": 1,
  "task_record": "W/outputs/task-one/run-state.json",
  "clips": [
    {
      "extraction": "W/private-context/source-windows/task-one/window-xxx/extraction.json",
      "window_range": [2, 6],
      "speed": 1.0,
      "purpose": "保留完整开场表达，供判断选段与节奏",
      "questions": ["句首和尾字是否完整？"]
    }
  ]
}
```

每段 `speed` 可在 1.0～1.5 之间按原声、动作和表达选择，不固定整条速度。输出为 `W/outputs/review-cuts/cut-v1/` 下的 `preview.mp4`、`preview.json`、`technical.json`、日志与三张抽帧；画布为 1080×1920、30 fps，含可选定格的总长不超过 70 秒。每段按帧取整，不足一帧的末帧/静音补齐及实际成片范围都记录在 `preview.json`。修订用新的 `task-id`，保留前版；已有输出或失败目录不覆盖。

需要同源非同期画面时，可给该段增加 `visual={"extraction":"实际抽取记录路径","window_range":[起点,终点]}`。它只替换画面，口播仍来自该段原来的 `extraction/window_range`；画面长度须足以按该段速度覆盖原声。Codex 须分别标明原声和画面的原片区间，并在作用说明中写清“同源非同期，口型待审”，不能说嘴部已经同步。可选静态 `crop` 和尾部 `end_hold_seconds` 的结构、限制见[制作链操作参考](pipeline-reference.md)。

这个入口不烧录字幕；未获准的字幕不应为了展示效果先变成正式画内文字。疑词保持在问题列表或明确未核验的 ASR 材料中。`preview.json` 固定为 `unreviewed_review_cut`，同款、原话、连续画面、试听和用户确认均为 `not_reviewed`；`technical.json` 的通过只证明媒体技术检查。生成过程中不改原任务卡，不创建采用或正式字幕结果，预览不能直接冒充正式 `package`。

## 生成原速听审卡

以下命令把已有粗剪与原速材料放入听审卡；可选的旧版成片只逐字节复制作为比较，不能成为新剪辑源。主窗口仍须有本次 `window transcribe-preview` 生成的未审核 ASR；`review-cut` 本身不运行转写。

```powershell
& $Python "$Install/cli.py" --workspace "$Workspace" review-card --spec "$Workspace/private-context/review-spec.json" --output "$Workspace/outputs/reviews/card-01/index.html"
```

`W` 须替换为独立工作目录的绝对路径。生成器仅使用 Python 标准库，不运行模型、不联网、不再次剪辑。输出目录必须尚未存在；已有目录一律拒绝覆盖。

输入示例（所有路径均须换成真实绝对路径）：

```json
{
  "extraction": "W/private-context/source-windows/task-one/window-xxx/extraction.json",
  "asr_preview": "W/private-context/source-windows/task-one/window-xxx/asr-preview-xxx.json",
  "title": "第 1 条原片听审",
  "poster": "W/private-context/source-windows/task-one/window-xxx/frame-2.png",
  "proposed_segments": [
    {"label": "拟开头", "window_range": [15, 25]},
    {"label": "拟接续", "window_range": [1, 10]}
  ],
  "questions": [
    {"id": "Q1", "window_range": [16, 21], "question": "请写出这句的完整原话，听不清可直接注明。"}
  ]
}
```

- 所有 `window_range` 均为所引用抽取窗口从 0 开始的秒数，不是完整直播时间。拟片段按数组顺序试听，问题 ID 须唯一。
- `poster` 可省略；填写时必须引用抽取记录中的真实原帧，且文件哈希、窗口时间和原片时间一致。
- spec、抽取记录、ASR 预览、转写缓存和可选海报必须在当前工作目录内。完整直播原片仍保留在工作目录外，遵循现有原片抽取规则。
- 校验原片与窗口实际 SHA-256、抽取范围映射、预览对抽取/缓存的绑定、逐条 ASR 原文和原片时间。完整原片仅作必要哈希校验，不复制进输出。
- 输出包括 `index.html`、`review-card.json`、`review.mp4` 和可选 `poster.*`。附加窗口和版本分别输出为 `source-2.mp4` 等、`version-1.mp4` 等。媒体全部来自已有文件的逐字节复制；复制前后校验哈希，完整直播不复制进卡片。生成失败可能留下未完成的新目录，重试须使用新的目录。

### 在浏览器中打开审核页

先核对这张已生成卡片的绑定，再用项目附带的 `serve_review.py` 启动本机只读服务。它支持视频按字节分段读取，供浏览器跳转到指定时间：

```powershell
$Card = Join-Path $Workspace 'outputs\reviews\card-01\index.html'
& $Python "$Install/cli.py" --workspace "$Workspace" review-card check --card "$Card"
if ($LASTEXITCODE -ne 0) { throw '卡片绑定已变化或不完整，请先生成新的审核卡' }
& $Python "$Install/tools/serve_review.py" --card "$Card" --port 8765
```

把输出地址 `http://127.0.0.1:8765/index.html` 打开在同一台电脑的浏览器中。审片时保持这个 PowerShell 窗口运行，结束后按 `Ctrl+C` 停止，再继续其他命令；关闭服务后原卡片与反馈文件仍保留。端口被占用时换一个空闲端口（1～65535），并打开本次输出的新地址。服务不会自动打开浏览器。

服务仅监听 `127.0.0.1`，只读提供卡片同目录的文件，不列目录、不读取子目录，也不上传、不接收反馈写入或修改审核状态。它不会自动执行绑定核验，因此仍须先运行上面的 `review-card check`；不要把商家其他资料混放到卡片目录。运营的反馈仍按下文复制回对话并保存。

页面打开后，可播放整个主窗口、点击时间回听或按拟成片顺序试听；原生控件支持暂停、拖动与音量，另有 0.8/1/1.25 倍速。原速回听按钮会切换到对应窗口并恢复 1 倍，范围结束自动暂停。页面中的 ASR 保持原文，不据机器识别擅自纠错；附加窗口没有 ASR 时只提供真实媒体与时间映射。桌面将版本并排，窄屏改为单列。

“实际听看范围”和“整窗同款”均不预选。请按实际情况选择，并在每题填写原话或“听不清”。点击“预览反馈”或“复制反馈”后得到可读文本，只有当前真实填写内容、明确未填写项及媒体指纹/范围。自动复制失败时会选中反馈文本，供手动复制。反馈不自动保存，关闭页面前请复制回对话。

播放结束、播放次数和播放时长都不构成人工审核证据。卡片上的选择只是用户声明；后续仍须按项目原有真实证据流程人工核对，不能把卡片生成、播放或复制当成审核通过。

## 放入已有的新旧版本

在原输入中增加 `versions`，最多两个。下面是结构示例，示例路径、哈希和时间必须由 AI 换为实际文件与实际导出范围：

```json
{
  "additional_extractions": ["W/private-context/source-windows/task-one/window-next/extraction.json"],
  "versions": [
    {
      "id": "v2", "label": "第二版待审",
      "path": "W/outputs/draft-v2/preview.mp4", "sha256": "实际文件SHA-256",
      "duration_seconds": 12,
      "changes": ["删掉重复解释，重新安排结尾展示；内容与顺序都有变化"],
      "segments": [
        {"label": "开头", "purpose": "用连续上身画面展示版型",
         "window_range": [15, 21], "video_range": [0, 4]},
        {"label": "结尾", "purpose": "保留完整收句与展示，仍待听看",
         "extraction": "W/private-context/source-windows/task-one/window-next/extraction.json",
         "window_range": [3, 9], "video_range": [4, 12]}
      ]
    }
  ],
  "questions": [
    {"id": "Q1", "question": "这个词及尾字是否完整？听不清请直说。",
     "extraction": "W/private-context/source-windows/task-one/window-next/extraction.json",
     "window_range": [3, 5], "video_ranges": [{"version": "v2", "range": [4, 6]}]}
  ]
}
```

- 保留原输入的 `extraction`、`asr_preview`、`title`、`proposed_segments`。`additional_extractions` 可省略；附加窗口必须来自同一原片，每窗最多 120 秒，并通过现有抽取映射校验。不会为附加窗口制造 ASR。
- 拟片段、版本段落和问题都可指定 `extraction`，省略时引用主窗口。只能引用本卡明确列入的抽取记录。核词针对保留的表达，不要求运营反复确认已删除的句子。
- `video_range` 与 `video_ranges[].range` 是该导出视频从 0 开始的秒数。问题可分别绑定新旧两版的不同区间；不要把同一时间硬套到不同版本。
- 每版须明确 `changes` 与每段 `purpose`，首版或纯参照版也写明实际情况。来源范围、速度、删句、非同期画面、定格及听感变化按实际说明；它们是制作者的记录，生成器只核验绑定和范围，不能自动证明剪辑判断正确。非同期画面可另列与口播重叠的段落，分别标出画面来源与作用。
- 版本须是当前工作区内已有的 MP4，提交实际 SHA-256。旧工作区文件先原样复制至当前工作区并核对字节，不静默重写历史证据。播放器以 1 倍播放已有导出文件，文件本身的加速不在此处重做。

使用 `review-cut` 产物时，由 Codex 读取 `preview.json` 后填写本节的 `versions`，不要直接把它当作听审卡 spec。版本路径、哈希和时长取本次实际预览；段落 `video_range` 取导出记录，原速回听范围取对应 `audio.window_range` 与抽取记录。非同期画面另列同一成片范围的画面段落，使用 `visual.window_range` 和画面抽取记录；它与口播段重叠是有意记录两条来源。新增窗口列入 `additional_extractions`。定格在 `changes` 中写明实际时长及成片范围，不把定格时长加进原声源区间。

## 保存自然语言反馈与重新审核

运营可以直接在对话说“更喜欢第二版，但结尾没听清”，或复制页面反馈。AI 原样保存到当前工作区 `private-context` 的 UTF-8 文本，再按本次真实听看版本、范围和问题运行：

```powershell
& $Python "$Install/cli.py" --workspace "$Workspace" review-card check --card "$Workspace/outputs/reviews/card-01/index.html"
& $Python "$Install/cli.py" --workspace "$Workspace" review-card save-feedback --card "$Workspace/outputs/reviews/card-01/index.html" --feedback-text "$Workspace/private-context/feedback-original.txt" --version v2 --range 4 6 --question Q1 --output "$Workspace/private-context/feedback-q1-v2.json"
```

`--question` 可省略，适合保存明确范围内的整体节奏或版本偏好。原片核词使用 `--version source`，有问题 ID 时定位该问题的原片窗口；此时 `--range` 填问题的 `window_range`。有问题 ID 的成片反馈范围必须与该问题绑定的版本范围完全一致。若用户只说偏好某版，没有说明实际听看范围，先保留对话原文，不能替用户补成全片已经听过；保存绑定的范围也不等于已完成该范围的试听。

保存记录包含原文、原文文件哈希、版本、媒体哈希、区间、可选问题及其原片范围、卡片绑定和记录时间；固定为 `pending`，不会修改同款、字幕、采用或最终审核状态。不同版本或问题分别保存为新文件，拒绝覆盖已有反馈。空内容、未知版本、越界或错误问题绑定拒绝保存。

生成器在复制后再次校验输入；`check` 和 `save-feedback` 会重核原片、窗口、ASR、输入说明、对比文件、卡片副本及页面的绑定。变化或缺失时明确提示“需要重新审核”，保存反馈会中止。保留旧反馈作为旧文件的历史证据，为新文件生成新卡，再确认受影响内容；不能把旧偏好、原速核词或技术检查自动迁移为新版整片通过。

离线网页只展示生成时的绑定，不在浏览器后台自动扫描磁盘；仅打开页面不等于重新核验。`check` 成功也只说明文件绑定仍一致，真实同款判断、准确原话、连续观看、最终加速听感和用户验收仍须分别记录。

## 改动后哪些内容要重新审核

| 改动 | 需要重新核对的内容 |
| --- | --- |
| 原片、窗口文件或来源映射变化 | 重新核对源哈希、同款、原声与区间；依赖旧来源的选段、核词和成片审核不能直接沿用。 |
| 选段、顺序、画面替换或裁切变化 | 重新检查新增范围的同款与原话、切口完整性、上下文、画面衔接和非同期口型；新成片重新听看与技术检查。 |
| 字幕文字、分句或位置变化 | 逐项核对改词证据、事实数字、时间与实际显示；字幕烧录生成新视频后重新核验技术和画面，不能只复用旧视频哈希。 |
| 速度变化 | 原源区间不变时可引用原速核词证据，但成片时间映射、字幕同步、动作和最终速度听感须重新检查。 |
| 句内切口或尾定格变化 | 切口落在 ASR 句内时重新确认实际留下的原话和完整性；定格时长变化后核对尾帧、静音与字幕退出位置。编辑参数会绑定正式采用记录，不能删字段沿用旧版。 |
| 封面或标题、正文变化 | 重新核对商品、原话依据、事实与显示。视频和原声字节未变时可按正式链核验已有媒体证据，不因文案修改重做转写；旧封面/文案审批不覆盖新版。 |

这些是审核影响范围，不是自动通过规则。Codex 保存用户自然语言原文，只把用户实际确认的版本、范围和事项记入对应证据；未确认项继续待审。正式字幕及交付仍走[组合、字幕与交付流程](pipeline-reference.md#4-组合字幕与交付)，不能通过复制预览或补写通过状态跳过门禁。
