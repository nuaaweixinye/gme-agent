<script setup>
import { computed } from "vue";
import { useWorkspace } from "../composables/useWorkspace";

const {
  failureJobGroups,
  selectedFailureJobId,
  selectedFailureJobFailures,
  selectedFailureId,
  selectedFailureIds,
  toggleFailureSelection,
  selectFailureJob,
  selectFailure,
  shortId,
  jobStatus,
  failureStatus,
  failureStatusTone,
  statusTone,
} = useWorkspace();

const openCount = computed(() =>
  selectedFailureJobFailures.value.filter((failure) => ["open", "resolved", "fix_failed"].includes(failure.status)).length,
);

function fullName(failure) {
  return failure?.test_suite && failure?.test_name ? `${failure.test_suite}.${failure.test_name}` : "未知测试";
}

function shortFile(path) {
  const value = String(path || "").replace(/\\/g, "/");
  const marker = "/tests/gme/";
  const index = value.lastIndexOf(marker);
  return index >= 0 ? value.slice(index + marker.length) : value.split("/").slice(-3).join("/");
}

function shortFailureId(id) {
  const value = String(id || "");
  return value.startsWith("gmefail-") ? `gmefail-${value.slice(-8)}` : shortId(value);
}
</script>

<template>
  <section class="panel failure-list-panel">
    <div class="failure-browser-section">
      <div class="panel-title-row">
        <div>
          <h2>来源测试任务</h2>
          <p>{{ failureJobGroups.length }} 个任务包含待处理失败</p>
        </div>
      </div>

      <div class="failure-task-list">
        <button
          v-for="group in failureJobGroups"
          :key="group.job.id"
          type="button"
          class="failure-task-item"
          :class="{ selected: selectedFailureJobId === group.job.id }"
          @click="selectFailureJob(group.job.id)"
        >
          <span class="failure-task-main">
            <span>
              <code>{{ shortId(group.job.id) }}</code>
              <span class="module-label">{{ group.job.module || "未知模块" }}</span>
            </span>
            <strong>{{ group.job.title || `${group.job.module || "模块"} 测试任务` }}</strong>
            <small>{{ jobStatus(group.job) }}</small>
          </span>
          <span class="failure-task-count">
            <strong>{{ group.failures.length }}</strong>
            <small>失败</small>
          </span>
        </button>

        <div v-if="!failureJobGroups.length" class="empty-state compact-empty">
          <strong>暂无待修复任务</strong>
          <span>测试任务产生真实失败后会显示在这里。</span>
        </div>
      </div>
    </div>

    <div class="failure-browser-divider"></div>

    <div class="failure-browser-section failure-cases-section">
      <div class="panel-title-row">
        <div>
          <h2>失败用例</h2>
          <p>{{ selectedFailureJobFailures.length }} 条记录，{{ openCount }} 条待修复</p>
        </div>
      </div>

      <div class="failure-list">
        <article
          v-for="failure in selectedFailureJobFailures"
          :key="failure.id"
          class="failure-item"
          :class="{ selected: selectedFailureId === failure.id }"
          @click="selectFailure(failure.id)"
        >
          <input
            class="failure-checkbox"
            type="checkbox"
            :checked="selectedFailureIds.includes(failure.id)"
            :disabled="!['open', 'resolved', 'fix_failed'].includes(failure.status)"
            :aria-label="`选择 ${fullName(failure)}`"
            @click.stop
            @change="toggleFailureSelection(failure.id)"
          />
          <span class="failure-item-content">
            <span class="failure-item-top">
              <code>{{ shortFailureId(failure.id) }}</code>
              <span class="status-pill" :class="failureStatusTone(failure)">{{ failureStatus(failure) }}</span>
            </span>
            <strong>{{ fullName(failure) }}</strong>
            <small>{{ shortFile(failure.file) }}{{ failure.line ? `:${failure.line}` : "" }}</small>
          </span>
        </article>

        <div v-if="selectedFailureJobId && !selectedFailureJobFailures.length" class="empty-state compact-empty">
          <strong>该任务暂无待处理失败</strong>
          <span>已修复和已忽略的用例不会显示；已提交测试 PR 的已知失败仍可继续修复。</span>
        </div>
      </div>
    </div>
  </section>
</template>
