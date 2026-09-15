# 后端总览：GME Test Agent 的功能与契约

- 面向：维护/扩展后端的人，以及判断"某个行为是不是预期"的人。
- 事实来源：本仓库源码。本文**不引用行号**（行号会漂移），引用的是模块名、常量名与表名；这些改动时会一起改。
- 阅读顺序建议：第 5 节（任务生命周期）和第 7 节（什么算通过）是这个工具的核心契约，其余是索引。

---

## 1. 三层结构

```
外层 DSH 会话（你与模型）          内层 DSH 会话（真正写代码的编码 agent）
   三个工具 gme_generate/         每个任务一棵独立 git worktree，
   gme_check/gme_decide           profile=sdk + sdk.patch.yml
        │                              ▲ workspace-write、禁用交互审批
        │ HTTP + Authorization: Bearer <token>   │ DeepSeek Harness Python SDK
        ▼                              │
   ┌──────────────────────────────────┴─────────────┐
   │ Python 后端（gme_agent）                        │
   │ 权威状态机 + 全部校验 + 落库 + 交付（PR）        │
   └─────────────────────────────────────────────────┘
```

三条硬规则：

1. **编码 agent 只写代码**。构建、跑测试、内存审计、manifest 校验、PR 都由后端自己做或复核；agent 的自述不算结果。
2. **权威状态只有一处**：`gme_agent.db`。进程重启后任务仍可续（会话 id 存在任务上）。
3. **两层会话不递归**：外层只谈任务与决策，内层只干活；内层的文件改动由后端在工作树上校验。

## 2. 进程与入口

| 入口 | 说明 |
|---|---|
| `backend/run_backend.py --config config.local.json --host 127.0.0.1 --port <port>` | HTTP 后端。`scripts/run_web_backend.ps1` 会用 `GME_AGENT_API_TOKEN` 注入令牌并默认 8765 端口 |
| `scripts/run_web.ps1` | 起后端 + 前端（开发用） |
| `frontend/`（Vue + Vite） | 本地 Web 界面；也可打包成 Electron 桌面版 |
| `python -m gme_agent.commands.cli <cmd>` | 无头 CLI：`create-test` / `run-tests` / `build` / `create-pr` / `cleanup` / `fix` / `mark-failure` / `jobs` / `failures` / `validate` |
| `scripts/setup_source.ps1` | 首次安装依赖、生成 `config.local.json` |

HTTP 鉴权：每个请求都要带 `Authorization: Bearer <token>`（令牌来自 `GME_AGENT_API_TOKEN`，启动时有最小长度校验，比对用常数时间比较）；另有来源白名单（`is_allowed_origin`）。未通过时返回 401 / 403，前端在 `frontend/src/api.js` 统一附带该头。

## 3. 模块地图

| 目录 | 职责 | 关键入口 |
|---|---|---|
| `api/server.py` | HTTP 层：路由、令牌校验、把请求转成 orchestrator 调用 | `create_server` / `run_server` / `_match_job_action` |
| `services/orchestrator.py` | **编排与并发**：创建各类任务、信号量、活动任务集、动作分发 | `create_test_generation_job` / `create_fix_job` / `_active_jobs` |
| `services/job_service.py` | 删除任务：清工作树、清工件、删库行、把被占用的失败放回 `open` | `delete_job_record` |
| `services/artifact_service.py` | 工件目录与 `diff.patch` / `manifest.json` | `artifact_dir_for_job` / `write_job_artifacts` |
| `services/failure_service.py` | 失败记录相关的薄封装 | — |
| `services/interface_catalog_service.py` | 接口目录查询与选择（下拉数据、结构化选择解析）；`MAX_CONCURRENT_GENERATION_JOBS = 10` 也定义在此 | — |
| `flows/test_generation_flow.py` | **生成**与**扩展/重试**两条流程（含知识注入、失败记录、升格） | `run_test_generation_job` / `run_test_extension_job` |
| `flows/build_test_flow.py` | 配置/构建、跑测试、**记录失败**（唯一写 failures/observations/test_case_results 的地方） | `run_configure_and_build` / `run_tests` / `record_failures` |
| `flows/pr_flow.py` | `create_pr`（在工作树上提交全部改动）与 `cleanup` | `run_pr_job` / `run_cleanup_job` |
| `flows/skip_pr_flow.py` | `skip_pr` / `selected_tests_pr`：把选中的测试**移植**到全新工作树再开 PR | `run_selected_tests_pr_job` |
| `flows/bug_fix_flow.py` / `bug_fix_pr_flow.py` | 缺陷修复流程：修 `module/` 源码并校验 | `run_fix_job` / `run_bug_fix_pr_job` |
| `flows/memory_audit_flow.py` | Release 内存审计（逐条测试解析 `Leaks` / `Bad delete pointers`） | `run_memory_audit_job` / `run_selected_memory_audit` |
| `flows/generated_test_edit_flow.py` | 删除已生成测试（`remove_tests`）并清理 manifest / 结果行 | `delete_generated_tests` |
| `harness/runner.py` | 内层会话：SDK 配置、技能暂存、`on_notification` → 工具事件、脱敏与截断 | `HarnessRunner.run` |
| `harness/sdk.patch.yml` | 内层 profile 的 patch：workspace-write 沙箱、禁用审批、权限预设 | — |
| `execution/runner.py` | 命令执行与 GTest 解析（文本 + XML） | `run_template_command` / `parse_gtest_xml` / `merge_failures` |
| `git/worktree.py` / `repositories.py` / `diff.py` | 工作树创建与清理、子模块/依赖准备、**改动范围校验** | `create_worktree` / `ensure_only_target_repo_changed` |
| `storage/db.py` | SQLite：建表、迁移、任务/事件/失败/结果读写 | `AgentDb` |
| `settings/config.py` | `AgentConfig`（全部配置项与默认值） | `load_config` / `save_config` / `resolved` |
| `settings/options.py` / `validation.py` | 下拉选项；`/api/validate` 的自检清单 | `load_config_options` / `validate_config` |
| `interface_catalog/` | 接口目录（`catalogs/{base,kernel,laws}.json`）与生成脚本 | `loader` / `generator` |
| `prompts/` | 提示词：生成 / 缺陷修复 / 跳过（含可选 `knowledge_block`） | `test_generation_prompt` 等 |
| `knowledge/` | **知识注入**：先验、检索、预算、渲染、编排、闭环升格 | 见 `docs/knowledge-injection.md` |
| `skills/<name>/SKILL.md` | 内层 agent 的技能契约（生成、模块分析、ACIS 分析、写测试、修缺陷） | 由 `config.test_generation_skill` 等选择 |
| `generated_tests.py` | manifest 读写与规则（必须在既有文件里、不许新建 `.cpp`、行尾过滤） | `load_generated_tests_manifest` |
| `interface_coverage.py` | 覆盖率工件的强制校验（九类维度、gap 状态机、清单映射） | `require_interface_coverage_artifacts` |
| `runtime.py` | 资源根（源码运行 vs 打包后的 `_internal`） | `skill_root` / `resource_root` |

## 4. 数据模型（SQLite：`database_path`）

| 表 | 主键 | 关键列 | 谁写 |
|---|---|---|---|
| `jobs` | `id` | `type` / `status` / `title` / `module` / `api_name` / `worktree_path` / `branch` / `harness_session_id` / `error` / `metadata_json` | orchestrator 与各 flow |
| `events` | `id` 自增 | `job_id` / `ts` / `level` / `message` | 所有流程与内层工具事件 |
| `failures` | `id`（`gmefail-…`） | `status` / `test_suite` / `test_name` / `file` / `line` / `reason` / `reproduce_command` / `skip_id` / `metadata_json` | 仅 `record_failures` 与修复流程 |
| `failure_observations` | `id` 自增 | `run_id` / `failure_id` / `outcome` / `reason` / `gtest_filter` | `record_failures`（每次运行追加一行） |
| `test_case_results` | `(job_id, test_suite, test_name)` | `status`(passed/failed/skipped) / `run_id` / `gtest_filter` | `record_failures` |

`failures.metadata_json` 里在用的键（新增键前先看这里）：

| 键 | 含义 |
|---|---|
| `module` / `interface_id` / `api_name` / `target_repo` / `gtest_filter` | 归因：这条失败属于哪个模块/接口；知识注入与升格都依赖它 |
| `fix_job_id` | 正在修它的修复任务（占用标记） |
| `knowledge` | 知识闭环：`confirmed_at` / `stability` / `injected_count` / `last_injected_at` |

`jobs.metadata_json` 里在用的键：`target_repo` / `target_repo_path` / `target_branch` / `target_base_branch` / `superproject_branch` / `prepared_paths` / `artifact_dir` / `generated_tests` / `generated_test_files` / `generated_gtest_filter` / `interface_coverage` / `selected_interfaces` / `knowledge` / `fix_target_repo` / `fix_candidate_repos` / `cleaned_worktree_path(s)`。

**失败记录的状态机**：`open → fixing → fix_ready / fix_failed`；`resolved` 表示"本次范围内重跑没再失败"；`ignored` 由人工标记。删除一个修复任务会把它占用的失败从 `fixing/fix_failed/fix_ready` 放回 `open`。

## 5. 任务生命周期与状态机

任务状态（`jobs.status`）：

| 状态 | 含义 | 谁设置 |
|---|---|---|
| `queued` | 已建任务，等待调度 | orchestrator |
| `creating_worktree` | 建工作树、拉依赖、准备目标仓库 | 生成 / 修复流程 |
| `running_agent` | 内层编码会话正在跑 | 生成 / 扩展 / 修复 |
| `checking_format` | clang-format 检查（修复流程） | 修复流程 |
| `building` | 配置 + 构建 | `run_configure_and_build` |
| `running_tests` | 跑 GTest | `run_tests` |
| `running_memory_audit` | Release 内存审计 | 内存审计流程 |
| `applying_skips` | 内层 agent 写 `GTEST_SKIP` | 生成 / `skip_pr` |
| `creating_pr` | 生成分支、提交、推送、开 PR | PR 流程 |
| `cleaning_worktree` / `worktree_cleaned` | 已移除工作树（任务与 metadata 保留） | `cleanup` |
| `needs_review` | **成功终点**：等你决定 PR / 跳过 / 修复 | 各流程 |
| `pr_created` | 已开 PR（PR 号在 metadata 里） | PR 流程 |
| `failed` | 失败终点：`error` 字段是原因 | 全部流程的 `except` |

生成任务的典型路径：

```
queued → creating_worktree → running_agent → (building) → running_tests
        → [有失败? applying_skips → 重新构建/重跑]
        → needs_review → [create_pr / skip_pr / selected_tests_pr] → pr_created
```

失败时的停点：任何一步抛异常都会把任务置 `failed` 并写 `error`，**工作树保留**（便于查证）；后续可用 `cleanup` 清理，或 `delete_job` 连记录一起删。

## 6. 工具、动作与 HTTP 路由

外层三个工具 → 后端动作 → orchestrator 方法：

| 工具 | 参数 | 后端动作 | 说明 |
|---|---|---|---|
| `gme_generate` | `kind=tests` | `POST /api/jobs/test-generation` | 按接口选择或自由文本目标建任务并**立即执行** |
| | `kind=batch` | `POST /api/jobs/test-generation/batch` | 按模块批量建任务 |
| | `kind=fix` | `POST /api/fix-jobs` | 对失败建**修 GME 源码**的任务（不是给测试加 skip） |
| | `kind=extend` | `POST /api/jobs/<id>/extend-tests` | 在既有工作树上继续生成 |
| | `kind=retry` | `POST /api/jobs/<id>/retry-tests` 或 `/test-generation/retry` | 重跑失败任务；**需要 `status == failed` 且有结构化接口选择** |
| `gme_check` | `resource=…` | 对应 GET 路由 | 只读：目录、任务、事件、失败（含观测）、结果、工件；大报告带 `next_offset` |
| `gme_decide` | `create_pr` | `POST /api/jobs/<id>/create-pr` | 在工作树上提交全部改动开 PR（**保留真实红灯**，交付分歧证据走这条） |
| | `skip_pr` | `POST /api/jobs/<id>/skip-pr` | 带 `GTEST_SKIP` 标记开 PR（**移植**路径，见下） |
| | `selected_tests_pr` | `POST /api/jobs/<id>/selected-tests-pr` | 只把选中的测试移植到全新工作树开 PR |
| | `remove_tests` | `POST /api/jobs/<id>/generated-tests/remove` | 删除已生成测试并清 manifest / 结果行 |
| | `cleanup` | `POST /api/jobs/<id>/cleanup` | 移除工作树，保留任务记录 |
| | `delete_job` | `POST /api/jobs/<id>/delete` | 删任务 / 事件 / 失败 / 结果 + 工作树 + 工件；被 `fixing` 占用的失败放回 `open` |
| 维护用（非工具） | — | `POST /api/jobs/<id>/run-tests`、`/build`、`/memory-audit` | 单步重跑 |

`gme_decide` 的每个动作都是**对外或破坏性**的，插件要求先向用户展示现状并取得明确同意（`confirm: true`）。

`skip_pr` 与 `create_pr` 的本质差别（重要）：

| | `create_pr` | `skip_pr` / `selected_tests_pr` |
|---|---|---|
| 搬运方式 | 直接在任务工作树上 `commit_all` | 把选中测试的**函数体**移植到全新工作树 |
| 同文件其它改动（新增 include / helper / 宏） | 随 PR 进入 | **不会**被搬运 |
| 失败用例 | 原样保留（真实红灯） | 被 `GTEST_SKIP` 抹平 |
| 后果 | 能交付分歧证据 | 依赖新增 include 的用例会在构建阶段失败（已知问题，修复建议见 `docs/pipeline-issues-transplant-and-actions.md`） |

## 7. 什么算通过（权威校验）

这些规则由后端执行，agent 的自述一律不算：

| 校验 | 规则 | 实现 |
|---|---|---|
| 改动范围 | 只有目标测试仓库（以及 `.gme-agent/` 等允许的支撑路径）允许改动；`module/`、`include/`、CMake、子仓库指针都不许动。`timer_res*.csv` 之类副产物被显式忽略 | `git/diff.py::ensure_only_target_repo_changed` |
| manifest 存在 | `.gme-agent/generated_tests.json` 必须存在；除非这一轮是结构化接口选择（允许空） | `generated_tests.py::require_generated_tests_manifest` |
| manifest 文件合法 | 只允许插入**已存在且已在 HEAD 中**的 `.cpp`；不许新建 `gme_agent_<module>_generated_test.cpp` | `ensure_generated_tests_use_existing_files` |
| 文件范围 | 结构化选择时，新增用例只能落在被选中的文件里（续做时允许保留旧条目） | `ensure_generated_tests_use_selected_files` |
| 接口覆盖率 | 三个工件必须齐全且自洽：九类覆盖维度各一次、契约里的候选 gap 全部进计划、`covered_*` 的 gap 必须与 manifest 条目一一对应、接口终态只能是 `complete/saturated/blocked/budget_exhausted` | `interface_coverage.py::require_interface_coverage_artifacts` |
| 构建 | `configure_command` 与 `build_command` 必须成功（模板占位符由后端渲染） | `build_test_flow.py::run_configure_and_build` |
| 测试 | 必须用 manifest 给出的精确 filter 跑本轮全部新增测试；只构建通过不算完成 | `build_test_flow.py::run_tests` |
| 内存审计 | 每条用例在独立 Release 目录逐条跑，`Leaks: 0` 且 `Bad delete pointers: 0` 才算通过（`not_applicable` 允许） | `memory_audit_flow.py` |
| 修复格式 | clang-format 检查（修复源码时） | `bug_fix_flow.py::_run_fix_format_check` |
| 修复完整性 | 修复任务的测试必须全绿 + 内存审计通过才能开 PR | `_require_full_tests_pass` / `_require_memory_audit_pass` |

## 8. 工件布局

`artifacts/<job_id>/`（`artifact_root`，随任务记录在 `metadata.artifact_dir`）：

| 文件 | 内容 |
|---|---|
| `test_generation_prompt.md` | 内层会话收到的**完整提示词**（可审计"到底喂了什么"） |
| `knowledge_context.md` | 本次注入的知识原文 + 组装记录（JSON）；仅在知识注入开启且产出了内容时存在 |
| `agent_result.txt` / `agent_extend_result.txt` | 内层会话的最终回复 |
| `gtest.xml` / `gtest_output.txt` | **权威**测试结果（后端自己跑的那一次） |
| `gtest_output_after_skip.txt` | 跳过后的重跑输出 |
| `build_output.txt` | 配置 + 构建日志 |
| `skip_prompt.md` / `agent_skip_result.txt` | 跳过流程的提示词与结果 |
| `diff.patch` | 目标仓库相对 base 的净 diff |
| `manifest.json` | 任务快照：job 行、失败列表、每条用例结果（给人看的汇总） |

工作树里的 `.gme-agent/`（**agent 产出**，由后端校验）：

| 文件 | 谁写 | 用途 |
|---|---|---|
| `generated_tests.json` | agent | 本轮生成的用例清单（file/suite/name/api/anchor/interface_id/gap_id/scenario） |
| `interface_coverage_plan.json` | agent | 每个接口的 gap 计划与终态 |
| `interface_contracts.json` | agent | 黑盒契约 + 九类维度的候选 gap |
| `existing_test_coverage.json` | agent | 已有测试覆盖分析 |
| `module_test_profile.md` / `acis_interface_candidates.md` | agent | 人类可读的分析笔记 |

## 9. 并发、幂等与危险动作

- **生成并发上限 10**（`MAX_CONCURRENT_GENERATION_JOBS`），**重试并发上限 3**（`MAX_CONCURRENT_RETRY_JOBS`）；工作树准备阶段另有 `_generation_prepare_lock` 串行化。
- **同一任务同时只能有一个动作**：命中 `_active_jobs` 直接拒绝，HTTP 返回 **409**，错误信息为 `Job <id> is already running another action.`。
- **不要手工编辑正在运行的任务工作树**：后端的 `ensure_only_target_repo_changed` 与 manifest 校验会把它判成越界改动，整个任务失败。
- **`delete_job` 是唯一会删数据的动作**：任务、事件、失败、结果行、工作树、工件全删，并把被它占用的失败放回 `open`。
- **`cleanup` 只删工作树**：任务记录与 metadata（含 `cleaned_worktree_path`）保留，之后不能再跑测试/开 PR。
- **幂等性**：`create_pr` / `skip_pr` / `selected_tests_pr` 都不是幂等的（会新建分支与 PR）；重复调用前先看任务状态。

## 10. 配置参考（`config.local.json` ↔ `AgentConfig`）

| 分组 | 字段 |
|---|---|
| 仓库与目录 | `gme_repo_path` / `worktree_root` / `artifact_root` / `database_path` |
| 分支 | `base_branch` / `test_base_branch` / `module_base_branch` / `github_remote` |
| Harness | `provider` / `model` / `reasoning_effort` / `dsh_home` / `dsh_profile` / `dsh_bin` / `agent_enabled` |
| 生成行为 | `initialize_submodules` / `test_target_repo` / `module_repo_root` / `pr_strategy` / `use_builtin_skills` / `test_generation_skill` / `bug_fix_skill` |
| 自动步骤 | `auto_run_build` / `auto_run_tests` / `auto_apply_skips` / `auto_rerun_after_skip` / `auto_create_pr`（默认全部 `false`：默认每一步都等你确认） |
| 命令模板 | `configure_command` / `build_command` / `test_command` / `test_executable` / `gtest_xml_path` |
| 知识注入 | `knowledge{enabled, weknora{base_url, api_key_env, timeout_ms, knowledge_bases[{label,id}]}, budgets{max_priors, max_kb_hits, max_chars}, closed_loop{enabled, min_stable_runs}}` |

命令模板可用占位符（未知占位符会被 `/api/validate` 报错）：`{worktree}` `{build_dir}` `{test_executable}` `{gtest_filter}` `{test_module_name}` `{develop_module_option}` `{test_module_option}` `{artifact_dir}` `{gtest_xml_path}`。

`/api/validate` 会逐项自检：GME 仓库与子模块、测试仓库/模块根路径、技能文件、git/cmake/clang-format/gh 是否在 PATH、`dsh_home`、SDK 是否安装、模板占位符。

## 11. 相关文档

| 文档 | 内容 |
|---|---|
| `README.md` | 工具是什么、最小上手路径 |
| `docs/源码运行使用说明.md` | 源码运行的完整环境要求与步骤 |
| `docs/gme-test-agent使用说明.md` | 桌面版使用说明 |
| `docs/knowledge-injection.md` | 知识注入与轨迹可观测：开关、配置、验证、降级表 |
| `docs/pipeline-issues-transplant-and-actions.md` | 两条已复现的流水线缺陷 + `skip_pr`/`create_pr` 取舍 + 动作语义实测 |
| `docs/reports/` | 按日期的任务运行报告（产出、PR、接口结论） |
| `docs/superpowers/specs|plans/` | 设计与实施记录：Harness 运行器集成、知识注入 |
| `skills/*/SKILL.md` | 内层编码 agent 的契约（工件格式、九类维度、边界） |
