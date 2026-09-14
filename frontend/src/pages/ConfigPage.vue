<script setup>
import { FolderOpen, Save, ShieldCheck } from "@lucide/vue";
import { useWorkspace } from "../composables/useWorkspace";

const {
  busyAction,
  validation,
  configForm,
  branchOptions,
  remoteOptions,
  modelOptions,
  selectedModelOption,
  isCustomModel,
  reasoningEffortOptions,
  saveConfig,
  chooseDirectory,
  chooseFile,
  validateEnvironment,
} = useWorkspace();
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <div>
        <span class="eyebrow">Configuration</span>
        <h1>配置</h1>
      </div>
      <div class="heading-actions">
        <button class="primary-button" type="button" :disabled="!!busyAction" @click="saveConfig">
          <Save :size="16" />
          保存配置
        </button>
      </div>
    </div>

    <div class="config-grid">
      <section class="panel">
        <h2>路径</h2>
        <label class="field">
          <span>GME 仓库</span>
          <div class="input-action">
            <input v-model="configForm.gme_repo_path" />
            <button class="ghost-button compact" type="button" @click="chooseDirectory('gme_repo_path')">
              <FolderOpen :size="15" />
              选择
            </button>
          </div>
        </label>
        <label class="field">
          <span>工作区根目录</span>
          <div class="input-action">
            <input v-model="configForm.worktree_root" />
            <button class="ghost-button compact" type="button" @click="chooseDirectory('worktree_root')">
              <FolderOpen :size="15" />
              选择
            </button>
          </div>
        </label>
        <label class="field">
          <span>产物目录</span>
          <div class="input-action">
            <input v-model="configForm.artifact_root" />
            <button class="ghost-button compact" type="button" @click="chooseDirectory('artifact_root')">
              <FolderOpen :size="15" />
              选择
            </button>
          </div>
        </label>
        <label class="field">
          <span>数据库</span>
          <div class="input-action">
            <input v-model="configForm.database_path" />
            <button
              class="ghost-button compact"
              type="button"
              @click="chooseFile('database_path', [{ name: 'SQLite 数据库', extensions: ['db', 'sqlite', 'sqlite3'] }, { name: '全部文件', extensions: ['*'] }])"
            >
              <FolderOpen :size="15" />
              选择
            </button>
          </div>
        </label>
      </section>

      <section class="panel">
        <h2>DS Harness 与 Git</h2>
        <div class="two-col">
          <label class="field">
            <span>基准分支</span>
            <select v-model="configForm.base_branch">
              <option v-for="item in branchOptions" :key="item" :value="item">{{ item }}</option>
            </select>
          </label>
          <label class="field">
            <span>Git 远端</span>
            <select v-model="configForm.github_remote">
              <option v-for="item in remoteOptions" :key="item" :value="item">{{ item }}</option>
            </select>
          </label>
        </div>
        <div class="two-col">
          <label class="field">
            <span>测试仓库基准分支</span>
            <input v-model.trim="configForm.test_base_branch" placeholder="main" />
          </label>
          <label class="field">
            <span>模块修复基准分支</span>
            <input v-model.trim="configForm.module_base_branch" placeholder="develop" />
          </label>
        </div>
        <div class="two-col">
          <label class="field">
            <span>模型</span>
            <select v-model="selectedModelOption">
              <option v-for="item in modelOptions" :key="item.id" :value="item.id">
                {{ item.display_name }}
              </option>
            </select>
            <input v-if="isCustomModel" v-model.trim="configForm.model" placeholder="输入模型 ID，例如 deepseek-v4-flash" />
          </label>
          <label class="field">
            <span>推理强度</span>
            <select v-model="configForm.reasoning_effort">
              <option v-for="item in reasoningEffortOptions" :key="item.value" :value="item.value">
                {{ item.label }}
              </option>
            </select>
          </label>
        </div>
        <div class="two-col">
          <label class="field">
            <span>DS Harness Provider</span>
            <input v-model.trim="configForm.provider" placeholder="deepseek-official" />
          </label>
          <label class="field">
            <span>DS Harness Profile</span>
            <input v-model.trim="configForm.dsh_profile" placeholder="sdk" />
          </label>
        </div>
        <div class="two-col">
          <label class="field">
            <span>DS Harness Home</span>
            <div class="input-action">
              <input v-model="configForm.dsh_home" />
              <button class="ghost-button compact" type="button" @click="chooseDirectory('dsh_home')">
                <FolderOpen :size="15" />
                选择
              </button>
            </div>
          </label>
          <label class="field">
            <span>SDK Runtime 路径（可选）</span>
            <div class="input-action">
              <input v-model="configForm.dsh_bin" placeholder="留空使用已安装 runtime" />
              <button class="ghost-button compact" type="button" @click="chooseFile('dsh_bin', [{ name: '可执行文件', extensions: ['exe'] }, { name: '全部文件', extensions: ['*'] }])">
                <FolderOpen :size="15" />
                选择
              </button>
            </div>
          </label>
        </div>
      </section>

      <section class="panel">
        <div class="panel-title-row">
          <h2>任务自动化</h2>
          <span class="mini-badge">保存后立即生效</span>
        </div>
        <div class="automation-setting-list">
          <label class="switch-row automation-switch">
            <input v-model="configForm.auto_run_build" type="checkbox" />
            <span aria-hidden="true"></span>
            <div>
              <strong>生成后自动构建</strong>
              <small>测试生成完成后，后端自动执行配置的构建命令。</small>
            </div>
          </label>
          <label class="switch-row automation-switch">
            <input v-model="configForm.auto_run_tests" type="checkbox" />
            <span aria-hidden="true"></span>
            <div>
              <strong>构建后自动运行测试</strong>
              <small>使用生成清单中的精确 GTest filter 自动运行本次测试。</small>
            </div>
          </label>
        </div>
        <p v-if="configForm.auto_run_tests && !configForm.auto_run_build" class="config-warning">
          建议同时开启“生成后自动构建”，否则测试会依赖工作树中已有的可执行文件。
        </p>
        <p class="config-help">
          设置只影响之后新建或继续执行的任务；已经处于“待审查”的任务不会自动重新运行。
        </p>
      </section>

      <section class="panel">
        <div class="panel-title-row">
          <h2>环境检查结果</h2>
          <button class="ghost-button compact" type="button" @click="validateEnvironment">
            <ShieldCheck :size="15" />
            检查
          </button>
        </div>
        <pre class="code-block">{{ validation ? JSON.stringify(validation, null, 2) : "暂无检查结果" }}</pre>
      </section>
    </div>
  </section>
</template>
