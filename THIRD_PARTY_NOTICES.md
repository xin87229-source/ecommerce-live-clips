# Third-party notices for ecommerce-live-clips

This file records third-party provenance. Original project code and documentation are licensed under [MIT](LICENSE), copyright (c) 2026 xin87229-source. Third-party notices below remain applicable; the project license does not relicense external media or runtime resources. See [release preparation](docs/release-preparation.md) for the exact distribution scope.

The intended source candidate includes the modified FFmpeg Skill subset described below. Python packages, model weights, FFmpeg/FFprobe executables, Python runtimes, virtual environments and fonts are installed or obtained separately and are not included in that candidate. If the distribution model changes, review the licenses and notices for the exact versions and binary builds being redistributed first.

## faster-whisper for V0.2a

- Repository: https://github.com/SYSTRAN/faster-whisper
- Current clean-environment test: version 1.2.1 installed from the package index using `requirements.txt`. The earlier local environment recorded installation from commit `ed9a06cd89a93e47838f564998a6c09b655d7f43`. Matching version strings do not establish byte identity between those installations; keep their installation evidence separate.
- License: MIT, copyright (c) 2023 SYSTRAN. The MIT permission/warranty terms reproduced below also apply to this copyright notice; the installed distribution retains its original LICENSE.
- Purpose: unmodified library API for local Chinese transcription, segment timestamps and built-in VAD restoration. No upstream CLI or cloud transcription service.
- Model: `Systran/faster-whisper-small`, pinned revision `536b0662742c02347bc0e980a01041f333bce120`, downloaded only into ignored local storage; weights are not redistributed.
- Direct runtime versions are pinned in `requirements.txt`; complete installed dependency inventory stays in private test evidence.
- AI-Youtube-Shorts-Generator: architecture inspiration only; no source code or prompt copied.

## FFmpeg Skill subset for V0.1

- Repository: https://github.com/kajisho5/ffmpeg-skill
- Pinned commit: `c3a4a5f0ef6629018308a7eaeb5a1f592e5defc1`
- License: MIT. Rechecked the [LICENSE at the pinned commit](https://github.com/kajisho5/ffmpeg-skill/blob/c3a4a5f0ef6629018308a7eaeb5a1f592e5defc1/LICENSE) on 2026-10-17 through the GitHub file API; blob SHA `6d76d5f6cce41e74b6cebdbf013faddf3abb1afe`. The complete notice and permission text are retained below.
- Integration: `pipeline/ffmpeg_subset.py` is a **modified, consolidated subset**, not the original upstream scripts or a full installation. Reviewed source: `scripts/probe.py`, its `_common.py::probe`, `scripts/cut.py`, `scripts/fit.py`, `scripts/check.py`, `scripts/look.py` at the pinned commit.
- Retained: selected probe fields, accurate seek/re-encoding, pad/crop/setsar filter construction, duration/aspect/codec/audio checks, three no-timecode frames.
- Local changes: fixed functions instead of upstream CLI/global helpers; one encode with decoded concat; strict input/output validation; no overwrite (`-n`); strict original-audio gate; full decode check; reject unsupported HDR/multitrack/non-square pixels; explicit full/limited-range SDR conversion for delivery; no font dependency. The reviewed cut.py clips overlong end times silently; this adapter rejects them. Original CLI tools are not exposed.
- Not included: `_common.py` as a whole, original CLI files, caption, export, batch, arbitrary tool paths, platform presets/loudness, speed changes, automatic downloads/updates, publishing or browser tools. Python standard library only; FFmpeg/FFprobe are separate local executables, not bundled or redistributed by this project.
- Updating requires a deliberate source review and rerunning the relevant tests under `tests/` plus real media validation; runtime never contacts upstream.

## Separately obtained runtime resources

- The dependency version baseline is [requirements.txt](requirements.txt); installation, sources and the distinction between a version match and a verified installation are documented in [dependencies.md](docs/dependencies.md). Installed packages retain their upstream license files. This notice is not a complete license inventory for a bundled Python environment; no such environment is distributed here.
- FFmpeg and FFprobe: [official source and download page](https://ffmpeg.org/download.html), [license explanation](https://ffmpeg.org/legal.html). Applicable terms depend on the actual build, including enabled GPL components; do not assume every executable is LGPL-only. The project's license does not relicense these executables.
- Model weights: [Systran/faster-whisper-small at the recorded revision](https://huggingface.co/Systran/faster-whisper-small/tree/536b0662742c02347bc0e980a01041f333bce120), whose model card declares MIT. The weights are not included; users obtain their own copy and retain the upstream terms.
- Fonts: no font is included. A system font's presence is not permission to redistribute it. The installation guide offers [Noto CJK](https://github.com/notofonts/noto-cjk) as an independently obtained alternative under its [SIL Open Font License 1.1](https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE). The example does not claim every font or font version has been rendered successfully in this project.
- Workspace render intermediates may contain copied font files. Workspaces, intermediate outputs and private media must not be included in the source candidate or repackaged as a public runtime bundle.

## Upstream MIT license

MIT License

Copyright (c) 2026 kajisho5

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Optional local audio second opinion (rc.17)

- Official repository: https://github.com/QwenAudio/Fun-ASR . Windows CPU runtime tag `runtime-llamacpp-v0.2.1`; exact executable/archive hashes and model revision are in [audio recheck](docs/audio-recheck.md). The runtime uses llama.cpp/ggml; upstream license notices remain applicable.
- Official model: `FunAudioLLM/Fun-ASR-Nano-GGUF`, revision `46e849502a867080d66d351b8dfb1018b607e509`; model card declares Apache-2.0. This is a model-specific statement, not a license claim for other ASR weights.
- No upstream code, executable, model, audio, transcript or environment is redistributed. Our standard-library adapter calls the separately obtained pinned local executable with local files only. No TypeSafe/Jev, cloud API dependency or automatic paid fallback.
