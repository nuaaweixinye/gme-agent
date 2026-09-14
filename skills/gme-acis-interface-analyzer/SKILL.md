---
name: gme-acis-interface-analyzer
description: 校验选中 GME 接口与 ACIS R35 的公共契约和可比较场景。用于确认签名、前置条件、输入分区、可链接性、公开可见性与可靠 oracle，并结合已有覆盖提出不重复的候选缺口。
---

# GME/ACIS 接口契约分析

本步骤只分析，不修改代码。读取 `.gme-agent/existing_test_coverage.json`，并优先检查：

- `module/<module>/`
- `module/kernel/` 中对应公开 API 包装、参数校验、outcome 和输出指针处理
- `include/`
- `_deps/acis/R35/`
- 目标接口所在的 `tests/gme` 文件及邻近已有测试

输出：

- `.gme-agent/acis_interface_candidates.md`
- `.gme-agent/interface_contracts.json`

## 两阶段覆盖发现

先做黑盒契约枚举，再做白盒实现分支枚举，最后与已有覆盖逐项相减。对每个接口确认：

1. GME 和 ACIS 的准确公共入口、签名、返回值、所有权和销毁规则。
2. 前置条件、有效/无效输入、容差边界、对象状态和调用顺序。
3. 当前测试目标是否能链接，是否会触及 private/protected 成员，是否依赖未准备模块、license、UI、网络或随机状态。
4. 可观察且稳定的 oracle：返回码、对象属性、求值、几何性质或已有一致比较模式。
5. 与 `covered_scenarios` 对照；语义已覆盖的候选不要再标为可生成缺口。
6. 从实现中枚举 `if/switch`、早返回、容差判断、空指针/错误分支、循环基数和退化路径；每个可公开触达的分支都必须在清单中有已有测试、候选 gap、阻塞证据或不适用解释。

不要只因头文件声明存在就断言可链接。优先采用已有测试的调用方式，并用实现、导出符号或构建配置补充证据。

## Oracle 一致性

- 按证据强度区分公开契约与实现细节。公开文档、公共声明中的明确约束和稳定的既有公共行为可作为契约证据；单侧实现分支、注释、当前返回对象布局或“代码接受该值”只能用于发现候选，不能单独证明该行为必须成立。
- 只测试调用者可观察的语义。指针地址相等、对象实例是否复用、缓存命中、分配次数、内部别名和具体派生类型默认属于实现细节；除非公共契约明确规定其身份、所有权或生命周期语义，否则不得作为 gap oracle。需要验证所有权时，改用契约明确的独立可修改性、有效期、销毁责任和重复释放行为。
- 采用已有比较 helper 前必须读取其实现或明确能力说明，确认它支持当前双方结果的运行时类型、退化形态和待比较属性。helper 对类型未知时返回失败、仅比较部分结构或依赖指针身份，均不能作为行为差异证据；应改用公共求值、属性、几何不变量或将场景阻塞。
- 区分普通输入契约与退化、容差相等、非法或未定义输入语义。除非公开文档、双方实现或可靠已有测试提供证据，不得把普通输入的不变量外推到退化输入。
- 一个候选同时使用 ACIS 差分结果和独立契约值时，先确认该契约适用于当前输入，并确认 ACIS 的实际可观察结果满足该契约。两套 oracle 不一致时不得把它们同时写入候选。
- 若 ACIS 行为稳定但与推测的数学性质或输入参数不一致，记录冲突证据；根据目标选择单一可靠 oracle，或将场景标记为 `blocked_invalid_contract` / `blocked_no_reliable_oracle`。不得用未经证明的期望值制造必然矛盾的断言。
- 每个 candidate gap 的 `oracle` 必须说明依据、适用前提和具体可观察量，使测试失败能够唯一归因于 GME/ACIS 语义差异或明确契约违反。仅能证明实现方式不同的候选必须阻塞，不得进入生成计划。

必须逐项审查九类覆盖维度：

1. `documented_contract`：每个参数、返回值、输出对象、所有权及文档约束。
2. `success_partitions`：正常输入等价类、枚举/布尔组合、正负零和主要对象状态。
3. `boundary_tolerance`：相等、近似相等、容差内外、空/单元素、最小最大边界。
4. `invalid_error`：公开 API 安全边界内的无效、冲突或不完整输入；比较双方 outcome、输出指针和状态。
   不得因为测试位于某个业务模块目录，就跳过 `module/kernel` 的公开入口。必须沿 GME 调用链确认契约由 kernel 包装层还是业务实现层承担。
5. `state_sequence`：初始化状态、重复调用、调用顺序、别名、幂等性和可见副作用。
6. `output_invariants`：文档直接约束、返回值、端点/导数、拓扑或其他不变量；不能只比较 GME 与 ACIS 相同。
7. `resource_lifecycle`：所有权、空输出、清理和可重复释放边界。
8. `implementation_branches`：实现条件、早返回、退化路径和不同算法分支。
9. `numerical_robustness`：平移、缩放、符号、近奇异条件及安全的有限极值。

控制组合爆炸：先覆盖单维边界，再对相互影响且可能触发不同实现路径的维度做风险驱动的 pairwise 组合；不要用随机数量代替分区依据。

## JSON 契约

写入 UTF-8 无 BOM 的 `interface_contracts.json`：

```json
{
  "schema_version": 1,
  "module": "laws",
  "interfaces": [
    {
      "interface_id": "catalog-interface-id",
      "unique_symbol": "exact UniqueSymbol",
      "target_file": "tests/gme/src/laws/existing_test.cpp",
      "gme_api": "qualified GME API",
      "acis_api": "qualified ACIS API",
      "preconditions": ["required condition"],
      "valid_input_partitions": ["partition"],
      "oracle": "stable comparison",
      "link_evidence": ["source or existing-test evidence"],
      "coverage_checklist": [
        {
          "category": "boundary_tolerance",
          "findings": ["implementation has a tolerance-controlled equality branch"],
          "existing_test_refs": ["ExistingSuite.ExistingCase"],
          "candidate_gap_ids": ["stable-interface-local-id"],
          "blocked_scenarios": []
        }
      ],
      "candidate_gaps": [
        {
          "gap_id": "stable-interface-local-id",
          "category": "boundary|partition|state|sequence|error",
          "scenario": "precise behavior not present in existing coverage",
          "preconditions": ["condition"],
          "oracle": "expected comparison",
          "uncovered_evidence": "which existing tests and implementation branches prove this is missing",
          "confidence": "high|medium"
        }
      ],
      "blocked_scenarios": [
        {
          "scenario": "candidate that cannot be tested reliably",
          "status": "blocked_invalid_contract|blocked_unlinkable|blocked_private_api|blocked_no_reliable_oracle",
          "reason": "concrete evidence"
        }
      ]
    }
  ]
}
```

每个选中接口必须恰有一个条目；没有候选时写空 `candidate_gaps`。九个 checklist category 必须各有一项并包含 findings、existing_test_refs、candidate_gap_ids、blocked_scenarios；每个候选 gap 至少被一个 checklist 项引用。`gap_id` 在该接口内稳定且唯一。Markdown 报告按接口解释契约证据、实现分支、可行候选、重复候选和阻塞场景，供协调器建立最终覆盖计划。
