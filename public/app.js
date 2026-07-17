const state = {
  legacy: {
    poems: [],
    styles: [],
    projects: [],
    images: [],
    jobs: [],
    config: {},
  },
  sop: {
    summary: null,
    poems: [],
    requirements: [],
    requirement_generation_failures: [],
    requirement_schema: null,
    directions: [],
    direction_generation_failures: [],
    direction_schema: null,
    instruction: null,
    instruction_versions: [],
    art_bible: null,
    art_bible_versions: [],
    style_packs: [],
    style_benchmark_poems: [],
    style_benchmark_runs: [],
    style_contracts: null,
    qc_policy: null,
    qc_policy_versions: [],
    qc_calibration: null,
    provider_status: null,
    production_report: { daily: [], anomalies: [], tasks: {} },
    batches: [],
    tasks: [],
    budget: null,
    review_queue: { groups: [], summary: {} },
    rework_orders: [],
    final_assets: [],
    export_packages: [],
    backups: [],
  },
  activeView: "overview",
  selectedPoems: new Set(),
  selectedRequirements: new Set(),
  selectedDirections: new Set(),
  poemQuery: "",
  poemStatus: "",
  requirementFilter: "all",
  importRecords: null,
  importPreview: null,
  batchEstimate: null,
  loading: false,
  currentImageId: null,
  reviewSelection: new Set(),
  reviewShowBlocked: false,
  assetQuery: "",
  queueTaskFilter: "",
  queueBatchFilter: "",
  queueErrorFilter: "",
  queueTaskQuery: "",
};

const roleLabels = {
  producer: "制片人",
  content_editor: "内容编辑",
  art_director: "美术指导",
  ai_operator: "AI 操作员",
  system_admin: "系统管理员",
};
const storedRole = localStorage.getItem("tang-sop-role");
const initialRole = roleLabels[storedRole] ? storedRole : "producer";
const ACTOR = {
  id: `local-${initialRole}`,
  role: initialRole,
};

const viewMeta = {
  overview: ["PRODUCTION CONTROL", "生产总览"],
  instructions: ["GLOBAL CREATIVE RULES", "AI 指令"],
  requirements: ["REQUIREMENT REVIEW", "需求板"],
  directions: ["ART DIRECTION", "方向板"],
  queue: ["GENERATION QUEUE", "生产队列"],
  review: ["REVIEW DESK", "审片台"],
  assets: ["FINAL ASSETS", "成品库"],
  resources: ["STYLE & SYSTEM", "资源与设置"],
};

const statusMeta = {
  imported: ["待校验", "neutral"],
  content_review: ["内容待审", "warning"],
  requirement_draft: ["待生成需求", "neutral"],
  requirement_review: ["需求待审", "warning"],
  direction_draft: ["待生成方向", "neutral"],
  direction_review: ["方向待审", "warning"],
  ready_for_production: ["待排产", "ready"],
  generating: ["生成中", "running"],
  candidate_review: ["待审片", "warning"],
  rework: ["返工中", "danger"],
  final_review: ["待终审", "warning"],
  approved: ["终审通过", "success"],
  exported: ["已交付", "success"],
  blocked: ["已阻塞", "danger"],
  paused: ["已暂停", "neutral"],
  archived: ["已归档", "neutral"],
};

const requirementStatusMeta = {
  draft: ["草稿", "neutral"],
  in_review: ["待审核", "warning"],
  approved: ["已通过", "success"],
  rejected: ["已退回", "danger"],
  disabled: ["已停用", "neutral"],
};

const directionTypeMeta = {
  narrative: ["叙事型", "叙"],
  atmospheric: ["意境型", "境"],
  symbolic: ["象征型", "象"],
};

const jobStatusMeta = {
  queued: ["排队中", "neutral"],
  running: ["生成中", "running"],
  completed: ["已完成", "success"],
  failed: ["失败", "danger"],
};

const batchStatusMeta = {
  draft: ["草稿", "neutral"],
  queued: ["排队中", "ready"],
  running: ["运行中", "running"],
  paused: ["已暂停", "warning"],
  completed: ["已完成", "success"],
  partially_failed: ["部分失败", "danger"],
  cancelled: ["已取消", "neutral"],
  budget_blocked: ["预算阻塞", "danger"],
};

const taskStatusMeta = {
  pending: ["待启动", "neutral"],
  ready: ["待执行", "ready"],
  running: ["执行中", "running"],
  succeeded: ["已成功", "success"],
  failed: ["失败", "danger"],
  retry_waiting: ["等待重试", "warning"],
  cancelled: ["已取消", "neutral"],
  blocked: ["需核对", "danger"],
};

const productionImageStatusMeta = {
  pending_qc: ["等待 QC", "warning"],
  review_ready: ["待审片", "ready"],
  qc_blocked: ["QC 隔离", "danger"],
  needs_manual_qc: ["需人工 QC", "warning"],
  selected: ["已入选", "success"],
  rejected: ["已淘汰", "neutral"],
  final_candidate: ["终审候选", "success"],
};

const qcDecisionMeta = {
  rejected: ["自动拒绝", "danger"],
  manual_review: ["人工复核", "warning"],
  candidate: ["合格候选", "ready"],
  recommended: ["优先推荐", "success"],
};

const qcDimensionLabels = {
  safety: "安全",
  technical_integrity: "技术完整",
  poem_relevance: "诗意相关",
  style_match: "风格匹配",
  historical_plausibility: "历史合理",
  composition: "构图",
  character_quality: "人物质量",
  series_consistency: "系列一致",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function asLines(value) {
  return Array.isArray(value)
    ? value.map((item) => String(item).trim()).filter(Boolean)
    : String(value || "")
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.message || payload.error || `请求失败（${response.status}）`);
    error.code = payload.code || "REQUEST_FAILED";
    error.status = response.status;
    throw error;
  }
  return payload;
}

function setSyncState(kind, copy) {
  const element = document.querySelector("#sync-state");
  element.className = `sync-state is-${kind}`;
  element.querySelector("strong").textContent = copy;
}

function showToast(message, kind = "success") {
  const region = document.querySelector("#toast-region");
  const toast = document.createElement("div");
  toast.className = `toast toast-${kind}`;
  toast.textContent = message;
  region.append(toast);
  requestAnimationFrame(() => toast.classList.add("is-visible"));
  setTimeout(() => {
    toast.classList.remove("is-visible");
    setTimeout(() => toast.remove(), 200);
  }, 3600);
}

async function loadData({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  if (!quiet) setSyncState("loading", "正在同步");
  try {
    const [legacy, sop] = await Promise.all([
      api("/api/bootstrap"),
      api("/api/sop/bootstrap"),
    ]);
    state.legacy = legacy;
    state.sop = sop;
    pruneSelection();
    renderAll();
    setSyncState("ready", `已同步 · ${formatDate(new Date().toISOString())}`);
  } catch (error) {
    console.error(error);
    setSyncState("error", "同步失败");
    showToast(error.message, "error");
  } finally {
    state.loading = false;
  }
}

async function refreshSop(successMessage = "") {
  try {
    state.sop = await api("/api/sop/bootstrap");
    pruneSelection();
    renderAll();
    if (
      state.queueTaskFilter ||
      state.queueBatchFilter ||
      state.queueErrorFilter ||
      state.queueTaskQuery
    ) {
      await refreshTaskPage({ offset: 0, quiet: true });
    }
    setSyncState("ready", `已同步 · ${formatDate(new Date().toISOString())}`);
    if (successMessage) showToast(successMessage);
  } catch (error) {
    console.error(error);
    showToast(error.message, "error");
  }
}

async function refreshTaskPage({ offset = 0, quiet = false } = {}) {
  const params = new URLSearchParams({ limit: "50", offset: String(Math.max(0, offset)) });
  if (state.queueTaskFilter) params.set("status", state.queueTaskFilter);
  if (state.queueBatchFilter) params.set("batch_id", state.queueBatchFilter);
  if (state.queueErrorFilter) params.set("error_code", state.queueErrorFilter);
  if (state.queueTaskQuery) params.set("q", state.queueTaskQuery);
  try {
    state.sop.task_page = await api(`/api/tasks?${params.toString()}`);
    renderQueue();
  } catch (error) {
    if (!quiet) showToast(error.message, "error");
  }
}

function pruneSelection() {
  const valid = new Set(state.sop.poems.map((poem) => poem.id));
  for (const poemId of state.selectedPoems) {
    if (!valid.has(poemId)) state.selectedPoems.delete(poemId);
  }
  const validRequirements = new Set(
    state.sop.requirements
      .filter((item) => item.status === "in_review")
      .map((item) => item.id),
  );
  for (const requirementId of state.selectedRequirements) {
    if (!validRequirements.has(requirementId)) {
      state.selectedRequirements.delete(requirementId);
    }
  }
  const validDirections = new Set(
    state.sop.directions
      .filter((item) => item.status === "in_review")
      .map((item) => item.id),
  );
  for (const directionId of state.selectedDirections) {
    if (!validDirections.has(directionId)) {
      state.selectedDirections.delete(directionId);
    }
  }
  const validImages = new Set(
    (state.sop.review_queue?.groups || []).flatMap((group) =>
      group.candidates.map((image) => image.id),
    ),
  );
  for (const imageId of state.reviewSelection) {
    if (!validImages.has(imageId)) state.reviewSelection.delete(imageId);
  }
}

function project() {
  return state.sop.summary?.project || {};
}

function poemById(poemId) {
  return state.sop.poems.find((poem) => poem.id === poemId);
}

function requirementById(requirementId) {
  return state.sop.requirements.find((item) => item.id === requirementId);
}

function directionsForPoem(poemId) {
  return state.sop.directions.filter((item) => item.poem_id === poemId);
}

function directionById(directionId) {
  return state.sop.directions.find((item) => item.id === directionId);
}

function productionImages() {
  return (state.sop.review_queue?.groups || []).flatMap(
    (group) => group.candidates || [],
  );
}

function productionImageById(imageId) {
  return productionImages().find((image) => image.id === imageId);
}

function publishedStylePacks() {
  return (state.sop.style_packs || []).filter((style) =>
    ["active", "limited"].includes(style.status),
  );
}

function statusBadge(status, map = statusMeta) {
  const [label, tone] = map[status] || [status || "未知", "neutral"];
  return `<span class="status-badge tone-${tone}">${escapeHtml(label)}</span>`;
}

function visiblePoems() {
  const query = state.poemQuery.trim().toLowerCase();
  return state.sop.poems.filter((poem) => {
    if (state.poemStatus && poem.status !== state.poemStatus) return false;
    if (!query) return true;
    const haystack = [
      poem.title,
      poem.author,
      poem.theme,
      poem.mood,
      ...(poem.imagery || []),
    ]
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  });
}

function renderChrome() {
  const summary = state.sop.summary;
  if (!summary) return;
  const completion = summary.completion_percent || 0;
  document.querySelector("#sidebar-project-name").textContent = summary.project.name;
  document.querySelector("#sidebar-progress-bar").style.width = `${completion}%`;
  document.querySelector("#sidebar-progress-copy").textContent =
    `${summary.total_poems} 首 · ${completion}% 进入终审或交付`;
  document.querySelector("#app-version").textContent = state.legacy.config.version || "0.3";
  document.querySelector("#current-role").value = ACTOR.role;

  const providerStatus = state.sop.provider_status || state.legacy.config.provider_status || {};
  const provider = providerStatus.provider || state.legacy.config.provider || "demo";
  const providerCard = document.querySelector("#provider-card");
  providerCard.classList.toggle("is-live", provider === "openai");
  document.querySelector("#provider-label").textContent =
    provider === "openai"
      ? `${providerStatus.model || state.legacy.config.model} · ${
          providerStatus.configured ? "已配置" : "待配置"
        }`
      : "本地演示引擎";

  document.querySelector("#nav-requirement-count").textContent =
    summary.todos.requirement_review || 0;
  document.querySelector("#nav-direction-count").textContent =
    summary.todos.direction_review || 0;
  document.querySelector("#nav-job-count").textContent = state.sop.batches.filter((batch) =>
    ["queued", "running", "paused", "budget_blocked"].includes(batch.status),
  ).length;
  document.querySelector("#nav-review-count").textContent =
    state.sop.review_queue?.summary?.review_ready || 0;
}

function applyRoleVisibility() {
  document.body.dataset.actorRole = ACTOR.role;
  document.querySelectorAll("[data-role-allow]").forEach((element) => {
    const allowed = element.dataset.roleAllow.split(",");
    element.hidden = !allowed.includes(ACTOR.role);
  });
}

function renderOverview() {
  const summary = state.sop.summary;
  if (!summary) return;
  const completed =
    (summary.status_counts.approved || 0) + (summary.status_counts.exported || 0);
  document.querySelector("#command-project-name").textContent = summary.project.name;
  document.querySelector("#command-progress-value").textContent =
    `${summary.completion_percent}%`;
  document.querySelector("#command-progress-bar").style.width =
    `${summary.completion_percent}%`;
  document.querySelector("#command-progress-meta").textContent =
    `${completed} / ${summary.total_poems} 首进入终审或交付`;

  const candidateCount = state.sop.review_queue?.summary?.review_ready || 0;
  const finalCount = (state.sop.final_assets || []).filter(
    (asset) => Boolean(asset.is_current),
  ).length;
  const activeTaskCount = state.sop.tasks.filter((task) =>
    ["pending", "ready", "running", "retry_waiting"].includes(task.status),
  ).length;
  const recentTasks = state.sop.tasks.filter((task) =>
    ["succeeded", "failed", "blocked"].includes(task.status),
  );
  const successRate = recentTasks.length
    ? Math.round(
        (recentTasks.filter((task) => task.status === "succeeded").length /
          recentTasks.length) *
          100,
      )
    : 0;
  const budget = state.sop.budget || {};
  const metrics = [
    ["项目诗词", summary.total_poems, "已进入生产项目", "neutral"],
    [
      "待处理审批",
      (summary.todos.requirement_review || 0) + (summary.todos.direction_review || 0),
      "需求与方向审核",
      "warning",
    ],
    ["待排产", summary.todos.ready_for_production || 0, "已通过方向门禁", "ready"],
    ["队列积压", activeTaskCount, "待执行与运行任务", activeTaskCount ? "ready" : "neutral"],
    ["任务成功率", `${successRate}%`, `${recentTasks.length} 个已收敛任务`, successRate >= 90 ? "success" : "warning"],
    [
      "预算余额",
      Number(budget.remaining || 0).toFixed(2),
      `${Number(budget.spent || 0).toFixed(2)} / ${Number(
        budget.hard_limit || 0,
      ).toFixed(2)} ${budget.currency || "USD"} 已用`,
      budget.soft_warning ? "warning" : "success",
    ],
  ];
  document.querySelector("#metric-grid").innerHTML = metrics
    .map(
      ([label, value, note, tone]) => `
        <article class="metric-card tone-card-${tone}">
          <span>${escapeHtml(label)}</span>
          <strong>${value}</strong>
          <small>${escapeHtml(note)}</small>
        </article>`,
    )
    .join("");

  const maxCount = Math.max(1, ...summary.stages.map((stage) => stage.count));
  document.querySelector("#pipeline-grid").innerHTML = summary.stages
    .filter((stage) => stage.key !== "blocked")
    .map(
      (stage, index) => `
        <button class="pipeline-step" type="button" data-stage-key="${stage.key}">
          <span class="pipeline-order">${String(index + 1).padStart(2, "0")}</span>
          <strong>${stage.count}</strong>
          <small>${escapeHtml(stage.label)}</small>
          <span class="pipeline-bar"><i style="width:${Math.max(
            stage.count ? 10 : 0,
            Math.round((stage.count / maxCount) * 100),
          )}%"></i></span>
        </button>`,
    )
    .join("");

  const todos = [
    {
      label: "审核 AI 需求",
      count: summary.todos.requirement_review || 0,
      note: "确认诗意、必须项和历史风险",
      view: "requirements",
      tone: "yellow",
    },
    {
      label: "审核画面方向",
      count: summary.todos.direction_review || 0,
      note: "每首至少批准一个方向",
      view: "directions",
      tone: "blue",
    },
    {
      label: "审核候选图片",
      count: candidateCount,
      note: "处理收藏、淘汰与终审候选",
      view: "review",
      tone: "blue",
    },
    {
      label: "处理失败任务",
      count: state.sop.tasks.filter((task) =>
        ["failed", "blocked"].includes(task.status),
      ).length,
      note: "核对结果未知项或重试可恢复错误",
      view: "queue",
      tone: "red",
    },
    {
      label: "创建生产批次",
      count: summary.todos.ready_for_production || 0,
      note: "通过门禁，等待排产",
      view: "queue",
      tone: "green",
    },
    {
      label: "待终审与成品",
      count: (summary.todos.final_review || 0) + finalCount,
      note: "完成质检后锁定交付版本",
      view: "assets",
      tone: "green",
    },
  ];
  document.querySelector("#todo-list").innerHTML = todos
    .map(
      (todo) => `
        <button class="todo-item tone-${todo.tone}" type="button" data-switch-view="${todo.view}">
          <span class="todo-count">${todo.count}</span>
          <span><strong>${escapeHtml(todo.label)}</strong><small>${escapeHtml(todo.note)}</small></span>
          <i>→</i>
        </button>`,
    )
    .join("");
}

function renderProductionReport() {
  const report = state.sop.production_report || { daily: [], anomalies: [], tasks: {} };
  document.querySelector("#report-period").textContent = `最近 ${report.days || 7} 天`;
  const reportMetrics = [
    ["生成图片", report.generated || 0],
    ["人工决策", report.reviewed || 0],
    ["返工单", report.reworks || 0],
    ["终审成品", report.finalized || 0],
    ["任务成功率", `${Number(report.tasks?.success_rate || 0).toFixed(1)}%`],
    ["QC 均分", Number(report.qc?.average_score || 0).toFixed(1)],
    ["待人工 QC", report.qc?.manual_review || 0],
    ["实际成本", Number(report.actual_cost || 0).toFixed(2)],
  ];
  document.querySelector("#report-metrics").innerHTML = reportMetrics
    .map(
      ([label, value]) => `<span><small>${escapeHtml(label)}</small><strong>${value}</strong></span>`,
    )
    .join("");

  const daily = report.daily || [];
  const maxDaily = Math.max(
    1,
    ...daily.map((item) => Math.max(item.generated, item.succeeded, item.finalized)),
  );
  document.querySelector("#report-trend").innerHTML = daily.length
    ? daily
        .map(
          (item) => `<article title="${item.date} · 生成 ${item.generated} · 成功 ${item.succeeded} · 终审 ${item.finalized}">
            <div>
              <i class="is-generated" style="height:${Math.round((item.generated / maxDaily) * 100)}%"></i>
              <i class="is-succeeded" style="height:${Math.round((item.succeeded / maxDaily) * 100)}%"></i>
              <i class="is-finalized" style="height:${Math.round((item.finalized / maxDaily) * 100)}%"></i>
            </div>
            <span>${escapeHtml(item.date.slice(5))}</span>
          </article>`,
        )
        .join("")
    : '<span class="muted-value">暂无生产数据</span>';

  const anomalies = report.anomalies || [];
  document.querySelector("#anomaly-total").textContent = `${report.anomaly_count || 0} 项`;
  document.querySelector("#anomaly-list").innerHTML = anomalies.length
    ? anomalies
        .map(
          (item) => `<button class="anomaly-item severity-${item.severity}" type="button" data-anomaly-view="${item.view}" data-anomaly-filter="${item.filter}">
            <span>${item.count}</span>
            <div><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(
              item.suggested_action,
            )}</small></div><i>→</i>
          </button>`,
        )
        .join("")
    : '<div class="empty-state compact"><strong>当前没有生产异常</strong><p>失败、阻塞、QC 隔离和预算停机会集中显示在这里。</p></div>';
}

function nextAction(poem) {
  if (["imported", "content_review"].includes(poem.status)) {
    return ["批准内容", "approve-content"];
  }
  if (poem.status === "requirement_draft") {
    return ["生成需求", "generate-requirement"];
  }
  if (poem.status === "requirement_review") {
    return ["审核需求", "open-requirement"];
  }
  if (poem.status === "direction_draft") {
    return ["生成方向", "generate-direction"];
  }
  if (poem.status === "direction_review") {
    return ["审核方向", "open-directions"];
  }
  if (poem.status === "ready_for_production") {
    return ["进入排产", "open-queue"];
  }
  if (["candidate_review", "rework", "final_review"].includes(poem.status)) {
    return ["进入审片", "open-review"];
  }
  if (poem.status === "blocked") {
    return ["查看阻塞", "open-overview"];
  }
  return ["查看详情", "open-requirement"];
}

function renderPoemTable() {
  const poems = visiblePoems();
  const body = document.querySelector("#poem-table-body");
  document.querySelector("#poem-table-empty").hidden = poems.length > 0;
  body.innerHTML = poems
    .map((poem) => {
      const action = nextAction(poem);
      const requirement = poem.requirement;
      const checked = state.selectedPoems.has(poem.id);
      return `
        <tr class="${checked ? "is-selected" : ""}">
          <td class="checkbox-cell">
            <input type="checkbox" data-select-poem="${poem.id}" ${
              checked ? "checked" : ""
            } aria-label="选择${escapeHtml(poem.title)}" />
          </td>
          <td>
            <div class="poem-cell">
              <button class="poem-detail-link" type="button" data-open-poem-detail="${poem.id}">${escapeHtml(poem.title)}</button>
              <span>${escapeHtml(poem.dynasty)} · ${escapeHtml(poem.author)}</span>
            </div>
          </td>
          <td>
            <div class="topic-cell">
              <span>${escapeHtml(poem.theme || "未分类")}</span>
              <small>${escapeHtml(poem.mood || "待补情绪")}</small>
            </div>
          </td>
          <td>${statusBadge(poem.status)}</td>
          <td>
            ${
              requirement
                ? `<button class="inline-version" type="button" data-poem-action="open-requirement" data-poem-id="${poem.id}">v${requirement.version} · ${
                    requirementStatusMeta[requirement.status]?.[0] || requirement.status
                  }</button>`
                : '<span class="muted-value">尚未生成</span>'
            }
          </td>
          <td>
            <span class="direction-count">
              <strong>${poem.approved_direction_count || 0}</strong> / ${
                poem.direction_count || 0
              } 通过
            </span>
          </td>
          <td>
            <button class="row-action" type="button" data-poem-action="${
              action[1]
            }" data-poem-id="${poem.id}">${action[0]} →</button>
          </td>
        </tr>`;
    })
    .join("");

  const visibleIds = poems.map((poem) => poem.id);
  const allChecked =
    visibleIds.length > 0 && visibleIds.every((id) => state.selectedPoems.has(id));
  const someChecked = visibleIds.some((id) => state.selectedPoems.has(id));
  const selectAll = document.querySelector("#select-all-poems");
  selectAll.checked = allChecked;
  selectAll.indeterminate = someChecked && !allChecked;
  document.querySelector("#selection-count").textContent = state.selectedPoems.size
    ? `已选择 ${state.selectedPoems.size} 首`
    : "未选择";
}

function renderRequirementTabs() {
  const failurePoems = new Set(
    (state.sop.requirement_generation_failures || []).map((item) => item.poem_id),
  );
  const counts = {
    all: state.sop.requirements.length,
    in_review: state.sop.requirements.filter((item) => item.status === "in_review").length,
    approved: state.sop.requirements.filter((item) => item.status === "approved").length,
    rejected: state.sop.requirements.filter((item) => item.status === "rejected").length,
    missing: state.sop.poems.filter((poem) => !poem.requirement).length,
    failed: failurePoems.size,
  };
  const tabs = [
    ["all", "全部"],
    ["in_review", "待审核"],
    ["approved", "已通过"],
    ["rejected", "已退回"],
    ["failed", "生成异常"],
    ["missing", "待生成"],
  ];
  document.querySelector("#requirement-tabs").innerHTML = tabs
    .map(
      ([key, label]) => `
        <button class="${state.requirementFilter === key ? "is-active" : ""}" type="button" data-requirement-filter="${key}">
          ${label}<span>${counts[key]}</span>
        </button>`,
    )
    .join("");
}

function requirementCard(requirement) {
  const content = requirement.content || {};
  const evidence = (content.evidence || [])[0];
  const failure = requirementFailureForPoem(requirement.poem_id);
  const confidenceEntries = Object.entries(content.confidence || {});
  const lowConfidenceCount = confidenceEntries.filter(
    ([, item]) => item?.level === "low" || item?.requires_review,
  ).length;
  return `
    <article class="requirement-card tone-border-${
      requirementStatusMeta[requirement.status]?.[1] || "neutral"
    }">
      <header>
        <div>
          <span>${escapeHtml(requirement.theme || "待分类")} · v${requirement.version}</span>
          <h3>${escapeHtml(requirement.poem_title)}</h3>
          <small>${escapeHtml(requirement.author)}</small>
        </div>
        ${statusBadge(requirement.status, requirementStatusMeta)}
      </header>
      <div class="requirement-thesis">
        <span>画面命题</span>
        <p>${escapeHtml(content.composition || "等待编辑补充画面构图")}</p>
      </div>
      <div class="token-block">
        <span>核心意象</span>
        <div>${(content.core_imagery || [])
          .slice(0, 5)
          .map((item) => `<i>${escapeHtml(item)}</i>`)
          .join("")}</div>
      </div>
      <div class="constraint-grid">
        <div><span>必须出现</span><strong>${(content.must_have || []).length}</strong></div>
        <div><span>禁止出现</span><strong>${(content.avoid || []).length}</strong></div>
        <div><span>风险项</span><strong>${(content.historical_risks || []).length}</strong></div>
        <div><span>锁定字段</span><strong>${(content.locked_fields || []).length}</strong></div>
        <div><span>待人工确认</span><strong>${lowConfidenceCount}</strong></div>
      </div>
      <p class="requirement-contract-line">${escapeHtml(
        requirement.schema_version || state.sop.requirement_schema?.schema_version || "legacy",
      )} · ${requirement.cache_hit ? "版本缓存命中" : "本次生成"} · ContentVersion ${escapeHtml(
        requirement.content_version_id || "未记录",
      )}</p>
      ${
        evidence
          ? `<p class="evidence-line">依据：${escapeHtml(evidence.quote)}</p>`
          : ""
      }
      ${
        failure
          ? `<p class="generation-error-note"><strong>${escapeHtml(failure.error_code)}</strong>${escapeHtml(
              failure.error_message || "需求生成失败",
            )}<small>自动修复 ${failure.repair_attempts || 0}/1 次 · ${formatDate(failure.completed_at)}</small></p>`
          : ""
      }
      ${
        requirement.rejection_reason
          ? `<p class="rejection-note">退回原因：${escapeHtml(
              requirement.rejection_reason,
            )}</p>`
          : ""
      }
      <footer>
        <div data-role-allow="content_editor,producer,system_admin">
          ${
            requirement.status === "in_review"
              ? `<label class="select-card"><input type="checkbox" data-select-requirement="${requirement.id}" ${state.selectedRequirements.has(requirement.id) ? "checked" : ""} /><span>批量</span></label>`
              : ""
          }
          <button class="card-link" type="button" data-requirement-action="edit" data-requirement-id="${requirement.id}">编辑详情</button>
          <button class="card-link" type="button" data-regenerate-requirement="${requirement.poem_id}">重算未锁字段</button>
        </div>
        <div>
          ${
            requirement.status === "in_review"
              ? `<div data-role-allow="content_editor,producer,system_admin"><button class="danger-button" type="button" data-requirement-action="reject" data-requirement-id="${requirement.id}">退回</button>
                 <button class="approve-button" type="button" data-requirement-action="approve" data-requirement-id="${requirement.id}">通过需求</button></div>`
              : requirement.status === "approved"
                ? `<div data-role-allow="art_director,producer,system_admin"><button class="approve-button" type="button" data-poem-action="generate-direction" data-poem-id="${requirement.poem_id}">生成三方向</button></div>`
                : ""
          }
        </div>
      </footer>
    </article>`;
}

function requirementFailureForPoem(poemId) {
  return (state.sop.requirement_generation_failures || []).find(
    (item) => item.poem_id === poemId,
  );
}

function missingRequirementCard(poem) {
  const failure = requirementFailureForPoem(poem.id);
  const firstIssue = failure?.validation?.final_issues?.[0];
  return `
    <article class="requirement-card is-missing ${failure ? "tone-border-danger" : ""}">
      <header>
        <div>
          <span>${escapeHtml(poem.theme || "待分类")}</span>
          <h3>${escapeHtml(poem.title)}</h3>
          <small>${escapeHtml(poem.author)}</small>
        </div>
        ${failure ? '<span class="status-badge tone-danger">生成异常</span>' : statusBadge("draft", requirementStatusMeta)}
      </header>
      <div class="missing-copy">
        <strong>${failure ? escapeHtml(failure.error_code || "需求生成异常") : "等待生成结构化需求"}</strong>
        <p>${
          failure
            ? escapeHtml(firstIssue?.message || failure.error_message || "输出未通过 RequirementCard Schema。")
            : "系统将分析诗意、意象、时空、必须项、禁用项和历史风险。"
        }</p>
        ${
          failure
            ? `<small class="generation-failure-meta">${escapeHtml(failure.schema_version)} · 自动修复 ${failure.repair_attempts || 0}/1 次 · ${formatDate(failure.completed_at)}</small>`
            : ""
        }
      </div>
      <footer data-role-allow="content_editor,producer,system_admin">
        <label class="select-card">
          <input type="checkbox" data-select-poem="${poem.id}" ${
            state.selectedPoems.has(poem.id) ? "checked" : ""
          } />
          <span>选择</span>
        </label>
        <button class="approve-button" type="button" data-poem-action="generate-requirement" data-poem-id="${
          poem.id
        }">${failure ? "↻ 修正后重试" : "✦ 生成需求"}</button>
      </footer>
    </article>`;
}

function renderRequirements() {
  renderRequirementTabs();
  let cards = [];
  if (state.requirementFilter === "failed") {
    const poemIds = [
      ...new Set(
        (state.sop.requirement_generation_failures || []).map((item) => item.poem_id),
      ),
    ];
    cards = poemIds
      .map((poemId) => {
        const requirement = state.sop.requirements.find((item) => item.poem_id === poemId);
        const poem = state.sop.poems.find((item) => item.id === poemId);
        return requirement ? requirementCard(requirement) : poem ? missingRequirementCard(poem) : "";
      })
      .filter(Boolean);
  } else if (state.requirementFilter === "missing") {
    cards = state.sop.poems
      .filter((poem) => !poem.requirement)
      .map(missingRequirementCard);
  } else {
    cards = state.sop.requirements
      .filter(
        (item) =>
          state.requirementFilter === "all" ||
          item.status === state.requirementFilter,
      )
      .map(requirementCard);
    if (state.requirementFilter === "all") {
      cards.push(
        ...state.sop.poems
          .filter((poem) => !poem.requirement)
          .slice(0, 12)
          .map(missingRequirementCard),
      );
    }
  }
  document.querySelector("#requirement-grid").innerHTML = cards.join("");
  document.querySelector("#requirement-selection-count").textContent =
    state.selectedRequirements.size
      ? `已选择 ${state.selectedRequirements.size} 条待审核需求`
      : "未选择待审核项";
  document.querySelector("#bulk-approve-requirements").disabled =
    state.selectedRequirements.size === 0;
  document.querySelector("#bulk-reject-requirements").disabled =
    state.selectedRequirements.size === 0;
  const empty = document.querySelector("#requirement-empty");
  empty.hidden = cards.length > 0;
  empty.innerHTML = "<strong>当前分组没有需求卡</strong><p>调整筛选，或选择诗词生成新需求。</p>";
  applyRoleVisibility();
}

function directionCard(direction) {
  const content = direction.content || {};
  const layers = content.interpretation_layers || {};
  const diversity = direction.validation?.diversity || {};
  const [typeName, typeMark] = directionTypeMeta[direction.type] || [
    direction.type,
    "策",
  ];
  return `
    <article class="direction-card status-${direction.status}">
      <header>
        <span class="direction-mark">${typeMark}</span>
        <div><small>${escapeHtml(typeName)} · v${direction.version}</small><h4>${escapeHtml(
          content.title || typeName,
        )}</h4></div>
        ${statusBadge(direction.status, requirementStatusMeta)}
      </header>
      <p class="direction-thesis">${escapeHtml(content.visual_thesis || content.subject || "")}</p>
      <p class="direction-subject">${escapeHtml(content.subject || "")}</p>
      <div class="direction-signature"><span>${escapeHtml(content.subject_mode || "—")}</span><span>${escapeHtml(content.shot_scale || "—")}</span><span>${escapeHtml(content.narrative_mode || "—")}</span></div>
      <dl>
        <div><dt>景别</dt><dd>${escapeHtml(content.shot || "—")}</dd></div>
        <div><dt>主体层</dt><dd>${escapeHtml(content.midground || "—")}</dd></div>
        <div><dt>光线</dt><dd>${escapeHtml(content.lighting || "—")}</dd></div>
        <div><dt>留白</dt><dd>${escapeHtml(content.whitespace || "—")}</dd></div>
        <div><dt>安全区</dt><dd>${escapeHtml(content.text_safe_area || "—")}</dd></div>
      </dl>
      <div class="interpretation-summary"><span>事实 <strong>${(layers.poem_facts || []).length}</strong></span><span>演绎 <strong>${(layers.reasonable_inferences || []).length}</strong></span><span>创意 <strong>${(layers.creative_choices || []).length}</strong></span></div>
      <p class="direction-contract-line">${escapeHtml(direction.schema_version || state.sop.direction_schema?.schema_version || "legacy")} · 差异轴下限 ${diversity.minimum_axis_differences ?? "—"}/3 · ${direction.cache_hit ? "缓存复验" : "本次策划"}</p>
      <div class="direction-preserve">
        <span>保持项</span>
        ${(content.preserve || [])
          .slice(0, 3)
          .map((item) => `<i>${escapeHtml(item)}</i>`)
          .join("")}
      </div>
      ${
        direction.rejection_reason
          ? `<p class="rejection-note">退回原因：${escapeHtml(
              direction.rejection_reason,
            )}</p>`
          : ""
      }
      <footer>
        <div data-role-allow="art_director,producer,system_admin">
          ${
            direction.status === "in_review"
              ? `<label class="select-card"><input type="checkbox" data-select-direction="${direction.id}" ${state.selectedDirections.has(direction.id) ? "checked" : ""} /><span>批量</span></label>`
              : ""
          }
          ${direction.status !== "disabled" ? `<button class="card-link" type="button" data-edit-direction="${direction.id}">编辑</button>` : ""}
          <button class="card-link" type="button" data-copy-direction="${direction.id}">复制新版本</button>
          ${direction.status !== "disabled" ? `<button class="card-link danger-link" type="button" data-disable-direction="${direction.id}">停用</button>` : ""}
        </div>
        <div data-role-allow="art_director,producer,system_admin">${
          direction.status === "in_review"
            ? `<button class="danger-button" type="button" data-direction-action="reject" data-direction-id="${direction.id}">退回</button>
               <button class="approve-button" type="button" data-direction-action="approve" data-direction-id="${direction.id}">批准方向</button>`
            : direction.status === "approved"
              ? `<span class="approved-copy">✓ 已进入排产门禁</span>`
              : `<span class="muted-value">${direction.status === "disabled" ? "已从新排产中移除" : "等待重新策划"}</span>`
        }</div>
      </footer>
    </article>`;
}

function directionFailureForPoem(poemId) {
  return (state.sop.direction_generation_failures || []).find(
    (item) => item.poem_id === poemId,
  );
}

function renderDirections() {
  const failedPoemIds = new Set(
    (state.sop.direction_generation_failures || []).map((item) => item.poem_id),
  );
  const poems = state.sop.poems.filter(
    (poem) =>
      failedPoemIds.has(poem.id) ||
      poem.direction_count > 0 ||
      ["direction_draft", "direction_review", "ready_for_production"].includes(
        poem.status,
      ),
  );
  const groups = poems.map((poem) => {
    const directions = directionsForPoem(poem.id);
    const failure = directionFailureForPoem(poem.id);
    const firstIssue = failure?.validation?.final_issues?.[0];
    return `
      <section class="direction-group">
        <header class="direction-group-head">
          <div>
            <span>${escapeHtml(poem.theme || "未分类")} · ${escapeHtml(poem.author)}</span>
            <h3>${escapeHtml(poem.title)}</h3>
          </div>
          <div>
            ${statusBadge(poem.status)}
            ${
              directions.length
                ? `<button class="card-link" type="button" data-role-allow="art_director,producer,system_admin" data-poem-action="generate-direction" data-poem-id="${poem.id}">重新生成三方向</button>`
                : `<button class="approve-button" type="button" data-role-allow="art_director,producer,system_admin" data-poem-action="generate-direction" data-poem-id="${poem.id}">✦ 生成三方向</button>`
            }
          </div>
        </header>
        ${
          failure
            ? `<div class="direction-generation-error"><div><strong>${escapeHtml(failure.error_code || "DIRECTION_SET_INVALID")}</strong><p>${escapeHtml(firstIssue?.message || failure.error_message || "三方向未通过生产门禁。")}</p><small>${escapeHtml(failure.schema_version)} · 自动修复 ${failure.repair_attempts || 0}/1 次 · ${formatDate(failure.completed_at)}</small></div><button class="danger-button" type="button" data-role-allow="art_director,producer,system_admin" data-poem-action="generate-direction" data-poem-id="${poem.id}">修正后重试</button></div>`
            : ""
        }
        ${
          directions.length
            ? `<div class="direction-row">${directions
                .map(directionCard)
                .join("")}</div>`
            : `<div class="direction-missing">${failure ? "当前没有写入半套方向；修正异常后重新原子生成三方向。" : "需求已通过，等待生成叙事、意境、象征三个画面方向。"}</div>`
        }
      </section>`;
  });
  document.querySelector("#direction-board").innerHTML = groups.join("");
  const empty = document.querySelector("#direction-empty");
  empty.hidden = groups.length > 0;
  empty.innerHTML =
    "<strong>还没有可策划的诗词</strong><p>先在需求板完成需求卡审批。</p>";
  document.querySelector("#direction-selection-count").textContent =
    state.selectedDirections.size
      ? `已选择 ${state.selectedDirections.size} 个待审核方向`
      : "未选择待审核方向";
  document.querySelector("#bulk-approve-directions").disabled =
    state.selectedDirections.size === 0;
  document.querySelector("#bulk-reject-directions").disabled =
    state.selectedDirections.size === 0;
  applyRoleVisibility();
}

function renderInstruction() {
  const instruction = state.sop.instruction;
  const versions = state.sop.instruction_versions || [];
  const container = document.querySelector("#instruction-content");
  if (!instruction) {
    container.innerHTML =
      '<div class="empty-state"><strong>尚未发布全局指令</strong><p>没有指令版本时，需求生成会被门禁阻止。</p></div>';
    applyRoleVisibility();
    return;
  }
  document.querySelector("#instruction-status").textContent =
    `已发布 · v${instruction.version}`;
  document.querySelector("#instruction-status").className =
    "status-pill status-running";
  const content = instruction.content || {};
  const sections = [
    ["目标受众", [content.audience]],
    ["视觉目标", [content.visual_goal]],
    ["构图原则", content.composition_rules || []],
    ["历史原则", content.historical_rules || []],
    ["全局禁用", content.global_avoid || []],
  ];
  container.innerHTML = `
    <section class="instruction-hero">
      <div>
        <span>当前生产版本</span>
        <h3>${escapeHtml(instruction.name)}</h3>
        <p>发布于 ${formatDate(instruction.published_at)} · 创建者 ${escapeHtml(
          instruction.created_by,
        )}</p>
      </div>
      <div class="instruction-version">v${instruction.version}</div>
    </section>
    <section class="instruction-grid">
      ${sections
        .map(
          ([title, items], index) => `
            <article>
              <span>${String(index + 1).padStart(2, "0")}</span>
              <h3>${escapeHtml(title)}</h3>
              <ul>${(items || [])
                .filter(Boolean)
                .map((item) => `<li>${escapeHtml(item)}</li>`)
                .join("")}</ul>
            </article>`,
        )
        .join("")}
    </section>
    <section class="version-note">
      <strong>版本规则</strong>
      <p>需求卡与生产批次记录指令版本。未来发布 v${
        instruction.version + 1
      } 后，已启动任务仍使用当前版本，避免结果静默漂移。</p>
    </section>
    <section class="version-history-panel">
      <div class="resource-section-head"><div><span class="resource-label">VERSION HISTORY</span><h3>指令版本记录</h3></div><strong>${versions.length} 个版本</strong></div>
      <div class="version-history-list">
        ${versions
          .map(
            (version) => `<article>
              <div><strong>v${version.version} · ${escapeHtml(version.name)}</strong><span>${formatDate(
                version.published_at || version.created_at,
              )} · ${escapeHtml(version.created_by)}</span></div>
              <div>${statusBadge(version.status, {
                draft: ["草稿", "warning"],
                published: ["已发布", "success"],
                retired: ["已退役", "neutral"],
              })}<span data-role-allow="content_editor,producer,system_admin"><button class="card-link" type="button" data-clone-instruction="${version.id}">克隆</button>${
                version.id !== instruction.id
                  ? `<button class="card-link" type="button" data-diff-instruction="${version.id}">与当前比较</button>`
                  : ""
              }${
                version.status === "draft"
                  ? `<button class="danger-button" type="button" data-retire-instruction="${version.id}">作废</button><button class="secondary-button" type="button" data-publish-instruction="${version.id}">发布</button>`
                  : ""
              }</span></div>
            </article>`,
          )
          .join("")}
      </div>
    </section>`;
  applyRoleVisibility();
}

function renderQueue() {
  const batches = state.sop.batches || [];
  const taskPage = state.sop.task_page || {
    items: state.sop.tasks || [],
    total: (state.sop.tasks || []).length,
    limit: 50,
    offset: 0,
    has_previous: false,
    has_next: false,
  };
  const tasks = taskPage.items || [];
  const visibleBatches = state.queueBatchFilter
    ? batches.filter((batch) => batch.id === state.queueBatchFilter)
    : batches;
  document.querySelector("#queue-task-status").value = state.queueTaskFilter;
  document.querySelector("#queue-task-search").value = state.queueTaskQuery;
  document.querySelector("#queue-error-filter").value = state.queueErrorFilter;
  document.querySelector("#queue-batch-filter").innerHTML = [
    '<option value="">全部批次</option>',
    ...batches.map(
      (batch) => `<option value="${batch.id}">${escapeHtml(batch.name)}</option>`,
    ),
  ].join("");
  document.querySelector("#queue-batch-filter").value = state.queueBatchFilter;
  const filterBanner = document.querySelector("#queue-filter-banner");
  const filterLabels = [
    state.queueTaskFilter
      ? `状态：${taskStatusMeta[state.queueTaskFilter]?.[0] || state.queueTaskFilter}`
      : "",
    state.queueBatchFilter
      ? `批次：${batches.find((batch) => batch.id === state.queueBatchFilter)?.name || state.queueBatchFilter}`
      : "",
    state.queueErrorFilter ? `错误：${state.queueErrorFilter}` : "",
    state.queueTaskQuery ? `搜索：${state.queueTaskQuery}` : "",
  ].filter(Boolean);
  filterBanner.hidden = filterLabels.length === 0;
  filterBanner.innerHTML = filterLabels.length
    ? `<span>当前筛选：${escapeHtml(filterLabels.join(" · "))}</span><button class="secondary-button" type="button" data-clear-queue-filter>清除筛选</button>`
    : "";
  const summary = {
    active: batches.filter((batch) => ["queued", "running"].includes(batch.status)).length,
    completed: batches.filter((batch) => batch.status === "completed").length,
    failed: batches.filter((batch) =>
      ["partially_failed", "budget_blocked"].includes(batch.status),
    ).length,
    ready: state.sop.summary?.todos.ready_for_production || 0,
  };
  document.querySelector("#queue-summary").innerHTML = [
    ["待排产诗词", summary.ready],
    ["执行中批次", summary.active],
    ["已完成批次", summary.completed],
    ["异常批次", summary.failed],
  ]
    .map(
      ([label, value]) =>
        `<article><span>${label}</span><strong>${value}</strong></article>`,
    )
    .join("");
  document.querySelector("#queue-list").innerHTML = visibleBatches.length
    ? visibleBatches
        .map(
          (batch) => {
            const batchTasks = tasks.filter((task) => task.batch_id === batch.id);
            const failed = batchTasks.filter((task) =>
              ["failed", "blocked"].includes(task.status),
            ).length;
            const actions = [];
            if (batch.status === "draft") {
              actions.push(["start", "启动批次", "approve-button"]);
              actions.push(["cancel", "取消", "danger-button"]);
            } else if (["queued", "running"].includes(batch.status)) {
              actions.push(["pause", "暂停", "secondary-button"]);
              actions.push(["cancel", "取消未开始", "danger-button"]);
            } else if (batch.status === "paused") {
              actions.push(["resume", "继续运行", "approve-button"]);
              actions.push(["cancel", "取消", "danger-button"]);
            } else if (batch.status === "partially_failed") {
              actions.push(["retry-failed", `重试失败 ${failed}`, "approve-button"]);
            } else if (batch.status === "budget_blocked") {
              actions.push(["start", "预算调整后重试", "secondary-button"]);
              actions.push(["cancel", "取消", "danger-button"]);
            }
            return `
              <article class="batch-card">
                <header>
                  <div class="batch-title">
                    <span class="job-indicator status-${batch.status}"></span>
                    <div><strong>${escapeHtml(batch.name)}</strong><p>${escapeHtml(
                      batch.provider,
                    )} · ${escapeHtml(batch.model)} · ${escapeHtml(
                      batch.style_id,
                    )} · ${batch.task_count} 个任务</p></div>
                  </div>
                  <div class="batch-head-side">
                    ${statusBadge(batch.status, batchStatusMeta)}
                    <time>${formatDate(batch.created_at)}</time>
                  </div>
                </header>
                <div class="batch-metrics">
                  <span><small>成功</small><strong>${batch.succeeded_count || 0}</strong></span>
                  <span><small>失败</small><strong>${batch.failed_count || 0}</strong></span>
                  <span><small>需核对</small><strong>${batch.blocked_count || 0}</strong></span>
                  <span><small>预计成本</small><strong>${Number(
                    batch.estimated_cost || 0,
                  ).toFixed(2)} ${escapeHtml(batch.currency)}</strong></span>
                  <span><small>实际成本</small><strong>${Number(
                    batch.actual_cost || 0,
                  ).toFixed(2)} ${escapeHtml(batch.currency)}</strong></span>
                </div>
                <div class="batch-progress-row">
                  <div><span style="width:${batch.progress || 0}%"></span></div>
                  <strong>${batch.progress || 0}%</strong>
                </div>
                ${
                  batchTasks.length
                    ? `<details class="task-details"><summary>查看 ${batchTasks.length} 个任务</summary><div>
                        ${batchTasks
                          .map(
                            (task) => `<article>
                              <span>${escapeHtml(task.poem_title)}</span>
                              <small>${escapeHtml(
                                directionTypeMeta[task.direction_type]?.[0] ||
                                  task.direction_type,
                              )} · 第 ${task.sample_index} 张</small>
                              ${statusBadge(task.status, taskStatusMeta)}
                              <i>${task.attempt_count} / ${task.max_attempts} 次</i>
                              ${
                                task.last_error_message
                                  ? `<p>${escapeHtml(task.last_error_message)}</p>`
                                  : ""
                              }
                            </article>`,
                          )
                          .join("")}
                      </div></details>`
                    : ""
                }
                <footer>
                  <span>批次 ID · ${escapeHtml(batch.id.slice(-8))}</span>
                  <div>${actions
                    .map(
                      ([action, label, className]) =>
                        `<button class="${className}" type="button" data-role-allow="ai_operator,producer,system_admin" data-batch-action="${action}" data-batch-id="${batch.id}">${label}</button>`,
                    )
                    .join("")}</div>
                </footer>
              </article>`;
          },
        )
        .join("")
    : `<div class="empty-state"><strong>${
        state.queueBatchFilter ? "没有符合筛选条件的批次" : "还没有生产批次"
      }</strong><p>${
        state.queueBatchFilter
          ? "清除筛选可查看全部批次。"
          : "批准画面方向后，点击“创建生产批次”统一排产。"
      }</p></div>`;

  const total = Number(taskPage.total || 0);
  const offset = Number(taskPage.offset || 0);
  const pageEnd = Math.min(total, offset + tasks.length);
  document.querySelector("#task-page-summary").textContent = `${total} 条匹配任务`;
  document.querySelector("#task-page-range").textContent = total
    ? `${offset + 1}–${pageEnd} / ${total}`
    : "0–0 / 0";
  document.querySelector("#task-page-previous").disabled = !taskPage.has_previous;
  document.querySelector("#task-page-previous").dataset.taskPageOffset = String(
    Math.max(0, offset - Number(taskPage.limit || 50)),
  );
  document.querySelector("#task-page-next").disabled = !taskPage.has_next;
  document.querySelector("#task-page-next").dataset.taskPageOffset = String(
    offset + Number(taskPage.limit || 50),
  );
  document.querySelector("#task-table-body").innerHTML = tasks.length
    ? tasks
        .map(
          (task) => `<tr>
            <td><strong>${escapeHtml(task.poem_title)}</strong><small class="table-subline">第 ${task.sample_index} 张 · ${escapeHtml(task.poem_id)}</small></td>
            <td><span>${escapeHtml(task.batch_name)}</span><small class="table-subline">${escapeHtml(task.provider)} · ${escapeHtml(task.model)}</small></td>
            <td>${escapeHtml(directionTypeMeta[task.direction_type]?.[0] || task.direction_type)}</td>
            <td>${statusBadge(task.status, taskStatusMeta)}</td>
            <td>${task.attempt_count} / ${task.max_attempts}</td>
            <td>${task.last_error_code ? `<strong class="error-code">${escapeHtml(task.last_error_code)}</strong><small class="table-subline">${escapeHtml(task.last_error_message)}</small>` : '<span class="muted-value">—</span>'}</td>
            <td>${formatDate(task.updated_at)}</td>
          </tr>`,
        )
        .join("")
    : '<tr><td colspan="7"><div class="empty-state compact"><strong>没有符合条件的任务</strong><p>调整状态、批次、错误码或搜索条件。</p></div></td></tr>';
  applyRoleVisibility();
}

function imageCard(image, final = false) {
  return `
    <article class="image-card">
      <button class="image-preview" type="button" data-open-image="${image.id}">
        <img src="${escapeHtml(image.url)}" alt="${escapeHtml(image.poem_title)}插图" loading="lazy" />
        <span>${image.generation_mode === "converge" ? "返工衍生" : "初始探索"}</span>
      </button>
      <div class="image-card-copy">
        <div><h3>${escapeHtml(image.poem_title)}</h3><p>${escapeHtml(
          image.author,
        )} · ${escapeHtml(image.style_name)}</p></div>
        ${statusBadge(image.decision === "final" ? "approved" : "candidate_review")}
      </div>
      ${
        final
          ? `<footer><a class="approve-button link-button" href="${escapeHtml(
              image.url,
            )}" download>保存原图</a></footer>`
          : `<footer>
              <button class="danger-button" type="button" data-image-action="rejected" data-image-id="${image.id}">淘汰</button>
              <button class="approve-button" type="button" data-image-action="selected" data-image-id="${image.id}">入选方向</button>
            </footer>`
      }
    </article>`;
}

function productionImageCard(image) {
  const qc = image.qc || {};
  const blocked = ["qc_blocked", "needs_manual_qc"].includes(image.status);
  const checked = state.reviewSelection.has(image.id);
  const directionLabel =
    directionTypeMeta[image.direction_type]?.[0] || image.direction_type || "方向";
  return `
    <article class="review-candidate ${blocked ? "is-qc-blocked" : ""} ${
      checked ? "is-compare-selected" : ""
    }">
      <label class="compare-check" title="加入 A/B 对比">
        <input type="checkbox" data-compare-image="${image.id}" ${checked ? "checked" : ""} />
        <span>对比</span>
      </label>
      <button class="image-preview" type="button" data-open-image="${image.id}">
        <img src="${escapeHtml(image.url)}" alt="${escapeHtml(
          image.poem_title,
        )}插图" loading="lazy" />
        <span>第 ${image.generation} 代 · ${escapeHtml(directionLabel)}</span>
        <strong class="qc-score ${blocked ? "is-danger" : ""}">${Math.round(
          Number(qc.score || 0),
        )}</strong>
      </button>
      <div class="review-candidate-copy">
        <div><strong>${escapeHtml(image.batch_name)}</strong><small>${escapeHtml(
          image.style_id,
        )} · ${escapeHtml(image.provider)}</small></div>
        <div>${statusBadge(qc.decision || "manual_review", qcDecisionMeta)}${statusBadge(
          image.status,
          productionImageStatusMeta,
        )}</div>
      </div>
      <div class="review-risk-row">
        ${(qc.problems || [])
          .slice(0, 2)
          .map(
            (problem) =>
              `<span class="${problem.severity === "critical" ? "is-danger" : ""}" title="${escapeHtml(
                problem.evidence || "",
              )}">${escapeHtml(problem.code)} · ${escapeHtml(problem.note)}</span>`,
          )
          .join("")}
        ${(qc.hard_failures || [])
          .slice(0, 2)
          .map((risk) => `<span class="is-danger">${escapeHtml(risk)}</span>`)
          .join("")}
        ${(qc.warnings || [])
          .slice(0, blocked ? 1 : 2)
          .map((risk) => `<span>${escapeHtml(risk)}</span>`)
          .join("")}
      </div>
      <footer>
        <span>${escapeHtml(qc.reviewer_kind || "local")} · ${Math.round(
          Number(qc.confidence || 0) * 100,
        )}% 置信 · ${image.width}×${image.height}</span>
        <button class="secondary-button" type="button" data-open-image="${image.id}">进入审片</button>
      </footer>
    </article>`;
}

function renderReview() {
  const queue = state.sop.review_queue || { groups: [], summary: {} };
  const groups = queue.groups
    .map((group) => ({
      ...group,
      candidates: group.candidates.filter(
        (image) =>
          state.reviewShowBlocked ||
          !["qc_blocked", "needs_manual_qc"].includes(image.status),
      ),
    }))
    .filter((group) => group.candidates.length);
  const visibleCount = groups.reduce(
    (total, group) => total + group.candidates.length,
    0,
  );
  document.querySelector("#review-summary").innerHTML = [
    ["待审候选", queue.summary.review_ready || 0],
    ["优先推荐", queue.summary.recommended || 0],
    ["已入选", queue.summary.selected || 0],
    ["待人工 QC", queue.summary.needs_manual_qc || 0],
    ["硬失败", queue.summary.qc_hard_blocked || 0],
  ]
    .map(
      ([label, value]) =>
        `<article><span>${label}</span><strong>${value}</strong></article>`,
    )
    .join("");
  document.querySelector("#review-grid").innerHTML = groups
    .map(
      (group) => `
        <section class="review-poem-group">
          <header>
            <div><span>${escapeHtml(group.author)}</span><h3>${escapeHtml(
              group.poem_title,
            )}</h3></div>
            <strong>${group.candidates.length} 张候选</strong>
          </header>
          <div class="review-grid">${group.candidates
            .map((image) => productionImageCard(image))
            .join("")}</div>
        </section>`,
    )
    .join("");
  const empty = document.querySelector("#review-empty");
  empty.hidden = visibleCount > 0;
  empty.innerHTML =
    "<strong>当前没有待审候选</strong><p>完成方向审批并启动生产批次后，候选会按诗词进入这里。</p>";
  const compareButton = document.querySelector("#compare-selected-button");
  compareButton.disabled = state.reviewSelection.size < 2;
  compareButton.textContent = `对比所选 ${state.reviewSelection.size} / 4`;
}

function renderAssets() {
  const allAssets = state.sop.final_assets || [];
  const query = state.assetQuery.trim().toLowerCase();
  const assets = allAssets.filter((asset) =>
    !query
      ? true
      : [asset.poem_title, asset.author, asset.style_id]
          .join(" ")
          .toLowerCase()
          .includes(query),
  );
  const currentAssets = allAssets.filter((asset) => asset.is_current);
  document.querySelector("#asset-summary").innerHTML = [
    ["当前成品", currentAssets.length],
    ["历史版本", allAssets.filter((asset) => !asset.is_current).length],
    ["已完成导出", (state.sop.export_packages || []).filter((item) => item.status === "completed").length],
    ["交付诗词", state.sop.summary?.status_counts?.exported || 0],
  ]
    .map(
      ([label, value]) =>
        `<article><span>${label}</span><strong>${value}</strong></article>`,
    )
    .join("");
  document.querySelector("#asset-grid").innerHTML = assets
    .map(
      (asset) => `<article class="final-asset-card">
        <a class="image-preview" href="${escapeHtml(asset.url)}" target="_blank" rel="noreferrer">
          <img src="${escapeHtml(asset.url)}" alt="${escapeHtml(
            asset.poem_title,
          )}终审成品" loading="lazy" />
          <span>${asset.is_current ? "当前交付版" : "历史版本"} · v${asset.version}</span>
        </a>
        <div class="final-asset-copy">
          <div><small>${escapeHtml(asset.author)} · ${escapeHtml(
            asset.style_id,
          )}</small><h3>${escapeHtml(asset.poem_title)}</h3></div>
          ${statusBadge(asset.is_current ? "approved" : "archived")}
          <dl>
            <div><dt>规格</dt><dd>${asset.width}×${asset.height}</dd></div>
            <div><dt>格式</dt><dd>${escapeHtml(asset.mime_type)}</dd></div>
            <div><dt>校验和</dt><dd>${escapeHtml(asset.checksum.slice(0, 12))}…</dd></div>
            <div><dt>锁定时间</dt><dd>${formatDate(asset.created_at)}</dd></div>
          </dl>
        </div>
        <footer><a class="secondary-button link-button" href="${escapeHtml(
          asset.url,
        )}" download>保存原图</a></footer>
      </article>`,
    )
    .join("");
  const empty = document.querySelector("#asset-empty");
  empty.hidden = assets.length > 0;
  empty.innerHTML =
    "<strong>还没有完成双终审的资产</strong><p>候选需要内容终审和美术终审均通过，才会锁定为当前交付版本。</p>";
  const exportButton = document.querySelector("#export-assets-button");
  exportButton.disabled = currentAssets.length === 0;
  exportButton.textContent = currentAssets.length
    ? `导出 ${currentAssets.length} 个当前成品`
    : "暂无可导出成品";
  document.querySelector("#export-list").innerHTML = (state.sop.export_packages || []).length
    ? state.sop.export_packages
        .map(
          (item) => `<article>
            <div><strong>${escapeHtml(item.name)}</strong><span>${formatDate(
              item.completed_at || item.created_at,
            )} · ${item.asset_count} 个成品</span></div>
            ${statusBadge(
              item.status,
              {
                creating: ["生成中", "running"],
                completed: ["已完成", "success"],
                failed: ["失败", "danger"],
              },
            )}
            ${
              item.status === "completed"
                ? `<a class="secondary-button link-button" href="/exports/${escapeHtml(
                    item.name,
                  )}/manifest.json" target="_blank" rel="noreferrer">查看 Manifest</a>`
                : `<span class="export-error">${escapeHtml(item.error || "")}</span>`
            }
          </article>`,
        )
        .join("")
    : '<div class="empty-state compact"><strong>暂无导出记录</strong><p>导出后会保留包名、数量、校验和与 Manifest 地址。</p></div>';
}

function styleBenchmarkAction(style) {
  const run = style.latest_benchmark;
  const status = run?.effective_status || run?.status || "";
  if (style.status === "draft" || (style.status === "benchmarking" && status === "failed")) {
    return `<button class="secondary-button" type="button" data-role-allow="art_director,producer,system_admin" data-start-style-benchmark="${escapeHtml(
      style.id,
    )}">${status === "failed" ? "重新跑基准测试" : "创建 5 首基准测试"}</button>`;
  }
  if (style.status === "benchmarking" && status === "awaiting_evaluation") {
    return `<button class="secondary-button" type="button" data-role-allow="art_director,producer,system_admin" data-evaluate-style-benchmark="${escapeHtml(
      run.id,
    )}">录入基准评估</button>`;
  }
  if (style.status === "benchmarking" && status === "passed") {
    return `<button class="primary-button" type="button" data-role-allow="art_director,producer,system_admin" data-publish-style="${escapeHtml(
      style.id,
    )}">发布此版本</button>`;
  }
  if (style.status === "benchmarking") {
    return `<button class="secondary-button" type="button" disabled>基准生成中 · ${escapeHtml(
      run?.batch_status || "待启动",
    )}</button>`;
  }
  return "";
}

function renderResources() {
  const styles = state.sop.style_packs || [];
  const artBible = state.sop.art_bible || {};
  const artBibleVersions = state.sop.art_bible_versions || [];
  const benchmarkPoems = state.sop.style_benchmark_poems || [];
  const readyBenchmarkCount = benchmarkPoems.filter(
    (item) =>
      item.poem_status === "ready_for_production" &&
      directionsForPoem(item.poem_id).some(
        (direction) => direction.is_current && direction.status === "approved",
      ),
  ).length;
  const provider = state.sop.provider_status || state.legacy.config.provider_status || {};
  const visualQc = provider.visual_qc || {};
  const qcPolicy = state.sop.qc_policy || {};
  const qcPolicyContent = qcPolicy.content || {};
  const qcThresholds = qcPolicyContent.thresholds || {};
  const qcCalibration = state.sop.qc_calibration || {};
  const budget = state.sop.budget || {};
  const hardLimit = Number(budget.hard_limit || 0);
  const spent = Number(budget.spent || 0);
  const usedPercent = hardLimit ? Math.min(100, Math.round((spent / hardLimit) * 100)) : 0;
  document.querySelector("#resource-grid").innerHTML = `
    <article class="resource-panel engine-panel">
      <span class="resource-label">IMAGE PROVIDER</span>
      <div class="engine-state">
        <i class="${provider.status === "ready" ? "is-live" : ""}"></i>
        <div><strong>${
          provider.status === "circuit_open"
            ? "Provider 已熔断"
            : provider.provider === "openai"
            ? provider.configured
              ? "图像 Provider 已配置"
              : "图像 Provider 待配置"
            : "本地演示引擎"
        }</strong><small>${escapeHtml(provider.model || "demo-renderer")}</small></div>
      </div>
      <p>${
        provider.status === "circuit_open"
          ? `连续失败已暂停同 Provider 批次，约 ${Number(
              provider.circuit?.retry_after_seconds || 0,
            )} 秒后可人工恢复。`
          : provider.live_generation
          ? "真实生成会记录模型、参数和任务结果。"
          : "当前不会产生 API 费用，适合验证 SOP 与状态流。"
      }</p>
      <div class="provider-specs">
        <span><small>并发</small><strong>${Number(provider.concurrency || 1)}</strong></span>
        <span><small>生成超时</small><strong>${Number(
          provider.timeouts_seconds?.generation || 0,
        )}s</strong></span>
        <span><small>最大尝试</small><strong>${Number(provider.max_attempts || 0)}</strong></span>
        <span><small>熔断</small><strong>${provider.circuit?.state === "open" ? "OPEN" : "CLOSED"}</strong></span>
      </div>
    </article>
    <article class="resource-panel budget-panel">
      <span class="resource-label">BUDGET GATE</span>
      <div class="budget-title"><h3>预算闸门</h3>${statusBadge(
        budget.soft_warning ? "soft_warning" : "normal",
        {
          normal: ["预算正常", "success"],
          soft_warning: ["已到软提醒线", "warning"],
        },
      )}</div>
      <div class="budget-bar"><span style="width:${usedPercent}%"></span></div>
      <div class="budget-values">
        <span><small>已用</small><strong>${spent.toFixed(2)}</strong></span>
        <span><small>预留</small><strong>${Number(budget.reserved || 0).toFixed(2)}</strong></span>
        <span><small>余额</small><strong>${Number(budget.remaining || 0).toFixed(2)} ${escapeHtml(
          budget.currency || "USD",
        )}</strong></span>
      </div>
      <form class="budget-form" id="budget-form" data-role-allow="producer,system_admin">
        <label><span>硬停止上限</span><input class="dialog-input" name="hard_limit" type="number" min="0" max="1000000" step="0.01" value="${hardLimit}" required /></label>
        <label><span>软提醒比例</span><input class="dialog-input" name="soft_ratio" type="number" min="0.1" max="1" step="0.05" value="${Number(
          budget.soft_ratio || 0.7,
        )}" required /></label>
        <button class="secondary-button" type="submit">保存预算规则</button>
      </form>
      <p>批次启动与失败重试都会重新校验可用余额；超出硬上限时不会调用 Provider。</p>
    </article>
    <article class="resource-panel qc-policy-panel">
      <span class="resource-label">VISUAL QC POLICY</span>
      <div class="qc-policy-title"><div><h3>${escapeHtml(
        qcPolicyContent.name || "自动质检政策未发布",
      )}</h3><small>${escapeHtml(qcPolicyContent.semantic_version || "—")} · ${escapeHtml(
        qcPolicy.schema_version || "qc-policy/v1",
      )}</small></div>${statusBadge(
        visualQc.status === "ready" || visualQc.status === "synthetic_demo"
          ? "ready"
          : "manual_review",
        {
          ready: [visualQc.real_visual_review ? "视觉 QC 已连接" : "演示评分器", "success"],
          manual_review: ["降级为人工 QC", "warning"],
        },
      )}</div>
      <p>${
        visualQc.real_visual_review
          ? `使用 ${escapeHtml(visualQc.model || "视觉模型")} 进行结构化多模态审查；确定性政策负责最终分流。`
          : visualQc.status === "synthetic_demo"
          ? "当前分数仅用于演示生产状态流，不代表真实视觉判断。"
          : "视觉审查未配置或已关闭，技术检查通过的图片仍会进入人工 QC，不会自动成为候选。"
      }</p>
      <div class="qc-threshold-grid">
        <span><small>拒绝</small><strong>&lt; ${Number(qcThresholds.reject_below || 60)}</strong></span>
        <span><small>人工复核</small><strong>&lt; ${Number(qcThresholds.manual_review_below || 75)}</strong></span>
        <span><small>候选</small><strong>${Number(qcThresholds.manual_review_below || 75)}+</strong></span>
        <span><small>推荐</small><strong>${Number(qcThresholds.recommended_from || 85)}+</strong></span>
      </div>
      <div class="qc-calibration-progress">
        <div><span>人工校准样本</span><strong>${Number(qcCalibration.sample_count || 0)} / ${Number(
          qcCalibration.target_count || 100,
        )}</strong></div>
        <div class="budget-bar"><span style="width:${Math.min(
          100,
          Math.round(
            (Number(qcCalibration.sample_count || 0) /
              Math.max(1, Number(qcCalibration.target_count || 100))) *
              100,
          ),
        )}%"></span></div>
        <small>误放 ${
          qcCalibration.false_pass_rate == null
            ? "待采样"
            : `${(Number(qcCalibration.false_pass_rate) * 100).toFixed(1)}%`
        } · 误杀 ${
          qcCalibration.false_reject_rate == null
            ? "待采样"
            : `${(Number(qcCalibration.false_reject_rate) * 100).toFixed(1)}%`
        }</small>
      </div>
    </article>
    <article class="resource-panel backup-panel">
      <span class="resource-label">BACKUP & RECOVERY</span>
      <div class="backup-title"><h3>生产数据备份</h3><button class="secondary-button" type="button" data-role-allow="system_admin" data-create-backup>立即备份</button></div>
      <p>使用 SQLite 在线备份 API，并复制诗词种子、旧状态、生成图片与历史导出；恢复只允许写入空目录。</p>
      <div class="backup-list">
        ${(state.sop.backups || []).length
          ? state.sop.backups
              .slice(0, 4)
              .map(
                (backup) => `<article><div><strong>${escapeHtml(
                  backup.name,
                )}</strong><span>${formatDate(backup.created_at)} · ${
                  backup.file_count
                } 个文件</span></div><button class="secondary-button" type="button" data-role-allow="system_admin" data-verify-backup="${escapeHtml(
                  backup.name,
                )}">校验</button></article>`,
              )
              .join("")
          : "<span>尚无备份。完成首个生产批次后建议立即创建。</span>"}
      </div>
    </article>
    <article class="resource-panel art-bible-panel">
      <span class="resource-label">GLOBAL ART BIBLE</span>
      <div class="art-bible-title"><div><h3>${escapeHtml(
        artBible.name || "尚未发布 Art Bible",
      )}</h3><small>${escapeHtml(artBible.semantic_version || "—")} · ${escapeHtml(
        artBible.schema_version || "—",
      )}</small></div>${statusBadge(artBible.status || "missing", {
        published: ["已发布", "success"],
        missing: ["缺失", "danger"],
      })}</div>
      <p>${escapeHtml(artBible.release_notes || "风格版本必须绑定已发布的全局美术规范。")}</p>
      <div class="art-bible-rule-grid">
        <span><small>色彩</small><strong>${artBible.content?.palette_rules?.length || 0}</strong></span>
        <span><small>空间</small><strong>${artBible.content?.spatial_rules?.length || 0}</strong></span>
        <span><small>文字禁令</small><strong>${artBible.content?.text_prohibitions?.length || 0}</strong></span>
        <span><small>历史边界</small><strong>${artBible.content?.historical_boundaries?.length || 0}</strong></span>
      </div>
      <div class="art-bible-versions">
        ${artBibleVersions
          .map(
            (version) => `<div><span>${escapeHtml(version.semantic_version)} · ${escapeHtml(
              version.status,
            )}</span>${version.status === "draft"
                ? `<button class="secondary-button" type="button" data-role-allow="art_director,producer,system_admin" data-publish-art-bible="${escapeHtml(
                    version.id,
                  )}">发布</button>`
                : ""}</div>`,
          )
          .join("")}
      </div>
    </article>
    <article class="resource-panel benchmark-pool-panel">
      <span class="resource-label">BENCHMARK POEM SET</span>
      <div class="benchmark-pool-title"><h3>12 首美术验证集</h3><strong>${readyBenchmarkCount} / ${benchmarkPoems.length} 已准备</strong></div>
      <p>覆盖山水、思乡、送别、边塞、田园、宫廷、儿童熟知和人物叙事；每首记录误读点与历史风险。</p>
      <div class="benchmark-topic-cloud">${benchmarkPoems
        .flatMap((item) => item.categories || [])
        .filter((item, index, array) => array.indexOf(item) === index)
        .map((item) => `<span>${escapeHtml(item)}</span>`)
        .join("")}</div>
      <small>新风格发布门槛：至少 ${Number(
        artBible.content?.benchmark_policy?.min_poems_per_release || 5,
      )} 首，每首 ${Number(
        artBible.content?.benchmark_policy?.min_samples_per_poem || 4,
      )} 张，风格匹配 ≥ ${Number(
        artBible.content?.benchmark_policy?.min_style_match_score || 75,
      )}，偏题率 ≤ ${Math.round(
        Number(artBible.content?.benchmark_policy?.max_off_topic_rate || 0.2) * 100,
      )}%。</small>
    </article>
    <section class="style-resource-section">
      <div class="resource-section-head"><div><span class="resource-label">STYLE LAB</span><h3>风格版本与发布门禁</h3></div><strong>${styles.length} 个版本</strong></div>
      <div class="style-resource-grid">
        ${styles
          .map(
            (style) => `
              <article class="style-card">
                <div class="palette">
                  ${(style.palette || [])
                    .map((color) => `<span style="--color:${escapeHtml(color)}"></span>`)
                    .join("")}
                </div>
                <div class="style-card-status"><span>${escapeHtml(
                  style.status.toUpperCase(),
                )} · ${escapeHtml(style.semantic_version || `v${style.version}`)}</span>${statusBadge(style.status, {
                  draft: ["草稿", "warning"],
                  benchmarking: ["基准测试", "warning"],
                  active: ["生产中", "success"],
                  limited: ["限量", "warning"],
                  retired: ["已退役", "neutral"],
                })}</div>
                <h4>${escapeHtml(style.name)}</h4>
                <p>${escapeHtml(style.description)}</p>
                <small>${escapeHtml(style.short_name)} · ${escapeHtml(
                  style.settings?.paper || "unspecified",
                )} paper</small>
                <small>适用：${escapeHtml((style.applicable_topics || []).join("、") || "未设置")}</small>
                <small>Art Bible：${escapeHtml(style.art_bible_version_id || "未绑定")}</small>
                <div class="style-contract-summary"><span>正例 ${(style.positive_examples || []).length}</span><span>反例 ${(style.negative_examples || []).length}</span><span>风险 ${(style.risks || []).length}</span></div>
                ${
                  style.latest_benchmark?.metrics?.sample_count
                    ? `<div class="style-metrics"><span><small>匹配</small><strong>${Number(
                        style.latest_benchmark.metrics.style_match_score || 0,
                      )}</strong></span><span><small>偏题</small><strong>${Math.round(
                        Number(style.latest_benchmark.metrics.off_topic_rate || 0) * 100,
                      )}%</strong></span><span><small>成本/张</small><strong>${Number(
                        style.latest_benchmark.metrics.average_sample_cost || 0,
                      ).toFixed(3)}</strong></span></div>`
                    : `<div class="style-gate-note ${style.release_gate?.passed ? "is-pass" : ""}">${escapeHtml(
                        style.release_gate?.message || "尚无基准测试",
                      )}</div>`
                }
                ${styleBenchmarkAction(style)}
              </article>`,
          )
          .join("")}
      </div>
    </section>`;
  applyRoleVisibility();
}

function statusSummary(items) {
  const counts = {};
  (items || []).forEach((item) => {
    const key = item.status || "unknown";
    counts[key] = (counts[key] || 0) + 1;
  });
  return Object.entries(counts)
    .map(([status, count]) => `<span>${statusBadge(status, {
      ...requirementStatusMeta,
      ...taskStatusMeta,
      ...productionImageStatusMeta,
    })}<strong>${count}</strong></span>`)
    .join("");
}

async function openPoemDetail(poemId) {
  const dialog = document.querySelector("#poem-detail-dialog");
  const content = document.querySelector("#poem-detail-content");
  const localPoem = poemById(poemId);
  document.querySelector("#poem-detail-title").textContent = localPoem
    ? `${localPoem.title} · 全链路详情`
    : "诗词生产详情";
  content.innerHTML =
    '<div class="empty-state compact"><strong>正在加载生产链路</strong><p>读取内容、需求、方向、任务、候选、终审与导出记录。</p></div>';
  if (!dialog.open) dialog.showModal();
  try {
    const detail = await api(`/api/poems/${poemId}`);
    const poem = detail.poem;
    const counts = detail.counts || {};
    document.querySelector("#poem-detail-title").textContent = `${poem.title} · 全链路详情`;
    const latestRequirement = detail.requirements.find((item) => item.is_current);
    const requirementRuns = detail.requirement_generation_runs || [];
    const latestRequirementRun = requirementRuns[0];
    const directionRuns = detail.direction_generation_runs || [];
    const latestDirectionRun = directionRuns[0];
    const currentDirections = detail.directions.filter((item) => item.is_current);
    const currentAsset = detail.final_assets.find((item) => item.is_current);
    content.innerHTML = `
      <section class="poem-detail-hero">
        <div><span>${escapeHtml(poem.dynasty)} · ${escapeHtml(poem.author)} · ${escapeHtml(poem.theme || "未分类")}</span><h3>${escapeHtml(poem.title)}</h3><p>${(poem.lines || []).map(escapeHtml).join("<br>")}</p></div>
        <div>${statusBadge(poem.status)}<small>更新于 ${formatDate(poem.updated_at)}</small>${poem.blocked_reason ? `<strong class="error-code">${escapeHtml(poem.blocked_reason)}</strong>` : ""}</div>
      </section>
      <section class="poem-chain-metrics">
        ${[
          ["内容版本", counts.content_versions || 0],
          ["需求版本", counts.requirements || 0],
          ["需求运行", counts.requirement_generation_runs || 0],
          ["方向版本", counts.directions || 0],
          ["方向运行", counts.direction_generation_runs || 0],
          ["生成任务", counts.tasks || 0],
          ["候选图片", counts.images || 0],
          ["返工单", counts.reworks || 0],
          ["成品版本", counts.final_assets || 0],
          ["导出记录", counts.exports || 0],
        ]
          .map(([label, value]) => `<article><span>${escapeHtml(label)}</span><strong>${value}</strong></article>`)
          .join("")}
      </section>
      <section class="poem-chain-grid">
        <article>
          <header><span>S1</span><div><strong>内容与需求</strong><small>版本冻结与证据</small></div></header>
          <p>内容 v${detail.content_versions[0]?.version || "—"} · ${escapeHtml(detail.content_versions[0]?.status || "缺失")}</p>
          <p>需求 ${latestRequirement ? `v${latestRequirement.version} · ${escapeHtml(latestRequirement.status)}` : "尚未生成"}</p>
          <p>${latestRequirementRun ? `${escapeHtml(latestRequirementRun.schema_version)} · ${escapeHtml(latestRequirementRun.status)} · 修复 ${latestRequirementRun.repair_attempts || 0}/1` : "尚无需求生成运行记录"}</p>
          ${latestRequirementRun?.error_code ? `<small class="error-code">${escapeHtml(latestRequirementRun.error_code)} · ${escapeHtml(latestRequirementRun.error_message)}</small>` : ""}
          ${latestRequirement ? `<small>${escapeHtml(latestRequirement.content?.composition || "未填写构图命题")}</small>` : ""}
        </article>
        <article>
          <header><span>S2</span><div><strong>画面方向</strong><small>当前版本 ${currentDirections.length} 个</small></div></header>
          <div class="poem-status-summary">${statusSummary(currentDirections) || '<span class="muted-value">尚无方向</span>'}</div>
          <p>${currentDirections.map((item) => `${directionTypeMeta[item.type]?.[0] || item.type} v${item.version}`).join(" · ") || "等待需求批准"}</p>
          <p>${latestDirectionRun ? `${escapeHtml(latestDirectionRun.schema_version)} · ${escapeHtml(latestDirectionRun.status)} · 差异轴下限 ${latestDirectionRun.validation?.diversity?.minimum_axis_differences ?? "—"}/3` : "尚无方向生成运行记录"}</p>
          ${latestDirectionRun?.error_code ? `<small class="error-code">${escapeHtml(latestDirectionRun.error_code)} · ${escapeHtml(latestDirectionRun.error_message)}</small>` : ""}
        </article>
        <article>
          <header><span>S3</span><div><strong>任务与候选</strong><small>批次执行结果</small></div></header>
          <div class="poem-status-summary">${statusSummary(detail.tasks) || '<span class="muted-value">尚无任务</span>'}</div>
          <p>${detail.images.length} 张候选 · ${detail.rework_orders.length} 张返工单</p>
        </article>
        <article>
          <header><span>S4</span><div><strong>终审与交付</strong><small>当前成品和历史包</small></div></header>
          <p>${currentAsset ? `成品 v${currentAsset.version} · ${currentAsset.width}×${currentAsset.height}` : "尚未锁定成品"}</p>
          <p>${detail.exports.length ? `已进入 ${detail.exports.length} 个导出包` : "尚未导出"}</p>
        </article>
      </section>
      ${
        detail.images.length
          ? `<section class="poem-detail-section"><header><div><span>CANDIDATES</span><h3>候选与衍生谱系</h3></div><strong>${detail.images.length} 张</strong></header><div class="poem-detail-images">${detail.images
              .slice(0, 12)
              .map(
                (image) => `<article><img src="${escapeHtml(image.url)}" alt="${escapeHtml(poem.title)}候选图" loading="lazy"><div><strong>第 ${image.generation} 代 · ${escapeHtml(directionTypeMeta[image.direction_type]?.[0] || image.direction_type)}</strong>${statusBadge(image.status, productionImageStatusMeta)}<small>QC ${Math.round(Number(image.qc?.score || 0))} · ${escapeHtml(image.model)}</small></div></article>`,
              )
              .join("")}</div></section>`
          : ""
      }
      <section class="poem-detail-section">
        <header><div><span>AUDIT TRAIL</span><h3>最近生产记录</h3></div><strong>${detail.audit_events.length} 条</strong></header>
        <div class="poem-audit-list">${detail.audit_events.length
          ? detail.audit_events
              .slice(0, 20)
              .map(
                (item) => `<article><i></i><div><strong>${escapeHtml(item.action)}</strong><span>${escapeHtml(item.actor_role)} · ${escapeHtml(item.actor_id)}</span></div><time>${formatDate(item.created_at)}</time></article>`,
              )
              .join("")
          : '<div class="empty-state compact"><strong>暂无审计记录</strong><p>阶段推进后会显示操作者与时间。</p></div>'}</div>
      </section>`;
  } catch (error) {
    content.innerHTML = `<div class="empty-state compact"><strong>详情加载失败</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function openInstructionDialog(sourceId = "") {
  const form = document.querySelector("#instruction-form");
  const current =
    state.sop.instruction_versions.find((item) => item.id === sourceId) ||
    state.sop.instruction ||
    {};
  const content = current.content || {};
  form.reset();
  form.elements.name.value = current.name
    ? `${current.name.replace(/\s+v\d+$/i, "")} v${Number(current.version || 0) + 1}`
    : "唐诗三百首全局创作规范";
  form.elements.audience.value = content.audience || "";
  form.elements.visual_goal.value = content.visual_goal || "";
  form.elements.composition_rules.value = (content.composition_rules || []).join("\n");
  form.elements.historical_rules.value = (content.historical_rules || []).join("\n");
  form.elements.global_avoid.value = (content.global_avoid || []).join("\n");
  document.querySelector("#instruction-dialog").showModal();
}

function openInstructionDiff(versionId) {
  const published = state.sop.instruction;
  const compared = state.sop.instruction_versions.find((item) => item.id === versionId);
  if (!published || !compared) return;
  const fields = [
    ["目标受众", "audience", false],
    ["视觉目标", "visual_goal", false],
    ["构图原则", "composition_rules", true],
    ["历史原则", "historical_rules", true],
    ["全局禁用", "global_avoid", true],
  ];
  const rows = fields
    .map(([label, key, isList]) => {
      const left = isList
        ? (published.content?.[key] || []).map(String)
        : [String(published.content?.[key] || "")];
      const right = isList
        ? (compared.content?.[key] || []).map(String)
        : [String(compared.content?.[key] || "")];
      const added = right.filter((item) => !left.includes(item));
      const removed = left.filter((item) => !right.includes(item));
      if (!added.length && !removed.length) return "";
      return `<article class="instruction-diff-row"><h3>${escapeHtml(label)}</h3>
        ${removed.map((item) => `<p class="is-removed">− ${escapeHtml(item)}</p>`).join("")}
        ${added.map((item) => `<p class="is-added">＋ ${escapeHtml(item)}</p>`).join("")}
      </article>`;
    })
    .filter(Boolean);
  document.querySelector("#instruction-diff-title").textContent = `当前 v${published.version} ↔ v${compared.version}`;
  document.querySelector("#instruction-diff-content").innerHTML = rows.length
    ? `<div class="instruction-diff-list">${rows.join("")}</div>`
    : '<div class="empty-state compact"><strong>内容没有差异</strong><p>名称或状态可能不同，但创作规则一致。</p></div>';
  document.querySelector("#instruction-diff-dialog").showModal();
}

async function retireInstructionVersion(versionId) {
  const reason = window.prompt("请输入作废草稿的原因：", "")?.trim() || "";
  if (!reason) return;
  try {
    await api(`/api/instructions/${versionId}/retire`, {
      method: "POST",
      body: JSON.stringify({
        reason,
        actor: ACTOR,
      }),
    });
    await refreshSop("指令草稿已作废，原因已写入审计记录。");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function saveInstructionVersion(event) {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector('button[type="submit"]');
  const data = new FormData(form);
  submit.disabled = true;
  try {
    const result = await api("/api/instructions", {
      method: "POST",
      body: JSON.stringify({
        project_id: project().id,
        name: data.get("name"),
        content: {
          audience: data.get("audience"),
          visual_goal: data.get("visual_goal"),
          composition_rules: asLines(data.get("composition_rules")),
          historical_rules: asLines(data.get("historical_rules")),
          global_avoid: asLines(data.get("global_avoid")),
        },
        actor: ACTOR,
      }),
    });
    document.querySelector("#instruction-dialog").close();
    await refreshSop(`指令 v${result.instruction.version} 已保存为草稿，发布前不会影响生产。`);
    switchView("instructions");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

async function publishInstructionVersion(instructionId) {
  if (!window.confirm("发布后当前线上指令会退役，新需求将绑定此版本。确认发布？")) return;
  try {
    const result = await api(`/api/instructions/${instructionId}/publish`, {
      method: "POST",
      body: JSON.stringify({ actor: ACTOR }),
    });
    await refreshSop(`指令 v${result.instruction.version} 已发布，历史任务仍保持原版本。`);
    switchView("instructions");
  } catch (error) {
    showToast(error.message, "error");
  }
}

function openStyleDialog() {
  const form = document.querySelector("#style-form");
  form.reset();
  form.elements.art_bible_version_id.innerHTML = (state.sop.art_bible_versions || [])
    .filter((item) => item.status === "published")
    .map(
      (item) =>
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)} · ${escapeHtml(
          item.semantic_version,
        )}</option>`,
    )
    .join("");
  document.querySelector("#style-dialog").showModal();
}

function commaList(value) {
  return String(value || "")
    .split(/[，,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function saveStyleVersion(event) {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector('button[type="submit"]');
  const data = new FormData(form);
  submit.disabled = true;
  try {
    const result = await api("/api/style-packs", {
      method: "POST",
      body: JSON.stringify({
        project_id: project().id,
        style_id: data.get("style_id"),
        name: data.get("name"),
        short_name: data.get("short_name"),
        semantic_version: data.get("semantic_version"),
        release_notes: data.get("release_notes"),
        art_bible_version_id: data.get("art_bible_version_id"),
        description: data.get("description"),
        prompt_fragment: data.get("prompt_fragment"),
        applicable_topics: commaList(data.get("applicable_topics")),
        palette: commaList(data.get("palette")),
        settings: {
          background: data.get("background"),
          foreground: data.get("foreground"),
          accent: data.get("accent"),
          paper: data.get("paper"),
        },
        visual_traits: {
          line: data.get("trait_line"),
          texture: data.get("trait_texture"),
          lighting: data.get("trait_lighting"),
          contrast: data.get("trait_contrast"),
          saturation: data.get("trait_saturation"),
          whitespace: data.get("trait_whitespace"),
        },
        character_design: {
          proportion: data.get("character_proportion"),
          expression: data.get("character_expression"),
          costume: data.get("character_costume"),
        },
        avoid: asLines(data.get("avoid")),
        risks: asLines(data.get("risks")),
        positive_examples: asLines(data.get("positive_examples")),
        negative_examples: asLines(data.get("negative_examples")),
        actor: ACTOR,
      }),
    });
    document.querySelector("#style-dialog").close();
    await refreshSop(`风格 ${result.style.style_id} v${result.style.version} 已保存为草稿。`);
    switchView("resources");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

function openArtBibleDialog() {
  const form = document.querySelector("#art-bible-form");
  form.reset();
  const current = state.sop.art_bible;
  if (current?.content) {
    [
      "palette_rules",
      "line_rules",
      "character_proportion_rules",
      "spatial_rules",
      "material_rules",
      "text_prohibitions",
      "historical_boundaries",
    ].forEach((field) => {
      form.elements[field].value = (current.content[field] || []).join("\n");
    });
    const policy = current.content.benchmark_policy || {};
    Object.entries(policy).forEach(([field, value]) => {
      if (form.elements[field]) form.elements[field].value = value;
    });
  }
  document.querySelector("#art-bible-dialog").showModal();
}

async function saveArtBibleVersion(event) {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector('button[type="submit"]');
  const data = new FormData(form);
  submit.disabled = true;
  try {
    const result = await api("/api/art-bibles", {
      method: "POST",
      body: JSON.stringify({
        project_id: project().id,
        name: data.get("name"),
        semantic_version: data.get("semantic_version"),
        release_notes: data.get("release_notes"),
        content: {
          palette_rules: asLines(data.get("palette_rules")),
          line_rules: asLines(data.get("line_rules")),
          character_proportion_rules: asLines(data.get("character_proportion_rules")),
          spatial_rules: asLines(data.get("spatial_rules")),
          material_rules: asLines(data.get("material_rules")),
          text_prohibitions: asLines(data.get("text_prohibitions")),
          historical_boundaries: asLines(data.get("historical_boundaries")),
          benchmark_policy: {
            benchmark_poem_count: Number(data.get("benchmark_poem_count")),
            min_poems_per_release: Number(data.get("min_poems_per_release")),
            min_samples_per_poem: Number(data.get("min_samples_per_poem")),
            min_style_match_score: Number(data.get("min_style_match_score")),
            max_off_topic_rate: Number(data.get("max_off_topic_rate")),
          },
        },
        actor: ACTOR,
      }),
    });
    document.querySelector("#art-bible-dialog").close();
    await refreshSop(`Art Bible ${result.art_bible.semantic_version} 已保存为草稿。`);
    switchView("resources");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

async function publishArtBibleVersion(versionId) {
  if (!window.confirm("发布后仅影响后续新建风格版本，已有版本保持原绑定。确认发布？")) return;
  try {
    const result = await api(`/api/art-bibles/${versionId}/publish`, {
      method: "POST",
      body: JSON.stringify({ actor: ACTOR }),
    });
    await refreshSop(`Art Bible ${result.art_bible.semantic_version} 已发布。`);
    switchView("resources");
  } catch (error) {
    showToast(error.message, "error");
  }
}

function openStyleBenchmarkDialog(versionId) {
  const style = (state.sop.style_packs || []).find((item) => item.id === versionId);
  if (!style) return;
  const form = document.querySelector("#style-benchmark-form");
  form.reset();
  form.elements.style_version_id.value = versionId;
  form.elements.provider.value = state.sop.provider_status?.provider || "demo";
  form.elements.model.value = state.sop.provider_status?.model || "demo-renderer";
  document.querySelector("#style-benchmark-title").textContent = `${style.name} ${
    style.semantic_version
  } · 基准测试`;
  let checked = 0;
  document.querySelector("#benchmark-poem-picker").innerHTML = (
    state.sop.style_benchmark_poems || []
  )
    .map((item) => {
      const eligible =
        item.poem_status === "ready_for_production" &&
        directionsForPoem(item.poem_id).some(
          (direction) => direction.is_current && direction.status === "approved",
        );
      const selected = eligible && checked < 5;
      if (selected) checked += 1;
      return `<label class="benchmark-poem-option ${eligible ? "" : "is-disabled"}"><input type="checkbox" name="poem_ids" value="${escapeHtml(
        item.poem_id,
      )}" ${selected ? "checked" : ""} ${eligible ? "" : "disabled"} /><span><strong>${escapeHtml(
        item.title,
      )}</strong><small>${escapeHtml((item.categories || []).join(" · "))}</small><em>${
        eligible ? "方向已批准" : "需先完成需求与方向审批"
      }</em></span></label>`;
    })
    .join("");
  updateBenchmarkSelectionNote();
  document.querySelector("#style-benchmark-dialog").showModal();
}

function updateBenchmarkSelectionNote() {
  const count = document.querySelectorAll(
    '#benchmark-poem-picker input[name="poem_ids"]:checked',
  ).length;
  document.querySelector("#benchmark-selection-note").textContent = `${count} 首 · ${
    count * 4
  } 张小样${count < 5 ? "，至少还需选择 5 首" : ""}`;
}

async function saveStyleBenchmark(event) {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector('button[type="submit"]');
  const data = new FormData(form);
  const poemIds = data.getAll("poem_ids");
  if (poemIds.length < 5) {
    showToast("至少选择 5 首已准备的基准诗。", "warning");
    return;
  }
  submit.disabled = true;
  try {
    const result = await api(`/api/style-packs/${data.get("style_version_id")}/benchmark`, {
      method: "POST",
      body: JSON.stringify({
        poem_ids: poemIds,
        provider: data.get("provider"),
        model: data.get("model"),
        unit_cost: Number(data.get("unit_cost")),
        actor: ACTOR,
      }),
    });
    document.querySelector("#style-benchmark-dialog").close();
    await refreshSop(`基准批次已启动：${result.batch.task_count} 张小样。`);
    switchView("resources");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

function openStyleEvaluationDialog(runId) {
  const run = (state.sop.style_benchmark_runs || []).find((item) => item.id === runId);
  if (!run) return;
  const form = document.querySelector("#style-evaluation-form");
  form.reset();
  form.elements.run_id.value = runId;
  document.querySelector("#style-evaluation-context").innerHTML = `<strong>${escapeHtml(
    run.style_name,
  )} ${escapeHtml(run.semantic_version)}</strong><p>${run.poem_ids.length} 首 · ${Number(
    run.task_count || 0,
  )} 张小样 · 批次 ${escapeHtml(run.batch_status || "未知")}</p>`;
  document.querySelector("#style-evaluation-dialog").showModal();
}

async function saveStyleEvaluation(event) {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector('button[type="submit"]');
  const data = new FormData(form);
  submit.disabled = true;
  try {
    const result = await api(`/api/style-benchmark-runs/${data.get("run_id")}/evaluate`, {
      method: "POST",
      body: JSON.stringify({
        style_match_score: Number(data.get("style_match_score")),
        off_topic_rate: Number(data.get("off_topic_percent")) / 100,
        favorite_rate: Number(data.get("favorite_percent")) / 100,
        notes: data.get("notes"),
        actor: ACTOR,
      }),
    });
    document.querySelector("#style-evaluation-dialog").close();
    await refreshSop(
      result.run.gate?.passed ? "基准测试通过，可以发布风格版本。" : "基准测试未达标，请修订后重试。",
    );
    switchView("resources");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

async function publishStyleVersion(versionId) {
  if (!window.confirm("系统将再次核验基准测试和 Art Bible；发布后历史批次不受影响。确认发布？")) return;
  try {
    const result = await api(`/api/style-packs/${versionId}/publish`, {
      method: "POST",
      body: JSON.stringify({ actor: ACTOR }),
    });
    await refreshSop(`风格 ${result.style.style_id} v${result.style.version} 已发布。`);
    switchView("resources");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function saveBudget(event) {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector('button[type="submit"]');
  const data = new FormData(form);
  submit.disabled = true;
  try {
    const result = await api(`/api/projects/${project().id}/budget`, {
      method: "PATCH",
      body: JSON.stringify({
        hard_limit: Number(data.get("hard_limit")),
        soft_ratio: Number(data.get("soft_ratio")),
        actor: ACTOR,
      }),
    });
    state.sop.budget = result.budget;
    renderOverview();
    renderResources();
    showToast("预算规则已保存，后续批次会按新上限重新校验。");
  } catch (error) {
    submit.disabled = false;
    showToast(error.message, "error");
  }
}

async function createProductionBackup() {
  try {
    const result = await api("/api/backups", {
      method: "POST",
      body: JSON.stringify({ actor: ACTOR }),
    });
    await refreshSop(`备份 ${result.backup.name} 已创建并通过完整性校验。`);
    switchView("resources");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function verifyProductionBackup(name) {
  try {
    const result = await api(`/api/backups/${name}/verify`, {
      method: "POST",
      body: JSON.stringify({ actor: ACTOR }),
    });
    showToast(
      result.backup.valid
        ? `备份 ${name} 校验通过。`
        : `备份 ${name} 校验失败。`,
      result.backup.valid ? "success" : "error",
    );
  } catch (error) {
    showToast(error.message, "error");
  }
}

function renderAll() {
  renderChrome();
  renderOverview();
  renderProductionReport();
  renderPoemTable();
  renderRequirements();
  renderDirections();
  renderInstruction();
  renderQueue();
  renderReview();
  renderAssets();
  renderResources();
  applyRoleVisibility();
}

function switchView(view) {
  if (!viewMeta[view]) view = "overview";
  state.activeView = view;
  document
    .querySelectorAll("[data-view-panel]")
    .forEach((panel) => panel.classList.toggle("is-active", panel.dataset.viewPanel === view));
  document
    .querySelectorAll("[data-view]")
    .forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
  document.querySelector("#page-eyebrow").textContent = viewMeta[view][0];
  document.querySelector("#page-title").textContent = viewMeta[view][1];
  history.replaceState(null, "", `#${view}`);
  window.scrollTo({ top: 0, behavior: "auto" });
}

function selectedIdsOrWarn() {
  const ids = [...state.selectedPoems];
  if (!ids.length) showToast("请先在生产表中选择诗词。", "warning");
  return ids;
}

async function generateRequirements(poemIds) {
  if (!poemIds.length) return;
  setSyncState("loading", `正在生成 ${poemIds.length} 首需求`);
  try {
    const result = await api("/api/requirements/generate", {
      method: "POST",
      body: JSON.stringify({
        project_id: project().id,
        poem_ids: poemIds,
        actor: ACTOR,
      }),
    });
    state.selectedPoems.clear();
    await refreshSop(
      `已生成 ${result.succeeded} 首需求${
        result.failed ? `，${result.failed} 首未处理` : ""
      }`,
    );
  } catch (error) {
    setSyncState("error", "需求生成失败");
    showToast(error.message, "error");
  }
}

async function regenerateRequirement(poemId) {
  if (!window.confirm("将创建新需求版本，只保留已锁字段，并使旧画面方向退出新排产。确认继续？")) return;
  try {
    const result = await api("/api/requirements/generate", {
      method: "POST",
      body: JSON.stringify({
        project_id: project().id,
        poem_ids: [poemId],
        preserve_locked: true,
        actor: ACTOR,
      }),
    });
    const item = result.results?.[0] || {};
    if (!item.ok) throw new Error(item.message || "需求重算未完成。");
    await refreshSop(
      item.preserved_fields?.length
        ? `需求已重算，保留 ${item.preserved_fields.length} 个锁定字段。`
        : "需求已重算为新版本。",
    );
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function bulkDecideRequirements(decision) {
  const ids = [...state.selectedRequirements];
  if (!ids.length) return;
  const reason =
    decision === "reject"
      ? window.prompt("请输入本批退回原因（会分别写入每条审计记录）：", "")?.trim() || ""
      : "";
  if (decision === "reject" && !reason) return;
  try {
    const result = await api("/api/requirements/bulk-decision", {
      method: "POST",
      body: JSON.stringify({
        requirement_ids: ids,
        decision,
        reason,
        actor: ACTOR,
      }),
    });
    state.selectedRequirements.clear();
    await refreshSop(
      `${result.succeeded} 条需求${decision === "approve" ? "已通过" : "已退回"}${
        result.failed ? `，${result.failed} 条未处理` : ""
      }。`,
    );
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function approveContent(poemId) {
  const poem = poemById(poemId);
  if (!poem) return;
  if (!window.confirm(`确认「${poem.title}」正文与来源无误，并推进到需求策划？`)) return;
  try {
    await api(`/api/poems/${poemId}/content/approve`, {
      method: "POST",
      body: JSON.stringify({ actor: ACTOR }),
    });
    await refreshSop("内容已批准，可以生成插图需求。");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function decideRequirement(requirementId, decision) {
  let reason = "";
  if (decision === "reject") {
    reason = window.prompt("请输入退回原因（将写入审计记录）：", "")?.trim() || "";
    if (!reason) return;
  }
  try {
    await api(`/api/requirements/${requirementId}/${decision}`, {
      method: "POST",
      body: JSON.stringify({ reason, actor: ACTOR }),
    });
    await refreshSop(decision === "approve" ? "需求已通过，可进入画面策划。" : "需求已退回。");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function generateDirections(poemIds) {
  if (!poemIds.length) return;
  const regenerating = poemIds.filter((poemId) => (poemById(poemId)?.direction_count || 0) > 0);
  if (
    regenerating.length &&
    !window.confirm(
      `其中 ${regenerating.length} 首已有方向版本。重新生成会保留锁定字段、创建新版本，并使未投产旧方向退出当前视图。确认继续？`,
    )
  ) {
    return;
  }
  setSyncState("loading", `正在策划 ${poemIds.length} 首方向`);
  try {
    const result = await api("/api/directions/generate", {
      method: "POST",
      body: JSON.stringify({
        project_id: project().id,
        poem_ids: poemIds,
        preserve_locked: true,
        actor: ACTOR,
      }),
    });
    state.selectedPoems.clear();
    await refreshSop(
      `已为 ${result.succeeded} 首生成三方向${
        result.failed ? `，${result.failed} 首未通过门禁` : ""
      }`,
    );
    if (result.succeeded) switchView("directions");
  } catch (error) {
    setSyncState("error", "方向生成失败");
    showToast(error.message, "error");
  }
}

async function decideDirection(directionId, decision) {
  let reason = "";
  if (decision === "reject") {
    reason = window.prompt("请输入方向退回原因：", "")?.trim() || "";
    if (!reason) return;
  }
  try {
    await api(`/api/directions/${directionId}/${decision}`, {
      method: "POST",
      body: JSON.stringify({
        reason,
        actor: ACTOR,
      }),
    });
    await refreshSop(decision === "approve" ? "方向已批准，诗词进入待排产。" : "方向已退回。");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function bulkDecideDirections(decision) {
  const ids = [...state.selectedDirections];
  if (!ids.length) return;
  const reason =
    decision === "reject"
      ? window.prompt("请输入本批方向退回原因（会逐条写入审计记录）：", "")?.trim() || ""
      : "";
  if (decision === "reject" && !reason) return;
  if (
    decision === "approve" &&
    !window.confirm(`确认批量批准 ${ids.length} 个已人工核对的方向？系统会逐条写入审计记录。`)
  ) {
    return;
  }
  try {
    const result = await api("/api/directions/bulk-decision", {
      method: "POST",
      body: JSON.stringify({
        direction_ids: ids,
        decision,
        reason,
        actor: ACTOR,
      }),
    });
    state.selectedDirections.clear();
    await refreshSop(
      `${result.succeeded} 个方向${decision === "approve" ? "已通过" : "已退回"}${
        result.failed ? `，${result.failed} 个未处理` : ""
      }。`,
    );
  } catch (error) {
    showToast(error.message, "error");
  }
}

function openDirectionEditor(directionId) {
  const direction = directionById(directionId);
  if (!direction) return;
  const content = direction.content || {};
  const form = document.querySelector("#direction-form");
  form.reset();
  form.elements.direction_id.value = direction.id;
  [
    "title",
    "visual_thesis",
    "subject",
    "subject_mode",
    "scene",
    "shot",
    "shot_scale",
    "narrative_mode",
    "foreground",
    "midground",
    "background",
    "action",
    "composition",
    "lighting",
    "palette",
    "whitespace",
    "text_safe_area",
    "risk_note",
    "art_director_note",
  ].forEach((key) => {
    form.elements[key].value = content[key] || "";
  });
  ["preserve", "avoid", "locked_fields"].forEach((key) => {
    form.elements[key].value = (content[key] || []).join("\n");
  });
  const layers = content.interpretation_layers || {};
  form.elements.poem_facts.value = formatLayerLines(layers.poem_facts, "evidence_quote");
  form.elements.reasonable_inferences.value = formatLayerLines(
    layers.reasonable_inferences,
    "basis",
  );
  form.elements.creative_choices.value = formatLayerLines(layers.creative_choices, "purpose");
  document.querySelector("#direction-dialog-title").textContent = `${direction.poem_title} · ${
    directionTypeMeta[direction.type]?.[0] || direction.type
  } v${direction.version}`;
  document.querySelector("#direction-dialog").showModal();
}

function formatLayerLines(items, secondKey) {
  return (items || [])
    .map((item) => `${item.claim || ""} | ${item[secondKey] || ""}`)
    .join("\n");
}

function parseLayerLines(value, secondKey) {
  return asLines(value).map((line) => {
    const separator = line.indexOf("|");
    const claim = (separator >= 0 ? line.slice(0, separator) : line).trim();
    const detail = (separator >= 0 ? line.slice(separator + 1) : "").trim();
    return { claim, [secondKey]: detail };
  });
}

async function saveDirectionRevision(event) {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector('button[type="submit"]');
  const data = new FormData(form);
  submit.disabled = true;
  try {
    const result = await api(`/api/directions/${data.get("direction_id")}/revise`, {
      method: "POST",
      body: JSON.stringify({
        content: {
          title: data.get("title"),
          visual_thesis: data.get("visual_thesis"),
          subject: data.get("subject"),
          subject_mode: data.get("subject_mode"),
          scene: data.get("scene"),
          shot: data.get("shot"),
          shot_scale: data.get("shot_scale"),
          narrative_mode: data.get("narrative_mode"),
          foreground: data.get("foreground"),
          midground: data.get("midground"),
          background: data.get("background"),
          action: data.get("action"),
          composition: data.get("composition"),
          lighting: data.get("lighting"),
          palette: data.get("palette"),
          whitespace: data.get("whitespace"),
          text_safe_area: data.get("text_safe_area"),
          preserve: asLines(data.get("preserve")),
          avoid: asLines(data.get("avoid")),
          risk_note: data.get("risk_note"),
          interpretation_layers: {
            poem_facts: parseLayerLines(data.get("poem_facts"), "evidence_quote"),
            reasonable_inferences: parseLayerLines(
              data.get("reasonable_inferences"),
              "basis",
            ),
            creative_choices: parseLayerLines(data.get("creative_choices"), "purpose"),
          },
          art_director_note: data.get("art_director_note"),
          locked_fields: asLines(data.get("locked_fields")),
        },
        actor: ACTOR,
      }),
    });
    document.querySelector("#direction-dialog").close();
    await refreshSop(`方向已保存为 v${result.direction.version}，等待重新审核。`);
    switchView("directions");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
}

async function copyDirection(directionId) {
  if (!window.confirm("复制会创建新的当前版本并回到待审核，确认继续？")) return;
  try {
    const result = await api(`/api/directions/${directionId}/copy`, {
      method: "POST",
      body: JSON.stringify({ actor: ACTOR }),
    });
    await refreshSop(`方向已复制为 v${result.direction.version}。`);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function disableDirection(directionId) {
  const reason = window.prompt("请输入停用原因（将写入审计记录）：", "")?.trim() || "";
  if (!reason) return;
  try {
    await api(`/api/directions/${directionId}/disable`, {
      method: "POST",
      body: JSON.stringify({
        reason,
        actor: ACTOR,
      }),
    });
    await refreshSop("方向已停用，不再进入新排产。");
  } catch (error) {
    showToast(error.message, "error");
  }
}

function openRequirement(poemId) {
  const poem = poemById(poemId);
  const requirement = poem?.requirement
    ? requirementById(poem.requirement.id)
    : null;
  if (!requirement) {
    showToast("这首诗尚未生成需求卡。", "warning");
    return;
  }
  const content = requirement.content || {};
  document.querySelector("#requirement-id").value = requirement.id;
  document.querySelector("#requirement-dialog-title").textContent =
    `${requirement.poem_title} · 需求卡`;
  document.querySelector("#requirement-dialog-meta").innerHTML = `
    ${statusBadge(requirement.status, requirementStatusMeta)}
    <span>版本 v${requirement.version}</span>
    <span>${escapeHtml(requirement.author)}</span>
    <span>更新于 ${formatDate(requirement.updated_at)}</span>`;
  document.querySelector("#requirement-composition").value =
    content.composition || "";
  document.querySelector("#requirement-must").value = (content.must_have || []).join("\n");
  document.querySelector("#requirement-avoid").value = (content.avoid || []).join("\n");
  document.querySelector("#requirement-note").value = content.editor_note || "";
  document.querySelector("#requirement-locked-fields").value = (
    content.locked_fields || []
  ).join("\n");
  const form = document.querySelector("#requirement-form");
  const locked = requirement.status === "approved";
  form.querySelectorAll("textarea").forEach((field) => {
    field.disabled = locked;
  });
  form.querySelector('button[type="submit"]').hidden = locked;
  document.querySelector("#requirement-dialog").showModal();
}

async function saveRequirement(event) {
  event.preventDefault();
  const id = document.querySelector("#requirement-id").value;
  try {
    await api(`/api/requirements/${id}`, {
      method: "PATCH",
      body: JSON.stringify({
        changes: {
          composition: document.querySelector("#requirement-composition").value.trim(),
          must_have: asLines(document.querySelector("#requirement-must").value),
          avoid: asLines(document.querySelector("#requirement-avoid").value),
          editor_note: document.querySelector("#requirement-note").value.trim(),
          locked_fields: asLines(
            document.querySelector("#requirement-locked-fields").value,
          ),
        },
        actor: ACTOR,
      }),
    });
    document.querySelector("#requirement-dialog").close();
    await refreshSop("需求已保存为新版本，等待重新审核。");
  } catch (error) {
    showToast(error.message, "error");
  }
}

function openBatchDialog() {
  const readyPoems = state.sop.poems.filter(
    (poem) => poem.status === "ready_for_production" && poem.approved_direction_count > 0,
  );
  if (!readyPoems.length) {
    showToast("当前没有通过方向门禁的待排产诗词。", "warning");
    switchView("directions");
    return;
  }
  const selectedReady = new Set(
    [...state.selectedPoems].filter((poemId) =>
      readyPoems.some((poem) => poem.id === poemId),
    ),
  );
  const checkedIds = selectedReady.size
    ? selectedReady
    : new Set(readyPoems.map((poem) => poem.id));
  document.querySelector("#batch-style").innerHTML = publishedStylePacks()
    .map(
      (style) =>
        `<option value="${style.style_id}" ${
          style.style_id === project().style_id ? "selected" : ""
        }>${escapeHtml(style.name)} · v${style.version}</option>`,
    )
    .join("");
  document.querySelector("#batch-poem-options").innerHTML = readyPoems
    .map(
      (poem) => `<label>
        <input type="checkbox" value="${poem.id}" ${
          checkedIds.has(poem.id) ? "checked" : ""
        } />
        <span><strong>${escapeHtml(poem.title)}</strong><small>${escapeHtml(
          poem.author,
        )} · ${poem.approved_direction_count} 个批准方向</small></span>
      </label>`,
    )
    .join("");
  document.querySelector("#batch-engine-copy").textContent = `${
    state.legacy.config.provider === "openai" ? state.legacy.config.model : "本地演示引擎"
  } · 剩余预算 ${Number(state.sop.budget?.remaining || 0).toFixed(2)} ${
    state.sop.budget?.currency || "USD"
  }`;
  state.batchEstimate = null;
  document.querySelector("#batch-estimate").hidden = true;
  document.querySelector("#create-batch-button").disabled = true;
  document.querySelector("#batch-dialog").showModal();
}

function batchFormPayload() {
  const poemIds = [
    ...document.querySelectorAll("#batch-poem-options input:checked"),
  ].map((input) => input.value);
  if (!poemIds.length) throw new Error("请至少选择一首待排产诗词。");
  return {
    project_id: project().id,
    poem_ids: poemIds,
    style_id: document.querySelector("#batch-style").value,
    aspect_ratio: document.querySelector("#batch-ratio").value,
    count_per_direction: Number(document.querySelector("#batch-count").value),
    priority: Number(document.querySelector("#batch-priority").value),
    name: document.querySelector("#batch-name").value.trim(),
  };
}

function renderBatchEstimate(estimate) {
  const container = document.querySelector("#batch-estimate");
  container.hidden = false;
  container.innerHTML = `
    <div class="batch-estimate-grid">
      <span><strong>${estimate.poem_count}</strong><small>首诗</small></span>
      <span><strong>${estimate.direction_count}</strong><small>批准方向</small></span>
      <span><strong>${estimate.task_count}</strong><small>生成任务</small></span>
      <span><strong>${Number(estimate.estimated_cost).toFixed(2)}</strong><small>${escapeHtml(
        estimate.currency,
      )} 预计成本</small></span>
      <span><strong>${Number(estimate.budget.remaining).toFixed(2)}</strong><small>剩余预算</small></span>
    </div>
    <div class="import-verdict ${
      estimate.can_start ? "is-ready" : "is-blocked"
    }">
      <strong>${
        estimate.can_start ? "✓ 预算门禁通过" : "× 预计成本超过剩余预算"
      }</strong>
      <span>${escapeHtml(
        estimate.warnings?.join("；") ||
          "启动后将按优先级进入持久队列，页面关闭不影响任务状态。",
      )}</span>
    </div>`;
  document.querySelector("#create-batch-button").disabled = false;
  document.querySelector("#create-batch-button").textContent = estimate.can_start
    ? "创建并启动"
    : "创建预算阻塞草稿";
}

async function estimateBatch(event) {
  event.preventDefault();
  try {
    const payload = batchFormPayload();
    const estimate = await api("/api/batches/estimate", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.batchEstimate = estimate;
    renderBatchEstimate(estimate);
  } catch (error) {
    state.batchEstimate = null;
    document.querySelector("#create-batch-button").disabled = true;
    showToast(error.message, "error");
  }
}

async function createAndStartBatch() {
  if (!state.batchEstimate) return;
  let batch;
  try {
    batch = (
      await api("/api/batches", {
        method: "POST",
        body: JSON.stringify({ ...batchFormPayload(), actor: ACTOR }),
      })
    ).batch;
    try {
      await api(`/api/batches/${batch.id}/start`, {
        method: "POST",
        body: JSON.stringify({ actor: ACTOR }),
      });
      showToast(`批次已启动，共 ${batch.task_count} 个任务。`);
    } catch (startError) {
      showToast(`批次已创建，但未启动：${startError.message}`, "warning");
    }
    document.querySelector("#batch-dialog").close();
    document.querySelector("#batch-form").reset();
    state.batchEstimate = null;
    state.selectedPoems.clear();
    await loadData({ quiet: true });
    switchView("queue");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function runBatchAction(batchId, action) {
  const body = { actor: ACTOR };
  if (action === "cancel" && !window.confirm("确认取消此批次中尚未开始的任务？")) {
    return;
  }
  if (action === "retry-failed") {
    const hasUnknown = state.sop.tasks.some(
      (task) =>
        task.batch_id === batchId &&
        task.status === "blocked" &&
        task.last_error_code === "OUTCOME_UNKNOWN",
    );
    if (
      hasUnknown &&
      !window.confirm("存在结果未知任务。请确认已核对外部账单与生成资产，仍要重新调用吗？")
    ) {
      return;
    }
    body.confirm_unknown = hasUnknown;
  }
  try {
    await api(`/api/batches/${batchId}/${action}`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    await loadData({ quiet: true });
    showToast(
      {
        start: "批次已启动。",
        pause: "已停止领取新任务，当前任务完成后暂停。",
        resume: "批次已继续运行。",
        cancel: "未开始任务已取消。",
        "retry-failed": "失败任务已重新进入队列。",
      }[action] || "批次状态已更新。",
    );
  } catch (error) {
    await loadData({ quiet: true });
    showToast(error.message, error.code === "BUDGET_BLOCKED" ? "warning" : "error");
  }
}

function parseImportRecords() {
  const raw = document.querySelector("#import-json").value.trim();
  if (!raw) throw new Error("请选择 JSON 文件或粘贴诗词数据。");
  let records;
  try {
    records = JSON.parse(raw);
  } catch (error) {
    throw new Error(`JSON 格式错误：${error.message}`);
  }
  if (!Array.isArray(records)) throw new Error("导入内容必须是 JSON 数组。");
  return records;
}

function renderImportPreview(preview) {
  const result = document.querySelector("#import-result");
  result.hidden = false;
  const counts = preview.counts;
  const issues = preview.items.filter(
    (item) => item.status === "invalid" || item.status === "conflict" || item.warnings.length,
  );
  result.innerHTML = `
    <div class="import-metrics">
      <span><strong>${counts.total}</strong> 总记录</span>
      <span><strong>${counts.new}</strong> 新增</span>
      <span><strong>${counts.unchanged}</strong> 未变化</span>
      <span class="${counts.conflict ? "has-error" : ""}"><strong>${counts.conflict}</strong> 冲突</span>
      <span class="${counts.invalid ? "has-error" : ""}"><strong>${counts.invalid}</strong> 无效</span>
      <span class="${counts.warnings ? "has-warning" : ""}"><strong>${counts.warnings}</strong> 警告</span>
    </div>
    <div class="import-verdict ${preview.can_commit ? "is-ready" : "is-blocked"}">
      <strong>${preview.can_commit ? "✓ 预检通过，可以提交" : "× 预检未通过，不能提交"}</strong>
      <span>${preview.can_commit ? "提交只会新增记录，不覆盖已有诗词。" : "请先处理无效字段或正文冲突。"}</span>
    </div>
    ${
      issues.length
        ? `<div class="import-issues">${issues
            .slice(0, 30)
            .map(
              (item) => `<article>
                <div><strong>${escapeHtml(item.title || `第 ${item.index + 1} 条`)}</strong><span>${escapeHtml(item.id)}</span></div>
                <span class="issue-status status-${item.status}">${escapeHtml(item.status)}</span>
                <p>${escapeHtml(
                  [
                    ...(item.errors || []),
                    ...(item.warnings || []),
                    item.conflict_fields?.length
                      ? `冲突字段：${item.conflict_fields.join("、")}`
                      : "",
                  ]
                    .filter(Boolean)
                    .join("；"),
                )}</p>
              </article>`,
            )
            .join("")}</div>`
        : ""
    }`;
  document.querySelector("#commit-import-button").disabled = !preview.can_commit;
}

async function previewImport(event) {
  event.preventDefault();
  try {
    state.importRecords = parseImportRecords();
    const preview = await api(`/api/projects/${project().id}/poems/import`, {
      method: "POST",
      body: JSON.stringify({ records: state.importRecords, commit: false }),
    });
    state.importPreview = preview;
    renderImportPreview(preview);
  } catch (error) {
    state.importPreview = null;
    document.querySelector("#commit-import-button").disabled = true;
    showToast(error.message, "error");
  }
}

async function commitImport() {
  if (!state.importPreview?.can_commit || !state.importRecords) return;
  try {
    const result = await api(`/api/projects/${project().id}/poems/import`, {
      method: "POST",
      body: JSON.stringify({
        records: state.importRecords,
        commit: true,
        actor: ACTOR,
      }),
    });
    document.querySelector("#import-dialog").close();
    state.importRecords = null;
    state.importPreview = null;
    document.querySelector("#import-form").reset();
    document.querySelector("#import-result").hidden = true;
    document.querySelector("#commit-import-button").disabled = true;
    await refreshSop(`已导入 ${result.imported} 首诗词，${result.unchanged} 首保持不变。`);
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function decideImage(imageId, decision) {
  try {
    const payload = await api(`/api/images/${imageId}`, {
      method: "PATCH",
      body: JSON.stringify({ decision }),
    });
    const index = state.legacy.images.findIndex((item) => item.id === imageId);
    if (index >= 0) state.legacy.images[index] = payload.image;
    renderChrome();
    renderOverview();
    renderReview();
    renderAssets();
    showToast(decision === "selected" ? "候选已入选方向。" : "候选已淘汰。");
  } catch (error) {
    showToast(error.message, "error");
  }
}

function openLegacyImage(imageId) {
  const image = state.legacy.images.find((item) => item.id === imageId);
  if (!image) return;
  state.currentImageId = imageId;
  document.querySelector("#image-dialog-title").textContent = image.poem_title;
  const preview = document.querySelector("#image-dialog-preview");
  preview.src = image.url;
  preview.alt = `${image.poem_title}插图`;
  const qcDone = Object.values(image.qc || {}).filter(Boolean).length;
  document.querySelector("#image-dialog-copy").innerHTML = `
    <span class="status-pill">${escapeHtml(image.style_name)}</span>
    <h3>${escapeHtml(image.poem_title)} · ${escapeHtml(image.author)}</h3>
    <p>${image.generation_mode === "converge" ? "收敛返工" : "方向探索"} · ${
      image.parent_image_id ? "有父候选" : "初始候选"
    } · ${formatDate(image.created_at)}</p>
    <dl>
      <div><dt>项目</dt><dd>${escapeHtml(image.project_name)}</dd></div>
      <div><dt>评审状态</dt><dd>${escapeHtml(image.decision)}</dd></div>
      <div><dt>质检进度</dt><dd>${qcDone} / 5</dd></div>
      <div><dt>生成引擎</dt><dd>${escapeHtml(image.provider)}</dd></div>
    </dl>
    <details><summary>查看生成提示词</summary><p>${escapeHtml(image.prompt)}</p></details>
    <div class="dialog-review-actions">
      <button class="danger-button" type="button" data-image-action="rejected" data-image-id="${
        image.id
      }">淘汰候选</button>
      <button class="approve-button" type="button" data-image-action="selected" data-image-id="${
        image.id
      }">入选方向</button>
    </div>`;
  if (!document.querySelector("#image-dialog").open) {
    document.querySelector("#image-dialog").showModal();
  }
}

function openProductionImage(imageId) {
  const image = productionImageById(imageId);
  if (!image) return;
  const qc = image.qc || {};
  const finalApprovals = image.final_approvals || {};
  const promptSegments = image.prompt_segments || {};
  const contentSource = promptSegments.content || {};
  const requirementSource = promptSegments.requirement || {};
  const directionSource = promptSegments.direction || {};
  const styleSource = promptSegments.style || {};
  const instructionSource = promptSegments.instruction || {};
  const blocked = ["qc_blocked", "needs_manual_qc"].includes(image.status);
  state.currentImageId = imageId;
  document.querySelector("#image-dialog-title").textContent = `${image.poem_title} · 候选审片`;
  const preview = document.querySelector("#image-dialog-preview");
  preview.src = image.url;
  preview.alt = `${image.poem_title}插图`;
  document.querySelector("#image-dialog-copy").innerHTML = `
    <div class="review-dialog-status">
      ${statusBadge(image.status, productionImageStatusMeta)}
      ${statusBadge(qc.decision || "manual_review", qcDecisionMeta)}
      <strong>QC ${Math.round(Number(qc.score || 0))}</strong>
      <span>${escapeHtml(qc.policy_version_id || "无政策版本")}</span>
    </div>
    <h3>${escapeHtml(image.poem_title)} · ${escapeHtml(image.author)}</h3>
    <p>${escapeHtml(directionTypeMeta[image.direction_type]?.[0] || image.direction_type)} · 第 ${
      image.generation
    } 代 · ${formatDate(image.created_at)}</p>
    <dl>
      <div><dt>生产批次</dt><dd>${escapeHtml(image.batch_name)}</dd></div>
      <div><dt>风格 / 引擎</dt><dd>${escapeHtml(image.style_id)} · ${escapeHtml(
        image.provider,
      )}</dd></div>
      <div><dt>文件规格</dt><dd>${image.width}×${image.height} · ${escapeHtml(
        image.mime_type,
      )}</dd></div>
      <div><dt>谱系</dt><dd>第 ${image.generation} 代 · ${image.child_count} 个子候选</dd></div>
      <div><dt>视觉审查</dt><dd>${escapeHtml(qc.reviewer_kind || "未执行")} · ${escapeHtml(
        qc.reviewer_model || "—",
      )}</dd></div>
      <div><dt>置信度</dt><dd>${Math.round(Number(qc.confidence || 0) * 100)}%</dd></div>
    </dl>
    <section class="source-trace-panel">
      <div class="source-trace-head"><strong>生产证据链</strong><span>${escapeHtml(
        image.prompt_template_version || "旧版 Prompt",
      )}</span></div>
      <div class="source-trace-grid">
        <article><small>诗文版本</small><strong>v${escapeHtml(
          contentSource.content_version || "—",
        )}</strong><span>${escapeHtml(contentSource.content_version_id || "未记录")}</span></article>
        <article><small>需求版本</small><strong>v${escapeHtml(
          requirementSource.version || "—",
        )}</strong><span>${escapeHtml(requirementSource.id || "未记录")}</span></article>
        <article><small>方向版本</small><strong>v${escapeHtml(
          directionSource.version || "—",
        )}</strong><span>${escapeHtml(directionSource.id || image.direction_id || "未记录")}</span></article>
        <article><small>风格版本</small><strong>v${escapeHtml(
          styleSource.version || "—",
        )}</strong><span>${escapeHtml(styleSource.version_id || image.style_version_id || "未记录")}</span></article>
        <article><small>指令版本</small><strong>v${escapeHtml(
          instructionSource.version || "—",
        )}</strong><span>${escapeHtml(instructionSource.id || "未记录")}</span></article>
      </div>
      <code title="Prompt SHA-256">SHA-256 · ${escapeHtml(
        image.prompt_hash || "旧数据未记录哈希",
      )}</code>
    </section>
    <section class="qc-detail ${blocked ? "is-blocked" : ""}">
      <div><strong>自动 QC · ${escapeHtml(qc.version || "未执行")}</strong><span>${escapeHtml(
        qc.status || "未执行",
      )}</span></div>
      <div class="qc-score-grid">
        ${Object.entries(qcDimensionLabels)
          .map(
            ([field, label]) => `<article><small>${label}</small><strong>${
              qc.scores?.[field] == null ? "—" : Math.round(Number(qc.scores[field]))
            }</strong></article>`,
          )
          .join("")}
      </div>
      <div class="qc-problem-list">
        ${(qc.problems || [])
          .map(
            (problem) => `<article class="severity-${escapeHtml(problem.severity)}">
              <div><strong>${escapeHtml(problem.code)}</strong><span>${escapeHtml(
                qcDimensionLabels[problem.dimension] || problem.dimension,
              )} · ${escapeHtml(problem.severity)}</span></div>
              <p>${escapeHtml(problem.note)}</p><small>可见证据：${escapeHtml(
                problem.evidence,
              )}</small>
            </article>`,
          )
          .join("")}
      </div>
      <ul>
        ${(qc.hard_failures || [])
          .map((item) => `<li class="is-danger">硬失败 · ${escapeHtml(item)}</li>`)
          .join("")}
        ${(qc.warnings || [])
          .map((item) => `<li>需人工确认 · ${escapeHtml(item)}</li>`)
          .join("")}
      </ul>
      ${
        qc.duplicate_of
          ? `<p>相似候选：${escapeHtml(qc.duplicate_of.slice(-8))}</p>`
          : ""
      }
      ${
        (qc.evidence?.observed_elements || []).length ||
        (qc.evidence?.missing_required_elements || []).length ||
        (qc.evidence?.uncertain_elements || []).length
          ? `<details class="qc-evidence"><summary>查看模型观察证据</summary>
              <p><strong>已观察：</strong>${escapeHtml(
                (qc.evidence?.observed_elements || []).join("、") || "无",
              )}</p>
              <p><strong>缺失项：</strong>${escapeHtml(
                (qc.evidence?.missing_required_elements || []).join("、") || "无",
              )}</p>
              <p><strong>不确定：</strong>${escapeHtml(
                (qc.evidence?.uncertain_elements || []).join("、") || "无",
              )}</p>
            </details>`
          : ""
      }
    </section>
    <section class="qc-calibration-panel" data-role-allow="content_editor,art_director,producer,system_admin">
      <div><strong>人工校准标签</strong><span>用于评估误放、误杀与阈值偏差，不会改写本次自动结果</span></div>
      <div>
        ${Object.entries(qcDecisionMeta)
          .map(
            ([decision, meta]) =>
              `<button class="secondary-button" type="button" data-qc-calibration="${decision}" data-image-id="${image.id}">${meta[0]}</button>`,
          )
          .join("")}
      </div>
    </section>
    <details><summary>查看冻结的六段式 Prompt</summary><p>${escapeHtml(
      [
        `Prompt hash：${image.prompt_hash || "未记录"}`,
        `模板：${image.prompt_template_version || "未记录"}`,
        "",
        image.prompt || "",
      ].join("\n"),
    )}</p></details>
    ${
      image.status === "final_candidate"
        ? `<section class="final-gate-panel">
            <div><strong>双终审门禁</strong><span>内容与美术均通过后自动锁定成品</span></div>
            <article>
              <span>内容终审</span>
              ${statusBadge(
                finalApprovals.content?.decision || "pending",
                {
                  pending: ["待审核", "warning"],
                  approved: ["已通过", "success"],
                  rejected: ["已退回", "danger"],
                },
              )}
              <div data-role-allow="content_editor,producer,system_admin"><button class="secondary-button" type="button" data-final-reviewer="content" data-final-decision="approved" data-image-id="${image.id}">内容通过</button><button class="danger-button" type="button" data-final-reviewer="content" data-final-decision="rejected" data-image-id="${image.id}">退回</button></div>
            </article>
            <article>
              <span>美术终审</span>
              ${statusBadge(
                finalApprovals.art?.decision || "pending",
                {
                  pending: ["待审核", "warning"],
                  approved: ["已通过", "success"],
                  rejected: ["已退回", "danger"],
                },
              )}
              <div data-role-allow="art_director,producer,system_admin"><button class="secondary-button" type="button" data-final-reviewer="art" data-final-decision="approved" data-image-id="${image.id}">美术通过</button><button class="danger-button" type="button" data-final-reviewer="art" data-final-decision="rejected" data-image-id="${image.id}">退回</button></div>
            </article>
          </section>`
        : ""
    }
    <div class="review-decision-form">
      <label><span>结构化理由</span><select class="dialog-select" id="review-reason-tag">
        <option value="">选择理由后再决策</option>
        <option>诗意准确</option><option>构图最佳</option><option>风格一致</option>
        <option>主体异常</option><option>历史风险</option><option>文字或水印</option>
        <option>重复构图</option><option>画质问题</option><option>其他</option>
      </select></label>
      <label><span>评审备注 / QC 覆盖原因</span><textarea id="review-decision-note" rows="3" placeholder="说明保留、淘汰或人工覆盖的依据"></textarea></label>
    </div>
    <div class="dialog-review-actions production-review-actions" data-role-allow="art_director,producer,system_admin">
      ${
        blocked
          ? `<button class="approve-button" type="button" data-qc-override="pass" data-image-id="${image.id}">人工复核通过</button>`
          : `<button class="danger-button" type="button" data-production-decision="rejected" data-image-id="${image.id}">X 淘汰</button>
             <button class="secondary-button" type="button" data-open-rework="${image.id}">R 返工</button>
             <button class="approve-button" type="button" data-production-decision="selected" data-image-id="${image.id}">S 入选</button>
             <button class="primary-button" type="button" data-production-decision="final_candidate" data-image-id="${image.id}">F 终审候选</button>`
      }
    </div>
    <small class="shortcut-hint">J / K 上下张 · S 入选 · X 淘汰 · F 终审候选 · R 返工</small>`;
  applyRoleVisibility();
  if (!document.querySelector("#image-dialog").open) {
    document.querySelector("#image-dialog").showModal();
  }
}

function openImage(imageId) {
  if (productionImageById(imageId)) openProductionImage(imageId);
  else openLegacyImage(imageId);
}

async function sendReviewDecision(imageId, decision) {
  const reason = document.querySelector("#review-reason-tag")?.value || "";
  const note = document.querySelector("#review-decision-note")?.value.trim() || "";
  if (decision !== "candidate" && !reason) {
    showToast("请先选择结构化审片理由。", "warning");
    return;
  }
  try {
    await api(`/api/images/${imageId}/decision`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        reason_tags: reason ? [reason] : [],
        note,
        actor: ACTOR,
      }),
    });
    document.querySelector("#image-dialog").close();
    state.reviewSelection.delete(imageId);
    await refreshSop(
      {
        selected: "候选已入选。",
        rejected: "候选已淘汰，决策已留痕。",
        final_candidate: "已进入终审候选。",
        candidate: "已撤回为待审候选。",
      }[decision],
    );
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function overrideProductionQc(imageId, decision) {
  const reason = document.querySelector("#review-decision-note")?.value.trim() || "";
  if (!reason) {
    showToast("人工覆盖必须在备注中说明核对依据。", "warning");
    return;
  }
  try {
    await api(`/api/images/${imageId}/qc-override`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        reason,
        actor: ACTOR,
      }),
    });
    document.querySelector("#image-dialog").close();
    await refreshSop("人工 QC 覆盖已记录，原自动结果保持不变。");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function submitQcCalibration(imageId, humanDecision) {
  const reason = document.querySelector("#review-reason-tag")?.value || "";
  const note = document.querySelector("#review-decision-note")?.value.trim() || "";
  try {
    await api(`/api/images/${imageId}/qc-calibration`, {
      method: "POST",
      body: JSON.stringify({
        human_decision: humanDecision,
        human_scores: {},
        reason_tags: reason ? [reason] : [],
        note,
        actor: ACTOR,
      }),
    });
    document.querySelector("#image-dialog").close();
    await refreshSop("人工校准样本已记录；自动 QC 原始结论保持不变。");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function submitFinalApproval(imageId, reviewerType, decision = "approved") {
  const reason = document.querySelector("#review-decision-note")?.value.trim() || "";
  const label = reviewerType === "content" ? "内容终审" : "美术终审";
  if (decision === "rejected" && !reason) {
    showToast("终审退回前请在备注中填写原因。", "warning");
    return;
  }
  if (
    !window.confirm(
      `确认${label}${decision === "approved" ? "通过" : "退回"}？该操作会写入不可变审计记录。`,
    )
  ) {
    return;
  }
  try {
    const result = await api(`/api/images/${imageId}/finalize`, {
      method: "POST",
      body: JSON.stringify({
        reviewer_type: reviewerType,
        decision,
        reason,
        actor: ACTOR,
      }),
    });
    document.querySelector("#image-dialog").close();
    await refreshSop(
      result.locked
        ? "内容与美术终审均已通过，成品版本已锁定。"
        : `${label}结论已记录，等待另一角色终审。`,
    );
  } catch (error) {
    showToast(error.message, "error");
  }
}

function openRework(imageId) {
  const image = productionImageById(imageId);
  if (!image) return;
  document.querySelector("#rework-image-id").value = imageId;
  document.querySelector("#rework-dialog-title").textContent = `${image.poem_title} · 创建返工单`;
  document.querySelector("#rework-preserve").value = (image.direction?.preserve || []).join("\n");
  document.querySelector("#rework-change").value = "";
  document.querySelector("#rework-avoid").value = (image.direction?.avoid || []).join("\n");
  document.querySelector("#rework-note").value = "";
  document.querySelector("#image-dialog").close();
  document.querySelector("#rework-dialog").showModal();
}

async function submitRework(event) {
  event.preventDefault();
  const imageId = document.querySelector("#rework-image-id").value;
  try {
    await api(`/api/images/${imageId}/rework`, {
      method: "POST",
      body: JSON.stringify({
        preserve: asLines(document.querySelector("#rework-preserve").value),
        change: asLines(document.querySelector("#rework-change").value),
        avoid: asLines(document.querySelector("#rework-avoid").value),
        note: document.querySelector("#rework-note").value.trim(),
        actor: ACTOR,
      }),
    });
    document.querySelector("#rework-dialog").close();
    await refreshSop("返工单已创建，保持项、修改项和禁止项均已留痕。");
  } catch (error) {
    showToast(error.message, "error");
  }
}

function openComparison() {
  const images = [...state.reviewSelection]
    .map((imageId) => productionImageById(imageId))
    .filter(Boolean)
    .slice(0, 4);
  if (images.length < 2) return;
  document.querySelector("#compare-grid").innerHTML = images
    .map(
      (image) => `<article>
        <button type="button" data-open-image="${image.id}" data-close-compare><img src="${escapeHtml(
          image.url,
        )}" alt="${escapeHtml(image.poem_title)}候选" /></button>
        <div><strong>${escapeHtml(image.poem_title)} · ${escapeHtml(
          directionTypeMeta[image.direction_type]?.[0] || image.direction_type,
        )}</strong><span>QC ${Math.round(Number(image.qc?.score || 0))} · 第 ${
          image.generation
        } 代</span>${statusBadge(image.status, productionImageStatusMeta)}</div>
      </article>`,
    )
    .join("");
  document.querySelector("#compare-dialog").showModal();
}

async function exportFinalAssets() {
  try {
    const estimate = await api("/api/exports/estimate", {
      method: "POST",
      body: JSON.stringify({ project_id: project().id }),
    });
    if (!estimate.can_export) {
      showToast("导出预检未通过，请先检查成品文件。", "error");
      return;
    }
    if (
      !window.confirm(
        `将导出 ${estimate.asset_count} 个当前成品及完整 Manifest，确认继续？`,
      )
    ) {
      return;
    }
    const result = await api("/api/exports", {
      method: "POST",
      body: JSON.stringify({
        project_id: project().id,
        actor: ACTOR,
      }),
    });
    await refreshSop(`交付包 ${result.package.name} 已生成，历史导出未被覆盖。`);
    switchView("assets");
  } catch (error) {
    showToast(error.message, "error");
  }
}

function handlePoemAction(action, poemId) {
  if (action === "approve-content") return approveContent(poemId);
  if (action === "generate-requirement") return generateRequirements([poemId]);
  if (action === "open-requirement") {
    switchView("requirements");
    return openRequirement(poemId);
  }
  if (action === "generate-direction") return generateDirections([poemId]);
  if (action === "open-directions") return switchView("directions");
  if (action === "open-queue") return switchView("queue");
  if (action === "open-review") return switchView("review");
  return switchView("overview");
}

document.addEventListener("click", (event) => {
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) switchView(viewButton.dataset.view);

  const switchButton = event.target.closest("[data-switch-view]");
  if (switchButton) switchView(switchButton.dataset.switchView);

  const poemDetail = event.target.closest("[data-open-poem-detail]");
  if (poemDetail) openPoemDetail(poemDetail.dataset.openPoemDetail);

  const actionButton = event.target.closest("[data-action]");
  if (actionButton) {
    const action = actionButton.dataset.action;
    if (action === "generate-selected-requirements") {
      generateRequirements(selectedIdsOrWarn());
    } else if (action === "generate-selected-directions") {
      generateDirections(selectedIdsOrWarn());
    } else if (action === "select-requirement-drafts") {
      state.selectedPoems = new Set(
        state.sop.poems
          .filter((poem) => poem.status === "requirement_draft")
          .map((poem) => poem.id),
      );
      renderPoemTable();
      renderRequirements();
      showToast(`已选择 ${state.selectedPoems.size} 首待生成需求的诗词。`);
    } else if (action === "select-direction-drafts") {
      state.selectedPoems = new Set(
        state.sop.poems
          .filter((poem) => poem.status === "direction_draft")
          .map((poem) => poem.id),
      );
      renderPoemTable();
      showToast(`已选择 ${state.selectedPoems.size} 首待策划诗词。`);
    } else if (action === "select-requirement-reviews") {
      state.selectedRequirements = new Set(
        state.sop.requirements
          .filter((item) => item.status === "in_review")
          .map((item) => item.id),
      );
      renderRequirements();
    } else if (action === "select-direction-reviews") {
      state.selectedDirections = new Set(
        state.sop.directions
          .filter((item) => item.status === "in_review")
          .map((item) => item.id),
      );
      renderDirections();
    } else if (action === "bulk-approve-requirements") {
      bulkDecideRequirements("approve");
    } else if (action === "bulk-reject-requirements") {
      bulkDecideRequirements("reject");
    } else if (action === "bulk-approve-directions") {
      bulkDecideDirections("approve");
    } else if (action === "bulk-reject-directions") {
      bulkDecideDirections("reject");
    }
  }

  const poemAction = event.target.closest("[data-poem-action]");
  if (poemAction) {
    handlePoemAction(poemAction.dataset.poemAction, poemAction.dataset.poemId);
  }

  const requirementAction = event.target.closest("[data-requirement-action]");
  if (requirementAction) {
    const requirement = requirementById(requirementAction.dataset.requirementId);
    if (!requirement) return;
    if (requirementAction.dataset.requirementAction === "edit") {
      openRequirement(requirement.poem_id);
    } else {
      decideRequirement(
        requirement.id,
        requirementAction.dataset.requirementAction,
      );
    }
  }

  const regenerate = event.target.closest("[data-regenerate-requirement]");
  if (regenerate) regenerateRequirement(regenerate.dataset.regenerateRequirement);

  const directionAction = event.target.closest("[data-direction-action]");
  if (directionAction) {
    decideDirection(
      directionAction.dataset.directionId,
      directionAction.dataset.directionAction,
    );
  }

  const directionEdit = event.target.closest("[data-edit-direction]");
  if (directionEdit) openDirectionEditor(directionEdit.dataset.editDirection);

  const directionCopy = event.target.closest("[data-copy-direction]");
  if (directionCopy) copyDirection(directionCopy.dataset.copyDirection);

  const directionDisable = event.target.closest("[data-disable-direction]");
  if (directionDisable) disableDirection(directionDisable.dataset.disableDirection);

  const batchAction = event.target.closest("[data-batch-action]");
  if (batchAction) {
    runBatchAction(batchAction.dataset.batchId, batchAction.dataset.batchAction);
  }

  const filterButton = event.target.closest("[data-requirement-filter]");
  if (filterButton) {
    state.requirementFilter = filterButton.dataset.requirementFilter;
    renderRequirements();
  }

  const imageAction = event.target.closest("[data-image-action]");
  if (imageAction) {
    decideImage(imageAction.dataset.imageId, imageAction.dataset.imageAction);
    document.querySelector("#image-dialog").close();
  }

  const productionDecision = event.target.closest("[data-production-decision]");
  if (productionDecision) {
    sendReviewDecision(
      productionDecision.dataset.imageId,
      productionDecision.dataset.productionDecision,
    );
  }

  const qcOverride = event.target.closest("[data-qc-override]");
  if (qcOverride) {
    overrideProductionQc(qcOverride.dataset.imageId, qcOverride.dataset.qcOverride);
  }

  const qcCalibration = event.target.closest("[data-qc-calibration]");
  if (qcCalibration) {
    submitQcCalibration(
      qcCalibration.dataset.imageId,
      qcCalibration.dataset.qcCalibration,
    );
  }

  const finalApproval = event.target.closest("[data-final-reviewer]");
  if (finalApproval) {
    submitFinalApproval(
      finalApproval.dataset.imageId,
      finalApproval.dataset.finalReviewer,
      finalApproval.dataset.finalDecision,
    );
  }

  const reworkButton = event.target.closest("[data-open-rework]");
  if (reworkButton) openRework(reworkButton.dataset.openRework);

  if (event.target.closest("[data-create-backup]")) createProductionBackup();

  const verifyBackup = event.target.closest("[data-verify-backup]");
  if (verifyBackup) verifyProductionBackup(verifyBackup.dataset.verifyBackup);

  const publishInstruction = event.target.closest("[data-publish-instruction]");
  if (publishInstruction) {
    publishInstructionVersion(publishInstruction.dataset.publishInstruction);
  }

  const cloneInstruction = event.target.closest("[data-clone-instruction]");
  if (cloneInstruction) openInstructionDialog(cloneInstruction.dataset.cloneInstruction);

  const diffInstruction = event.target.closest("[data-diff-instruction]");
  if (diffInstruction) openInstructionDiff(diffInstruction.dataset.diffInstruction);

  const retireInstruction = event.target.closest("[data-retire-instruction]");
  if (retireInstruction) {
    retireInstructionVersion(retireInstruction.dataset.retireInstruction);
  }

  const publishStyle = event.target.closest("[data-publish-style]");
  if (publishStyle) publishStyleVersion(publishStyle.dataset.publishStyle);

  const publishArtBible = event.target.closest("[data-publish-art-bible]");
  if (publishArtBible) publishArtBibleVersion(publishArtBible.dataset.publishArtBible);

  const startStyleBenchmark = event.target.closest("[data-start-style-benchmark]");
  if (startStyleBenchmark) {
    openStyleBenchmarkDialog(startStyleBenchmark.dataset.startStyleBenchmark);
  }

  const evaluateStyleBenchmark = event.target.closest("[data-evaluate-style-benchmark]");
  if (evaluateStyleBenchmark) {
    openStyleEvaluationDialog(evaluateStyleBenchmark.dataset.evaluateStyleBenchmark);
  }

  const anomaly = event.target.closest("[data-anomaly-view]");
  if (anomaly) {
    const view = anomaly.dataset.anomalyView;
    const filter = anomaly.dataset.anomalyFilter || "";
    if (view === "queue") {
      state.queueTaskFilter = filter;
      refreshTaskPage({ offset: 0 });
    } else if (view === "review") {
      state.reviewShowBlocked = true;
      document.querySelector("#review-show-blocked").checked = true;
      renderReview();
    } else if (view === "overview" && filter === "blocked") {
      state.poemStatus = "blocked";
      document.querySelector("#poem-status-filter").value = "blocked";
      renderPoemTable();
    } else if (view === "requirements" && filter === "failed") {
      state.requirementFilter = "failed";
      renderRequirements();
    }
    switchView(view);
  }

  if (event.target.closest("[data-clear-queue-filter]")) {
    state.queueTaskFilter = "";
    state.queueBatchFilter = "";
    state.queueErrorFilter = "";
    state.queueTaskQuery = "";
    refreshTaskPage({ offset: 0 });
  }

  const taskPageButton = event.target.closest("[data-task-page-offset]");
  if (taskPageButton && !taskPageButton.disabled) {
    refreshTaskPage({ offset: Number(taskPageButton.dataset.taskPageOffset || 0) });
  }

  const imageOpen = event.target.closest("[data-open-image]");
  if (imageOpen) {
    if (imageOpen.closest("#compare-dialog")) {
      document.querySelector("#compare-dialog").close();
    }
    openImage(imageOpen.dataset.openImage);
  }

  const close = event.target.closest("[data-close-dialog]");
  if (close) close.closest("dialog")?.close();
});

document.addEventListener("change", (event) => {
  if (event.target.matches('#benchmark-poem-picker input[name="poem_ids"]')) {
    updateBenchmarkSelectionNote();
  }
  const checkbox = event.target.closest("[data-select-poem]");
  if (checkbox) {
    if (checkbox.checked) state.selectedPoems.add(checkbox.dataset.selectPoem);
    else state.selectedPoems.delete(checkbox.dataset.selectPoem);
    renderPoemTable();
    renderRequirements();
  }
  const compareImage = event.target.closest("[data-compare-image]");
  if (compareImage) {
    if (compareImage.checked) {
      if (state.reviewSelection.size >= 4) {
        compareImage.checked = false;
        showToast("一次最多对比 4 张候选。", "warning");
      } else {
        state.reviewSelection.add(compareImage.dataset.compareImage);
      }
    } else {
      state.reviewSelection.delete(compareImage.dataset.compareImage);
    }
    renderReview();
  }
  const requirementSelection = event.target.closest("[data-select-requirement]");
  if (requirementSelection) {
    if (requirementSelection.checked) {
      state.selectedRequirements.add(requirementSelection.dataset.selectRequirement);
    } else {
      state.selectedRequirements.delete(requirementSelection.dataset.selectRequirement);
    }
    renderRequirements();
  }
  const directionSelection = event.target.closest("[data-select-direction]");
  if (directionSelection) {
    if (directionSelection.checked) {
      state.selectedDirections.add(directionSelection.dataset.selectDirection);
    } else {
      state.selectedDirections.delete(directionSelection.dataset.selectDirection);
    }
    renderDirections();
  }
});

document.querySelector("#select-all-poems").addEventListener("change", (event) => {
  const poems = visiblePoems();
  poems.forEach((poem) => {
    if (event.target.checked) state.selectedPoems.add(poem.id);
    else state.selectedPoems.delete(poem.id);
  });
  renderPoemTable();
  renderRequirements();
});

document.querySelector("#poem-search").addEventListener("input", (event) => {
  state.poemQuery = event.target.value;
  renderPoemTable();
});

document.querySelector("#poem-status-filter").addEventListener("change", (event) => {
  state.poemStatus = event.target.value;
  renderPoemTable();
});

let queueSearchTimer = null;
document.querySelector("#queue-task-search").addEventListener("input", (event) => {
  state.queueTaskQuery = event.target.value.trim();
  clearTimeout(queueSearchTimer);
  queueSearchTimer = setTimeout(() => refreshTaskPage({ offset: 0 }), 240);
});
document.querySelector("#queue-task-status").addEventListener("change", (event) => {
  state.queueTaskFilter = event.target.value;
  refreshTaskPage({ offset: 0 });
});
document.querySelector("#queue-batch-filter").addEventListener("change", (event) => {
  state.queueBatchFilter = event.target.value;
  refreshTaskPage({ offset: 0 });
});
document.querySelector("#queue-error-filter").addEventListener("change", (event) => {
  state.queueErrorFilter = event.target.value.trim();
  refreshTaskPage({ offset: 0 });
});

document.querySelector("#refresh-button").addEventListener("click", () => loadData());
document.querySelector("#current-role").addEventListener("change", (event) => {
  ACTOR.role = event.target.value;
  ACTOR.id = `local-${ACTOR.role}`;
  localStorage.setItem("tang-sop-role", ACTOR.role);
  renderAll();
  showToast(`已切换为${roleLabels[ACTOR.role]}视图；关键写操作仍由服务端校验角色。`);
});
document.querySelector("#open-instruction-button").addEventListener("click", () => openInstructionDialog());
document.querySelector("#instruction-form").addEventListener("submit", saveInstructionVersion);
document.querySelector("#open-art-bible-button").addEventListener("click", openArtBibleDialog);
document.querySelector("#art-bible-form").addEventListener("submit", saveArtBibleVersion);
document.querySelector("#open-style-button").addEventListener("click", openStyleDialog);
document.querySelector("#style-form").addEventListener("submit", saveStyleVersion);
document.querySelector("#style-benchmark-form").addEventListener("submit", saveStyleBenchmark);
document.querySelector("#style-evaluation-form").addEventListener("submit", saveStyleEvaluation);
document.querySelector("#requirement-form").addEventListener("submit", saveRequirement);
document.querySelector("#direction-form").addEventListener("submit", saveDirectionRevision);
document.querySelector("#open-batch-button").addEventListener("click", openBatchDialog);
document.querySelector("#batch-form").addEventListener("submit", estimateBatch);
document.querySelector("#create-batch-button").addEventListener("click", createAndStartBatch);
document.addEventListener("submit", (event) => {
  if (event.target.matches("#budget-form")) saveBudget(event);
});
document.querySelector("#batch-form").addEventListener("change", () => {
  state.batchEstimate = null;
  document.querySelector("#batch-estimate").hidden = true;
  document.querySelector("#create-batch-button").disabled = true;
  document.querySelector("#create-batch-button").textContent = "创建并启动";
});
document.querySelector("#open-import-button").addEventListener("click", () => {
  document.querySelector("#import-dialog").showModal();
});
document.querySelector("#import-form").addEventListener("submit", previewImport);
document.querySelector("#commit-import-button").addEventListener("click", commitImport);
document.querySelector("#rework-form").addEventListener("submit", submitRework);
document.querySelector("#compare-selected-button").addEventListener("click", openComparison);
document.querySelector("#export-assets-button").addEventListener("click", exportFinalAssets);
document.querySelector("#review-show-blocked").addEventListener("change", (event) => {
  state.reviewShowBlocked = event.target.checked;
  renderReview();
});
document.querySelector("#asset-search").addEventListener("input", (event) => {
  state.assetQuery = event.target.value;
  renderAssets();
});
document.querySelector("#import-file").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  if (file.size > 2_000_000) {
    showToast("导入文件不能超过 2 MB。", "error");
    return;
  }
  try {
    document.querySelector("#import-json").value = await file.text();
    showToast(`已读取 ${file.name}，请点击“预检数据”。`);
  } catch (error) {
    showToast(`无法读取文件：${error.message}`, "error");
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
    return;
  }
  if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;
  const dialog = document.querySelector("#image-dialog");
  if (!dialog.open || !productionImageById(state.currentImageId)) return;
  const images = productionImages().filter(
    (image) =>
      state.reviewShowBlocked ||
      !["qc_blocked", "needs_manual_qc"].includes(image.status),
  );
  const currentIndex = images.findIndex((image) => image.id === state.currentImageId);
  if (["j", "J", "ArrowRight"].includes(event.key) && images.length) {
    event.preventDefault();
    openProductionImage(images[(currentIndex + 1) % images.length].id);
  } else if (["k", "K", "ArrowLeft"].includes(event.key) && images.length) {
    event.preventDefault();
    openProductionImage(images[(currentIndex - 1 + images.length) % images.length].id);
  } else if (["s", "S"].includes(event.key)) {
    sendReviewDecision(state.currentImageId, "selected");
  } else if (["x", "X"].includes(event.key)) {
    sendReviewDecision(state.currentImageId, "rejected");
  } else if (["f", "F"].includes(event.key)) {
    sendReviewDecision(state.currentImageId, "final_candidate");
  } else if (["r", "R"].includes(event.key)) {
    openRework(state.currentImageId);
  }
});

const initialView = location.hash.slice(1);
switchView(viewMeta[initialView] ? initialView : "overview");
loadData();

setInterval(async () => {
  const hasActiveBatch = state.sop.batches.some((batch) =>
    ["queued", "running"].includes(batch.status),
  );
  const hasLegacyJob = state.legacy.jobs.some((job) =>
    ["queued", "running"].includes(job.status),
  );
  if (!hasActiveBatch && !hasLegacyJob) return;
  try {
    await loadData({ quiet: true });
  } catch (error) {
    console.error(error);
  }
}, 2500);
