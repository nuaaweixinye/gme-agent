# 测试生成的知识注入与轨迹可观测

设计见 `docs/superpowers/specs/2026-09-15-test-generation-knowledge-injection-design.md`，
实施计划见 `docs/superpowers/plans/2026-09-15-test-generation-knowledge-injection.md`。

## 它做什么

生成（以及扩展/重试）任务在建提示词之前：

1. 从 `gme_agent.db` 取本地先验。匹配分三档并带权重：`interface_id` 精确命中 > `api_name` 命中 > 同模块命中；同档内"已确认"优先，再按稳定复现次数降序。没有 `interface_id` 也没有 `api_name` 的失败永远不会被注入。
2. 逐库检索 WeKnora（kb00 / kb01 / kb02 / kb03），**每个库一次请求**——实测四库一起查时 10 条命中全部来自 KB00，把其余库挤掉了。每个库有独立超时与独立降级。
3. 按 `local > kb01 > kb02 > kb03 > kb00` 的顺序与 4000 字预算合并，超出预算按优先级裁剪并标注 `（已截断）`。
4. 渲染成提示词的一节，并把原文写到 `artifacts/<job_id>/knowledge_context.md`。
5. 写一条 `knowledge/assembled` 事件，把各来源命中数、命中的接口、耗时、是否降级记进 `jobs.metadata.knowledge`。

记录失败后，若 `knowledge.closed_loop.enabled`，满足全部四项判据的失败会写
`failures.metadata_json.knowledge`（不新增表、不改 `status`），下次任务即成为**已确认先验**：

1. 真实分歧：reason 里出现比较 helper（`same_acis_gme`，或 `gme_*` / `acis_*` 形态的标识符），且不含链接/编译/超时类标记；
2. 至少 `min_stable_runs` 个**不同 `run_id`** 观测到 failed；
3. 该用例仍在 `.gme-agent/generated_tests.json` 中；
4. `metadata_json` 含 `interface_id` 或 `api_name`。

每次被注入时 `injected_count += 1`，用于事后评估注入是否真的提升了命中率。

内层编码会话的工具调用会以 `agent/tool-call` / `agent/tool-result` 事件落库：整行经脱敏、单行 600 字符上限（保留行首的工具名）、每轮 200 条上限，超限时记一条 `agent/tool-events-suppressed`。

## 打开它

`config.local.json`（或设置页保存后的配置文件）里：

```json
"knowledge": {
  "enabled": true,
  "weknora": {
    "base_url": "http://<weknora-host>/api/v1",
    "api_key_env": "WEKNORA_API_KEY",
    "timeout_ms": 3000,
    "knowledge_bases": [
      { "label": "kb00", "id": "<kb00-knowledge-base-id>" },
      { "label": "kb01", "id": "<kb01-knowledge-base-id>" },
      { "label": "kb02", "id": "<kb02-knowledge-base-id>" },
      { "label": "kb03", "id": "<kb03-knowledge-base-id>" }
    ]
  },
  "budgets": { "max_priors": 8, "max_kb_hits": 6, "max_chars": 4000 },
  "closed_loop": { "enabled": true, "min_stable_runs": 2 }
}
```

密钥从环境变量读（默认 `WEKNORA_API_KEY`，本机在 `~/.dsh/.env（或任何能导出该环境变量的地方）`）。后端进程必须能读到它，
否则 KB 会写成 `API key is not set` 而只用本地先验。默认 `enabled: false`，不配置就与改造前完全一致
（不检索、不落盘、不发事件、不升格，提示词逐字节不变）。

`label` 决定来源标签与优先级，`id` 是知识库 UUID；四个库的 id 就是上表这些值。`knowledge_bases` 留空会回落到这四个默认库。

## 怎么验证

```powershell
$py = "C:\ProgramData\Miniconda3\envs\agent\python.exe"

# 1. 检索连通性（逐库命中数 + 延迟；离线时会看到 degraded，属预期）
$env:WEKNORA_API_KEY = ((Get-Content ~/.dsh/.env（或任何能导出该环境变量的地方） | Where-Object { $_ -match '^WEKNORA_API_KEY' }) -replace '^WEKNORA_API_KEY=','')
Set-Location <repo>
& $py -c "import sys,os; sys.path.insert(0,'backend'); from gme_agent.knowledge.weknora import WeKnoraClient; from gme_agent.settings.config import AgentConfig; c=AgentConfig().knowledge.weknora; client=WeKnoraClient(base_url=c.base_url, api_key=os.environ.get(c.api_key_env,''), sources=c.source_ids(), timeout_ms=c.timeout_ms); [print(o.source, len(o.hits), o.error[:60]) for o in client.search_all('derivative_law evaluate 导数 容差', max_results=6)]"

# 2. 分歧判据（只读，在仓库根目录执行；SQLite 只读模式不会建库）
& $py -c "import sys,sqlite3; sys.path.insert(0,'backend'); from gme_agent.knowledge.promote import is_divergence_reason; db=sqlite3.connect('file:gme_agent.db?mode=ro', uri=True); db.row_factory=sqlite3.Row; rows=db.execute('select reason from failures').fetchall(); print(sum(1 for r in rows if is_divergence_reason(r['reason'])), '/', len(rows))"

# 3. 跑一个小任务后看三处证据
Get-Content artifacts\<job_id>\knowledge_context.md
& $py -c "import sqlite3; db=sqlite3.connect('gme_agent.db'); print([r[0] for r in db.execute(\"select message from events where message like 'knowledge/%' or message like 'agent/tool-%' order by id\")][:20])"
& $py -c "import json,sqlite3; db=sqlite3.connect('gme_agent.db'); db.row_factory=sqlite3.Row; print([json.loads(r['metadata_json']).get('knowledge') for r in db.execute('select metadata_json from failures')][:3])"
```

## Bootstrap 注意

2026-09-15 实测：任务库里 13 条失败**每条只有 1 个 `run_id`**。`min_stable_runs: 2`（默认）下，
它们会先以"疑似分歧"（`observed`）注入；只有同一用例在第二次运行里再次失败，才会变成"已确认分歧"。
若希望立刻把现有 13 条升格，可临时设 `"min_stable_runs": 1` 跑一次任务，再改回 2。

## 降级行为

| 情形 | 表现 |
|---|---|
| 知识注入未开启 | 不检索、不落盘、不发事件、不升格，提示词与改造前逐字节一致 |
| `WEKNORA_API_KEY` 未设置 | 只用本地先验，`knowledge_context.md` 的"检索状态"写明 `API key is not set` |
| 单个库超时/报错 | 该库为空并写明原因（HTTP 状态 + 响应片段、超时毫秒数、传输失败、非 JSON），其余库与本地先验照常注入 |
| 命中为空 | 不写空节，只在元数据里记 `hit_count: 0` |
| 超出字数预算 | 按优先级裁剪并标注 `（已截断）` |
| 注入内部异常 | 记 `knowledge/injection-failed` 警告，任务继续（不注入） |
| 升格失败/清单读不到 | 记 `knowledge/promotion-skipped` / `knowledge/promotion-failed` 警告，任务继续 |
| 事件写入者本身抛错 | 工具事件计数并吞掉，绝不把已经跑完的 turn 变成失败任务 |
