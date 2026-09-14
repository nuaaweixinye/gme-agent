from __future__ import annotations


def bug_fix_prompt(
    failure: dict | list[dict],
    target_repo: str = "",
    *,
    candidate_repos: list[str] | None = None,
    test_repo: str = "",
    test_file: str = "",
    gtest_filter: str = "",
    before_output: str = "",
    configure_command: str = "",
    build_command: str = "",
    test_command: str = "",
) -> str:
    failures = failure if isinstance(failure, list) else [failure]
    primary = failures[0] if failures else {}
    reproduce = primary.get("reproduce_command") or "运行所选失败对应的准确 GTest filter。"
    candidates = list(dict.fromkeys(str(path).strip() for path in (candidate_repos or []) if str(path).strip()))
    if target_repo and not candidates:
        candidates = [target_repo]
    if len(candidates) == 1:
        target_rule = f"- 生产代码只能修改 `{candidates[0]}`。可以读取 GME worktree 的其他内容作为上下文。\n"
    elif candidates:
        target_rule = (
            "- 可追踪的生产源码仓库为："
            + "、".join(f"`{path}`" for path in candidates)
            + "。必须先沿调用链定位真实缺陷归属，最终只能修改其中一个仓库；若需要跨仓库修改，停止并说明。\n"
        )
    else:
        target_rule = ""
    fallback_filter = ":".join(
        ".".join(part for part in [str(item.get("test_suite") or ""), str(item.get("test_name") or "")] if part)
        for item in failures
    )
    failure_details = "\n".join(
        f"- {item.get('id')}: {item.get('test_suite')}.{item.get('test_name')}\n  {item.get('reason')}"
        for item in failures
    )
    output_excerpt = (before_output or "").strip()
    if len(output_excerpt) > 4000:
        output_excerpt = output_excerpt[-4000:]
    return f"""你正在 GME 仓库中工作。

目标：
- 修复生产代码中这个已确认的 GME/ACIS 行为差异。
- 本任务必须同时修复以下 {len(failures)} 条失败：
{failure_details}
- GTest filter：{gtest_filter or fallback_filter}
- 已复制到当前修复 worktree 的复现测试文件：{test_file or test_repo}

复现命令：
```powershell
{reproduce}
```

当前修复 worktree 的验证命令：
```powershell
{configure_command or "# 配置命令由 GME Test Agent 管理。"}
{build_command or "# 构建命令由 GME Test Agent 管理。"}
{test_command or "# 测试命令由 GME Test Agent 管理。"}
```

修复前观察到的失败输出：
```text
{output_excerpt or "GME Test Agent 已在调用 DeepSeek Harness 智能体前复现所选测试失败。"}
```

规则：
- GME superproject worktree 已准备测试所属业务模块与 `module/kernel`；测试模块不等于缺陷源码归属仓库。
{target_rule.rstrip()}
- 不要修改 `include/` 路径下的文件。
- 不要修改 `{test_repo or "tests/gme"}` 或任何测试文件。复制过来的生成测试只作为验证输入。
- 不要添加 `GTEST_SKIP`、弱化断言、删除测试或修改期望值。
- 修改前必须从公开 API 声明和入口开始，沿调用链找到实际计算或状态变化的位置；检查参数语义、输入规范化、边界条件、错误处理、返回值和对象所有权。
- 如果业务模块只负责解析或内部算法，而公开 `api_*` 包装、参数校验或 outcome 处理位于 `module/kernel`，必须把修复归属到第一个产生差异的真实实现仓库。
- 对照失败输入下 GME 与 ACIS 的执行路径，明确第一个产生行为差异的位置。未完成调用链和根因分析前不要修改源码。
- 阅读相邻实现和已有测试，排除无效测试、未定义行为或不可靠 oracle。
- 修改必须尽可能小，并让该失败场景下的 GME 行为与 ACIS 一致。
- 不要针对当前测试输入硬编码结果，也不要绕过或重复已有内部算法。
- 使用局部补丁并保持原文件编码、BOM 和换行方式；不要整文件转码或格式化无关代码。
- 新增注释只允许使用简洁中文解释非显然逻辑，不要增加批量注释或文档块。
- 修改代码后构建 tests 目标。
- 构建完成后，运行给定的联合 GTest filter；每一条所选测试都必须执行且通过。
- 如果构建或任意所选测试仍失败，继续修复模块实现并重复构建/测试，直到全部通过，或能够明确说明为什么不存在安全的生产代码修复方案。
- 返回前在工具可用时检查修改过的 C/C++ 文件是否符合 clang-format；需要格式化时只处理本次修改文件，并复查 diff 没有无关变化。
- 智能体返回后，GME Test Agent 会重新执行修改范围检查、clang-format 检查、准确目标测试、当前模块全量测试和目标测试 Release 内存审计；不要把未实际执行的检查描述为已通过。

交付物：
- 只包含生产代码修改。
- 公开 API 入口、关键调用链、第一个差异点及根因位置。
- 实际修改的源码仓库，并说明为什么缺陷属于该仓库而不是测试所属模块。
- 实际执行过的构建和测试结果摘要；未执行的检查必须明确标为未验证。
- 仍然存在的风险或后续事项。
"""
