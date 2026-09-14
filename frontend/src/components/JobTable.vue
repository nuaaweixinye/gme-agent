<script setup>
import { computed, ref, watch } from "vue";
import { Clipboard } from "@lucide/vue";
import { jobTypeLabels, useWorkspace } from "../composables/useWorkspace";

const {
  testJobs,
  selectedJob,
  selectedJobId,
  selectJob,
  copyText,
  shortId,
  jobStatus,
  statusTone,
} = useWorkspace();

const currentPage = ref(1);
const pageSize = ref(20);

const totalPages = computed(() => Math.max(1, Math.ceil(testJobs.value.length / pageSize.value)));
const paginatedJobs = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value;
  return testJobs.value.slice(start, start + pageSize.value);
});
const visiblePages = computed(() => {
  const total = totalPages.value;
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);

  const pages = new Set([1, total]);
  for (let page = currentPage.value - 2; page <= currentPage.value + 2; page += 1) {
    if (page > 1 && page < total) pages.add(page);
  }
  return [...pages].sort((left, right) => left - right);
});

watch([() => testJobs.value.length, pageSize], () => {
  currentPage.value = Math.min(currentPage.value, totalPages.value);
});

function changePage(page) {
  currentPage.value = Math.min(Math.max(1, page), totalPages.value);
}

function changePageSize() {
  currentPage.value = 1;
}
</script>

<template>
  <section class="panel table-panel">
    <div class="panel-title-row">
      <h2>测试任务</h2>
      <button class="ghost-button compact" type="button" @click="copyText(selectedJob?.worktree_path || '')">
        <Clipboard :size="15" />
        复制工作区
      </button>
    </div>
    <div class="table-wrap">
      <table class="data-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>类型</th>
            <th>状态</th>
            <th>标题</th>
            <th>模块</th>
            <th>目标仓库</th>
            <th>测试目标</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="job in paginatedJobs"
            :key="job.id"
            :class="{ selected: selectedJobId === job.id }"
            @click="selectJob(job.id)"
          >
            <td><code>{{ shortId(job.id) }}</code></td>
            <td>{{ jobTypeLabels[job.type] || job.type }}</td>
            <td><span class="status-pill" :class="statusTone(job.status)">{{ jobStatus(job) }}</span></td>
            <td>{{ job.title }}</td>
            <td>{{ job.module }}</td>
            <td>{{ job.metadata?.target_repo || "" }}</td>
            <td class="truncate">{{ job.api_name }}</td>
            <td>{{ job.updated_at }}</td>
          </tr>
          <tr v-if="!testJobs.length">
            <td colspan="8" class="empty-cell">暂无任务</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="testJobs.length" class="table-pagination">
      <div class="pagination-summary">
        共 {{ testJobs.length }} 个任务
        <label>
          每页
          <select v-model.number="pageSize" @change="changePageSize">
            <option :value="10">10</option>
            <option :value="20">20</option>
            <option :value="50">50</option>
          </select>
          条
        </label>
      </div>
      <nav class="pagination-pages" aria-label="测试任务分页">
        <button type="button" :disabled="currentPage === 1" @click="changePage(currentPage - 1)">上一页</button>
        <template v-for="(page, index) in visiblePages" :key="page">
          <span v-if="index > 0 && page - visiblePages[index - 1] > 1" class="pagination-ellipsis">…</span>
          <button
            type="button"
            :class="{ active: page === currentPage }"
            :aria-current="page === currentPage ? 'page' : undefined"
            @click="changePage(page)"
          >
            {{ page }}
          </button>
        </template>
        <button type="button" :disabled="currentPage === totalPages" @click="changePage(currentPage + 1)">下一页</button>
      </nav>
    </div>
  </section>
</template>
