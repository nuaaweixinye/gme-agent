---
name: gme-test-generation
description: 协调 GME 接口的语义覆盖提升闭环。用于先分析选中接口的已有测试与 GME/ACIS 契约，再规划不重复的功能缺口，分批生成、构建、运行和内存审计测试，直到接口覆盖完整、饱和、阻塞或安全预算耗尽。
---

# GME 接口覆盖生成协调器

## 完成定义

测试数量不是目标。对每个选中接口，必须先证明已有覆盖，再只为未覆盖功能生成测试，并循环验证到以下终态之一：

- `complete`：可识别且可验证的功能缺口均已覆盖。
- `saturated`：新候选均与已有测试重复，没有新增价值。
- `blocked`：剩余场景因契约、链接、可见性或 oracle 问题无法可靠测试。
- `budget_exhausted`：仅在安全执行预算确实耗尽时使用；保留 `planned` 缺口供继续任务恢复。

除 `budget_exhausted` 外，结束时不得存在 `planned` 缺口。

## 顺序闭环

1. 对当前接口调用 `gme-module-test-analyzer`，生成 `.gme-agent/module_test_profile.md` 和 `.gme-agent/existing_test_coverage.json`。
2. 调用 `gme-acis-interface-analyzer`，生成 `.gme-agent/acis_interface_candidates.md` 和 `.gme-agent/interface_contracts.json`。
   契约分析必须同时读取测试所属业务模块与 `module/kernel` 的公开 API 包装、参数校验和 outcome 处理；测试目录归属不能替代完整调用链分析。
3. 对照两份 JSON 建立 `.gme-agent/interface_coverage_plan.json`。计划必须复用契约候选的准确 `gap_id` 和 `scenario`，且包含每个契约候选；只把有契约依据、可靠 oracle 且未被已有测试覆盖的场景标为 `planned`。
4. 调用 `gme-test-writer`，每轮消费 4–8 个 `planned` 缺口；不足 4 个时全部处理，超过 8 个时继续下一轮。不存在接口级测试总数上限。
5. 每轮构建、运行精确 filter，并对新增测试逐条进行 Release 内存审计。
6. 审查每条失败的全部断言，确认差分 oracle、硬编码期望和公开契约在当前输入下彼此相容，并排除比较 helper 不支持、对象身份、别名、缓存、分配策略或清理方式造成的假差异；无法归因到公共可观察语义的测试必须删除并回写阻塞证据。
7. 更新缺口状态，再重新检查计划；仍有可执行的 `planned` 缺口就继续第 4 步。
8. 所有已知 gap 处理后，重新审查接口声明、实现条件、已有测试与本轮测试的九类覆盖清单。发现新缺口就补入 contracts/plan 并继续；没有未规划场景时才写 `closure_audit`。
9. 多接口任务按提示词列出的顺序逐个收敛，避免把不同接口的诊断混在一轮中。

继续已有任务时，先读取既有 `.gme-agent/generated_tests.json`；此前生成的测试属于已有覆盖，必须参与去重，且不得从累计清单中丢失。

## 状态模型

每个缺口只能使用：

- `planned`
- `covered_passed`
- `covered_difference_found`
- `blocked_invalid_contract`
- `blocked_unlinkable`
- `blocked_private_api`
- `blocked_no_reliable_oracle`
- `rejected_duplicate`

GME/ACIS 断言失败首先是候选行为差异，不自动代表 GME 缺陷。只有测试可构建、正常产生 GTest 结果、内存审计通过，并且失败可唯一归因于双方公共可观察语义不同时，才保留测试并标记 `covered_difference_found`；不要立即添加 `GTEST_SKIP`。

差异归类前必须确认：输入满足双方前置条件；签名、参数含义和输出解释一致；使用的比较 helper 支持当前运行时类型与属性；断言未依赖无明确契约的指针身份、对象复用、缓存、分配策略、内部别名或具体派生类型；清理逻辑正确处理别名和引用计数。任一项无法确认时，使用准确的阻塞状态，不得把实现细节差异升级为产品问题。

构建错误、异常退出、无标准 GTest 结果、泄漏或错误释放表示测试实现无效。只修本轮生成测试；修不稳就删除测试及 manifest 条目，并使用准确的阻塞状态和证据，不得修改生产代码来迁就测试。

多个 oracle 只有在当前输入及其前置条件下能同时成立时才可进入同一测试。退化输入不得默认继承普通输入 invariant；若 GME 已匹配 ACIS 而额外期望仍失败，该测试属于无效 oracle，不能计为有效行为差异。实现源码可用于定位分支和提出候选，但单侧实现、注释或当前对象布局不能替代公开契约。

## 机器可读工件

所有 JSON 使用 UTF-8 无 BOM、`schema_version: 1`。三个分析/计划文件只能包含本次选中的接口；每个选中接口都必须带准确的：

- `interface_id`
- `unique_symbol`
- `target_file`

`existing_test_coverage.json` 的接口条目包含 `existing_tests` 和 `covered_scenarios`。`interface_contracts.json` 的接口条目包含 `candidate_gaps` 和九项 `coverage_checklist`。每个候选必须包含 `category`、`preconditions`、`oracle`、`uncovered_evidence`。`interface_coverage_plan.json` 必须包含每个候选、终态 `status`、`gaps` 和最终 `closure_audit`；每个 gap 至少包含稳定的 `gap_id`、精确 `scenario`、`status`，阻塞/重复项还必须包含 `reason`，覆盖项必须包含 `test.suite` 与 `test.name`。

九个必审 category：`documented_contract`、`success_partitions`、`boundary_tolerance`、`invalid_error`、`state_sequence`、`output_invariants`、`resource_lifecycle`、`implementation_branches`、`numerical_robustness`。每项记录 `findings`、`existing_test_refs`、`candidate_gap_ids`、`blocked_scenarios`；不适用也要在 findings 中给出证据。

`.gme-agent/generated_tests.json` 必须存在；没有新增价值时允许空数组。每个标记为 `covered_passed` 或 `covered_difference_found` 的缺口都必须有且仅有一个清单条目；通过强化已有 `TEST_F` 覆盖的缺口也必须加入清单。每个条目必须包含：

```json
{
  "file": "tests/gme/src/<module>/existing_test.cpp",
  "suite": "ExistingSuite",
  "name": "NewTestName",
  "api": "api_under_test",
  "anchor": "NearbyExistingTest",
  "interface_id": "catalog-interface-id",
  "gap_id": "stable-gap-id",
  "scenario": "exactly the same scenario as the plan"
}
```

一个缺口最多对应一个生成测试；`scenario` 必须与计划逐字一致。

## 代码边界

- 只修改配置的测试仓库（通常是 `tests/gme`）和 `.gme-agent/`。
- 不修改 `module/`、`include/`、`module_lib/`、`_deps/acis/`、CMake、子仓库指针或无关测试。
- `module/<module>` 与 `module/kernel` 仅用于读取接口完整调用链，任何生产源码都不得在测试生成任务中修改。
- 只在接口目录指定的现有 `.cpp` 中插入测试，不新建测试文件，不创建 `gme_agent_*_generated_test.cpp`。
- 复用已有 fixture/suite、include、容差、初始化和生命周期模式；不新增 helper、fixture、宏或共享工具。
- 新测试使用 `TEST_F`。所有逻辑直接位于测试体内，不添加任何新注释；第一条语句是准确的 `RecordProperty("UniqueSymbol", "...")`。
- 不调用 private/protected 成员，不因头文件有声明就假设 API 可链接。
- 同时使用差分 oracle 和接口自身契约/invariant。不要只证明 GME 与 ACIS 相同；还要证明返回值、边界值、导数、拓扑或状态符合公开契约。
- 安全的非法输入要比较双方 `outcome`、输出指针和状态，不要因为没有成功结果就自动标记无可靠 oracle。
- 编辑前记录目标 C++ 的编码、BOM 和换行；只做最小局部补丁。结束时检查原始字节、`git diff`、乱码和已有中文注释。
- 清理 `timer_res_.csv`、日志、缓存及其他测试副产物，最终不留越界改动。

## 验证

优先使用任务提示词提供的配置、构建和测试命令。每轮必须：

1. 构建 Debug `tests` 目标。
2. 用本轮新增测试的精确 GTest filter 运行；仅构建通过不算完成。
3. 使用独立 Release 目录并启用 `-DTEST_MEMORY_AUDIT=ON`。
4. 每条测试在独立工作目录运行，即使 GTest 断言失败也读取 `gme_mmgr.1.log`。
5. 仅当 `Leaks: 0` 且 `Bad delete pointers: 0` 时内存审计通过；缺少日志不得声称通过。

每次修复或删除测试后重新构建、运行和审计。第一次生成不要添加 `GTEST_SKIP`。

## 最终报告

列出每个接口终态、各 gap 状态计数、修改文件、生成测试与精确 filter、功能结果、内存审计结果、行为差异、阻塞证据和仍为 `planned` 的缺口。不要用“生成了多少条”判断任务完成。
