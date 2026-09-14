# GME Test Agent

GME Test Agent 是一个本地测试工作流工具，用于选择 GME 接口、生成 GME 与 ACIS 对比测试、构建和运行测试、审查失败，并提交选中的测试 PR。工具同时提供基于失败测试的 GME 缺陷修复工作流。

编码任务由 DeepSeek Harness 执行：Python 后端为每个任务在独立 worktree 中启动一个隔离的 `sdk` profile SDK runtime（workspace-write 沙箱、禁用交互审批），会话持久化并支持跨进程恢复；文件改动、manifest、构建、测试和 PR 仍由 Python 后端权威校验与交付。

## 使用方式

- [源码运行使用说明](docs/源码运行使用说明.md)
- [桌面版使用说明](docs/gme-test-agent使用说明.md)

**快速开始（Windows）—— 一条命令完成环境准备：**

```powershell
git clone https://github.com/nuaaweixinye/gme-agent.git
cd gme-agent
scripts\install.ps1 -GmeRepo D:\GME     # 自检工具链 → 建 .venv → 装依赖 → 写 config.local.json → 打印后续步骤
scripts\run_web.ps1                     # 启动后端
```

`install.ps1` 会检查 Python/Node/git/gh、创建 `.venv`、安装两个**不在 PyPI 上**的 DeepSeek Harness wheel（来自本仓库 [harness-sdk-0.1.2a5 release](https://github.com/nuaaweixinye/gme-agent/releases/tag/harness-sdk-0.1.2a5)，原因见 [docs/harness-sdk-wheels.md](docs/harness-sdk-wheels.md)）、写入 `config.local.json`、做一次安装校验，并在最后打印把 DeepSeek Harness 插件指向这份检出的两种做法（环境变量或 profile 补丁）。加 `-SkipFrontend` 可跳过前端依赖。

自测：`scripts\run_tests.ps1`（未生成接口目录时会输出 `OK (skipped=7)`）。

手动安装（不用脚本）时：

激活任意兼容的 Python 环境后执行：

```powershell
scripts\setup_source.ps1
# 修改 config.local.json 中的 gme_repo_path，并确认 dsh_home 指向已配置凭据的 DS Harness 目录
scripts\run_web.ps1
```

Python 需要 3.10 或更高版本，Node.js 需要 18 或更高版本，后端依赖 `deepseek-harness-sdk` 与 `deepseek-harness-runtime-bin`。完整的 GME、编译工具和 GitHub CLI 要求见源码运行使用说明。

## 关于这份公开副本

- 这是上述工作流工具的**公开副本**，维护者为 [@nuaaweixinye](https://github.com/nuaaweixinye)。
- 它**不含 GME 专有数据**：接口目录（`backend/gme_agent/interface_catalog/catalogs/*.json`）是从 GME 仓库生成的产物，不随仓库分发。拿到 GME 检出与 ACIS 符号表后自行生成：

  ```powershell
  python scripts/generate_interface_catalog.py --gme-root <GME 检出路径> --acis-symbol-dir <ACIS 符号表目录> --module base --module kernel --module laws
  ```

  生成前 `gme_check` 的 `catalogs` 资源会返回空列表，这是预期行为，不是故障。
- 自测：`python -m unittest discover -s tests`。未生成接口目录时，7 个依赖目录的用例会跳过（输出 `OK (skipped=7)`）；生成后即会真正执行。
- 运行任务仍然需要你自己的 GME 仓库、编译工具链、GitHub CLI，以及在 `dsh_home` 中可用的 DeepSeek 凭据。
- 需要 GME 侧全部内容（含接口目录与内部笔记）的，请向维护者申请私有仓库访问。

## 与 DeepSeek Harness 插件配对

配套的 Harness 插件 `dsh-gme-workflow` 提供三个工具（`gme_generate` / `gme_check` / `gme_decide`）来驱动这个后端：

```sh
dsh plugin --profile web add dsh-gme-workflow
```

插件仓库：<https://github.com/nuaaweixinye/dsh-gme-workflow>，安装与配置（`backendRoot`、`GME_TEST_AGENT_ROOT`）见其 README 与 `docs/setup.md`。

