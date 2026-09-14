---
name: gme-module-test-analyzer
description: 分析选中 GME 接口的全部已有测试并建立语义覆盖基线。用于按完整 UniqueSymbol 定位测试，提取输入分区、对象状态、调用序列、oracle 和断言，产出后续缺口规划所需的机器可读覆盖证据。
---

# GME 已有测试覆盖分析

本步骤只分析，不修改测试或生产代码。输出：

- `.gme-agent/module_test_profile.md`
- `.gme-agent/existing_test_coverage.json`

## 分析方法

对每个选中接口分别执行：

1. 用完整 `UniqueSymbol` 搜索整个目标测试仓库，再用类名、限定方法名和 GME API 名补充搜索。
2. 阅读所有命中测试的完整 `TEST_F` 测试体；不要只看文件名、测试名或目录归属。
3. 逐条记录测试文件、suite/name、输入分区、关键数值、对象初始状态、调用序列、返回/输出、oracle、断言和清理方式。
4. 将语义相同但数值不同的测试归为同一已覆盖场景；将输入、状态、调用路径或 oracle 有本质差异的测试分开。
5. 记录可复用的 fixture、include、比较容差、生命周期模式和最接近的插入锚点。不要建议新建 generated 文件或 helper。
6. 建立“参数/状态/实现分支 -> 已有测试”的反向索引。特别标出只测了普通值、只测了零点、只比较双方但未断言接口契约、以及因 `GTEST_SKIP` 未执行的测试。

已有 GME/ACIS 差异测试仍算已覆盖场景；覆盖表示场景被有效执行和比较，不等于 GME 行为与 ACIS 相同。
带 `GTEST_SKIP` 且在 API 调用前退出的测试不算有效覆盖，但必须记录为“已有未执行测试”，供 writer 选择重新启用而不是复制。

## JSON 契约

写入 UTF-8 无 BOM 的 `existing_test_coverage.json`：

```json
{
  "schema_version": 1,
  "module": "laws",
  "interfaces": [
    {
      "interface_id": "catalog-interface-id",
      "unique_symbol": "exact UniqueSymbol",
      "target_file": "tests/gme/src/laws/existing_test.cpp",
      "existing_tests": [
        {
          "file": "tests/gme/src/laws/existing_test.cpp",
          "suite": "ExistingSuite",
          "name": "ExistingCase",
          "input_partition": "precise partition",
          "object_state": "relevant state",
          "call_sequence": ["construction", "GME/ACIS call", "observation"],
          "oracle": "what is compared",
          "assertions": ["observable assertion"]
        }
      ],
      "covered_scenarios": ["normalized semantic scenario"],
      "recommended_anchor": "ExistingSuite.ExistingCase"
    }
  ]
}
```

每个选中接口必须恰有一个条目；即使没有已有测试，也要写空 `existing_tests` 和 `covered_scenarios`。接口身份与目录输入必须逐字一致。

Markdown 报告按接口列出：搜索证据、逐条已有测试、已覆盖场景、fixture/比较模式、推荐插入点，以及看似缺失但尚待契约分析确认的场景。不要在本阶段把猜测直接定为可生成缺口。
