# 快速开始：从原片到本地待审包

首轮面向会使用 Codex 的女装电商运营：Windows、Python 3.12、中文、一场直播中的一个商品，使用完整仓库内的 Skill。首次下载、独立虚拟环境、FFmpeg、模型和字体准备请先完成 [依赖安装](dependencies.md)，然后回到本文。下列路径和 ID 是示意；使用时换成自己机器上的真实绝对路径。命令在 Windows PowerShell 中运行，`$Python` 始终指向本次独立虚拟环境。原片与商品参考保持只读，`<工作目录>` 必须与原素材目录彼此独立。程序只会验证记录与文件绑定，商品同款、口播含义、字幕事实和连续听看须由有能力的操作者实际判断。

日常使用时，运营只需告诉 AI 素材位置、商品参考和目标；下列命令与 JSON 由 AI 执行和填写。若 AI 没有听审能力，应提供可播放的审查片和疑词，交由实际听过的人反馈，再如实记录审核人和反馈来源。不能让运营只回复一个“通过”，却未看到对应版本的材料。

首次环境配置后，日常优先走 [一次委托制作与接续](one-click.md)。下文保留各阶段的详细操作供 AI 查阅与恢复，不要求运营逐条执行。女装类别和风格适配见 [商品适配](apparel-editing.md)，使用真实观察选择剪法。

## 0. 给 AI 的最小任务说明

在 Codex 中打开完整项目，提供下面四项即可。日常工作不要求运营手写 JSON。

> 请读取本项目 .agents/skills/ecommerce-live-clips/SKILL.md。我使用 Windows，原片位于【真实原片路径】，目标女装是【商品名称】，参考图位于【真实图片路径】，独立工作区使用【真实目录】。先检查环境，用原片中的一个 30–60 秒短窗口做 test。请给我真实粗剪、原速核词片段和明确时间点；机器识别不确定的词保留待审。不要把技术检查当成人审，也不要上传或发布。

30–60 秒是建议试跑长度，不是成片长度。首版 `apparel` 的已确认同款原片区间并集至少 20 秒，表达仍需完整；不要为满足数字误认同款。找不到足够有效材料时可以得到零条。首轮只检索局部就保持 `--scope test`，不能声称已盘点整场。

## 1. 选工作目录并检查环境

```powershell
# 每个新 PowerShell 会话设置一次；不修改系统编码或全局环境
$env:PYTHONUTF8 = '1'
$Install = 'D:\eclip\project'
$Python = 'D:\eclip\envs\trial-01\Scripts\python.exe'
$Workspace = 'D:\eclip\workspaces\shop-a'
$FFmpeg = 'D:\eclip\tools\ffmpeg\bin\ffmpeg.exe'
$FFprobe = 'D:\eclip\tools\ffmpeg\bin\ffprobe.exe'
$Font = 'D:\eclip\fonts\NotoSansCJKsc-Regular.otf'
$Model = 'D:\eclip\models\small'
$SourceRoot = 'D:\eclip\source'
& $Python "$Install/cli.py" --workspace "$Workspace" init --profile apparel --ffmpeg "$FFmpeg" --ffprobe "$FFprobe" --font "$Font" --model "$Model" --source-root "$SourceRoot"
& $Python "$Install/cli.py" --workspace "$Workspace" doctor
```

`init` 只写一次 `config.json`，不会覆盖已有配置。已有工作目录可直接 `doctor`。本例明确使用 `apparel`；`basic` 仍可用，但不计作首轮女装试用之外的品类验证。`doctor` 不安装工具、不下载模型，也不证明模型真实转写或成片听看。依赖和版本见 [依赖说明](dependencies.md)。

若任一步返回错误就先停止后续命令。`doctor` 返回 `ready: true` 仅说明已配置基础资源；保存输出和安装耗时。先用自己的真实短素材完成第 2 步的抽窗与转写，再进入业务制作，不用整场长片测试依赖。

没有业务素材也可以先跑四秒 [技术样例](../examples/README.md)，它自行生成图案和测试音，不动业务任务卡：

```powershell
& $Python "$Install/tools/smoke_test.py" --workspace "$Workspace" --name install-01 --load-model
```

查看返回的 `report.json` 和 `video.mp4`。示例里的“技术测试 / 不是直播原话”是固定标签；模型加载成功也不证明中文转写准确。已存在同名输出会拒绝覆盖，重试用新名称。接下来仍须用自己的真实素材完成以下步骤。

## 2. 建立原片任务，实际审核同款窗口

```powershell
& $Python "$Install/cli.py" --workspace "$Workspace" new-task --source '<原直播视频>' --product-name '示例商品' --reference '<同款实物参考图>' --task-id example-01 --scope test
& $Python "$Install/cli.py" --workspace "$Workspace" window inspect --task-record "$Workspace/outputs/example-01/run-state.json"
# 示例为原片 0–30 秒；必须改成已查看的真实范围，且不超出原片
$WindowStart = 0.0
$WindowEnd = 30.0
& $Python "$Install/cli.py" --workspace "$Workspace" window extract --task-record "$Workspace/outputs/example-01/run-state.json" --start $WindowStart --end $WindowEnd
```

`inspect` 的抽帧只是线索，不是同款结论。先看商品参考及目标区间画面、听原声，再按原片真实帧时间选窗口。`extract` 会返回 `extraction.json`；逐张看其三张 `sample_frames`，完整听 `audio_review`，在 `$Workspace/private-context` 写与这些路径、哈希、原时间相符的审核 JSON。结构见 [操作参考：原片与同款窗口](pipeline-reference.md#1-原片与同款窗口)。没有实际能力或发现错款时保持 `unknown`，不要填写 `matched`。

若已配置本地模型，且本次抽取窗口实际时长不超过 120 秒，可以先看**未审核 ASR 预览**辅助定位口播：

```powershell
& $Python "$Install/cli.py" --workspace "$Workspace" window transcribe-preview --extraction '<本次extraction.json绝对路径>'
```

预览返回 `preview_path` 与原片时间轴对应的机器转写片段；文件仍标记 `same_product=unknown`、`audio_listening=not_performed`、`transcript_verified=false`。它不更新任务卡，也不能替代亲自听审或给错字提供纠错证据。模型缺失、窗口超过 120 秒或缺少听审能力时，不要借这个入口填写 `matched`。

需要运营听审时，可用 `review-card` 把本次窗口、机器转写、拟用顺序和疑词生成一张本地审核卡，操作见[人工听审卡](human-review.md)。运营可点时间回听、填写实际听到的原话，再把反馈复制给 AI；不需要编辑 JSON。播放过视频不代表已审核，页面不会自动把任务改为通过。收到真实反馈后，AI 保存用户原话并只记录已确认的范围。

需要先比较选段、顺序或速度时，可用独立的 `review-cut` 从本次原片窗口生成待审预览，再放入听审卡，见[待审粗剪方法](human-review.md#从原片生成独立待审粗剪)。它不改原任务卡、不烧录未获准字幕，也不能直接作为正式交付包。

卡片生成后，用同一个独立 Python 环境核对绑定并启动本机审核页；把示例中的卡片路径换成本次实际输出：

```powershell
$Card = Join-Path $Workspace 'outputs\reviews\card-01\index.html'
& $Python "$Install/cli.py" --workspace "$Workspace" review-card check --card "$Card"
if ($LASTEXITCODE -ne 0) { throw '卡片需要重新审核，请先处理绑定问题' }
& $Python "$Install/tools/serve_review.py" --card "$Card" --port 8765
```

在本机浏览器打开输出的 `http://127.0.0.1:8765/index.html`，保留 PowerShell 窗口以便播放和跳转；结束后按 `Ctrl+C` 停止服务，再继续后续命令。端口占用时改为另一个空闲端口并使用新地址。该工具只读提供本卡同目录文件，不列目录、不访问子目录、不上传或保存反馈；它不替代前面的绑定检查。反馈关闭前复制回对话，具体方法见[审核页与反馈](human-review.md#在浏览器中打开审核页)。

```powershell
& $Python "$Install/cli.py" --workspace "$Workspace" window create-review --task-record "$Workspace/outputs/example-01/run-state.json" --review '<实际审核JSON>'
& $Python "$Install/cli.py" --workspace "$Workspace" quality check-task --task-record "$Workspace/outputs/example-01/run-state.json"
```

返回的 `window_tasks` 给出窗口视频、独立子任务卡和精确裁切映射。多个同款窗口应先逐一抽取并听看，再把全部已确认窗口写进**同一份**审核 JSON，调用一次 `create-review`；重叠窗口必须先解决。该命令创建子任务卡，不需要人工改写原任务卡。

## 3. 建材料池和批次草稿

对每个窗口，使用其视频所在目录和相应子任务卡；读取转写块，按真实 segment 逐块提交候选，最后保留完整候选池：

```powershell
& $Python "$Install/cli.py" --workspace "$Workspace" suggest prepare --source-dir '<窗口视频所在目录>' --task-record '<窗口子任务卡>'
& $Python "$Install/cli.py" --workspace "$Workspace" suggest block --job '<返回的窗口job目录>' --block '<真实块ID>' --task-record '<窗口子任务卡>'
& $Python "$Install/cli.py" --workspace "$Workspace" suggest submit --job '<窗口job目录>' --block '<真实块ID>' --selection '<private-context中的选择JSON>' --task-record '<窗口子任务卡>'
& $Python "$Install/cli.py" --workspace "$Workspace" suggest merge --job '<窗口job目录>' --task-record '<窗口子任务卡>'
& $Python "$Install/cli.py" --workspace "$Workspace" suggest finish --job '<窗口job目录>' --ranking '<private-context中的排名JSON>' --task-record '<窗口子任务卡>'
```

`block`/`submit` 对每个真实块重复；选择与排名 JSON 的字段见 [操作参考：候选](pipeline-reference.md#2-分窗转写候选与合并)。不要为未听到的数字、价格或商品功效填词。将父任务卡中**全部已确认窗口**的真实 `job_path` 和 `create-review` 返回的 `import_entry.mapping` 写入导入说明，再合并到原直播时间轴：

```powershell
& $Python "$Install/cli.py" --workspace "$Workspace" import merge-jobs --spec '<private-context中的导入说明JSON>' --output "$Workspace/private-context/selector-v02a/example-merged"
& $Python "$Install/cli.py" --workspace "$Workspace" workflow draft --job "$Workspace/private-context/selector-v02a/example-merged" --task-record "$Workspace/outputs/example-01/run-state.json" --batch-id example-batch --output "$Workspace/private-context/example-batch.json"
```

这个草稿的 `candidate_inventory` 来自真实候选，但 `items`、排除理由、主线差异和 `why_not_more` 仍为空；它不等于可采用方案。此示例的 `--scope test` 专用于局部窗口试作。合并窗口 job 的 ASR 覆盖范围是 `reviewed_windows_only`，不能声称整场已盘点或已得出最终最大条数。要做整场 `production` 方案，须有独立、真实、绑定原片的人工全场 `full_review` 记录；已确认同款窗口须全部转写，不要求给非同款整场做 ASR。详见[操作参考：全场盘点证据](pipeline-reference.md#3-批次方案与采用)。

## 4. 采用、制作、审查与交付

根据真实候选补全方案后，按 [操作参考：批次方案与采用](pipeline-reference.md#3-批次方案与采用) 执行 `batch validate`、`pair-hashes`、`report`、`adopt` 和 `workflow bind`。一条已采用方案对应一张独立子任务卡；不可把父任务卡、其他条目的证据或旧采用指纹套给它。

然后依次运行 `combine`、`subtitle prepare/preflight/render`、`package prepare/preflight/finish`，最后以 `quality audit` 和 `quality check-package` 检查当前成品。`package prepare` 必须取得字幕阶段真实生成的 `subtitle.srt`，有 `subtitle.ass` 时也随包交付；两者均按字节哈希绑定，不需额外字幕参数。每一步的确切命令与输入 JSON 在 [制作链操作参考](pipeline-reference.md#4-组合字幕与交付)；`status` 可查看本工作目录任务，不会自动替你恢复 `UNKNOWN` 状态。

字幕按原声时间显示：单个完整转写句可保留到 5 秒，多个转写句合并仍限 3.2 秒。不会为满足默认字幕时长而强制加速或改写原片时间；过长或不易读的句子仍需根据真实听感和画面调整。

交付包只是本地文件。质量记录中的技术通过，不能替代连续观看、全片原声试听、商品同款判断或用户确认。证据缺失时应保留待审状态；首版不会上传、挂车或提交平台。批次本地收尾用 `records local-close` 读取当前包和任务卡，详见[操作参考：本地收尾](pipeline-reference.md#5-分项-qa-与交付边界)。



## 5. 接续、恢复与排错

关闭对话不等于任务丢失。重新打开同一个完整项目，恢复 `$Install`、`$Python`、`$Workspace` 三个真实路径，再读已有状态：

```powershell
$env:PYTHONUTF8 = '1'
& $Python "$Install/cli.py" --workspace "$Workspace" status
& $Python "$Install/cli.py" --workspace "$Workspace" doctor
```

让 AI 读取对应 `run-state.json`、本次产物和真实反馈，从最后成功阶段接续。不要再次 `init` 覆盖旧配置，不要通过改任务号回避 `UNKNOWN`。源文件、选段、顺序、速度或字幕变动后，重新核对受影响证据，修订使用新输出目录。

| 表现 | 下一步 |
| --- | --- |
| 找不到 Python，或依赖装了仍提示缺包 | 用 `& $Python -c "import sys; print(sys.executable)"` 核对实际解释器；再对同一个 `$Python` 执行 `-m pip check`。 |
| 安装报没有对应版本/轮子或网络错误 | 保存完整错误、Python 位数和版本；核对 [版本基线](dependencies.md)。不混装全局包，不取消版本限制后宣称复现。 |
| `doctor` 中 FFmpeg/字体/模型失败 | 检查工作区 `config.json` 的绝对路径与文件存在性。`init` 不覆盖配置，备份后只改错误资源路径，再检查。 |
| 模型文件存在但加载失败 | 检查是否下成网页/LFS 指针、是否缺文件及模型格式；按固定模型 revision 重新下载到新目录。保留失败记录。 |
| `DLL load failed` | 先确认 x64 Python、完整错误及失败库。按该库官方要求核对 Windows 运行库；不从下载站找单个 DLL，不先安装 GPU 套件。 |
| 字幕是方框、中文缺字 | 检查配置字体，再生成短片看真实显示。可换有许可的中文字体文件；读取字体成功不等于中文字形已验证。 |
| 原片与工作区互相包含 | 重新选择相互独立的源目录与工作区；不要把原片放进 `outputs`。 |
| 错款、听不清或字幕门禁拒绝 | 回看参考和原速片段，把不确定处交给实际听看的人。不能把 `unknown` 改成 `matched` 来继续。 |
| 输出目录已存在 | 读取旧结果；修订换新目录并保留前版。失败产生的目录也不直接覆盖。 |
| 输入为 HDR/多音轨/非方形像素等不支持媒体 | 记录真实媒体信息；在另一个文件中做明确转换并另行验收，原片保留。不会转换就停止该素材，不伪改标签。 |
| 听审页不能跳转或反馈没保存 | 先确认 `serve_review.py` 仍在运行，并从它输出的本机地址打开；仍不能播放时用本地播放器回听并保留准确时间点。页内反馈关闭前复制给 AI，播放次数不等于审核记录。 |
| 临时目录权限失败 | 为本次 PowerShell 进程选择工作区内的临时目录；不关闭权限保护，也不改系统配置。 |

仅在确有临时目录错误时使用：

```powershell
$TaskTemp = Join-Path $Workspace 'tmp'
New-Item -ItemType Directory -Path $TaskTemp -Force | Out-Null
$env:TEMP = $TaskTemp
$env:TMP = $TaskTemp
```

换电脑时重新建虚拟环境和配置资源路径，保留原片及工作区的备份；历史记录里的绝对路径和哈希不能批量替换。需要迁移已有任务时先核对映射，不能把新路径视为旧人审自动通过。

## 6. 首次短素材的完成标准

逐项写真实结果，缺一项就注明待完成：

- 环境：独立安装、`pip check`、导入检查、模型本地加载和 `doctor` 结果可回查。
- 短窗：从原片新抽取，来源与映射明确；本地转写可运行，机器词和人工核词分开。
- 审核：真实同款判断、原速核词及最终速度听感分别记录；得到真实可播放粗剪和时间点清晰的听审材料。
- 输出：完成实际字幕/封面检查、完整解码、30 fps CFR 检查、连续观看和全片试听；未审项保留待审，本地包不等于可发布成片。
- 接续：关掉当前对话后能用 `status` 找回任务；后续新素材建立新任务，不修改源码或复用旧素材审核结论。

合成技术样例只能验证工具、字幕和文件产出，不能代替真实女装素材、同款判断或业务验收。外部三人试用的记录和通过条件见 [外部试用](external-trial.md)。项目公开状态以 [release-status.md](release-status.md) 为准。
