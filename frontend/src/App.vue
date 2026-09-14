<script setup>
import { onBeforeUnmount, onMounted } from "vue";
import { FlaskConical, RefreshCw, Settings2, ShieldCheck, Wrench } from "@lucide/vue";
import ConfigPage from "./pages/ConfigPage.vue";
import FixAgentPage from "./pages/FixAgentPage.vue";
import TestAgentPage from "./pages/TestAgentPage.vue";
import { useWorkspace } from "./composables/useWorkspace";

const navItems = [
  { id: "test", label: "测试 Agent", detail: "生成、构建、运行测试", icon: FlaskConical },
  { id: "fix", label: "修复 Agent", detail: "复现、修源码、验证测试", icon: Wrench },
  { id: "config", label: "设置", detail: "环境、路径、模型", icon: Settings2 },
];

const {
  activePage,
  loading,
  busyAction,
  error,
  notice,
  backendOk,
  jobStats,
  failureStats,
  refreshAll,
  refreshRuntime,
  validateEnvironment,
} = useWorkspace();

let refreshTimer = null;

onMounted(async () => {
  await refreshAll();
  refreshTimer = window.setInterval(() => refreshRuntime(true), 2500);
});

onBeforeUnmount(() => {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
  }
});
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">G</div>
        <div>
          <strong>GME 测试用例 Agent</strong>
          <span>Vue 工作台</span>
        </div>
      </div>

      <nav class="nav-list">
        <button
          v-for="item in navItems"
          :key="item.id"
          class="nav-item"
          :class="{ active: activePage === item.id }"
          type="button"
          @click="activePage = item.id"
        >
          <component :is="item.icon" :size="20" />
          <span>
            <strong>{{ item.label }}</strong>
            <small>{{ item.detail }}</small>
          </span>
        </button>
      </nav>

      <div class="side-metrics">
        <div>
          <span>测试</span>
          <strong>{{ jobStats.total }}</strong>
        </div>
        <div>
          <span>失败</span>
          <strong>{{ failureStats.open }}</strong>
        </div>
      </div>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <div class="connection" :class="{ ok: backendOk }">
          <span></span>
          {{ backendOk ? "后端已连接" : "后端未连接" }}
        </div>
        <div class="topbar-actions">
          <button class="ghost-button" type="button" @click="refreshAll">
            <RefreshCw :size="16" />
            刷新
          </button>
          <button class="primary-button" type="button" @click="validateEnvironment">
            <ShieldCheck :size="16" />
            环境检查
          </button>
        </div>
      </header>

      <div v-if="error" class="alert danger">
        {{ error }}
        <button type="button" @click="error = ''">关闭</button>
      </div>
      <div v-if="notice" class="alert success">{{ notice }}</div>

      <ConfigPage v-show="activePage === 'config'" />
      <FixAgentPage v-show="activePage === 'fix'" />
      <TestAgentPage v-show="activePage === 'test'" />
    </main>

    <div v-if="loading || busyAction" class="busy-bar">
      {{ busyAction || "加载中" }}
    </div>
  </div>
</template>
