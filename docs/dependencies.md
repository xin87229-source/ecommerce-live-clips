# 首次安装与本地依赖

首版试用范围为 **Windows x64、Python 3.12、中文、单场单款、女装电商运营**，使用完整项目与其中的 Skill。`basic` 功能继续保留，非女装验证延后。当前已验证与未验证的状态只看 [release-status.md](release-status.md)；本文是安装办法，不是新电脑安装成功回执。

## 1. 取得完整项目

从公开仓库 [xin87229-source/ecommerce-live-clips](https://github.com/xin87229-source/ecommerce-live-clips) 选择 **Code → Download ZIP**，或[下载 main 完整源码 ZIP](https://github.com/xin87229-source/ecommerce-live-clips/archive/refs/heads/main.zip)。也可以使用维护者提供、已核对版本和清单的完整候选包。解压到自己有写入权限的新目录，保留 `.agents` 隐藏目录；不能只下载 `SKILL.md`，也不能把私人迁移包当试用包。

GitHub 的 main ZIP 随源码更新，不是固定版本的 GitHub Release。首次试用先记录仓库提交号与 `VERSION`；维护者提供的候选包还应核对其 `SOURCE-MANIFEST.json` 和 ZIP SHA-256。下载后打开包含 `cli.py` 的实际目录：GitHub ZIP 通常多包一层 `ecommerce-live-clips-main`，不要把其上一级空目录当安装目录。当前正式发行与验证进度见 [验证状态](release-status.md)。

至少确认以下文件存在：`cli.py`、`requirements.txt`、`.agents/skills/ecommerce-live-clips/SKILL.md`、`pipeline/`、`presets/`、`docs/`、`tests/` 和 `THIRD_PARTY_NOTICES.md`。有候选清单与 SHA-256 时先核对；解压后源码目录、原片目录、商家工作区分别保存。候选打包方法见 [发布准备](release-preparation.md)。

本文使用的路径是示例，可整体换成另一块盘：

```text
D:\eclip\project\             完整项目源码
D:\eclip\envs\trial-01\       本次新建虚拟环境
D:\eclip\tools\ffmpeg\bin\   自行取得的 FFmpeg/FFprobe
D:\eclip\models\small\       自行下载的本地模型
D:\eclip\fonts\              自行取得的中文字体
D:\eclip\source\             自己有使用权的原片与商品参考
D:\eclip\workspaces\shop-a\   商家配置、缓存、听审记录和产物
```

## 2. 安装 Python 并新建独立虚拟环境

若已经有正常的 64 位 Python 3.12，直接核对版本。否则从 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/) 取得 Python install manager，按官方界面安装，再在 PowerShell 中执行下面的安装命令。已有旧 `py` 启动器时，`pymanager` 可避免名称冲突。安装管理器和运行时属于使用者主动安装的步骤，项目不会静默安装。[官方 Windows 说明](https://docs.python.org/3/using/windows.html)

```powershell
# 仅对本次会话及其 Python 子进程生效，避免中文 Windows 的 GBK 输出被按 UTF-8 读取
$env:PYTHONUTF8 = '1'

# 仅在已安装官方 Python install manager、且尚无 Python 3.12 时执行
pymanager install 3.12

# 核对输出为 Python 3.12、64 位；不是就先停止
py -3.12 -c "import sys,struct,platform; print(sys.version); print(struct.calcsize('P')*8, platform.machine())"

$Install = 'D:\eclip\project'
$Venv = 'D:\eclip\envs\trial-01'
if (Test-Path -LiteralPath $Venv) { throw '请选择尚不存在的虚拟环境目录，保留原环境' }
py -3.12 -m venv "$Venv"
if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败' }
$Python = Join-Path $Venv 'Scripts\python.exe'
& $Python -m pip install -r "$Install\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请保留完整错误，不继续制作' }
& $Python -m pip check
& $Python -c "import PIL,faster_whisper,ctranslate2,av,onnxruntime; print('依赖导入完成')"
```

依赖安装会连接软件包源。使用绝对路径调用 `$Python`，无需激活脚本、修改 PowerShell 执行策略或系统 PATH。若 `py` 不可用，把上面两个 `py -3.12` 换成 `& '实际Python3.12的python.exe绝对路径'`。不要使用全局 `pip`，也不要把另一台机器的 `venv` 复制过来冒充安装完成；虚拟环境应在目标位置重建。[Python venv 文档](https://docs.python.org/3.12/library/venv.html)

截至 2026-10-16，本机记录为 Python 3.12.14，以下 8 项与 `requirements.txt` 一致：

| 依赖 | 版本 |
| --- | --- |
| faster-whisper | 1.2.1 |
| ctranslate2 | 4.8.2 |
| av | 18.1.0 |
| onnxruntime | 1.29.0 |
| tokenizers | 0.23.2 |
| huggingface-hub | 1.30.0 |
| numpy | 2.5.3 |
| Pillow | 12.3.0 |

这份清单是本机版本基线，未锁定全部传递依赖和下载文件哈希。尤其本机 faster-whisper 来自固定提交 `ed9a06cd89a93e47838f564998a6c09b655d7f43`；只看到 PyPI 安装后的相同版本号，不能证明字节相同。外部安装结果以自己保存的 `pip freeze`、导入检查和短素材实测为准；任何版本不可取得时，记录失败，不自行删掉版本限制后声称复现成功。

2026-10-18又在同一电脑新建了空虚拟环境，从软件包源按该requirements实际安装，并通过依赖检查、导入、中文媒体制作、本地模型加载和20秒真实原窗转写。该次使用PyPI发行版的faster-whisper，未复制旧虚拟环境。FFmpeg、模型文件和系统字体仍复用本机资源，因此不能当作新电脑安装或全部资源重新下载成功。

2026-10-28 从固定公开源码ZIP再次新建独立环境，按全部固定版本安装并通过依赖检查、官方模型/字体重新下载、离线加载、技术样例和真实窗口转写。FFmpeg及基础Python仍复用本机资源，不算新电脑；本条素材的内容核词和正式交付仍待审。实际记录见[公开包试做](tasks/2026-10-28-public-package-trial.md)。

## 3. 取得 FFmpeg 与 FFprobe

从 [FFmpeg 官方下载页](https://ffmpeg.org/download.html) 的 Windows EXE Files 进入其列出的 Windows 构建提供方，下载 x64 构建并解压。FFmpeg 官方提供源码，Windows 可执行文件由所列第三方构建方提供；不要把构建方称为 FFmpeg 官方发行版。保留下载来源、版本和构建方提供的校验值。

在解压目录找到 `bin\ffmpeg.exe` 与 `bin\ffprobe.exe`，按真实位置赋值：

```powershell
$FFmpeg = 'D:\eclip\tools\ffmpeg\bin\ffmpeg.exe'
$FFprobe = 'D:\eclip\tools\ffmpeg\bin\ffprobe.exe'
& $FFmpeg -version
& $FFprobe -version
& $FFmpeg -hide_banner -encoders 2>&1 | Select-String 'libx264|aac'
& $FFmpeg -hide_banner -filters 2>&1 | Select-String '\bass\b|\bsubtitles\b'
```

需要 `libx264`、AAC 和 ASS 字幕渲染能力；列表出现名称后仍需实际输出验证。本机记录为 `9.0.1-essentials_build-www.gyan.dev`，不是对其他构建的兼容承诺。`doctor` 只运行版本检查，不检查全部编码器和滤镜。首版由本地 CPU 转写，不需要为此安装 CUDA。

## 4. 单独下载本地 faster-whisper 模型

使用 [Systran/faster-whisper-small 的固定版本](https://huggingface.co/Systran/faster-whisper-small/tree/536b0662742c02347bc0e980a01041f333bce120)。它是多语言 small 模型，可用于中文；不要换成英文专用 `.en` 模型，或把其他格式的 Whisper 权重目录填入配置。

下列是**主动联网下载步骤**，约 486 MB，尚未包括 Python 依赖和临时空间。使用刚安装好的虚拟环境；若网络不可达，保留错误并通过同一官方模型页手动下载，不在报错后暗中切换不明镜像。下载公开模型不需要把店铺或 AI 账号凭据写入项目。[Hugging Face 下载文档](https://huggingface.co/docs/huggingface_hub/guides/download)

```powershell
$Model = 'D:\eclip\models\small'
& $Python -c "import sys; from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-small', revision='536b0662742c02347bc0e980a01041f333bce120', local_dir=sys.argv[1], allow_patterns=['model.bin','config.json','tokenizer.json','vocabulary.txt','README.md'])" "$Model"
if ($LASTEXITCODE -ne 0) { throw '模型下载未完成' }
Get-ChildItem -LiteralPath $Model
& $Python -c "import sys; from faster_whisper import WhisperModel; WhisperModel(sys.argv[1],device='cpu',compute_type='int8',cpu_threads=6,local_files_only=True); print('本地模型加载完成')" "$Model"
```

目录中应有 `model.bin`、`config.json`、`tokenizer.json`、`vocabulary.txt`。手动下载要取得真实权重，不能仅保存网页或 Git LFS 指针。项目调用时使用本地路径及 `local_files_only=True`，不会因模型缺失自动下载。[faster-whisper 官方用法](https://github.com/SYSTRAN/faster-whisper)

模型加载成功不等于实际转写成功，更不等于识别准确。首次短窗口仍需运行 `window transcribe-preview`，并实际回听机器结果。

## 5. 取得中文字体

可以使用本机已合法安装、允许该用途的中文字体并填写其绝对文件路径；不要把系统字体复制到公开包。本机历史验证使用过微软雅黑，不代表它获准随项目再分发。

没有合适字体时，可从 [Noto CJK 官方字体仓库](https://github.com/notofonts/noto-cjk) 的 [Sans 下载说明](https://github.com/notofonts/noto-cjk/blob/main/Sans/README.md) 取得简体中文静态 OTF，解压到字体目录，例如 `NotoSansCJKsc-Regular.otf`。保留其 [SIL Open Font License 1.1](https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE)。这是可获取的替代字体，不代表本项目已验证该字体在所有机器上的渲染结果。

可复现的已测文件：[NotoSansCJKsc-Regular.otf](https://raw.githubusercontent.com/notofonts/noto-cjk/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf)，来自官方仓库固定提交 `f8d157532fbfaeda587e826d4cd5b21a49186f7c`；同时保存该提交的[许可文件](https://raw.githubusercontent.com/notofonts/noto-cjk/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/LICENSE)。字体 SHA-256 为 `2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b`，大小 16,437,364 字节。下载到自己的字体目录即可，无需系统安装；程序仍不自动下载。

```powershell
$Font = 'D:\eclip\fonts\NotoSansCJKsc-Regular.otf'
Get-FileHash -LiteralPath $Font -Algorithm SHA256
& $Python -c "import sys; from PIL import ImageFont; f=ImageFont.truetype(sys.argv[1],42); print(f.getname())" "$Font"
```

这只验证字体可读取。还要在短片中看字幕是否为汉字、是否缺字或遮挡商品；标题、价格、尺码等敏感内容必须核对原话。项目不自动安装字体到系统。

2026-10-20 已在同一 Windows 电脑的新工作区用上述文件实际生成4秒中文技术样例，检查重点词66→92字号、黄色和加粗效果；日志确认普通与加粗均使用Noto，Regular字体的加粗由渲染器合成。另用此字体通过13项交付测试，包含合成素材完整制作。复用既有清洁虚拟环境与FFmpeg，未重新安装模型；不算新电脑安装、真实口播核词、所有字形或商家素材版式验收。更换字体后应重新检查本次字幕换行及商品遮挡。

## 6. 建工作区、检查并试跑

继续 [快速开始](quickstart.md)。`config.json` 中的字段与资源对应如下，配置留在商家工作区，不提交到源码目录：

| 配置字段 | 内容 |
| --- | --- |
| `ffmpeg_path`、`ffprobe_path` | 上面两个现有程序的绝对路径 |
| `font_path` | 现有中文字体文件绝对路径 |
| `model_path` | 含模型文件的目录绝对路径 |
| `source_root` | 原片范围目录，与工作区互不包含 |

`doctor` 通过后仍须完成模型转写、中文字幕、完整解码和真实听看。转写及媒体程序在本地运行；Codex 或其他 AI 宿主如何处理发给它的文字、图像和文件，取决于该宿主设置，本文不承诺整个助手离线。原片或业务资料发往任何外部服务前，应按当次授权范围处理。

可以先运行 [四秒技术样例](../examples/README.md)，检查新环境的图案/测试音生成、中文烧录、真实帧提取和媒体技术规格；可选 `--load-model` 只验证本地模型初始化。它没有真人口播，不验证 ASR 准确性或女装业务。缺少可公开真实素材时，按同页的拍摄与授权规格另行准备。

开发者检查可运行 `& $Python -B -m unittest discover -s "$Install\tests" -v`。它不替代短素材实测或外部用户试用。恢复与常见错误见 [快速开始末尾](quickstart.md#5-接续恢复与排错)。
