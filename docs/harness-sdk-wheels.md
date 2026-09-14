# The pinned DeepSeek Harness wheels

`requirements.txt` pins two DeepSeek Harness packages:

```
deepseek-harness-sdk==0.1.2a5
deepseek-harness-runtime-bin==0.1.2a5
```

**Neither version exists on PyPI.** PyPI's 0.1.2 line starts at `0.1.2a3`, so a plain `pip install -r requirements.txt` cannot resolve them. They are published on this repository's [harness-sdk-0.1.2a5 release](https://github.com/nuaaweixinye/gme-agent/releases/tag/harness-sdk-0.1.2a5), and `scripts\install.ps1` installs them before running pip.

## Installing by hand

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install `
  "https://github.com/nuaaweixinye/gme-agent/releases/download/harness-sdk-0.1.2a5/deepseek_harness_sdk-0.1.2a5-py3-none-any.whl" `
  "https://github.com/nuaaweixinye/gme-agent/releases/download/harness-sdk-0.1.2a5/deepseek_harness_runtime_bin-0.1.2a5-py3-none-win_amd64.whl"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the direct download is blocked, `gh release download harness-sdk-0.1.2a5 --repo nuaaweixinye/gme-agent --dir .wheels` fetches the same files (the installer falls back to it automatically).

## Why not a published version

The backend resumes an existing coding session within one task, and only this build's API offers that:

| SDK version | `DeepSeekHarness.run()` | Resuming an existing session |
|---|---|---|
| `0.1.2a5` (this release) | `…, session_id=None, resume_if_exists=False, on_notification=None` | works |
| `0.1.2a3`, `0.1.2rc1`, `0.1.5rc1` (PyPI) | `…, session_id=None, on_notification=None` — no `resume_if_exists` | `0.1.2rc1` ends with `finish reason 'error'`; `0.1.5rc1` raises `session "…" already exists` |

Measured by running `tests\test_harness_runner_e2e.py` against each version, so the pin stays until a published SDK supports resuming.

## Provenance, licence and platform

- Built from the DeepSeek Harness source at `0.1.2-alpha.5` (`python/sdk`, `python/sdk-runtime`), branch `hotfix-windows-acl-temp-access`, commit `e05fca2`, published at <https://github.com/nuaaweixinye/GME-ds>.
- Upstream project: <https://github.com/deepseek-ai/deepseek-harness>, MIT licensed (© 2026 DeepSeek). This repository only redistributes the built wheels.
- `deepseek_harness_runtime_bin` is a `win_amd64` package: **Windows only**. `deepseek_harness_sdk` is pure Python.
