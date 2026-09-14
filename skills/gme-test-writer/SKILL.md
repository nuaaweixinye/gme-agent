---
name: gme-test-writer
description: 将 GME 接口覆盖计划中的未覆盖缺口写成现有 tests/gme 文件内的 GME vs ACIS TEST_F。用于小批消费 planned gap、维护生成清单，并依据构建、功能运行和内存审计结果更新缺口状态。
---

# GME 覆盖缺口测试编写

## 输入与范围

开始前读取：

- `.gme-agent/existing_test_coverage.json`
- `.gme-agent/interface_contracts.json`
- `.gme-agent/interface_coverage_plan.json`
- `.gme-agent/generated_tests.json`（若已存在）
- 目标现有 `.cpp` 及其 fixture

每轮选择同一接口的 4–8 个 `planned` gap；不足 4 个时全部处理，超过 8 个时分批继续。不得为计划外场景编写测试，不得为一个 gap 写多个测试，也不得把阻塞接口替换为其他 API。接口不存在测试总数上限。

写入前再次以完整 `UniqueSymbol` 和场景关键词搜索已有测试及累计生成清单。如果语义已覆盖，不写代码，将 gap 标为 `rejected_duplicate` 并记录重复测试证据。

## 编写规则

- 只修改计划指定的现有 `tests/gme` `.cpp`、覆盖计划和生成清单；不修改生产代码、ACIS、CMake、子仓库指针或无关测试。
- 不创建新测试文件、helper、fixture、类、头文件、宏或共享工具。
- 使用目标文件已有 suite 的 `TEST_F`。所有 setup、输入、GME/ACIS 调用、比较和清理直接位于测试体内。
- 每个新测试体第一条语句是 `RecordProperty("UniqueSymbol", "计划中的准确符号")`。
- 不添加任何新注释，也不删除、移动、改写或重新格式化已有注释。
- 只调用公共且可链接的 API；遵守已确认的前置条件和对象生命周期。
- 测试名描述 gap 场景，禁止 `GeneratedCase1` 等无语义命名。
- 除 GME/ACIS 差分比较外，按 gap oracle 直接断言公开契约，例如 outcome、输出指针、端点值、导数、拓扑、对象状态或其他 invariant；避免双方共同错误仍通过。
- 对公开 API 安全范围内的无效输入，比较双方失败方式和输出状态。只有未定义行为或无稳定观察方式才使用阻塞状态。

### Oracle 一致性门禁

- 断言必须针对公共可观察语义，不能把实现手段当作结果。除非契约明确要求，禁止比较指针地址、对象身份、缓存复用、分配次数、内部别名或具体派生类型；返回不同实例但值和行为等价，不构成失败。涉及所有权时只验证文档规定的有效期、独立可修改性、销毁责任和释放行为，并按实际别名关系清理，避免重复释放或漏减引用。
- 使用 `judge_*`、结构相等或其他比较 helper 前，必须确认其实现支持当前运行时类型和需要验证的全部属性。helper 的“不支持/未知”返回值不得解释为 GME 与 ACIS 不等；无法改用公共求值、属性或数学/几何不变量时，将 gap 标为 `blocked_no_reliable_oracle`。
- 实现代码、实现注释、当前缓存行为或参数在实现中未被拒绝，只能帮助选择输入，不能单独成为硬编码期望。每个期望必须能追溯到公开契约、可靠参考结果或对当前输入成立的可验证不变量。
- 写断言前逐项检查计划中的 oracle 是否适用于当前输入。相等端点、零长度区间、容差合并、空对象等退化输入不得自动继承普通输入性质。
- 同一测试同时使用 ACIS 结果和硬编码契约值时，必须先用该输入确认 ACIS 观察值与契约值相容；只有相容时才能同时断言。
- 若 ACIS 与独立预期冲突，不得生成“GME 既等于 ACIS 又等于另一不同值”的断言。差分场景只比较可靠的共同可观察行为；契约无法确认时删除该测试并标记准确的阻塞状态。
- 运行测试后检查全部失败断言。若 GME 已与 ACIS 一致但额外 invariant 仍失败，判定为测试 oracle 无效，不得保留为 GME 行为差异，也不得用 `GTEST_SKIP` 掩盖。
- 每条保留测试的断言必须能同时成立；测试失败必须可归因于单一明确的 GME/ACIS 可观察语义差异或有证据的公开契约违反。仅观察到实现细节不同、helper 不支持或契约不确定时不得保留为失败测试。

编辑前按原始字节记录编码、BOM 与 CRLF/LF；仅用最小局部补丁，保持三者不变。编辑后检查原始字节和 `git diff`，确保无整文件变化、乱码、混合换行或已有中文注释变化。

## 清单映射

`.gme-agent/generated_tests.json` 是累计清单，UTF-8 无 BOM。保留旧条目；每个标记为 `covered_passed` 或 `covered_difference_found` 的缺口都必须有且仅有一个条目。通过强化已有 `TEST_F` 覆盖的缺口也必须加入清单。条目格式：

```json
{
  "file": "tests/gme/src/<module>/existing_test.cpp",
  "suite": "ExistingSuite",
  "name": "DescriptiveGapCase",
  "api": "api_under_test",
  "anchor": "NearbyExistingTest",
  "interface_id": "catalog-interface-id",
  "gap_id": "planned-gap-id",
  "scenario": "exactly the same scenario as interface_coverage_plan.json"
}
```

同步 `.gme-agent/generated_tests.md`，记录修改文件、测试名、接口、gap、场景和精确 GTest filter。没有可生成 gap 时仍写合法的空 `tests` 数组。

## 每轮验证与状态更新

优先使用任务提示词提供的命令：

1. 构建 Debug `tests` 目标。
2. 只运行本轮新增测试的精确 filter，并确认每条产生标准 `OK`、`FAILED` 或 `SKIPPED` 结果。
3. 使用独立 Release 目录启用 `-DTEST_MEMORY_AUDIT=ON`。
4. 每条测试在独立工作目录运行；即使断言失败，也读取该目录的 `gme_mmgr.1.log`。
5. 只有 `Leaks: 0` 且 `Bad delete pointers: 0` 才通过内存审计。

据结果更新计划：

- 构建、功能测试和内存审计均有效，GME/ACIS 相同：`covered_passed`。
- 测试有效且内存审计通过，且 GME/ACIS 的公共可观察语义不同，并已排除前置条件、helper 能力、对象身份、缓存和清理方式造成的假差异：保留测试，标记 `covered_difference_found`。
- LNK2019/unresolved external：删除测试及 manifest 条目，标记 `blocked_unlinkable`。
- private/protected 访问：先尝试公共行为；不可替代则删除并标记 `blocked_private_api`。
- 契约无效、测试异常退出或无法构造有效输入：修不稳则删除并标记 `blocked_invalid_contract`。
- 多个 oracle 在当前输入下互相矛盾：删除测试，记录双方实际值与适用前提，标记 `blocked_invalid_contract`；不得标记为 `covered_difference_found`。
- 无稳定可观察结果：删除并标记 `blocked_no_reliable_oracle`。
- 发现语义重复：不生成或删除重复测试，标记 `rejected_duplicate`。

覆盖状态必须写 `test: {"suite": "...", "name": "..."}`，阻塞和重复状态必须写具体 `reason`。每次修复或删除后重新构建、运行和审计。不得用 `GTEST_SKIP` 掩盖差异、构建问题或内存问题。

每轮结束清理 `timer_res_.csv`、日志、缓存等副产物，把控制权交回协调器继续处理剩余 `planned` gap。

所有已知 gap 完成后不要自行宣布接口完整；把控制权交回协调器进行九维 `closure_audit`。复审发现的新 gap 必须进入后续批次。
