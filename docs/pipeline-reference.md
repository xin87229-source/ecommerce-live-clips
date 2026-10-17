# 本地制作链操作参考

本文给会使用 Codex 的操作者。各阶段以安装目录中的 `cli.py` 为入口；`<安装目录>`、`<工作目录>`、`<原片>` 等尖括号文字只是虚构占位，必须换成当前机器上的真实绝对路径。JSON 片段说明字段形状，`<实际…>` 也必须由本次文件、哈希、听看记录和用户指令取得，不能照抄为证据。所有任务资料与产物保存在工作目录；原片保持只读。CLI 命令中的 `--workspace` 要放在阶段名之前。

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" init --profile basic --ffmpeg "<ffmpeg程序>" --ffprobe "<ffprobe程序>" --font "<可显示中文的字体文件>" --model "<已安装模型目录>"
python "<安装目录>/cli.py" --workspace "<工作目录>" doctor
python "<安装目录>/cli.py" --workspace "<工作目录>" new-task --source "<原片>" --product-name "示例商品" --reference "<实物参考图>" --task-id example-01
python "<安装目录>/cli.py" --workspace "<工作目录>" status
```

`init` 只创建工作目录的 `config.json`，已有配置不会覆盖。`basic` 无同款原区间最低秒数；`apparel` 要求完整盘点后已确认同款原区间并集至少 20 秒。两种预设都要求表达完整，成片至多 70 秒、1～1.5 倍速、1080×1920 和 30 fps。`doctor` 只检查文件、工具和依赖是否可用，不安装、下载或执行转写。`new-task` 生成 `outputs/<task-id>/run-state.json`，其中 `source_review.conclusion` 为 `unknown`；创建任务不等于已核验商品。

## 1. 原片与同款窗口

窗口阶段只在当前工作目录写抽帧、试听片段、映射与子任务卡，不修改原片：

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" window inspect --task-record "<工作目录>/outputs/example-01/run-state.json"
python "<安装目录>/cli.py" --workspace "<工作目录>" window extract --task-record "<工作目录>/outputs/example-01/run-state.json" --start <已核对的帧起点秒> --end <已核对的终点秒>
```

`inspect` 只给整场元数据与三张采样帧，不证明同款或全场覆盖。操作者先看参考与原片并听对应原声，再选原片的真实帧起点；`extract` 输出 `extraction.json`、窗口副本、三张代表画面和完整窗口试听文件。它会拒绝与已抽窗口重叠、无法精确映射的边界与异常音画。

对当前工作目录内**已抽取、证据完整、实际时长不超过 120 秒**的窗口，可选择运行本地 ASR 预览，用原片时间轴口播提示辅助听审；必须事先配置本地转写模型：

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" window transcribe-preview --extraction "<本次extraction.json绝对路径>"
```

返回的 `preview_path` 指向新建的 `asr-preview-*.json`。其中 `extraction.path/sha256`、`source_sha256`、`window_sha256`、`transcript_cache.path/sha256`、`asr_config_hash` 绑定输入和模型配置；`segments` 同时给出 `window_range` 与 `source_range`，并保留未核验的 ASR `text`。文件明确写 `status=unreviewed_asr_preview`、`same_product=unknown`、`audio_listening=not_performed`、`transcript_verified=false`。预览不写任务卡、不改变同款状态；ASR 可能错字，也不是实际听审或已核验转写。没有听看能力时在此停止同款确认，不得靠预览填 `matched` 或纠正事实数字。

### 可选：先生成待审粗剪 `review-cut`

需要先比较选段、顺序和节奏时，使用独立预览入口。它接受本次原片任务与已抽取窗口，不要求把同款或核词填成通过。原任务须为 `NOT_UPLOADED_NOT_SUBMITTED`、`submitted=false`，且没有 `batch_ref` 或采用指纹；不能借已采用/已提交的任务生成新的未提交身份。运营用自然语言表达意图和反馈，由 Codex 生成与维护 JSON。

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" review-cut --spec "<工作目录>/private-context/cut-spec.json" --task-id preview-v1
```

最小输入示例，数字只展示字段类型；必须依据实际窗口和表达选择区间与速度：

```json
{
  "schema_version": 1,
  "task_record": "<工作目录>/outputs/example-01/run-state.json",
  "clips": [
    {
      "extraction": "<工作目录>/private-context/source-windows/example-01/window-xxx/extraction.json",
      "window_range": [2, 6],
      "speed": 1.0,
      "purpose": "保留连续原声与上身画面，检查开头是否完整"
    }
  ]
}
```

`clips` 按成片顺序排列，`window_range` 为对应窗口从 0 开始的实际秒数。每项都须有 `extraction`、`window_range`、`speed`、非空 `purpose`。所有抽取记录必须位于当前工作区，并绑定同一个原片任务、原片路径及哈希；程序重核抽取记录、窗口和源文件。旧成片只能作为听审卡中的比较版本，不能填到这里作为新剪辑来源。

| 可选字段 | 结构与约束 |
| --- | --- |
| 片段 `visual` | `{"extraction":"实际抽取记录路径","window_range":[起点,终点]}`；单独指定同源画面，原声仍取片段主区间。画面按相同速度须覆盖原声段，帧取整不足一帧的部分明确补末帧。 |
| 片段 `crop` | `{"x":0,"y":0,"width":720,"height":1280}`；数值是画面像素的结构示例。四项须为偶数整数，起点非负、尺寸为正并在实际画面内；静态裁切后等比补边，不提供动态跟随。 |
| 片段或顶层 `questions` | 字符串数组，原样保存疑问，不自动解释或批准；省略时为空。 |
| 顶层 `end_hold_seconds` | 0～2 秒，省略为 0；按最近的 30 fps 帧取整，定格最后一帧并补静音。 |

每段速度在 1.0～1.5 之间按实际表达选择，可各不相同；`atempo` 保持原声音高，但最终听感仍须实际听审。每段时长向上取整到 30 fps 的完整帧，末尾不足一帧时补末帧与静音；含尾定格总长不得超过 70 秒。输出固定为 1080×1920、30 fps。不要为凑固定时长或固定倍速保留重复句、截断表达。

新输出目录为 `outputs/review-cuts/<task-id>/`，已有目录拒绝覆盖。包含 `preview.mp4`、`preview.json`、`technical.json`、`media.log` 和三张抽帧。`preview.json.segments` 记录实际 `video_range`、帧区间、速度、问题、原声 `audio` 与画面 `visual` 各自的窗口/原片范围及哈希；非同期画面显式标记，`visual.requested_window_range` 保留请求范围，实际消费范围单独记录。`end_hold` 记录定格的实际帧数和成片位置。补齐秒数来自时间计划，不冒充人工判断的实际有声截止。

预览固定标为 `unreviewed_review_cut`，审批项全部为 `not_reviewed`；技术通过写入单独的 `technical.json`。生成或失败都不改原任务卡，不生成采用证据、正式 `result.json` 或交付包。此入口不烧录字幕；尚未获准的文字保留在问题或未核验 ASR 中，不能先烧录后冒充原话已经确认。

预览生成后，按[听审卡的新旧版本方法](human-review.md#放入已有的新旧版本)把实际视频、哈希和区间写入 `review-card` 的 `versions`。原声和非同期画面分别标注来源与作用；主窗口的 ASR 预览仍须单独准备。运营可以直接比较、原速回听并用自然语言反馈。`review-cut` 的 JSON 不是 `review-card` 的输入格式，不能直接混用；正式制作仍须完成下面的同款、采用、字幕和交付流程。

### 记录实际同款审核

查看实际画面与窗口原声后，在当前工作目录的 `private-context` 写审查 JSON；最小结构如下：

```json
{
  "conclusion": "matched",
  "reviewer": "<实际审核人或有听看能力的AI>",
  "reviewed_at": "<实际带时区时间>",
  "windows": [{
    "extraction": "<本次窗口的extraction.json绝对路径>",
    "visual": [
      {"at": <sample_frames[0].source_at>, "path": "<sample_frames[0].path>", "sha256": "<sample_frames[0].sha256>", "finding": "<实际看到的首部同款依据>"},
      {"at": <sample_frames[1].source_at>, "path": "<sample_frames[1].path>", "sha256": "<sample_frames[1].sha256>", "finding": "<实际看到的中部同款依据>"},
      {"at": <sample_frames[2].source_at>, "path": "<sample_frames[2].path>", "sha256": "<sample_frames[2].sha256>", "finding": "<实际看到的尾部同款依据>"}
    ],
    "speech": {"type": "audio_listening", "range": [<extraction.source_span[0]>, <extraction.source_span[1]>],
               "path": "<extraction.audio_review.path>", "sha256": "<extraction.audio_review.sha256>",
               "finding": "<实际听到的完整窗口同款与表达依据>"}
  }]
}
```

`visual` 的三项顺序、时间、路径和哈希要逐项复制本次 `extraction.json.sample_frames`，`speech` 绑定本次 `audio_review` 和 `source_span`；`finding` 必须来自实际画面和原声检查。`matched` 是操作者明确做出的判断，不是脚本自动识别。同款未确认或无法听看时保留 `unknown`，不要调用 `create-review` 冒充通过。

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" window create-review --task-record "<工作目录>/outputs/example-01/run-state.json" --review "<工作目录>/private-context/<实际窗口审查JSON>"
python "<安装目录>/cli.py" --workspace "<工作目录>" quality check-task --task-record "<工作目录>/outputs/example-01/run-state.json"
```

`create-review` 只接受第一次确认的原任务卡；成功后它把 `confirmed_windows` 和 `source_review` 绑定到父任务，并为每个窗口创建独立子任务卡。返回的 `window_tasks[].task_record`、`source_video` 与 `import_entry` 是下一阶段的真实输入。已有审核、采用、提交或状态改变的旧卡不会回滚覆盖。

## 2. 分窗转写、候选与合并

`suggest prepare` 会对传入目录中唯一一条视频完整转写，因此对混款整场应使用上一步生成的窗口视频和窗口子任务卡，不直接把整场送入 `prepare`。每个窗口放在 `source_windows.extract` 创建的独立目录，目录中应只有一条视频。

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" suggest prepare --source-dir "<单视频窗口目录>" --task-record "<该窗口任务卡>"
python "<安装目录>/cli.py" --workspace "<工作目录>" suggest block --job "<prepare返回的job目录>" --block "<manifest中的块ID>" --task-record "<该窗口任务卡>"
```

逐块读取真实转写，在工作目录 `private-context` 下写选择 JSON。口播候选只引用块中连续的真实 `segment` ID，不填自由时间或改写原话；分数六项分别以 `hook≤15`、`audience≤15`、`product≤25`、`experience≤15`、`complete≤20`、`clarity≤10` 计。一个块可没有候选；风险旗标只用源码允许的枚举。示意：

```json
{
  "block_hash": "<当前块hash>",
  "candidates": [{
    "start_segment_id": "<真实首segment ID>", "end_segment_id": "<真实末segment ID>",
    "scores": {"hook": 0, "audience": 0, "product": 0,
               "experience": 0, "complete": 0, "clarity": 0},
    "category": "<实际表达类型>", "reason": "<选择理由>", "risk_flags": []
  }]
}
```

展示单元还需要 `kind=visual`、准确起止秒、画面引用和覆盖整个单元的实际原声审核，不能把 ASR 空白当作静音。没有这些证据就只做已验证的口播候选。

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" suggest submit --job "<job目录>" --block "<块ID>" --selection "<private-context中的选择JSON>" --task-record "<该窗口任务卡>"
python "<安装目录>/cli.py" --workspace "<工作目录>" suggest merge --job "<job目录>" --task-record "<该窗口任务卡>"
```

所有块提交后 `merge` 生成 `ranking-input.json`。排名 JSON 形如 `{"input_hash":"<ranking-input中的hash>","order":["<全部实际候选ID，逐个且不重复>"]}`。批次模式必须保留全池，不能用 `--top` 截掉候选。

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" suggest finish --job "<job目录>" --ranking "<private-context中的排名JSON>" --task-record "<该窗口任务卡>"
```

如果使用多个窗口，将已完整转写和分类的窗口 job 映回原直播；即使只有一个窗口，批次流程也应基于原片父任务和原时间轴建合并 job。导入说明写在工作目录 `private-context` 中。每个 `offset_seconds`、`mapping` 直接取对应 `create-review` 返回的 `import_entry`，`job_path` 取其窗口任务卡生成的真实 job；示意：

```json
{
  "source": {"path": "<原直播文件>", "sha256": "<原直播实际哈希>",
             "duration_seconds": <原直播实际时长>},
  "task_record": "<原直播任务卡>",
  "jobs": [{"job_path": "<完整窗口job目录>", "offset_seconds": <核实的原直播起点秒>,
            "mapping": {"source_sha256": "<import_entry.mapping.source_sha256>",
                        "window_sha256": "<import_entry.mapping.window_sha256>",
                        "offset_seconds": <import_entry.mapping.offset_seconds>,
                        "range": [<import_entry.mapping.range[0]>, <import_entry.mapping.range[1]>],
                        "evidence": {"path": "<import_entry.mapping.evidence.path>", "sha256": "<import_entry.mapping.evidence.sha256>"}}}]
}
```

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" import merge-jobs --spec "<导入说明JSON>" --output "<工作目录>/private-context/selector-v02a/<新job名>"
```

窗口不能在原直播时间轴上重叠；脚本会拒绝冲突，不自动删掉原话。只导入已确认零起点且全段完成分类的窗口。合并结果的 `coverage_scope` 是 `reviewed_windows_only`，不代表整场已转写。

## 3. 批次方案与采用

`workflow draft` 从真实候选生成**不可直接采用**的方案草稿。对于导入窗口的 job，它只把已确认同款窗口与已转写窗口的交集记为初始 `searched_ranges`；AI 须实际盘点原片剩余区间、核验商品、审阅候选并填写完整方案：

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" workflow draft --job "<合并job目录>" --task-record "<原片父任务卡>" --batch-id example-batch --output "<工作目录>/private-context/example-batch.json"
```

草稿的关键初始值是 `coverage.scope`（从转写覆盖计算）、`coverage.searched_ranges`（已确认同款与已转写范围的交集）、真实候选摘要 `candidate_inventory`、`items: []`、每个候选对应的 `exclusions` 中 `usable: null` 和空 `reason`、空 `why_not_more`。`draft_help` 只说明待填字段，不是审查证据；不要把草稿直接提交采用。

`batch` 命令提供 `validate`、`report`、`pair-hashes`、`adopt`；没有自动做主线和同款判断的 `propose` 子命令。最小单条方案的字段形状（以下为填好审查后的方案，不是 `draft` 原样结果）：

```json
{
  "version": "single-style-batch-v1", "batch_id": "example-batch",
  "job_path": "<合并或单窗口job目录>",
  "source": {"path": "<原直播文件>", "sha256": "<原直播实际哈希>",
             "duration_seconds": <实际时长秒>},
  "product": {"name": "示例商品", "code": "<父任务卡稳定商品标识>", "id": null,
              "reference": {"path": "<父任务卡参考图路径>", "sha256": "<父任务卡参考图哈希>"}},
  "coverage": {"scope": "reviewed_windows_only", "source_sha256": "<原直播实际哈希>",
               "evidence": {"path": "<实际盘点记录>", "sha256": "<实际哈希>"},
               "searched_ranges": [[<已检索开始>, <已检索结束>]],
               "confirmed_ranges": [[<已确认同款开始>, <已确认同款结束>]],
               "pending_ranges": []},
  "items": [{"item_id": "example-item", "task_id": "example-video-01",
             "order": ["<真实候选ID>"], "speed": 1.0,
             "speed_reason": "<原声语速依据>", "mainline": "<独立主线>",
             "completeness": "<关键前提和结尾完整性依据>",
             "roles": {"<真实候选ID>": "main"}}],
  "exclusions": {}, "reuse": [], "pair_reviews": [],
  "why_not_more": "<整场盘点后剩余材料为何不能再形成完整新主线>"
}
```

`product` 必须与父任务卡的整个对象一致，示例的 `id: null` 只适用于任务卡本来就是 `null` 的情况。`exclusions` 必须覆盖全部未使用候选；每项可写 `{"reason":"<具体处置原因>","usable":false}`。两条以上须逐对写 `pair_reviews` 的 `items/mainline/speech/visual/basis/conclusion/content_hash`；共用辅助单元须在 `reuse` 中逐对披露。`pair-hashes` 可从当前方案计算比较绑定值，变更条目后重算，不能沿用旧判断。

导入窗口的 ASR 只覆盖 `transcript.covered_ranges`，与**人工整场盘点范围**分别记录。尚未看完整场时保持 `coverage.scope=reviewed_windows_only`，`searched_ranges` 必须落在转写窗口内；只有父任务明确 `scope=test` 才能采用这种局部计划，`recommended_count` 不代表整场最终最大条数。要用于 `production`，操作者须真实检索完整原片，填写 `coverage.scope=full_source`、`searched_ranges=[[0, 原片时长]]`，并在 `coverage.full_review` 引用独立的人工盘点 JSON。所有 `confirmed_ranges` 必须位于 `transcript.covered_ranges` 内，候选也必须落在已转写窗口；**不要求把非同款的整场视频全部做 ASR**。`pending_ranges` 未清零时不可采用。抽取窗口的子任务不得声称覆盖了其父原片全场。

`full_review` 引用形如 `{"path":"<工作目录>/private-context/full-review.json","sha256":"<该文件真实SHA-256>"}`。该文件为实际人工审查记录，字段如下；源哈希、时长及三个区间列表必须与方案完全一致，变更后须重新审查和采用，不能只改方案里的 `scope`。

```json
{"schema_version":1,"conclusion":"full_source_reviewed",
 "source_sha256":"<原直播实际SHA-256>","duration_seconds":<原直播实际时长秒>,
 "searched_ranges":[[0,<原直播实际时长秒>]],
 "confirmed_ranges":[[<真实同款起点>,<真实同款终点>]],"pending_ranges":[],
 "reviewer":"<实际审查者>","reviewed_at":"<带时区的实际完成时间>",
 "method":"continuous_playback","finding":"<整场其他时段及本款范围的真实核对结论>"}
```

这是操作者对整场已检索的可追溯声明；脚本验证绑定和区间，不会代替人观看整场，也不会从短窗口样本推断全场同款产量。`batch validate` 的 `searched_seconds` 与 `transcribed_seconds` 分别表示人工检索与 ASR 覆盖秒数。

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" batch validate --plan "<private-context中的方案JSON>"
python "<安装目录>/cli.py" --workspace "<工作目录>" batch pair-hashes --plan "<方案JSON>"
python "<安装目录>/cli.py" --workspace "<工作目录>" batch report --plan "<方案JSON>" --output "<private-context中的方案说明.md>"
python "<安装目录>/cli.py" --workspace "<工作目录>" batch adopt --plan "<方案JSON>" --items example-item --evidence "<实际采用指令记录>" --at "<带时区的实际采用时间>" --task-record "<原片父任务卡>"
```

`adopt` 可使用与方案的源、商品和已确认窗口精确相符的父任务卡；局部方案还要求父任务 `scope=test`。若是旧条目的原创性修订，则必须使用对应子任务卡，不能借父卡解锁。`adopt` 的证据须能回查真实用户指令及采用范围；AI 自己写的假用户确认不成立。采用后用 `workflow bind` 根据方案和父任务创建独立条目任务卡，自动写入 `task_context.batch_ref` 和当前采用指纹；不能手改旧成片任务卡以套用新版本：

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" workflow bind --plan "<已采用方案JSON>" --item-id example-item --task-record "<原片父任务卡>"
```

## 4. 组合、字幕与交付

`review-cut` 只产生独立待审预览，不能作为 `combine`、`subtitle` 或 `package` 的成功结果。不得把预览重命名、补写采用或 QA 通过来进入正式链；未确认的同款、字幕事实及阻断问题仍须按原有门禁处理。普通待核词也不能由 Codex 猜成“用户已确认”。

普通组合注释写当前已采用顺序、倍速和完整性理由：

```json
{"candidates_hash": "<当前candidate-clips.json的key值>",
 "order": ["<已采用候选ID，按顺序>"], "speed": 1.0,
 "speed_reason": "<实际听感依据>", "content_complete_reason": "<表达完整的具体依据>"}
```

### 保留待审版的编辑方式

如果待审版使用句内截取、非同期画面、逐段速度或尾定格，Codex 将实际编辑参数写入批次条目的 `edit_plan`，并在组合注释中提供完全相同的 `edit_plan`。运营只需审片、用自然语言确认事实和采用范围，不需要编写 JSON。预览文件不能作为正式剪辑的源；正式制作仍读取经过哈希和来源映射校验的原片窗口。

```json
{"schema_version":1,"end_hold_seconds":0.6,"clips":[
  {"candidate_id":"<已采用候选ID>","purpose":"<本段实际作用>",
   "extraction":{"path":"<当前工作区的extraction.json绝对路径>","sha256":"<文件哈希>"},
   "window_range":[2,5],"speed":1.2,
   "visual":{"extraction":{"path":"<画面抽取记录绝对路径>","sha256":"<文件哈希>"},
             "window_range":[8,11.2]}}
]}
```

此例数字仅说明结构。`clips` 必须与已采用候选一一同序；`window_range` 使用窗口相对秒数，实际原声须在该候选的原片范围内。音频和画面都须来自同一原片、处于已确认同款区间；不同窗口须各自保留抽取映射。`speed` 可逐段省略以使用条目的默认值，范围为 1～1.5，按实际听感选择。`visual` 可省略以保留同期画面；它只替换画面，原声仍取音频范围，不能把画面原有说话伪记为无声。裁切可选 `crop={x,y,width,height}`，用实际源像素的偶数坐标及尺寸。画面范围须足以覆盖原声，尾定格可为 0～2 秒，实际长度按 30fps 帧数记录；这些是工具支持范围，不是每条内容的品质目标。

原候选保留完整 ASR 边界和原文，不为适配切口篡改候选。如果实际保留范围截断某个 ASR 段，剪辑项须列 `boundary_transcripts`，每项包含 `segment_id`、原片绝对 `source_range`、实际保留的 `text` 和核词 `evidence`。证据引用含 `type=audio_listening|verified_transcript`、文件 `path/sha256`、原片 `source_sha256`、对应 `segment_ids` 及相同 `range`；证明文件的 `boundary_transcripts` 列表中须有唯一同源、同段、同范围、同文字条目。该条目的 `confirmation` 须含 `status=confirmed`、`type=user_message|audio_listening`、实际执行者 `actor`、含时区时间 `at`、原反馈文件 `path/sha256` 和包含准确原话的 `quote`。反馈原文件确实包含此引用才可绑定；程序验证绑定，不能认证听到的事实。没有可靠原话就停在待审预览，不能生成确认占位内容。

`batch validate/adopt` 核对实际保留区间及含定格的预计总长；候选盘点的分配秒数仍按原候选范围计算，不等于最终成片时长。`combine` 保存完整 `render_timeline`、每段帧区间及成片哈希，字幕阶段重新核对当前采用记录、原片窗口、核词文件与成片字节，再按实际逐段速度映射；无声定格不延长最后字幕。交付包记录默认速度、实际逐段速度、定格及编辑时间轴哈希。改变或删除这些依赖会拒绝复用旧审核；修改后重新采用、制作并审查受影响部分。

`source-review` 必须放在 `private-context`。它应包含 `authorized_by_user=true`（仅在授权确实存在时）、`candidates_hash`、`source_sha256`、与任务卡相符的 `material.product_name/product_code/source_video`，以及覆盖 **全部** 候选 ID 的 `candidate_evidence`。每项写布尔 `matched`、实际画面结论 `visual`、原声结论 `speech`；不匹配项还须写具体 `reason`。程序核验的是记录绑定，不会自动识别实物同款。

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" combine --job "<job目录>" --annotations "<组合注释JSON>" --source-review "<private-context中的逐候选审核JSON>" --task-id example-video-01 --task-record "<该条任务卡>"
python "<安装目录>/cli.py" --workspace "<工作目录>" subtitle prepare --composition "<组合输出目录>"
```

`subtitle prepare` 返回 `selected` 文件、`selected_hash`、`selection_binding` 和候选哈希。阅读 `selected.json` 后，在 `private-context` 写字幕 review；每句 `segment_ids` 必须按组合顺序完整消费，不能跨剪辑接点，`lines` 只能是清理或有证据纠错后的原话。示意：

```json
{
  "selected_hash": "<prepare返回的selected_hash>",
  "content_decision": {
    "selection_hash": "<prepare返回的selection_hash>",
    "candidates_hash": "<prepare返回的candidates_hash>",
    "content_goal": "purchase_understanding", "viewer_question": "<观众实际问题>",
    "opening": {"segment_id": "<首句segment ID>", "quote": "<首句原话>",
                "range": [<原片起点>, <原片终点>], "visual": "<已看画面说明或明确未审>"},
    "roles": [{"clip_id": "<首片段ID>", "role": "<此段在整条中的作用>"}],
    "repetition": "<重复性检查>", "context_dependency": "<上下文是否完整>",
    "ending": "<结尾依据>", "conclusion": "pass", "issues": []
  },
  "clip_positions": {},
  "cues": [{"segment_ids": ["<按序segment ID>"], "lines": ["<实际原话字幕>"]}]
}
```

`content_decision.roles` 要按实际片段顺序覆盖每段；展示段开头使用 `opening.kind=visual` 等真实展示绑定，不能填口播占位。最多两行，默认同一片段字幕位置固定。数字、价格、尺寸和事实疑点不能猜；纠错须在 `corrections` 中保留 `from/to/reason` 和绑定当前源哈希、完整 cue、精确原区间的 `audio_listening` 或 `verified_transcript` 文件证据。`risks` 使用 `ordinary/correctable/blocking` 分级；事实或阻断问题没有可靠证据时停在预检。只改字幕时不重做转写或组合视频。

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" subtitle preflight --composition "<组合输出目录>" --review "<private-context中的字幕review.json>" --task-record "<该条任务卡>"
python "<安装目录>/cli.py" --workspace "<工作目录>" subtitle render --composition "<组合输出目录>" --review "<字幕review.json>" --task-id example-video-01 --task-record "<该条任务卡>"
python "<安装目录>/cli.py" --workspace "<工作目录>" package prepare --source "<字幕输出目录>" --task-id example-package-01 --task-record "<该条任务卡>"
```

`package prepare` 返回 `publish-package` 路径，并生成 `logs/facts.json`、`logs/frames.json` 与候选帧联系表。它从当前已完成的字幕阶段复制实际 `subtitle.srt`，存在 `subtitle.ass` 时一并复制；缺少 SRT 会拒绝建包。`publish.json.subtitles.srt` 记录固定包内路径与 SHA-256，`ass` 可选，最终 `delivery_files` 把字幕与成片、封面、文案一同绑定。阅读实际字幕、真实帧后在 `private-context` 写 draft。必需字段为 `facts_hash`、三个不同且各为 10～16 字的 `title_candidates`、`selected_title`（0～2）、`selection_reason`、20～40 字的 `caption`、3～5 个 `hashtags`、`cover_reason`、`evidence`（当前 `cue_id/quote`）、`template_cover`（1～3 行文字及至少一项 `line_index/cue_id/quote` 引用）。真实帧封面还要 `cover_frame` 整数 ID，以及 `frame_review` 中与所选帧字节和时间一致的 `sha256/frame_time/conclusion=matched/reviewer/finding/reviewed_at`。自带商品图改用 `--cover-mode user_image --user-image <图片> --image-review <实际查看记录>`。这些查看记录须来自真正的画面检查。

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" package preflight --package "<publish-package目录>" --draft "<private-context中的draft.json>" --task-record "<该条任务卡>"
python "<安装目录>/cli.py" --workspace "<工作目录>" package finish --package "<publish-package目录>" --draft "<draft.json>" --task-record "<该条任务卡>"
```

封面默认 `source_frame`、`3x4`。若只需改未提交包的标题/封面文字，可用 `package retext` 并给新 `--task-id`；不能覆盖原包。`finish` 与 `retext` 都核对字幕源文件及交付清单哈希，不能给已改变的字幕重新签名。标题、正文和封面不得凭空加入价格、库存、销量、优惠或效果承诺。`finish` 仅形成本地交付包，发布字段保持空值。

## 5. 分项 QA 与交付边界

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" quality check-task --task-record "<该条任务卡>"
python "<安装目录>/cli.py" --workspace "<工作目录>" quality audit --task-record "<该条任务卡>" --result "<字幕输出目录>/result.json" --evidence "<实际QA证据JSON>"
python "<安装目录>/cli.py" --workspace "<工作目录>" quality check-package --task-record "<该条任务卡>" --package "<publish-package目录>"
```

`audit` 的 JSON 顶层可含 `technical`、`continuous_visual`、`audio_review`、`user_confirmation`。每个声称 `passed` 或 `failed` 的项目必须有 `status/video_sha256/coverage/evidence/actor/at/limitations`；证据类型分别只接受 `media_check`、`continuous_playback`、`audio_listening`、`user_message`，其中 `evidence` 是实际文件的 `type/path/sha256`。技术、连续画面、音频试听若声称 `passed`，`coverage` 均须覆盖 `[0, 成片总时长]`；只听看一小段可以记录实际范围，但不得填整片 `passed`。脚本技术通过不能代替连续观看、实际试听、商品核对或用户确认。没有能力或尚未完成时保留 `not_reviewed`/`unknown`，不要补造证据。`quality check-package` 是本地包检查，不代表任何平台发布或用户已验收。

本地批次收尾使用 `records local-close`。先在 `private-context` 写引用文件，例如 `{"items":[{"item_id":"example-item","run_state":"<子任务run-state.json>","package":"<子任务publish-package目录>"}]}`，每个条目恰好一项，再运行：

```text
python "<安装目录>/cli.py" --workspace "<工作目录>" records local-close --plan "<已采用方案JSON>" --records "<引用JSON>" --out "<工作目录>/outputs/local-close-01"
```

新目录中有 `batch-summary.json`、`report.md`、`delivery-checklist.md`。每项均先核对当前任务卡：`BLOCKED` / `FAILED` 等尚无包的条目可只填 `run_state`，总表保留原状态、记录的原因和下一步，明确无成片及 QA 结论；若有包而该条包内文件或 QA 校验失败，该条显示 `needs_repair` 与具体原因，不阻断其他合格条目。任务 ID、批次引用、包身份或提交状态冲突仍拒绝整个收尾。合格包的 `report.md` 和清单会给出可点击的成片、封面本地链接。平台状态标为本地流程范围外，不会伪造 `published`；输出目录已存在则拒绝覆盖。旧 `records close` 依赖 `publish_result`，不适用于本地交付。

遇到源、候选、顺序、倍速、采用版本或成片字节变化，重新核对受影响的哈希和证据；旧 QA 不自动沿用。任务失败保留现场并换修订目录，严禁覆盖已存在输出。当前首版只制作和本地交付，不执行上传、挂车、排期或平台提交。

源文件、选段、字幕、速度及封面文案分别影响哪些审核，见[改动后的审核影响表](human-review.md#改动后哪些内容要重新审核)。保存运营的自然语言原文，并把确认限定到实际版本与范围；文件绑定成功、偏好某版或技术通过都不会自动成为最终内容验收。
