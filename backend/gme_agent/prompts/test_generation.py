from __future__ import annotations

from collections import defaultdict
from typing import Any


def test_generation_prompt(
    module: str,
    api_name: str,
    target_repo: str = "tests/gme",
    build_guidance: str | None = None,
    *,
    selected_interfaces: list[dict[str, Any]] | None = None,
) -> str:
    return _generation_prompt(
        module,
        api_name,
        target_repo,
        build_guidance,
        list(selected_interfaces or []),
        continuation=False,
    )


def continue_test_generation_prompt(
    module: str,
    api_name: str,
    target_repo: str = "tests/gme",
    build_guidance: str | None = None,
    *,
    selected_interfaces: list[dict[str, Any]] | None = None,
) -> str:
    return _generation_prompt(
        module,
        api_name,
        target_repo,
        build_guidance,
        list(selected_interfaces or []),
        continuation=True,
    )


def _generation_prompt(
    module: str,
    api_name: str,
    target_repo: str,
    build_guidance: str | None,
    selected: list[dict[str, Any]],
    *,
    continuation: bool,
) -> str:
    module_path = module.strip().replace("\\", "/").strip("/")
    target = f"选中的 {len(selected)} 个接口" if selected else (api_name or "选中的 API")
    mode = "继续已有任务，重新分析本次选中接口" if continuation else "开始新的覆盖提升任务"
    placement = (
        "只修改结构化选择中列出的现有 `.cpp` 文件"
        if selected
        else f"插入 `{target_repo}/src/{module_path}/` 下职责最匹配的现有 `.cpp` 文件"
    )
    return f"""你正在 GME superproject worktree 中{mode}。

目标：针对 {target} 提升 GME vs ACIS 的语义场景覆盖。不要按固定总数生成测试，也没有每接口总上限；先证明已有覆盖，再系统枚举未覆盖且可验证的功能缺口，生成不重复测试并循环到接口完整、饱和、阻塞或安全预算耗尽。

- 目标模块：`{module_path}`
- 目标测试仓库：`{target_repo}`
- 放置规则：{placement}

{_selection_block(selected, continuation=continuation)}

必须按以下闭环执行：
1. 使用 `gme-module-test-analyzer`，以完整 `UniqueSymbol`、类名和方法名搜索所有已有测试，逐条分析输入、状态、调用序列和断言；写 `.gme-agent/module_test_profile.md` 与 `.gme-agent/existing_test_coverage.json`。
2. 使用 `gme-acis-interface-analyzer` 做黑盒契约分析和白盒实现分支分析；必须逐项审查九类覆盖维度：`documented_contract`、`success_partitions`、`boundary_tolerance`、`invalid_error`、`state_sequence`、`output_invariants`、`resource_lifecycle`、`implementation_branches`、`numerical_robustness`。写 `.gme-agent/acis_interface_candidates.md` 与 `.gme-agent/interface_contracts.json`。
3. 对照已有覆盖和接口契约创建 `.gme-agent/interface_coverage_plan.json`。计划只能复用 `interface_contracts.json` 中候选的准确 `gap_id` 与 `scenario`；契约中的每个候选都必须进入计划，不得为了缩短任务省略。
4. 按选中接口顺序处理；每轮使用 `gme-test-writer` 消费 4–8 个 `planned` 缺口。某接口不足 4 个时全部处理，超过 8 个时分批循环；没有接口级测试总数上限。
5. 每轮构建、用精确 GTest filter 运行新增测试，并逐条完成 Release 内存审计。然后更新缺口状态，再回到第 4 步；只要仍有可执行的 `planned` 缺口就不得停止。
6. 所有已知 gap 处理后，重新读取接口声明、实现分支、已有测试和本轮测试，执行一次九维闭环复审；新发现缺口必须补入 contracts 和 plan 后继续生成。只有复审没有未规划场景时，接口才可标为 `complete`、`saturated`、`blocked` 或 `budget_exhausted`。除 `budget_exhausted` 外不得残留 `planned` 缺口。

状态规则：
- GME/ACIS 断言差异首先是候选发现，不自动代表 GME 缺陷。只有失败可唯一归因于双方公共可观察语义不同时，才保留测试并标记 `covered_difference_found`；不得立即 `GTEST_SKIP`。
- 归类前必须确认输入满足双方前置条件、参数与输出含义一致、比较 helper 支持当前运行时类型，并排除无契约依据的指针身份、对象复用、缓存、内部别名、分配策略和错误清理造成的假差异。无法确认可靠 oracle 时删除测试并使用准确阻塞状态。
- 构建失败、异常退出、无标准 GTest 结果或内存审计失败表示测试无效；只修本次测试，修不稳则删除 manifest 条目，并按准确原因标记 `blocked_invalid_contract`、`blocked_unlinkable`、`blocked_private_api` 或 `blocked_no_reliable_oracle`。`unresolved external/LNK2019` 属于不可链接。
- 构建并运行通过的缺口标记 `covered_passed`。与已有测试语义重复的候选标记 `rejected_duplicate`，并写明重复证据。
- “GME 与 ACIS 相同”不是唯一 oracle。凡接口文档定义了返回值、端点值、导数、拓扑、对象状态或其他不变量，测试必须同时加入直接契约断言，避免两端共同错误仍被判为通过。
- 不要仅因非法输入没有成功结果就放弃测试；只要调用符合公开 API 安全边界，就比较 ACIS/GME 的 `outcome`、输出指针和状态变化。只有可能触发未定义行为或缺少稳定观察方式时才阻塞。

硬性边界：
- 只生成测试，不修改 `module/`、`include/`、`module_lib/`、`_deps/acis/`、CMake、子仓库指针或无关测试。
- 测试代码只能修改 `{target_repo}`；非测试改动只能位于 `.gme-agent/`。结束前删除 `timer_res_.csv`、日志、缓存等越界副产物。
- 不创建新的测试 `.cpp` 或 `gme_agent_<module>_generated_test.cpp`；不新增 helper、fixture、宏或共享工具。复用指定文件已有 suite/fixture、include、容差和生命周期模式。
- 新测试必须使用 `TEST_F`。每个测试所有构造、调用、对比和清理都直接写在函数体内，不添加任何新注释。
- 每个新增 `TEST_F` 函数体第一条语句必须是准确的 `RecordProperty("UniqueSymbol", "...")`。
- 编辑前记录目标 C++ 的编码、BOM 和换行，使用最小局部补丁并保持不变；检查 `git diff`、乱码和已有中文注释。
- `.gme-agent/*.json` 必须为 UTF-8 无 BOM。{_continuation_rule(continuation)}

机器可读文件必须使用 `schema_version: 1`，且只包含本次选中的接口。`existing_test_coverage.json` 和 `interface_contracts.json` 的每个接口必须包含准确的 `interface_id`、`unique_symbol`、`target_file`，并分别包含 `existing_tests`、`candidate_gaps` 数组。

`interface_contracts.json` 的每个候选 gap 还必须包含 `category`、`preconditions`、`oracle` 和说明为何现有测试未覆盖的 `uncovered_evidence`。每个接口必须包含九项 `coverage_checklist`；每项格式如下，九个 category 必须恰好各出现一次：
```json
{{
  "category": "boundary_tolerance",
  "findings": ["implementation uses an equality tolerance; inside/outside cases are distinct"],
  "existing_test_refs": ["ExistingSuite.ExistingCase"],
  "candidate_gap_ids": ["stable-gap-id"],
  "blocked_scenarios": []
}}
```

`.gme-agent/interface_coverage_plan.json` 核心结构：
```json
{{
  "schema_version": 1,
  "module": "{module_path}",
  "interfaces": [{{
    "interface_id": "catalog-interface-id",
    "unique_symbol": "exact UniqueSymbol",
    "target_file": "{target_repo}/src/{module_path}/existing_test.cpp",
    "status": "complete|saturated|blocked|budget_exhausted",
    "closure_audit": {{
      "performed": true,
      "reviewed_categories": ["documented_contract", "success_partitions", "boundary_tolerance", "invalid_error", "state_sequence", "output_invariants", "resource_lifecycle", "implementation_branches", "numerical_robustness"],
      "remaining_unplanned_scenarios": [],
      "evidence": ["re-read declarations, implementation branches, existing tests, and generated tests"]
    }},
    "gaps": [{{
      "gap_id": "stable-interface-local-id",
      "scenario": "precise non-duplicate behavior",
      "status": "planned|covered_passed|covered_difference_found|blocked_invalid_contract|blocked_unlinkable|blocked_private_api|blocked_no_reliable_oracle|rejected_duplicate",
      "reason": "evidence when blocked or duplicate",
      "test": {{"suite": "ExistingSuite", "name": "NewTestName"}}
    }}]
  }}]
}}
```

`.gme-agent/generated_tests.json` 必须存在；没有可新增缺口时允许 `tests` 为空。每个标记为 `covered_passed` 或 `covered_difference_found` 的缺口都必须有且仅有一个对应条目；通过强化已有 `TEST_F` 覆盖的缺口也必须加入清单。每条清单记录必须映射到唯一的已覆盖缺口：
```json
{{
  "tests": [{{
    "file": "{target_repo}/src/{module_path}/existing_test.cpp",
    "suite": "ExistingSuite",
    "name": "NewTestName",
    "api": "api_or_class_under_test",
    "anchor": "nearby existing test name",
    "interface_id": "catalog-interface-id",
    "gap_id": "stable-interface-local-id",
    "scenario": "exactly the same scenario as the plan"
  }}]
}}
```

{_build_guidance_block(module_path, build_guidance)}

最终回复列出每个接口的状态和缺口计数、生成测试与精确 filter、功能结果、内存审计结果、差异发现、阻塞项及剩余 `planned` 缺口。不要用“达到测试数量”作为完成条件。
"""


def _selection_block(interfaces: list[dict[str, Any]], *, continuation: bool) -> str:
    if not interfaces:
        return "结构化接口选择：未提供。按自由文本目标执行，但仍遵循覆盖闭环。"

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for interface in interfaces:
        grouped[str(interface.get("target_file") or "")].append(interface)
    lines = [
        "结构化接口选择（权威输入）：",
        "- 只能为下列接口生成测试；不得用其他 API 替换阻塞接口。",
        "- 测试数量由可证明的未覆盖场景决定，不设每接口总配额；单个验证批次处理 4–8 个 gap。",
    ]
    if continuation:
        lines.append("- 本次只重新规划下列接口；保留此前生成清单中的其他测试。")
    for file_path, file_interfaces in grouped.items():
        lines.append(f"- 目标文件：`{file_path}`")
        for interface in file_interfaces:
            lines.append(
                "  - "
                f"接口 ID `{interface.get('id')}`；"
                f"fixture `{interface.get('test_suite')}`；"
                f"UniqueSymbol `{interface.get('unique_symbol')}`"
            )
    return "\n".join(lines)


def _continuation_rule(continuation: bool) -> str:
    if not continuation:
        return ""
    return (
        "继续任务时先读取并保留既有 generated_tests 清单；重新生成本次接口的三个覆盖工件，"
        "旧测试也必须作为已有覆盖参与去重。"
    )


def _build_guidance_block(module_path: str, build_guidance: str | None) -> str:
    if build_guidance:
        return build_guidance.strip()
    develop_option, test_option = _module_cmake_options(module_path)
    return f"""构建验证命令：
- GME Test Agent 未提供任务专用命令时，使用以下默认命令。
- 构建目录：`{{worktree}}/build/vscode`
- 配置：
  `cmake -S {{worktree}} -B {{worktree}}/build/vscode -G "Visual Studio 17 2022" -A x64 -DBUILD_ALL_MODULE=OFF -DBUILD_DEMO=OFF -DBUILD_BENCHTEST=OFF -DBUILD_TEST=ON -DBUILD_FORMAT=OFF {develop_option} {test_option}`
- 构建：
  `cmake --build {{worktree}}/build/vscode --config Debug --target tests --parallel`
- 构建成功后，必须使用 `.gme-agent/generated_tests.json` 中新增测试的准确 filter 运行测试；仅构建通过不算完成：
  `{{worktree}}/build/vscode/Debug/tests.exe --gtest_filter=<exact-generated-filter>`"""


def _module_cmake_options(module_path: str) -> tuple[str, str]:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in module_path).strip("_").upper()
    if not normalized:
        return "", ""
    return f"-DDEVELOP_{normalized}=ON", f"-DTEST_{normalized}=ON"
