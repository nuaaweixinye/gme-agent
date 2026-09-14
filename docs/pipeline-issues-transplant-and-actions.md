# GME 测试代理流水线：两个已复现的问题

- 记录时间：2026-09-14
- 代码位置：`backend/gme_agent/`（本仓库）
- 观测环境：Windows，本地 GME agent 服务，模块 `laws`，目标仓库 `tests/gme`
- 触发任务：`1df9a761-46bb-4c4a-ae5b-0834beb215b0`、`c0b0f8e6-2340-4dce-bc27-87ed7d9f1c01`
- 代码行号以记录时的 `HEAD` 为准，后续可能漂移

## 前提与定位（先读这一节）

**失败用例是这条流水线的预期产物，不是缺陷。**

`api_*` 对照测试的存在意义就是拿 GME 与 ACIS 对同一输入的返回做差分。差分断言失败时，测试正在**报告一个真实的 GME↔ACIS 行为分歧** —— 这正是要交付的东西。因此：

- 一条测试失败，应视为"发现了一条分歧"，把它保留下来（连同失败记录、根因、复现命令）本身就有价值，即使它让测试套件变红。
- `skip_pr` 的 `GTEST_SKIP` 是把红灯**抹平**的**可选**手段，只适用于"团队要求默认 CI 必须绿"的场景；它不是默认动作，也不该是唯一能在流水线里交付测试的路径。对失败记录采用 `skip_pr` 时，失败记录仍为 `open`，证据以记录形式保留，但源码里的红灯信号消失了。
- 因此下面的问题 1 不是"失败用例没被跳过"，而是**"能保住失败证据的那条路径与需要绿灯的路径，其行为不一致且后者有缺陷"**。

两条 PR 路径对失败证据的处理正好相反，这是判断问题严重度的关键：

| 路径 | 是否携带同文件的其他改动 | 对失败用例 | 适用场景 |
|---|---|---|---|
| `create_pr`（`pr_flow.run_pr_job`） | ✅ 在任务工作树上 `commit_all` | **原样保留，真实失败** | 交付分歧证据（推荐默认） |
| `skip_pr` / `selected_tests_pr`（`skip_pr_flow.py`） | ❌ 只搬测试函数体 | 被 `GTEST_SKIP` 抹平 | 需要默认 CI 绿的团队 |

## 摘要

| # | 问题 | 影响 | 严重度 |
|---|---|---|---|
| 1 | `skip_pr` / `selected_tests_pr` 只把**测试函数体**移植到新 PR 工作树，同文件的其他配套改动（新增 `#include`、helper、宏等）被静默丢弃 | 任何依赖新增 include 的"已知失败跳过 PR"**必然构建失败**，且失败发生在 skip agent 已跑完之后 | 高 |
| 2 | 动作的可用性与语义存在三个陷阱：`fix` 修的是源码；`retry` 有双重前置条件使 goal 任务永远不可重试；创建 fix 任务会让失败记录不再是 `open`，与 `skip_pr` 形成竞态 | 误操作、无法恢复、看似随机的失败 | 中 |

---

## 问题 1：`skip_pr` / `selected_tests_pr` 丢弃同文件的配套改动

### 现象

对一个含已知失败用例的任务执行 `skip_pr`，流程顺利完成：
`creating_pr` → 新建 PR 工作树 → `applying_skips`（skip agent 成功写入 `GTEST_SKIP`）→ 随后**在构建阶段失败**：

```
tests/gme/src/laws/kernel_kernapi_test.cpp(380,5): error C2039: "GME_EXCEPTION_ACCESS_VIOLATION": 不是 "GME" 的成员
tests/gme/src/laws/kernel_kernapi_test.cpp(380,5): error C2065: "GME_EXCEPTION_ACCESS_VIOLATION": 未声明的标识符
tests/gme/src/laws/kernel_kernapi_test.cpp(380,5): error C2737: "gtest_ar": 必须初始化 const 对象
```

任务最终置为 `failed`，`error` 字段为上述构建日志。失败的 PR 工作树随后被清理。

### 机制

两条创建 PR 的路径对"改动的搬运方式"完全不同：

- **普通 `create_pr`** → `flows/pr_flow.py:29 run_pr_job`，在第 45 行对**任务自己的工作树**执行 `commit_all(target_path, ...)`。生成 agent 对该文件做过的所有改动都会被提交，因此不受本问题影响。
- **`skip_pr` / `selected_tests_pr`** → `flows/skip_pr_flow.py:42 run_selected_tests_pr_job`，走"移植"路径：
  1. `_checkout_fresh_pr_branch`（`:576`）在**全新工作树**上 `git checkout -b <branch> <remote>/<base_branch>`，即一棵干净的 latest main；
  2. `_extract_selected_test_blocks`（`:502`）从**任务工作树**里按 `_test_blocks` 切出被选中测试的**函数体文本**，并记录相邻测试名作为插入锚点；
  3. `_insert_selected_test_blocks`（`:591`）把这些函数体插入**新工作树**的对应文件；
  4. `_validate_fresh_pr_files`（`:667`）只校验三件事：既有 main 测试未被改动、新增测试集合恰好等于选中集合、`git diff --numstat` 无删除行。

第 2 步取的是**函数体**（`SelectedTestBlock.text = source.text[start:end]`，`:529`），文件里任何**不属于测试函数体**的改动都不在搬运范围内。因此生成 agent 为让该用例编译而新增的

```cpp
#include "gme/base/error_message/errorbase.err"
```

不会被带到新工作树。用例体里引用 `GME::GME_EXCEPTION_ACCESS_VIOLATION` 时就失去声明，产生 `C2039` / `C2065`。

### 证据与推断边界

已直接核实：

- 任务工作树 `.../worktrees/testgen-laws-20260914-123736-1df9a761/tests/gme/src/laws/kernel_kernapi_test.cpp` 中 `errorbase.err` include **存在**，且该工作树构建通过、11 条用例实际运行；
- 构建失败信息指明 `GME_EXCEPTION_ACCESS_VIOLATION` 未声明，行号 380 正是该用例体内的断言；
- `_validate_fresh_pr_files` 明确要求"PR 必须是纯新增"，因此失败工作树里不可能出现被搬运之外的改动。

属推断的一步：失败的 `pr-verify-laws-20260914-132520-1df9a761-254500` 工作树已被清理，无法直接读取其文件内容来逐行比对。结论由"新工作树 = latest main + 仅插入的函数体"这一构造过程推出，与构建错误一致。

### 影响面

- 任何生成用例依赖**新增 include、helper 函数、宏、命名空间别名、测试夹具改动**的任务，其 `skip_pr` / `selected_tests_pr` 都会在构建阶段失败。
- 失败发现得太晚：skip agent 已经跑完并写入 `agent_skip_result.txt`，整个工作树已创建并拉取依赖，全部作废。
- 现有校验无法提前发现：`_validate_fresh_pr_files` 比对的是**测试块文本集合**，不做可编译性检查，所以问题只在 `_run_configure_and_build`（`:157`）暴露。
- 对"需要默认 CI 绿"的团队，本问题使这类任务**无法通过任何路径产出绿灯 PR**：`skip_pr` 是唯一能产出绿灯 PR 的路径，而它必然构建失败。此时只剩两个选择 —— 改走 `create_pr` 接受红灯（但这就失去了 `skip_pr` 的意义），或手工把缺失的配套改动补进 PR 分支。

### 建议

1. **最小修复**：在 `_insert_selected_test_blocks` 之外，对每个 `rel_path` 额外搬运"任务侧相对 base 的**非测试块新增行**"（典型即 include 区的新增行）。这不会违反 `_validate_fresh_pr_files`：新增行不计入测试块集合比对，且 numstat 删除数仍为 0。
2. **兜底校验**：在 skip agent 运行**之前**先做一次编译前置检查（或至少检查被移植用例体引用的标识符在新工作树中是否可见），把失败提前到代价更小的一步。
3. **错误可诊断**：构建失败时在 `error` 中标注"疑似由未搬运的配套改动导致"，并保留失败工作树以便比对。
4. **定位提示**：`skip_pr` 既然是"抹平红灯"的可选手段，就应在动作说明与 PR 正文中写明它消除的是一条**真实分歧证据**，并给出保留红灯的替代路径（`create_pr`），避免使用者把"绿灯"误当成目标本身。

---

## 问题 2：动作的可用性与语义陷阱

### 2a 创建 fix 任务会让失败记录不再是 `open`，与 `skip_pr` 竞态

- `services/orchestrator.py:279`：创建 fix 任务时执行 `update_failure(status="fixing")`。
- `flows/skip_pr_flow.py:334 _latest_open_failures_for_job`：只收集 `status == "open"` 的失败。
- `_selected_manifest_tests`（`:249`）在请求为空时抛：`Select at least one generated test before creating a PR.`

因此当某个失败正被 fix 任务占用时，`skip_pr` 会因"选不到任何测试"而失败，且错误信息完全没提到真正原因。

本次实测：先发起 `fix`（误判为"修测试"），再发起 `skip_pr`，两者并发放置，`skip_pr` 以该错误失败：

```
error: "Select at least one generated test before creating a PR."
```

补充：`services/job_service.py:92` 在删除任务时会把 `fixing` / `fix_failed` / `fix_ready` 的失败重新置回 `open` —— 这解释了删除误建任务后失败记录自动恢复为 `open`。

**建议**：`skip_pr` 在选不到 open 失败时，应检查是否存在同测试的 `fixing` 失败并明确报出（例如"该失败正由 fix 任务 X 处理"）；或允许按 job 维度取最近失败而不限 `status`。

### 2b `retry` 的前置条件使 goal 型任务永远无法重试

`services/orchestrator.py:395 _test_generation_retry_details` 有三重门槛：

- `:402` `job.status != "failed"` → `ValueError("Task <id> is not failed")`（实测：对 `needs_review` 任务 retry 返回该错误）
- `:411`-`:413` `metadata.selected_interfaces` 为空 → `ValueError("Task <id> has no structured interface selection to retry")`
- `:404`-`:405` 任务正在执行 → `JobAlreadyActiveError("Job <id> is already running another action.")`（实测 HTTP 409）

而**以自由文本 goal 创建的任务**（如本次的 `1df9a761-46bb-4c4a-ae5b-0834beb215b0`，以及更早的 `5e9bbd87-...`）metadata 中只有 `target_repo` / `pr_strategy` / 分支字段，**没有 `selected_interfaces`**。也就是说这类任务一旦失败，`retry` 一定不可用 —— 即使满足了 `status == "failed"`。

**建议**：把 goal 一并存入 metadata，允许 goal 任务按其原 prompt 重放；或在错误信息中直接说明"该任务由 goal 创建，不支持 retry"。

### 2c 命名与语义不易由工具描述推断

- `gme_decide skip_pr` 的实际语义是**"带着已知失败跳过标记开 PR"**（`orchestrator.py:305 create_skip_pr_for_job` → `run_skip_pr_job` → `run_selected_tests_pr_job(ctx, job_id, None)`），**不是**"跳过 / 不创建 PR"。
- `gme_generate fix` 是**修复 GME 源码**，产生 `type=bug_fix` 任务、`fix_candidate_repos` 指向 `module/laws`、`module/kernel`；它不是"给测试加 skip"。
- 给测试加 skip 的入口是 `skip_pr` / `selected_tests_pr`；`remove_tests` 才是删除已生成测试（`flows/generated_test_edit_flow.py`）。

**建议**：在工具描述中为每个动作补一行定义 + 前置条件，尤其是 `skip_pr` 与 `fix` 这两个容易误读的名字。

---

## 附 A：本次会话已实测确认的其他行为

| 行为 | 实测结果 |
|---|---|
| `create_pr`（`pr_flow.run_pr_job`） | 在任务工作树上 `commit_all` 并推送；生成 agent 的全部改动随 PR 进入（`#1301`、`#1302` 均为此路径，分别 `+392/-0`、`+436/-0`） |
| `cleanup` | 执行 `git worktree remove --force`，任务置 `worktree_cleaned`，**保留任务记录与 metadata**（含 `cleaned_worktree_path`） |
| `delete_job` | 删除 job / events / failures / 结果行、工作树与 artifacts，并把被占用的失败重新置 `open` |
| 并发动作 | 同一任务上并发请求返回 HTTP 409 `Job <id> is already running another action.` |
| `test_generation_flow` | `auto_apply_skips` 关闭时只写出 `skip_prompt.md` 供人工处理；本次服务为该状态 |

## 附 B：相关任务与产物

| 任务 | 状态 | 说明 |
|---|---|---|
| `1df9a761-46bb-4c4a-ae5b-0834beb215b0` | `failed` | 问题 1 的复现任务；工作树完好（11 条用例：10 通过 / 1 失败未跳过）；失败记录 `gmefail-31250debcc` 为 `open`。**那条失败用例即预期产物**（GME 对 `x+y` + 标量区间访问违例、ACIS 正常返回 5），后续应以 `create_pr` 交付并接受红灯，而不是走 `skip_pr` 抹平 |
| `c0b0f8e6-2340-4dce-bc27-87ed7d9f1c01` | 已删除 | 问题 2a 的触发源（`fix` 误用），已删除 |
| `013f4390-7474-4413-8810-fc920b67dcd4` | `pr_created` | PR `GME-Org/Test#1302` |
| `a15e2398-1628-4e54-84d8-18e8e7c367b5` | `pr_created` | PR `GME-Org/Test#1301` |

问题 1 的完整证据链（构建日志、skip agent 输出、diff）位于：
`artifacts/1df9a761-46bb-4c4a-ae5b-0834beb215b0/` 下的
`build_output.txt`、`agent_skip_result.txt`、`skip_prompt.md`、`gtest_output.txt`、`diff.patch`。
