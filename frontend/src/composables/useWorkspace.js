import { computed, reactive, ref, watch } from "vue";
import { api } from "../api";

export const jobTypeLabels = {
  test_generation: "生成测试",
  bug_fix: "修复缺陷",
};

export const jobStatusLabels = {
  queued: "排队中",
  creating_worktree: "创建工作区",
  running_agent: "调用 DS Harness",
  checking_format: "格式检查中",
  building: "构建中",
  running_tests: "测试中",
  running_memory_audit: "内存审计中",
  applying_skips: "添加 skip",
  creating_pr: "创建 PR",
  cleaning_worktree: "清理工作区",
  needs_review: "待审查",
  failed: "失败",
  pr_created: "PR 已创建",
  worktree_cleaned: "工作区已清理",
};

export const failureStatusLabels = {
  open: "未修复",
  resolved: "本次未失败",
  fixing: "修复中",
  fix_ready: "待审查",
  fixed: "已修复",
  ignored: "已忽略",
  fix_failed: "修复失败",
};

const activePage = ref("test");
const testWorkspaceTab = ref("generate");
const fixWorkspaceTab = ref("select");
const activeJobTab = ref("results");
const codeViewMode = ref("tests");
const gtestFilterMode = ref("suggested");
const selectedCodeFile = ref("");
const loading = ref(false);
const busyAction = ref("");
const error = ref("");
const notice = ref("");
const backendOk = ref(false);
const validation = ref(null);
const jobs = ref([]);
const failures = ref([]);
const events = ref([]);
const testCaseResults = ref([]);
const artifacts = ref({ artifact_dir: "", files: [], contents: {} });
const selectedJobId = ref("");
const fixEvents = ref([]);
const fixArtifacts = ref({ artifact_dir: "", files: [], contents: {} });
const selectedFixJobId = ref("");
const selectedFailureJobId = ref("");
const selectedFailureId = ref("");
const selectedFailureIds = ref([]);
const selectedGeneratedTestKeys = ref([]);
const interfaceCatalogModules = ref([]);
const interfaceCatalog = ref({ module: "", summary: {}, files: [] });
const interfaceCatalogLoading = ref(false);
const interfaceSearch = ref("");
const activeCatalogFile = ref("");
const selectedInterfaceIds = ref([]);
const lastToggledInterfaceId = ref("");
const CUSTOM_MODEL_VALUE = "__custom__";
const ALL_CATALOG_FILES = "__all__";

const configOptions = ref({
  branches: [],
  remotes: [],
  modules: [],
  models: [],
  reasoning_efforts: ["low", "medium", "high", "xhigh", "max", "ultra"],
  model_error: "",
  reasoning_effort_options: ["", "low", "medium", "high", "xhigh", "max", "ultra"],
});

const configForm = reactive({
  gme_repo_path: "",
  worktree_root: "",
  artifact_root: "",
  database_path: "",
  base_branch: "",
  test_base_branch: "main",
  module_base_branch: "develop",
  github_remote: "",
  provider: "",
  model: "",
  reasoning_effort: "",
  dsh_home: "",
  dsh_profile: "",
  dsh_bin: "",
  initialize_submodules: false,
  test_target_repo: "",
  module_repo_root: "",
  pr_strategy: "",
  use_builtin_skills: true,
  test_generation_skill: "",
  bug_fix_skill: "",
  configure_command: "",
  build_command: "",
  test_command: "",
  test_executable: "",
  gtest_xml_path: "",
  agent_enabled: true,
  auto_run_build: false,
  auto_run_tests: false,
  auto_apply_skips: false,
  auto_rerun_after_skip: false,
  auto_create_pr: false,
});

const testForm = reactive({
  module: "laws",
  gtestFilter: "",
});

function failureTimestamp(failure) {
  return failure?.updated_at || failure?.created_at || "";
}

function realFailureKey(failure) {
  return [failure?.job_id || "", failure?.test_suite || "", failure?.test_name || ""].join("\x1f");
}

function latestRealFailures(items) {
  const latest = new Map();
  for (const failure of items || []) {
    const key = realFailureKey(failure);
    const current = latest.get(key);
    if (!current || failureTimestamp(failure) > failureTimestamp(current)) {
      latest.set(key, failure);
    }
  }
  return Array.from(latest.values()).sort((left, right) => failureTimestamp(right).localeCompare(failureTimestamp(left)));
}

const testJobs = computed(() => jobs.value.filter((job) => job.type === "test_generation"));
const fixJobs = computed(() => jobs.value.filter((job) => job.type === "bug_fix"));
const selectedJob = computed(() => testJobs.value.find((job) => job.id === selectedJobId.value) || null);
const selectedFixJob = computed(() => fixJobs.value.find((job) => job.id === selectedFixJobId.value) || null);
const currentFailures = computed(() => latestRealFailures(failures.value));
const repairableFailures = computed(() => {
  const sourceJobs = new Map(testJobs.value.map((job) => [job.id, job]));
  return currentFailures.value.filter((failure) => {
    const sourceJob = sourceJobs.get(failure.job_id);
    if (!sourceJob || ["fixed", "ignored"].includes(failure.status)) return false;
    if (failure.status === "resolved" && !isSubmittedFailure(failure, sourceJob)) return false;
    return true;
  });
});
const failureJobGroups = computed(() =>
  testJobs.value
    .map((job) => {
      const groupedFailures = repairableFailures.value.filter((failure) => failure.job_id === job.id);
      return {
        job,
        failures: groupedFailures,
        openCount: groupedFailures.filter((failure) => failure.status === "open").length,
        activeCount: groupedFailures.filter((failure) => ["fixing", "fix_ready"].includes(failure.status)).length,
        latestAt: groupedFailures.reduce(
          (latest, failure) => (failureTimestamp(failure) > latest ? failureTimestamp(failure) : latest),
          job.updated_at || job.created_at || "",
        ),
      };
    })
    .filter((group) => group.failures.length)
    .sort((left, right) => right.latestAt.localeCompare(left.latestAt)),
);
const selectedFailureJob = computed(() =>
  failureJobGroups.value.find((group) => group.job.id === selectedFailureJobId.value)?.job || null,
);
const selectedFailureJobFailures = computed(() =>
  failureJobGroups.value.find((group) => group.job.id === selectedFailureJobId.value)?.failures || [],
);
const selectedFailure = computed(() => currentFailures.value.find((failure) => failure.id === selectedFailureId.value) || failures.value.find((failure) => failure.id === selectedFailureId.value) || null);
const selectedRepairFailures = computed(() =>
  selectedFailureIds.value
    .map((id) => repairableFailures.value.find((failure) => failure.id === id))
    .filter(Boolean),
);
const selectedInterfaceFailures = computed(() => {
  const selected = selectedFailure.value;
  if (!selected) return [];
  const apiName = failureApiName(selected);
  return selectedFailureJobFailures.value.filter((failure) => {
    if (!["open", "resolved", "fix_failed"].includes(failure.status)) return false;
    return failureApiName(failure) === apiName;
  });
});
const selectedJobFailures = computed(() => currentFailures.value.filter((failure) => failure.job_id === selectedJobId.value));
const selectedJobOpenFailures = computed(() => selectedJobFailures.value.filter((failure) => failure.status === "open"));
const eventsText = computed(() => events.value.map((event) => `[${event.ts}] ${event.level}: ${event.message}`).join("\n"));
const diffText = computed(() => artifacts.value.contents?.["diff.patch"] || "");
const fixEventsText = computed(() => fixEvents.value.map((event) => `[${event.ts}] ${event.level}: ${event.message}`).join("\n"));
const fixDiffText = computed(() => fixArtifacts.value.contents?.["diff.patch"] || "");
const afterSkipGtestOutput = computed(() => artifacts.value.contents?.["gtest_output_after_skip.txt"] || "");
const latestGtestOutput = computed(() => latestArtifactContent(["gtest_output.txt", "gtest_output_after_skip.txt", "gtest_output_selected_pr.txt"]));
const agentText = computed(
  () => artifacts.value.contents?.["agent_skip_result.txt"] || artifacts.value.contents?.["agent_extend_result.txt"] || artifacts.value.contents?.["agent_result.txt"] || "",
);
const fixAgentText = computed(() => fixArtifacts.value.contents?.["agent_result.txt"] || "");
const agentNotesText = computed(() => {
  const contents = artifacts.value.contents || {};
  return Object.keys(contents)
    .filter((name) => name.startsWith(".gme-agent/"))
    .sort()
    .map((name) => `# ${name}\n\n${contents[name]}`)
    .join("\n\n");
});
const jobDetailJson = computed(() =>
  JSON.stringify(
    {
      job: selectedJob.value,
      failures: selectedJobFailures.value,
      artifacts: artifacts.value.files || [],
      artifact_dir: artifacts.value.artifact_dir || "",
    },
    null,
    2,
  ),
);
const failureDetailJson = computed(() => JSON.stringify({ failure: selectedFailure.value }, null, 2));
const selectedJobModule = computed(() => selectedJob.value?.module || testForm.module || "");
const generatedTestInfo = computed(() => {
  const metadata = selectedJob.value?.metadata || {};
  const files = Array.isArray(metadata.generated_test_files) ? metadata.generated_test_files : [];
  const tests = Array.isArray(metadata.generated_tests) ? metadata.generated_tests : [];
  const filter = metadata.generated_gtest_filter || tests.map((test) => `${test.suite}.${test.name}`).join(":");
  const suites = [...new Set(tests.map((test) => test.suite).filter(Boolean))];
  const fileCount = files.length || new Set(tests.map((test) => test.file).filter(Boolean)).size;
  const testCount = tests.length || (filter && filter !== "*" ? filter.split(":").filter(Boolean).length : 0);
  const summaryParts = [];
  if (testCount) summaryParts.push(`${testCount} 条测试`);
  if (fileCount) summaryParts.push(`${fileCount} 个文件`);
  if (suites.length) summaryParts.push(`${suites.length} 个 suite`);
  const filterSummary = summaryParts.join(" · ") || "全部测试";
  if (files.length || tests.length || filter) {
    return {
      file: files.length ? files.map((file) => `tests/gme/${file}`).join(", ") : "tests/gme/src/<module>/<existing_test>.cpp",
      files,
      tests,
      filter: filter || "*",
      suite: suites.join(", "),
      testCount,
      fileCount,
      suiteCount: suites.length,
      filterSummary,
    };
  }
  const moduleName = String(selectedJobModule.value || "module").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  return {
    file: `tests/gme/src/${moduleName}/<existing_test>.cpp`,
    files: [],
    tests: [],
    filter: "",
    suite: "",
    testCount: 0,
    fileCount: 0,
    suiteCount: 0,
    filterSummary: "无生成测试",
  };
});
const memoryAuditSummary = computed(() => {
  const content = artifacts.value.contents?.["memory_audit_results.json"];
  if (content) {
    try {
      return JSON.parse(content);
    } catch {
      return null;
    }
  }
  return selectedJob.value?.metadata?.memory_audit || null;
});
const allGeneratedTestRows = computed(() => {
  const tests = generatedTestInfo.value.tests || [];
  const failuresByKey = new Map(selectedJobFailures.value.map((failure) => [`${failure.test_suite}.${failure.test_name}`, failure]));
  const resultsByKey = new Map(testCaseResults.value.map((result) => [`${result.test_suite}.${result.test_name}`, result]));
  return tests.map((test, index) => {
    const fullName = `${test.suite}.${test.name}`;
    const failure = failuresByKey.get(fullName) || null;
    const persistedResult = resultsByKey.get(fullName) || null;
    const status = generatedTestStatus(fullName, failure, persistedResult, latestGtestOutput.value);
    return {
      index: index + 1,
      file: normalizePath(test.file || ""),
      suite: test.suite || "",
      name: test.name || "",
      fullName,
      api: test.api || "",
      anchor: test.anchor || "",
      line: failure?.line || "",
      reason: failure?.reason || "",
      status,
      statusLabel: generatedTestStatusLabel(status),
      failure,
    };
  });
});
const submittedTestNames = computed(() => {
  const metadata = selectedJob.value?.metadata || {};
  const names = new Set((metadata.submitted_test_names || []).map((name) => String(name)));
  if (metadata.skip_pr_url || metadata.pr_url) {
    for (const name of gtestNamesWithStatus(afterSkipGtestOutput.value, "SKIPPED")) names.add(name);
  }
  return names;
});
const submittedPrByTestName = computed(() => {
  const metadata = selectedJob.value?.metadata || {};
  const history = Array.isArray(metadata.test_prs) ? metadata.test_prs : [];
  const result = new Map();
  for (const item of history) {
    const url = extractPrUrl(item?.url);
    for (const name of item?.tests || []) {
      result.set(String(name), { url, branch: String(item?.branch || "") });
    }
  }
  return result;
});
const submittedTestRows = computed(() => {
  const fallbackUrl = jobLatestPrUrl(selectedJob.value);
  return allGeneratedTestRows.value
    .filter((row) => submittedTestNames.value.has(row.fullName))
    .map((row) => {
      const pr = submittedPrByTestName.value.get(row.fullName) || {};
      const prUrl = pr.url || fallbackUrl;
      return {
        ...row,
        prUrl,
        prLabel: pullRequestLabel(prUrl),
        prBranch: pr.branch || "",
      };
    });
});
const generatedTestRows = computed(() => allGeneratedTestRows.value.filter((row) => !submittedTestNames.value.has(row.fullName)));
const selectedGeneratedTestRows = computed(() => {
  const selected = new Set(selectedGeneratedTestKeys.value);
  return generatedTestRows.value.filter((row) => selected.has(row.fullName));
});
const selectedGeneratedTestCount = computed(() => selectedGeneratedTestRows.value.length);
const testResultSummary = computed(() => {
  const rows = generatedTestRows.value;
  const fileCount = generatedTestInfo.value.files?.length || new Set(rows.map((row) => row.file).filter(Boolean)).size;
  return {
    files: fileCount,
    total: rows.length,
    passed: rows.filter((row) => row.status === "passed").length,
    failed: rows.filter((row) => row.status === "failed").length,
    skipped: rows.filter((row) => row.status === "skipped").length,
    submitted: submittedTestRows.value.length,
    unknown: rows.filter((row) => row.status === "unknown").length,
    hasOutput: Boolean(latestGtestOutput.value),
  };
});
const diffFiles = computed(() => parseDiffFiles(diffText.value));
const codeChangeFiles = computed(() => {
  const testsByFile = new Map();
  for (const row of generatedTestRows.value) {
    const key = normalizePath(row.file);
    if (!key) continue;
    if (!testsByFile.has(key)) testsByFile.set(key, []);
    testsByFile.get(key).push(row);
  }
  const filesByPath = new Map(diffFiles.value.map((file) => [normalizePath(file.path), file]));
  for (const file of generatedTestInfo.value.files || []) {
    const key = normalizePath(file);
    if (!filesByPath.has(key)) filesByPath.set(key, { path: key, additions: 0, deletions: 0, raw: "", addedText: "" });
  }
  return Array.from(filesByPath.values())
    .map((file) => {
      const path = normalizePath(file.path);
      const tests = testsByFile.get(path) || [];
      const blocks = tests.map((test) => ({
        ...test,
        code: extractAddedTestBlock(file.addedText || "", test.suite, test.name),
      }));
      return {
        ...file,
        path,
        tests,
        blocks,
        failedCount: tests.filter((test) => test.status === "failed").length,
      };
    })
    .filter((file) => file.path)
    .sort((left, right) => left.path.localeCompare(right.path));
});
const selectedCodeChangeFile = computed(() => codeChangeFiles.value.find((file) => file.path === selectedCodeFile.value) || codeChangeFiles.value[0] || null);
const workflowSteps = computed(() => {
  const job = selectedJob.value;
  const fileNames = new Set((artifacts.value.files || []).map((file) => file.name));
  const steps = [
    {
      label: "准备 worktree",
      detail: job?.worktree_path || "等待创建任务",
      state: stepState(job, "creating_worktree", Boolean(job?.worktree_path)),
    },
    {
      label: "DS Harness 生成测试",
      detail: generatedTestInfo.value.file,
      state: stepState(job, "running_agent", Boolean(job?.harness_session_id || fileNames.has("agent_result.txt") || fileNames.has("agent_extend_result.txt"))),
    },
    {
      label: "CMake 构建",
      detail: "构建 tests 目标",
      state: stepState(job, "building", fileNames.has("build_output.txt")),
    },
    {
      label: "GTest 运行",
      detail: testForm.gtestFilter ? "使用手动过滤器" : generatedTestInfo.value.filterSummary,
      state: stepState(job, "running_tests", fileNames.has("gtest_output.txt")),
    },
    {
      label: "内存审计",
      detail: memoryAuditSummary.value
        ? `${memoryAuditSummary.value.total || 0} 条测试 · ${memoryAuditSummary.value.leaking || 0} 条泄漏`
        : "逐条运行测试并读取 gme_mmgr.1.log",
      state: stepState(job, "running_memory_audit", fileNames.has("memory_audit_results.json")),
    },
    {
      label: "准备选中测试",
      detail: "失败测试写入 GTEST_SKIP，通过测试保持原样",
      state: stepState(job, "applying_skips", fileNames.has("agent_skip_result.txt") || fileNames.has("gtest_output_selected_pr.txt")),
    },
    {
      label: "创建测试 PR",
      detail: jobLatestPrUrl(job) || "等待选择测试",
      state: stepState(job, "creating_pr", job?.status === "pr_created"),
    },
  ];
  if (job?.status === "failed") {
    const failedIndex = steps.findIndex((step) => step.state !== "done");
    if (failedIndex >= 0) {
      steps[failedIndex] = { ...steps[failedIndex], state: "failed" };
    }
  }
  return steps;
});

const jobStats = computed(() => ({
  total: testJobs.value.length,
  running: testJobs.value.filter((job) => job.active || ["creating_worktree", "running_agent", "checking_format", "building", "running_tests", "running_memory_audit", "applying_skips", "creating_pr"].includes(job.status)).length,
  review: testJobs.value.filter((job) => job.status === "needs_review").length,
  failed: testJobs.value.filter((job) => job.status === "failed").length,
}));

const failureStats = computed(() => ({
  total: repairableFailures.value.length,
  open: repairableFailures.value.filter((failure) => ["open", "resolved", "fix_failed"].includes(failure.status)).length,
  fixing: repairableFailures.value.filter((failure) => ["fixing", "fix_ready"].includes(failure.status)).length,
  fixed: currentFailures.value.filter((failure) => failure.status === "fixed").length,
}));

const catalogFiles = computed(() => interfaceCatalog.value.files || []);
const allCatalogInterfaces = computed(() => catalogFiles.value.flatMap((file) => file.interfaces || []));
const catalogScopeIsAll = computed(() => activeCatalogFile.value === ALL_CATALOG_FILES);
const catalogInterfacesInScope = computed(() => {
  if (catalogScopeIsAll.value) return allCatalogInterfaces.value;
  const file = catalogFiles.value.find((item) => item.path === activeCatalogFile.value);
  return file?.interfaces || [];
});
const visibleCatalogInterfaces = computed(() => {
  const query = interfaceSearch.value.trim().toLowerCase();
  return catalogInterfacesInScope.value
    .filter((item) => {
      if (!query) return true;
      return [item.name, item.unique_symbol, item.source_catalog, item.kind, item.test_suite]
        .some((value) => String(value || "").toLowerCase().includes(query));
    })
    .map((item) => ({ ...item }));
});
const selectedCatalogInterfaces = computed(() => {
  const selected = new Set(selectedInterfaceIds.value);
  return allCatalogInterfaces.value.filter((item) => selected.has(item.id));
});
const selectedCatalogFileCounts = computed(() => {
  const selected = new Set(selectedInterfaceIds.value);
  return Object.fromEntries(
    catalogFiles.value.map((file) => [
      file.path,
      (file.interfaces || []).filter((item) => selected.has(item.id)).length,
    ]),
  );
});
const allVisibleCatalogInterfacesSelected = computed(() => (
  visibleCatalogInterfaces.value.length > 0
  && visibleCatalogInterfaces.value.every((item) => selectedInterfaceIds.value.includes(item.id))
));
const catalogSelectionSummary = computed(() => {
  const files = new Set(selectedCatalogInterfaces.value.map((item) => item.target_file));
  const interfaceCount = selectedCatalogInterfaces.value.length;
  const batchSize = Number(interfaceCatalog.value.max_selected_interfaces || 5);
  const maxBatchJobs = Number(interfaceCatalog.value.max_batch_jobs || 5);
  const maxBatchInterfaces = Number(
    interfaceCatalog.value.max_batch_interfaces || batchSize * maxBatchJobs,
  );
  const maxConcurrentGenerationJobs = Number(
    interfaceCatalog.value.max_concurrent_generation_jobs || 10,
  );
  return {
    fileCount: files.size,
    interfaceCount,
    batchSize,
    batchCount: interfaceCount ? Math.ceil(interfaceCount / batchSize) : 0,
    maxBatchJobs,
    maxBatchInterfaces,
    maxConcurrentGenerationJobs,
  };
});

const branchOptions = computed(() => withCurrent(configOptions.value.branches, configForm.base_branch));
const remoteOptions = computed(() => withCurrent(configOptions.value.remotes, configForm.github_remote));
const moduleOptions = computed(() => {
  const modules = interfaceCatalogModules.value.map((item) => item.module).filter(Boolean);
  return modules.length ? modules : ["base", "laws", "kernel"];
});
const modelOptions = computed(() => {
  return [
    ...(configOptions.value.models || []),
    { id: CUSTOM_MODEL_VALUE, display_name: "自定义模型" },
  ];
});
const selectedModelOption = computed({
  get() {
    return (configOptions.value.models || []).some((item) => item.id === configForm.model)
      ? configForm.model
      : CUSTOM_MODEL_VALUE;
  },
  set(value) {
    configForm.model = value === CUSTOM_MODEL_VALUE ? "" : value;
  },
});
const isCustomModel = computed(() => selectedModelOption.value === CUSTOM_MODEL_VALUE);
const supportedReasoningEfforts = computed(() => {
  const model = (configOptions.value.models || []).find((item) => item.id === configForm.model);
  return model?.supported_reasoning_efforts?.length
    ? model.supported_reasoning_efforts
    : configOptions.value.reasoning_efforts || [];
});
const reasoningEffortOptions = computed(() => {
  const labels = {
    none: "无",
    minimal: "最小",
    low: "低",
    medium: "中",
    high: "高",
    xhigh: "超高",
    max: "最大",
    ultra: "极致",
  };
  return [...new Set(supportedReasoningEfforts.value)].map((value) => ({ value, label: labels[value] || value }));
});

watch(
  [
    () => configForm.model,
    () => configForm.reasoning_effort,
    () => configOptions.value.models,
    () => configOptions.value.reasoning_efforts,
  ],
  () => {
    const model = (configOptions.value.models || []).find((item) => item.id === configForm.model);
    const supported = supportedReasoningEfforts.value;
    if (!supported.length || supported.includes(configForm.reasoning_effort)) return;
    const preferred = model?.default_reasoning_effort;
    if (preferred && supported.includes(preferred)) {
      configForm.reasoning_effort = preferred;
    } else if (supported.includes("medium")) {
      configForm.reasoning_effort = "medium";
    } else {
      configForm.reasoning_effort = supported[0];
    }
  },
  { deep: true, immediate: true },
);

let noticeTimer = null;

watch(
  codeChangeFiles,
  (files) => {
    if (!files.length) {
      selectedCodeFile.value = "";
      return;
    }
    if (!files.some((file) => file.path === selectedCodeFile.value)) {
      selectedCodeFile.value = files[0].path;
    }
  },
  { immediate: true },
);

watch(
  generatedTestRows,
  (rows) => {
    const valid = new Set(rows.map((row) => row.fullName));
    selectedGeneratedTestKeys.value = selectedGeneratedTestKeys.value.filter((key) => valid.has(key));
  },
  { immediate: true },
);

watch(selectedJobId, () => {
  selectedGeneratedTestKeys.value = [];
  testForm.gtestFilter = "";
  gtestFilterMode.value = "suggested";
});

watch(fixWorkspaceTab, (tab) => {
  if (tab === "select") syncFailureSelection();
});

watch(
  () => testForm.module,
  (module) => {
    if (module && module !== interfaceCatalog.value.module) {
      loadInterfaceCatalog(module);
    }
  },
);

export function useWorkspace() {
  return {
    activePage,
    testWorkspaceTab,
    fixWorkspaceTab,
    activeJobTab,
    codeViewMode,
    gtestFilterMode,
    selectedCodeFile,
    loading,
    busyAction,
    error,
    notice,
    backendOk,
    validation,
    configOptions,
    configForm,
    testForm,
    jobs,
    testJobs,
    fixJobs,
    failures,
    currentFailures,
    events,
    artifacts,
    selectedJobId,
    fixEvents,
    fixArtifacts,
    selectedFixJobId,
    selectedFailureJobId,
    selectedFailureId,
    selectedFailureIds,
    selectedJob,
    selectedFixJob,
    selectedFailureJob,
    selectedFailure,
    selectedRepairFailures,
    selectedInterfaceFailures,
    repairableFailures,
    failureJobGroups,
    selectedFailureJobFailures,
    selectedJobFailures,
    selectedJobOpenFailures,
    eventsText,
    diffText,
    fixEventsText,
    fixDiffText,
    latestGtestOutput,
    agentText,
    fixAgentText,
    agentNotesText,
    jobDetailJson,
    failureDetailJson,
    generatedTestInfo,
    memoryAuditSummary,
    generatedTestRows,
    submittedTestRows,
    selectedGeneratedTestKeys,
    interfaceCatalogModules,
    interfaceCatalog,
    interfaceCatalogLoading,
    interfaceSearch,
    activeCatalogFile,
    selectedInterfaceIds,
    selectedGeneratedTestRows,
    selectedGeneratedTestCount,
    testResultSummary,
    codeChangeFiles,
    selectedCodeChangeFile,
    workflowSteps,
    jobStats,
    failureStats,
    catalogFiles,
    catalogScopeIsAll,
    visibleCatalogInterfaces,
    selectedCatalogInterfaces,
    selectedCatalogFileCounts,
    allVisibleCatalogInterfacesSelected,
    catalogSelectionSummary,
    branchOptions,
    remoteOptions,
    moduleOptions,
    modelOptions,
    selectedModelOption,
    isCustomModel,
    reasoningEffortOptions,
    refreshAll,
    refreshRuntime,
    loadSelectedJobData,
    loadSelectedFixJobData,
    selectJob,
    selectFixJob,
    selectFailureJob,
    selectFailure,
    toggleFailureSelection,
    selectCurrentInterfaceFailures,
    saveConfig,
    chooseDirectory,
    chooseFile,
    validateEnvironment,
    selectCatalogFile,
    selectAllCatalogFiles,
    toggleCatalogFileInterfaces,
    toggleCatalogInterface,
    toggleVisibleCatalogInterfaces,
    clearCatalogSelection,
    createTestJob,
    retrySelectedTestJob,
    buildSelectedJob,
    runSelectedTests,
    runSelectedMemoryAudit,
    createSelectedTestsPr,
    deleteSelectedGeneratedTests,
    deleteSelectedJob,
    deleteSelectedFixJob,
    createFixPr,
    fixSelectedFailure,
    fixSelectedFailures,
    reproduceSelectedFailure,
    markSelectedFailure,
    failureFilter,
    shortId,
    jobStatus,
    failureStatus,
    failureStatusTone,
    statusTone,
    copyText,
    openPr,
    openPrUrl,
    setError,
  };
}

async function refreshAll() {
  loading.value = true;
  try {
    await loadConfig();
    await loadConfigOptions();
    await loadInterfaceCatalogOptions();
    await loadInterfaceCatalog(testForm.module);
    await refreshRuntime(true);
    backendOk.value = true;
  } catch (err) {
    backendOk.value = false;
    setError(err);
  } finally {
    loading.value = false;
  }
}

async function refreshRuntime(silent = false) {
  if (!silent) loading.value = true;
  try {
    const [jobsData, failuresData] = await Promise.all([api.listJobs(), api.listFailures()]);
    jobs.value = jobsData.jobs || [];
    failures.value = failuresData.failures || [];
    const selectableFailures = latestRealFailures(failures.value);
    const selectableTestJobs = jobs.value.filter((job) => job.type === "test_generation");
    const selectableFixJobs = jobs.value.filter((job) => job.type === "bug_fix");
    if (selectedJobId.value && !selectableTestJobs.some((job) => job.id === selectedJobId.value)) selectedJobId.value = "";
    if (!selectedJobId.value && selectableTestJobs.length) selectedJobId.value = selectableTestJobs[0].id;
    if (selectedFixJobId.value && !selectableFixJobs.some((job) => job.id === selectedFixJobId.value)) selectedFixJobId.value = "";
    if (selectedFailureId.value && !selectableFailures.some((failure) => failure.id === selectedFailureId.value)) selectedFailureId.value = "";
    if (fixWorkspaceTab.value === "select") syncFailureSelection();
    else if (!selectedFailureId.value && selectableFailures.length) selectedFailureId.value = selectableFailures[0].id;
    await Promise.all([loadSelectedJobData(), loadSelectedFixJobData()]);
    backendOk.value = true;
  } catch (err) {
    backendOk.value = false;
    if (!silent) setError(err);
  } finally {
    if (!silent) loading.value = false;
  }
}

async function loadConfig() {
  Object.assign(configForm, await api.getConfig());
}

async function loadConfigOptions() {
  try {
    const [options, modelData] = await Promise.all([api.getOptions(), api.getModels()]);
    configOptions.value = {
      ...configOptions.value,
      ...options,
      models: modelData.models || [],
      reasoning_efforts: modelData.reasoning_efforts || configOptions.value.reasoning_efforts,
      model_error: modelData.error || "",
    };
  } catch (err) {
    setError(err);
  }
}

async function loadInterfaceCatalogOptions() {
  const data = await api.listInterfaceCatalogs();
  interfaceCatalogModules.value = data.modules || [];
  const modules = interfaceCatalogModules.value.map((item) => item.module);
  if (!modules.includes(testForm.module)) {
    testForm.module = modules[0] || "laws";
  }
}

async function loadInterfaceCatalog(module) {
  if (!module) return;
  interfaceCatalogLoading.value = true;
  try {
    const previousModule = interfaceCatalog.value.module;
    const data = await api.getInterfaceCatalog(module);
    interfaceCatalog.value = data || { module, summary: {}, files: [] };
    const validFiles = new Set((interfaceCatalog.value.files || []).map((file) => file.path));
    const validIds = new Set(
      (interfaceCatalog.value.files || []).flatMap((file) => (file.interfaces || []).map((item) => item.id)),
    );
    if (previousModule !== module) {
      activeCatalogFile.value = "";
      selectedInterfaceIds.value = [];
      lastToggledInterfaceId.value = "";
      interfaceSearch.value = "";
    } else {
      if (
        activeCatalogFile.value !== ALL_CATALOG_FILES
        && !validFiles.has(activeCatalogFile.value)
      ) activeCatalogFile.value = "";
      selectedInterfaceIds.value = selectedInterfaceIds.value.filter((id) => validIds.has(id));
    }
    if (!activeCatalogFile.value && interfaceCatalog.value.files?.length) {
      activeCatalogFile.value = interfaceCatalog.value.files[0].path;
    }
  } catch (err) {
    setError(err);
  } finally {
    interfaceCatalogLoading.value = false;
  }
}

function selectCatalogFile(path) {
  activeCatalogFile.value = path;
  lastToggledInterfaceId.value = "";
}

function selectAllCatalogFiles() {
  selectCatalogFile(ALL_CATALOG_FILES);
}

function toggleCatalogFileInterfaces(path) {
  const file = catalogFiles.value.find((item) => item.path === path);
  const interfaceIds = (file?.interfaces || []).map((item) => item.id);
  const selected = new Set(selectedInterfaceIds.value);
  const allSelected = interfaceIds.length > 0
    && interfaceIds.every((interfaceId) => selected.has(interfaceId));
  updateCatalogSelection(interfaceIds, !allSelected);
}

function toggleCatalogInterface(interfaceId, checked, selectRange = false) {
  let interfaceIds = [interfaceId];
  if (selectRange && lastToggledInterfaceId.value) {
    const visibleIds = visibleCatalogInterfaces.value.map((item) => item.id);
    const anchorIndex = visibleIds.indexOf(lastToggledInterfaceId.value);
    const currentIndex = visibleIds.indexOf(interfaceId);
    if (anchorIndex >= 0 && currentIndex >= 0) {
      const start = Math.min(anchorIndex, currentIndex);
      const end = Math.max(anchorIndex, currentIndex);
      interfaceIds = visibleIds.slice(start, end + 1);
    }
  }
  updateCatalogSelection(interfaceIds, checked);
  lastToggledInterfaceId.value = interfaceId;
}

function updateCatalogSelection(interfaceIds, checked) {
  const selected = new Set(selectedInterfaceIds.value);
  if (checked) {
    for (const interfaceId of interfaceIds) selected.add(interfaceId);
    if (selected.size > catalogSelectionSummary.value.maxBatchInterfaces) {
      setError(
        `本次操作会选中 ${selected.size} 个接口，`
        + `一次最多选择 ${catalogSelectionSummary.value.maxBatchInterfaces} 个。`,
      );
      return;
    }
  } else {
    for (const interfaceId of interfaceIds) selected.delete(interfaceId);
  }
  selectedInterfaceIds.value = [...selected];
}

function toggleVisibleCatalogInterfaces() {
  updateCatalogSelection(
    visibleCatalogInterfaces.value.map((item) => item.id),
    !allVisibleCatalogInterfacesSelected.value,
  );
}

function clearCatalogSelection() {
  selectedInterfaceIds.value = [];
}

async function loadSelectedJobData() {
  if (!selectedJobId.value) {
    events.value = [];
    testCaseResults.value = [];
    artifacts.value = { artifact_dir: "", files: [], contents: {} };
    return;
  }
  const [eventsData, artifactsData, testResultsData] = await Promise.all([
    api.getJobEvents(selectedJobId.value),
    api.getJobArtifacts(selectedJobId.value),
    api.getJobTestResults(selectedJobId.value),
  ]);
  events.value = eventsData.events || [];
  artifacts.value = artifactsData || { artifact_dir: "", files: [], contents: {} };
  testCaseResults.value = testResultsData.results || [];
}

async function loadSelectedFixJobData() {
  if (!selectedFixJobId.value) {
    fixEvents.value = [];
    fixArtifacts.value = { artifact_dir: "", files: [], contents: {} };
    return;
  }
  const [eventsData, artifactsData] = await Promise.all([
    api.getJobEvents(selectedFixJobId.value),
    api.getJobArtifacts(selectedFixJobId.value),
  ]);
  fixEvents.value = eventsData.events || [];
  fixArtifacts.value = artifactsData || { artifact_dir: "", files: [], contents: {} };
}

async function selectJob(jobId) {
  const job = jobs.value.find((item) => item.id === jobId);
  if (job && job.type !== "test_generation") {
    setError("测试 Agent 只能打开测试生成任务。");
    return;
  }
  selectedJobId.value = jobId;
  await loadSelectedJobData();
}

async function selectFixJob(jobId) {
  const job = jobs.value.find((item) => item.id === jobId);
  if (job && job.type !== "bug_fix") {
    setError("修复 Agent 只能打开修复任务。");
    return;
  }
  selectedFixJobId.value = jobId;
  await loadSelectedFixJobData();
}

function selectFailure(failureId) {
  selectedFailureId.value = failureId;
  const failure = currentFailures.value.find((item) => item.id === failureId) || failures.value.find((item) => item.id === failureId);
  if (failure?.job_id) selectedFailureJobId.value = failure.job_id;
}

function failureApiName(failure) {
  const explicit = String(failure?.metadata?.api_name || "").trim();
  if (explicit) return explicit;
  const sourceJob = testJobs.value.find((job) => job.id === failure?.job_id);
  return String(sourceJob?.api_name || "").trim();
}

function toggleFailureSelection(failureId) {
  const id = String(failureId || "");
  if (!id) return;
  const current = new Set(selectedFailureIds.value);
  if (current.has(id)) {
    current.delete(id);
  } else {
    const candidate = repairableFailures.value.find((failure) => failure.id === id);
    const first = selectedRepairFailures.value[0];
    if (candidate && first && failureApiName(candidate) !== failureApiName(first)) {
      setError("一次修复任务只能选择同一接口的失败测试。");
      return;
    }
    current.add(id);
  }
  selectedFailureIds.value = Array.from(current);
}

function selectCurrentInterfaceFailures() {
  selectedFailureIds.value = selectedInterfaceFailures.value.map((failure) => failure.id);
}

function selectFailureJob(jobId) {
  selectedFailureJobId.value = jobId;
  selectedFailureIds.value = [];
  syncFailureSelection();
}

function syncFailureSelection() {
  const groups = failureJobGroups.value;
  if (!groups.some((group) => group.job.id === selectedFailureJobId.value)) {
    selectedFailureJobId.value = groups[0]?.job.id || "";
  }
  const visibleFailures = selectedFailureJobFailures.value;
  if (!visibleFailures.some((failure) => failure.id === selectedFailureId.value)) {
    selectedFailureId.value = visibleFailures[0]?.id || "";
  }
}

function isSubmittedFailure(failure, sourceJob) {
  const metadata = sourceJob?.metadata || {};
  const submittedIds = new Set((metadata.skip_failure_ids || []).map((id) => String(id)));
  const submittedNames = new Set((metadata.submitted_test_names || []).map((name) => String(name)));
  const fullName = failure?.test_suite && failure?.test_name ? `${failure.test_suite}.${failure.test_name}` : "";
  return submittedIds.has(String(failure?.id || "")) || (fullName && submittedNames.has(fullName));
}

async function saveConfig() {
  await runAction("保存配置", async () => {
    Object.assign(configForm, await api.saveConfig({ ...configForm }));
    await loadConfigOptions();
  });
}

async function chooseDirectory(field) {
  if (!window.gmeAgent?.selectDirectory) {
    setError("当前浏览器模式不支持系统目录选择，请使用 Electron exe。");
    return;
  }
  const selected = await window.gmeAgent.selectDirectory(configForm[field] || "");
  if (selected) configForm[field] = selected;
}

async function chooseFile(field, filters = []) {
  if (!window.gmeAgent?.selectFile) {
    setError("当前浏览器模式不支持系统文件选择，请使用 Electron exe。");
    return;
  }
  const selected = await window.gmeAgent.selectFile(configForm[field] || "", filters);
  if (selected) configForm[field] = selected;
}

async function validateEnvironment() {
  await runAction("环境检查", async () => {
    validation.value = await api.validate();
    activePage.value = "config";
  });
}

async function createTestJob() {
  const batchCount = catalogSelectionSummary.value.batchCount;
  const label = batchCount > 1 ? `批量生成 ${batchCount} 个测试任务` : "生成测试";
  await runAction(label, async () => {
    if (!selectedInterfaceIds.value.length) {
      throw new Error("请至少选择一个接口。");
    }
    const result = await api.createTestJobsBatch({
      module: testForm.module,
      interface_ids: selectedCatalogInterfaces.value.map((item) => item.id),
      batch_size: catalogSelectionSummary.value.batchSize,
    });
    selectedJobId.value = result.jobs?.[0]?.id || "";
    activePage.value = "test";
    testWorkspaceTab.value = "review";
  });
}

async function retrySelectedTestJob() {
  if (selectedJob.value?.status !== "failed") {
    setError("只有失败的测试任务可以继续。");
    return;
  }
  await withSelectedJob("继续失败任务", (job) => api.retryTestJob(job.id));
}

async function buildSelectedJob() {
  await withSelectedJob("构建选中任务", (job) => api.buildJob(job.id));
}

async function runSelectedTests() {
  if (gtestFilterMode.value === "suggested" && !generatedTestInfo.value.testCount) {
    setError("当前任务没有生成测试；建议 Filter 为空，不会自动运行全部测试。");
    return;
  }
  await withSelectedJob("运行选中测试", (job) =>
    api.runTests(job.id, {
      gtest_filter: gtestFilterMode.value === "custom"
        ? testForm.gtestFilter || "*"
        : generatedTestInfo.value.filter || "*",
    }),
  );
}

async function runSelectedMemoryAudit() {
  if (gtestFilterMode.value === "suggested" && !generatedTestInfo.value.testCount) {
    setError("当前任务没有生成测试；建议 Filter 为空，无法运行生成测试的内存审计。");
    return;
  }
  await withSelectedJob("运行内存审计", (job) =>
    api.runMemoryAudit(job.id, {
      gtest_filter: gtestFilterMode.value === "custom"
        ? testForm.gtestFilter || "*"
        : generatedTestInfo.value.filter || "*",
    }),
  );
}

async function createSelectedTestsPr() {
  const job = selectedJob.value;
  if (!job) {
    setError("请先选择一个任务。");
    return;
  }
  if (job.active) {
    setError("当前任务正在执行其他操作，请等待完成后再提交 PR。");
    return;
  }
  const rows = selectedGeneratedTestRows.value;
  if (!rows.length) {
    setError("请先选择要提交的生成测试。");
    return;
  }
  const unknown = rows.filter((row) => row.status === "unknown");
  if (unknown.length) {
    setError(`有 ${unknown.length} 个选中测试尚未确认，请先运行这些测试再创建 PR。`);
    return;
  }

  const passed = rows.filter((row) => row.status === "passed").length;
  const failed = rows.filter((row) => row.status === "failed").length;
  const skipped = rows.filter((row) => row.status === "skipped").length;
  const detail = [
    `将提交 ${rows.length} 个选中测试。`,
    `通过 ${passed} 个，失败 ${failed} 个，已跳过 ${skipped} 个。`,
    failed ? "失败测试会先增加 GTEST_SKIP()，未选中的生成测试不会进入 PR。" : "未选中的生成测试不会进入 PR。",
  ].join("\n");
  if (!window.confirm(detail)) return;

  await runAction("提交选中测试 PR", async () => {
    await api.createSelectedTestsPr(
      job.id,
      rows.map((row) => ({ suite: row.suite, name: row.name })),
    );
    selectedGeneratedTestKeys.value = [];
  });
}

async function deleteSelectedGeneratedTests() {
  const job = selectedJob.value;
  if (!job) {
    setError("请先选择一个任务。");
    return;
  }
  if (job.active) {
    setError("当前任务正在执行其他操作，请等待完成后再删除测试。");
    return;
  }
  if (job.type !== "test_generation") {
    setError("只能删除测试生成任务中的生成测试。");
    return;
  }
  const rows = selectedGeneratedTestRows.value;
  if (!rows.length) {
    setError("请先选择要删除的生成测试。");
    return;
  }
  const names = rows.map((row) => row.fullName).join("\n");
  if (!window.confirm(`确定要删除选中的 ${rows.length} 个生成测试吗？\n\n${names}`)) return;
  await runAction("删除选中测试", async () => {
    await api.deleteGeneratedTests(
      job.id,
      rows.map((row) => ({ suite: row.suite, name: row.name })),
    );
    selectedGeneratedTestKeys.value = [];
  });
}

async function deleteSelectedJob() {
  const job = selectedJob.value;
  if (!job) {
    setError("请先选择一个任务。");
    return;
  }
  if (job.active) {
    setError("当前任务正在执行其他操作，请等待完成后再删除任务。");
    return;
  }
  if (!window.confirm("确定要删除当前测试任务吗？会同时清理工作区、产物和任务记录，此操作不可恢复。")) return;
  await runAction("删除测试任务", async () => {
    await api.deleteJob(job.id, { cleanup_worktree: true, delete_artifacts: true });
    selectedJobId.value = "";
    selectedFailureId.value = "";
  });
}

async function deleteSelectedFixJob() {
  const job = selectedFixJob.value;
  if (!job) {
    setError("请先选择一个修复任务。");
    return;
  }
  if (job.active) {
    setError("当前修复任务正在执行，请等待完成后再删除。");
    return;
  }
  if (!window.confirm("确定要删除当前修复任务吗？会同时清理修复工作区和产物，并将关联失败恢复为未修复，此操作不可恢复。")) return;
  await runAction("删除修复任务", async () => {
    await api.deleteJob(job.id, { cleanup_worktree: true, delete_artifacts: true });
    selectedFixJobId.value = "";
  });
}

async function createFixPr() {
  const job = selectedFixJob.value;
  if (!job) {
    setError("请先选择一个修复任务。");
    return;
  }
  if (job.active) {
    setError("当前修复任务正在执行其他操作，请等待完成后再提交 PR。");
    return;
  }
  if (job.metadata?.fix_validated !== true) {
    setError("修复任务尚未通过完整验证，不能提交 PR。");
    return;
  }
  await runAction("提交修复 PR", () => api.createPr(job.id));
}

async function fixSelectedFailure() {
  if (!selectedFailure.value) {
    setError("请先选择一个失败用例。");
    return;
  }
  await runAction("修复选中失败", async () => {
    const job = await api.fixFailure(selectedFailure.value.id);
    selectedFixJobId.value = job.id;
    activePage.value = "fix";
    fixWorkspaceTab.value = "review";
  });
}

async function reproduceSelectedFailure() {
  const failure = selectedFailure.value;
  if (!failure) {
    setError("请先选择一个失败用例。");
    return;
  }
  if (!failure.job_id) {
    setError("这个失败用例没有关联任务。");
    return;
  }
  const filter = failureFilter(failure);
  testForm.gtestFilter = filter;
  selectedJobId.value = failure.job_id;
  activePage.value = "test";
  testWorkspaceTab.value = "review";
  await runAction("复现失败", () => api.runTests(failure.job_id, { gtest_filter: filter }));
}

async function markSelectedFailure(status) {
  if (!selectedFailure.value) {
    setError("请先选择一个失败用例。");
    return;
  }
  await runAction(`标记为${failureStatusLabels[status] || status}`, () => api.setFailureStatus(selectedFailure.value.id, status));
}

async function withSelectedJob(label, action) {
  if (!selectedJob.value) {
    setError("请先选择一个任务。");
    return;
  }
  if (selectedJob.value.active) {
    setError("当前任务正在执行其他操作，请等待完成后再试。");
    return;
  }
  await runAction(label, () => action(selectedJob.value));
}

async function runAction(label, action) {
  busyAction.value = label;
  error.value = "";
  try {
    await action();
    setNotice(`${label}已提交`);
    await refreshRuntime(true);
  } catch (err) {
    setError(err);
  } finally {
    busyAction.value = "";
  }
}

function failureFilter(failure) {
  return failure?.test_suite && failure?.test_name ? `${failure.test_suite}.${failure.test_name}` : "*";
}

function normalizePath(path) {
  return String(path || "")
    .replace(/\\/g, "/")
    .replace(/^tests\/gme\//, "")
    .replace(/^\/+/, "");
}

function generatedTestStatus(fullName, failure, persistedResult, output) {
  if (["passed", "failed", "skipped"].includes(persistedResult?.status)) {
    return persistedResult.status;
  }
  if (failure?.status === "open") return "failed";
  if (hasGtestLine(output, "FAILED", fullName)) return "failed";
  if (hasGtestLine(output, "OK", fullName)) return "passed";
  if (hasGtestLine(output, "SKIPPED", fullName)) return "skipped";
  return output ? "unknown" : "unknown";
}

function generatedTestStatusLabel(status) {
  return {
    passed: "通过",
    failed: "失败",
    skipped: "已跳过",
    unknown: "未确认",
  }[status] || status;
}

function latestArtifactContent(names) {
  const contents = artifacts.value.contents || {};
  const candidates = names.filter((name) => contents[name]);
  if (!candidates.length) return "";
  const timed = candidates
    .map((name) => ({ name, mtime: artifactMtime(name) }))
    .filter((item) => item.mtime > 0)
    .sort((left, right) => right.mtime - left.mtime);
  if (timed.length) return contents[timed[0].name] || "";
  return contents["gtest_output_after_skip.txt"] || contents["gtest_output.txt"] || "";
}

function artifactMtime(name) {
  const item = (artifacts.value.files || []).find((file) => file.name === name);
  return Number(item?.mtime || 0);
}

function gtestNamesWithStatus(output, status) {
  if (!output) return [];
  const names = [];
  const pattern = new RegExp(`\\[\\s*${status}\\s*\\]\\s+([^\\s(]+)`, "g");
  let match;
  while ((match = pattern.exec(output)) !== null) {
    names.push(match[1]);
  }
  return names;
}

function hasGtestLine(output, status, fullName) {
  if (!output || !fullName) return false;
  const escaped = escapeRegExp(fullName);
  return new RegExp(`\\[\\s*${status}\\s*\\]\\s+${escaped}(?:\\s|$)`).test(output);
}

function parseDiffFiles(diff) {
  const files = [];
  let current = null;
  for (const line of String(diff || "").split(/\r?\n/)) {
    const fileMatch = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (fileMatch) {
      current = {
        path: normalizePath(fileMatch[2]),
        additions: 0,
        deletions: 0,
        rawLines: [line],
        addedLines: [],
      };
      files.push(current);
      continue;
    }
    if (!current) continue;
    current.rawLines.push(line);
    if (line.startsWith("+") && !line.startsWith("+++")) {
      current.additions += 1;
      current.addedLines.push(line.slice(1));
    } else if (line.startsWith("-") && !line.startsWith("---")) {
      current.deletions += 1;
    }
  }
  return files.map((file) => ({
    path: file.path,
    additions: file.additions,
    deletions: file.deletions,
    raw: file.rawLines.join("\n"),
    addedText: file.addedLines.join("\n"),
  }));
}

function extractAddedTestBlock(text, suite, name) {
  if (!text || !suite || !name) return "";
  const decl = new RegExp(`TEST_F\\s*\\(\\s*${escapeRegExp(suite)}\\s*,\\s*${escapeRegExp(name)}\\s*\\)\\s*\\{`);
  const match = decl.exec(text);
  if (!match) return "";
  const brace = text.indexOf("{", match.index);
  const end = matchingBrace(text, brace);
  if (end < 0) return text.slice(match.index).trim();
  let start = match.index;
  const before = text.slice(0, start);
  const previousTest = Math.max(before.lastIndexOf("TEST_F("), before.lastIndexOf("TEST("));
  const commentStart = Math.max(before.lastIndexOf("/**"), before.lastIndexOf("/*"));
  if (commentStart > previousTest) start = commentStart;
  return text.slice(start, end + 1).trim();
}

function matchingBrace(text, start) {
  if (start < 0) return -1;
  let depth = 0;
  let state = "code";
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i];
    const next = text[i + 1] || "";
    if (state === "code") {
      if (ch === "/" && next === "/") {
        state = "line";
        i += 1;
      } else if (ch === "/" && next === "*") {
        state = "block";
        i += 1;
      } else if (ch === '"') {
        state = "string";
      } else if (ch === "'") {
        state = "char";
      } else if (ch === "{") {
        depth += 1;
      } else if (ch === "}") {
        depth -= 1;
        if (depth === 0) return i;
      }
    } else if (state === "line") {
      if (ch === "\n") state = "code";
    } else if (state === "block") {
      if (ch === "*" && next === "/") {
        state = "code";
        i += 1;
      }
    } else if (state === "string") {
      if (ch === "\\") i += 1;
      else if (ch === '"') state = "code";
    } else if (state === "char") {
      if (ch === "\\") i += 1;
      else if (ch === "'") state = "code";
    }
  }
  return -1;
}

function escapeRegExp(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function withCurrent(values, current) {
  const seen = new Set();
  const result = [];
  for (const value of [current, ...(values || [])]) {
    const item = String(value || "").trim();
    if (!item || seen.has(item)) continue;
    seen.add(item);
    result.push(item);
  }
  return result;
}

function shortId(id) {
  return id ? id.slice(0, 8) : "";
}

function jobStatus(job) {
  return jobStatusLabels[job.status] || job.status;
}

function failureStatus(failure) {
  const sourceJob = testJobs.value.find((job) => job.id === failure?.job_id);
  if (failure?.status === "resolved" && isSubmittedFailure(failure, sourceJob)) return "未修复";
  return failureStatusLabels[failure.status] || failure.status;
}

function failureStatusTone(failure) {
  const sourceJob = testJobs.value.find((job) => job.id === failure?.job_id);
  if (failure?.status === "open") return "danger";
  if (failure?.status === "resolved" && isSubmittedFailure(failure, sourceJob)) return "danger";
  return statusTone(failure?.status);
}

async function fixSelectedFailures() {
  const selected = selectedRepairFailures.value.length
    ? selectedRepairFailures.value
    : (selectedFailure.value ? [selectedFailure.value] : []);
  if (!selected.length) {
    setError("请先选择至少一个失败用例。");
    return;
  }
  if (new Set(selected.map(failureApiName)).size > 1) {
    setError("一次修复任务只能选择同一接口的失败测试。");
    return;
  }
  await runAction(`修复选中的 ${selected.length} 条失败`, async () => {
    const job = await api.fixFailures(selected.map((failure) => failure.id));
    selectedFixJobId.value = job.id;
    selectedFailureIds.value = [];
    activePage.value = "fix";
    fixWorkspaceTab.value = "review";
  });
}

function statusTone(status) {
  if (["failed", "fix_failed"].includes(status)) return "danger";
  if (["needs_review", "fix_ready"].includes(status)) return "warning";
  if (["pr_created", "fixed", "worktree_cleaned"].includes(status)) return "success";
  if (["running_agent", "checking_format", "building", "running_tests", "running_memory_audit", "fixing", "creating_pr", "applying_skips"].includes(status)) return "info";
  return "neutral";
}

function stepState(job, activeStatus, done) {
  if (!job) return "pending";
  if (job.status === activeStatus) return "active";
  return done ? "done" : "pending";
}

async function copyText(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    setNotice("已复制");
  } catch {
    setError("复制失败，请手动选择文本。");
  }
}

function openPr() {
  const url = jobLatestPrUrl(selectedJob.value);
  if (!url) {
    setError("选中任务还没有 PR 链接。");
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

function openPrUrl(value) {
  const url = extractPrUrl(value);
  if (!url) {
    setError("这条已提交测试没有可用的 PR 链接。");
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

function pullRequestLabel(value) {
  const url = extractPrUrl(value);
  const match = /\/pull\/(\d+)/.exec(url);
  return match ? `#${match[1]}` : "打开 PR";
}

function jobLatestPrUrl(job) {
  const metadata = job?.metadata || {};
  const history = Array.isArray(metadata.test_prs) ? metadata.test_prs : [];
  const candidates = [
    ...history.map((item) => item?.url).reverse(),
    metadata.selected_pr_url,
    metadata.pr_url,
    metadata.skip_pr_url,
  ];
  for (const value of candidates) {
    const url = extractPrUrl(value);
    if (url) return url;
  }
  return "";
}

function extractPrUrl(value) {
  const matches = String(value || "").match(/https?:\/\/[^\s]+\/pull\/\d+(?:[^\s]*)?/g);
  return matches?.length ? matches[matches.length - 1].replace(/[.,;)]*$/, "") : "";
}

function setNotice(message) {
  notice.value = message;
  if (noticeTimer) window.clearTimeout(noticeTimer);
  noticeTimer = window.setTimeout(() => {
    notice.value = "";
  }, 3000);
}

function setError(err) {
  error.value = err?.message || String(err);
}
