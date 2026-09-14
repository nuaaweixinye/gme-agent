# GME Test Agent 使用流程

本文档面向拿到 `GME Test Agent-0.1.0-x64.exe` 的使用者。

## 1. 使用前准备

使用者电脑上需要已有以下环境：

- Windows x64
- 本地 GME 仓库，例如 `D:\GME`
- DeepSeek Harness Python SDK 与配套 SDK runtime 已安装到后端使用的 Python 环境
- `dsh_home`（默认 `C:/Users/<用户名>/.dsh`）中已配置可用的 DeepSeek 凭据
- Git
- CMake
- Visual Studio 2022 C++ Build Tools
- `clang-format`
- GitHub CLI `gh`
- 已执行 `gh auth login`，并且账号有目标测试仓库的 push/PR 权限


## 2. DeepSeek Harness 运行方式与凭据

GME Test Agent 的编码工作全部通过 DeepSeek Harness 执行，系统中有两个互不递归的 Harness 进程：

- DS Harness Web（对话进程）：承载用户对话和 GME Workflow 工具调用。
- DS Harness SDK runtime（编码进程）：由 Python 后端按任务启动的独立 `sdk` profile，只包含文件、搜索和 PowerShell 工具，不加载 GME Workflow 插件。

### 2.1 配置与凭据

GME Test Agent 不在配置文件中保存模型凭据。编码凭据存放在 `dsh_home` 指向的 Harness 配置目录中（默认 `C:/Users/<用户名>/.dsh`），`sdk` profile 从该目录读取凭据。请按 DeepSeek Harness 官方文档在该目录中完成凭据配置。

登录 DeepSeek 和登录 GitHub 是两件独立的事情：

- DS Harness 凭据用于生成测试和修复代码。
- `gh auth login` 用于推送分支和创建 GitHub PR。

### 2.2 会话与沙箱

- 每个 GME 任务对应一个持久化的 Harness session：首次生成创建 session，扩展和重试恢复同一 session。
- 每次智能体调用都会启动并关闭一个 SDK runtime 子进程；会话事件写入 `dsh_home`，因此下次调用可以在新进程中恢复。
- 编码智能体运行在 `workspace-write` 沙箱中，工作区根目录就是任务 worktree；交互式审批通过受信任 patch 关闭。
- 智能体的自然语言回复不代表成功：文件改动、manifest、构建和测试结果仍由 Python 后端权威校验。

### 2.3 凭据安全要求

- 不要把 API Key 写入 `config.local.json`。
- 不要把 API Key 写入 Skill、prompt、日志、源码或测试文件。
- 不要将 API Key 提交到 GitHub。
- 不要在截图、作业报告或聊天记录中暴露完整 API Key。
- API Key 泄露后应立即撤销并创建新 Key。
- 多人使用时，每个人应使用自己的凭据，不要共享同一个个人 Key。

## 3. 获取工具

```text
GME Test Agent-0.1.0-x64.exe
```

使用者双击 exe 即可启动。

## 4. 第一次启动会自动创建什么

第一次打开时，工具会在当前 Windows 用户的应用数据目录下创建自己的本地数据。

典型路径类似：

```text
C:\Users\<用户名>\AppData\Roaming\GME Test Agent\
```

其中会自动生成：

```text
config.local.json
gme_agent.db
worktrees\
artifacts\
logs\
```

说明：

- `config.local.json`：本机配置文件。
- `gme_agent.db`：本地 SQLite 数据库，保存任务、日志、失败用例记录。
- `worktrees\`：每个任务独立创建的 GME worktree。
- `artifacts\`：prompt、运行日志、gtest xml、patch 等产物。
- `logs\`：桌面程序和后端日志。

使用者不需要提前准备数据库。`gme_agent.db` 不存在时，后端会自动创建。

## 5. 首次配置

启动后进入“设置”页，重点检查这些配置：

- `gme_repo_path`：本机 GME 仓库路径，例如 `D:/GME`
- `worktree_root`：任务 worktree 存放目录
- `artifact_root`：任务产物存放目录
- `database_path`：本地数据库路径，首次启动会自动指向本机应用数据目录
- `base_branch`：创建任务 worktree 的基础分支，通常是 `main`
- `github_remote`：远端名，通常是 `origin`
- `provider`：DS Harness provider，默认 `deepseek-official`
- `model`：DS Harness 使用的模型；可从当前配置中选择，也可以选择“自定义模型”后输入模型 ID
- `reasoning_effort`：推理强度；设置页会提供 DeepSeek 支持的档位
- `dsh_home`：DS Harness 配置目录，凭据与 `sdk` profile 存放在这里
- `dsh_profile`：编码使用的 Harness profile，默认 `sdk`
- `dsh_bin`：可选的 SDK runtime 可执行文件路径；留空时使用已安装的 runtime wheel

配置完成后，点击“环境检查”。

环境检查通过后再开始生成测试。

## 6. 生成测试流程

进入“测试 Agent”页：

1. 选择模块，例如 `laws`、`base`。
2. 在“测试目标 / 提示词”里填写想扩展的测试方向。
3. 点击“新建任务并生成”。
4. 等待任务完成。

工具会为该任务创建独立 worktree，不会直接修改原始 `D:\GME` 工作目录。

生成完成后，可以在页面中查看：

- 当前任务
- 生成文件路径
- 构建日志
- 测试结果

## 7. 构建和运行测试

如果任务生成后还没有构建或运行，可以手动点击：

- “构建”
- “运行测试”

运行测试后，如果存在 GME vs ACIS 差异，失败会进入 failure 表。

失败记录会包含：

- failure id
- suite
- test name
- 文件路径
- 行号
- 失败原因

## 8. 加 skip 并创建 PR

如果确认当前失败是 GME 和 ACIS 的真实差异，可以点击：

```text
加 skip 并创建 PR
```

该流程会：

1. 读取当前选中任务的最新 open failures。
2. 只给这些失败测试加：

```cpp
GTEST_SKIP() << "[gme-agent-known-failure:gmefail-xxx] reason";
```

3. 使用当前 worktree 的 `.clang-format` 格式化 generated test 文件。
4. 重新运行这些失败测试对应的 GTest filter。
5. 确认 skip 后 failure 清零。
6. 创建新的 skip 专用分支。
7. 只提交测试仓库中相关 generated test 文件。
8. 创建普通 PR。

注意：

- PR 不会提交无关文件。
- PR 中 generated test 文件会被裁剪为只包含本次失败并加 skip 的测试。
- 本地任务 worktree 会保留完整 generated 文件，方便继续扩展。

## 9. 继续扩展已有任务

如果想在已有任务基础上继续生成测试：

1. 选中任务。
2. 修改“测试目标 / 提示词”。
3. 点击“继续扩展选中任务”。
4. 重新构建和运行测试。

如果又出现新的真实失败，可以再次点击“加 skip 并创建 PR”。

每次 skip PR 都会创建新的独立 skip 分支。

## 10. 数据和日志位置

如果需要排查问题，可以查看：

```text
C:\Users\<用户名>\AppData\Roaming\GME Test Agent\logs\
```

常用文件：

```text
backend.out.log
backend.err.log
```

任务产物在：

```text
C:\Users\<用户名>\AppData\Roaming\GME Test Agent\artifacts\
```

任务 worktree 在：

```text
C:\Users\<用户名>\AppData\Roaming\GME Test Agent\worktrees\
```

具体路径也可以在“设置”页查看。

## 11. 常见问题

### 环境检查提示 DeepSeek Harness Python SDK 失败

说明后端使用的 Python 环境中没有安装 `deepseek-harness-sdk` 和 `deepseek-harness-runtime-bin`。安装与后端匹配的两个 wheel 后重新执行环境检查。

### 环境检查提示 DS Harness 配置目录不存在

检查 `dsh_home` 配置是否指向本机 Harness 配置目录（默认 `C:/Users/<用户名>/.dsh`），并确认其中已配置可用的 DeepSeek 凭据和 `sdk` profile。

### 环境检查提示 gh 不存在

安装 GitHub CLI，并登录：

```powershell
gh auth login
```

登录账号需要有目标仓库的 push/PR 权限。

### 构建失败

检查：

- Visual Studio 2022 C++ Build Tools 是否安装
- CMake 是否在 PATH 中
- GME 仓库和子模块是否完整

### PR 创建失败

检查：

- `gh auth status`
- `git remote -v`
- 当前账号是否有目标仓库写权限
- 网络是否能访问 GitHub

### clang-format 失败

检查：

- `clang-format` 是否在 PATH 中
- 当前 GME worktree 根目录是否有 `.clang-format`
