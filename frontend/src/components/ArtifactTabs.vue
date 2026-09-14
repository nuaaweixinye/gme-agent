<script setup>
import { GitPullRequest, Trash2 } from "@lucide/vue";
import { computed } from "vue";
import { useWorkspace } from "../composables/useWorkspace";

const {
  activeJobTab,
  busyAction,
  codeViewMode,
  selectedCodeFile,
  selectedJob,
  selectedJobFailures,
  eventsText,
  diffText,
  agentText,
  generatedTestRows,
  submittedTestRows,
  selectedGeneratedTestKeys,
  selectedGeneratedTestCount,
  testResultSummary,
  codeChangeFiles,
  selectedCodeChangeFile,
  openPr,
  openPrUrl,
  createSelectedTestsPr,
  deleteSelectedGeneratedTests,
} = useWorkspace();

const debugTabs = [
  { value: "logs", label: "日志" },
  { value: "codex", label: "智能体输出" },
];

const debugTabValues = new Set(debugTabs.map((tab) => tab.value));
const debugSelectValue = computed(() => (debugTabValues.has(activeJobTab.value) ? activeJobTab.value : ""));
</script>

<template>
  <section class="panel detail-panel review-panel">
    <div class="tabbar review-tabbar">
      <button type="button" :class="{ active: activeJobTab === 'results' }" @click="activeJobTab = 'results'">测试结果</button>
      <button type="button" :class="{ active: activeJobTab === 'changes' }" @click="activeJobTab = 'changes'">代码变更</button>
      <button type="button" :class="{ active: activeJobTab === 'failures' }" @click="activeJobTab = 'failures'">失败用例</button>

      <select class="debug-select" :value="debugSelectValue" @change="activeJobTab = $event.target.value || 'results'">
        <option value="">调试信息</option>
        <option v-for="tab in debugTabs" :key="tab.value" :value="tab.value">{{ tab.label }}</option>
      </select>

      <button class="tab-action" type="button" @click="openPr">
        <GitPullRequest :size="15" />
        打开最新 PR
      </button>
    </div>

    <div v-if="activeJobTab === 'results'" class="review-content">
      <div class="result-strip">
        <div>
          <span>修改文件</span>
          <strong>{{ testResultSummary.files }}</strong>
        </div>
        <div>
          <span>新增测试</span>
          <strong>{{ testResultSummary.total }}</strong>
        </div>
        <div>
          <span>通过</span>
          <strong>{{ testResultSummary.passed }}</strong>
        </div>
        <div class="danger">
          <span>失败</span>
          <strong>{{ testResultSummary.failed }}</strong>
        </div>
        <div>
          <span>已提 PR</span>
          <strong>{{ testResultSummary.submitted }}</strong>
        </div>
        <div>
          <span>未确认</span>
          <strong>{{ testResultSummary.unknown }}</strong>
        </div>
      </div>

      <div class="review-actions">
        <span>已选择 {{ selectedGeneratedTestCount }} 个生成测试</span>
        <div class="review-action-buttons">
          <button class="success-button compact" type="button" :disabled="!!busyAction || selectedJob?.active || !selectedGeneratedTestCount" @click="createSelectedTestsPr">
            <GitPullRequest :size="14" />
            提交选中测试 PR
          </button>
          <button class="danger-button compact" type="button" :disabled="!!busyAction || selectedJob?.active || !selectedGeneratedTestCount" @click="deleteSelectedGeneratedTests">
            <Trash2 :size="14" />
            删除选中测试
          </button>
        </div>
      </div>

      <div class="review-table-wrap result-table-scroll">
        <table class="review-table">
          <thead>
            <tr>
              <th class="check-cell"></th>
              <th>状态</th>
              <th>测试</th>
              <th>API</th>
              <th>文件</th>
              <th>行号</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="test in generatedTestRows" :key="test.fullName" :class="{ failed: test.status === 'failed' }">
              <td class="check-cell">
                <input v-model="selectedGeneratedTestKeys" type="checkbox" :value="test.fullName" :aria-label="`选择 ${test.fullName}`" />
              </td>
              <td><span class="status-pill" :class="test.status">{{ test.statusLabel }}</span></td>
              <td><code>{{ test.fullName }}</code></td>
              <td>{{ test.api || "-" }}</td>
              <td class="truncate">{{ test.file }}</td>
              <td>{{ test.line || "-" }}</td>
            </tr>
            <tr v-if="!generatedTestRows.length">
              <td colspan="6" class="empty-cell">暂无生成测试清单</td>
            </tr>
          </tbody>
        </table>
      </div>

      <section v-if="submittedTestRows.length" class="submitted-tests-section">
        <div class="submitted-tests-heading">
          <div>
            <h3>已提交 PR 测试</h3>
            <span>这些测试仍保留在当前任务中，但不会重复进入新的 PR。</span>
          </div>
          <span class="mini-badge">{{ submittedTestRows.length }} 个</span>
        </div>

        <div class="review-table-wrap submitted-table-scroll">
          <table class="review-table">
            <thead>
              <tr>
                <th>结果</th>
                <th>测试</th>
                <th>API</th>
                <th>文件</th>
                <th>PR</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="test in submittedTestRows" :key="`submitted-${test.fullName}`">
                <td><span class="status-pill" :class="test.status">{{ test.statusLabel }}</span></td>
                <td><code>{{ test.fullName }}</code></td>
                <td>{{ test.api || "-" }}</td>
                <td class="truncate">{{ test.file }}</td>
                <td>
                  <button class="ghost-button compact" type="button" :disabled="!test.prUrl" @click="openPrUrl(test.prUrl)">
                    <GitPullRequest :size="14" />
                    {{ test.prLabel }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>

    <div v-else-if="activeJobTab === 'changes'" class="review-content code-review">
      <div v-if="codeChangeFiles.length" class="code-review-layout">
        <aside class="change-file-list">
          <button
            v-for="file in codeChangeFiles"
            :key="file.path"
            type="button"
            :class="{ active: selectedCodeFile === file.path, failed: file.failedCount }"
            @click="selectedCodeFile = file.path"
          >
            <strong>{{ file.path }}</strong>
            <span>+{{ file.additions }} / -{{ file.deletions }} · {{ file.tests.length }} 测试</span>
          </button>
        </aside>

        <div class="change-detail">
          <div class="change-heading">
            <div>
              <h3>{{ selectedCodeChangeFile?.path }}</h3>
              <span>新增 {{ selectedCodeChangeFile?.additions || 0 }} 行，删除 {{ selectedCodeChangeFile?.deletions || 0 }} 行</span>
            </div>
            <div class="segmented">
              <button type="button" :class="{ active: codeViewMode === 'tests' }" @click="codeViewMode = 'tests'">新增 TEST_F</button>
              <button type="button" :class="{ active: codeViewMode === 'raw' }" @click="codeViewMode = 'raw'">完整 diff</button>
            </div>
          </div>

          <div v-if="codeViewMode === 'tests'" class="test-blocks">
            <article
              v-for="block in selectedCodeChangeFile?.blocks || []"
              :key="block.fullName"
              class="test-block"
              :class="{ failed: block.status === 'failed' }"
            >
              <div class="test-block-title">
                <div>
                  <strong>{{ block.fullName }}</strong>
                  <span>{{ block.api || "未标注 API" }}</span>
                </div>
                <span class="status-pill" :class="block.status">{{ block.statusLabel }}</span>
              </div>
              <pre class="code-block compact-code">{{ block.code || "没有从 diff 中提取到完整 TEST_F 块，可切换到完整 diff 查看。" }}</pre>
            </article>
            <p v-if="!(selectedCodeChangeFile?.blocks || []).length" class="empty-hint">这个文件没有关联到 generated_tests.json 中的新增测试。</p>
          </div>

          <pre v-else class="code-block tall">{{ selectedCodeChangeFile?.raw || "暂无代码变更" }}</pre>
        </div>
      </div>
      <p v-else class="empty-hint">暂无代码变更</p>
    </div>

    <div v-else-if="activeJobTab === 'failures'" class="review-content">
      <div class="review-actions">
        <span>已选择 {{ selectedGeneratedTestCount }} 个生成测试</span>
        <div class="review-action-buttons">
          <button class="success-button compact" type="button" :disabled="!!busyAction || selectedJob?.active || !selectedGeneratedTestCount" @click="createSelectedTestsPr">
            <GitPullRequest :size="14" />
            提交选中测试 PR
          </button>
          <button class="danger-button compact" type="button" :disabled="!!busyAction || selectedJob?.active || !selectedGeneratedTestCount" @click="deleteSelectedGeneratedTests">
            <Trash2 :size="14" />
            删除选中测试
          </button>
        </div>
      </div>

      <div class="review-table-wrap result-table-scroll">
        <table class="review-table">
          <thead>
            <tr>
              <th class="check-cell"></th>
              <th>状态</th>
              <th>失败测试</th>
              <th>文件</th>
              <th>行号</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="failure in selectedJobFailures" :key="failure.id">
              <td class="check-cell">
                <input
                  v-model="selectedGeneratedTestKeys"
                  type="checkbox"
                  :value="`${failure.test_suite}.${failure.test_name}`"
                  :disabled="!generatedTestRows.some((test) => test.fullName === `${failure.test_suite}.${failure.test_name}`)"
                  :aria-label="`选择 ${failure.test_suite}.${failure.test_name}`"
                />
              </td>
              <td><span class="status-pill" :class="failure.status === 'open' ? 'failed' : 'passed'">{{ failure.status }}</span></td>
              <td><code>{{ failure.test_suite }}.{{ failure.test_name }}</code></td>
              <td class="truncate">{{ failure.file }}</td>
              <td>{{ failure.line || "-" }}</td>
              <td class="reason-cell">{{ failure.reason }}</td>
            </tr>
            <tr v-if="!selectedJobFailures.length">
              <td colspan="6" class="empty-cell">当前任务没有失败用例</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <pre v-else-if="activeJobTab === 'logs'" class="code-block tall">{{ eventsText || "暂无日志" }}</pre>
    <pre v-else-if="activeJobTab === 'codex'" class="code-block tall">{{ agentText || "暂无智能体输出" }}</pre>
    <pre v-else class="code-block tall">{{ diffText || selectedJob?.id || "暂无任务" }}</pre>
  </section>
</template>
