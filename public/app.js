const state = {
  poems: [], styles: [], projects: [], images: [], jobs: [], config: null,
  activeProjectId: null, currentImageId: null, generationMode: "explore",
  styleFilter: "all", decisionFilter: "all", projectFilter: "active",
  knownTerminalJobs: new Set(), pollTimer: null,
};

const viewMeta = {
  today: { eyebrow: "今日创作台", title: "从方向到成品，每一步都有依据" },
  projects: { eyebrow: "系列管理", title: "先定义边界，再建立一致性" },
  poems: { eyebrow: "内容中心", title: "从诗句中提炼视觉命题" },
  styles: { eyebrow: "视觉资产", title: "把风格变成可复用的基线" },
  gallery: { eyebrow: "美术评审", title: "做决策，而不只是收藏图片" },
};
const statusNames = { queued: "等待生成", running: "生成中", completed: "已完成", failed: "生成失败" };
const decisionNames = { candidate: "待评审", selected: "已入选", rejected: "已淘汰", final: "可交付" };
const feedbackLabels = ["增加留白", "缩小主体", "修正服饰", "统一色彩", "降低饱和", "增强诗意", "减少现代感", "简化背景"];
const qcLabels = {
  poem_relevance: "诗意与核心意象准确",
  period_accuracy: "时代、服饰与器物可信",
  series_consistency: "人物与系列风格一致",
  visual_integrity: "结构完整，无乱码和畸形",
  layout_safety: "标题与诗文排版空间安全",
};

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}
async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
}
function getPoem(id) { return state.poems.find((item) => item.id === id); }
function getStyle(id) { return state.styles.find((item) => item.id === id); }
function getProject(id) { return state.projects.find((item) => item.id === id); }
function getActiveProject() { return getProject(state.activeProjectId) || state.projects[0]; }
function activeImages() { return state.images.filter((item) => item.project_id === getActiveProject()?.id && !item.hidden); }
function formatDate(value) {
  const date = value ? new Date(value) : new Date();
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast${type === "error" ? " is-error" : ""}`;
  toast.textContent = message;
  document.querySelector("#toast-region").append(toast);
  window.setTimeout(() => { toast.classList.add("is-leaving"); window.setTimeout(() => toast.remove(), 200); }, 3200);
}
function projectMetrics(project) {
  const images = state.images.filter((item) => item.project_id === project.id && !item.hidden);
  const finals = images.filter((item) => item.decision === "final");
  return {
    images, candidates: images.filter((item) => item.decision === "candidate").length,
    selected: images.filter((item) => item.decision === "selected").length,
    rejected: images.filter((item) => item.decision === "rejected").length,
    finals: finals.length, covered: new Set(images.map((item) => item.poem_id)).size,
    completedPoems: new Set(finals.map((item) => item.poem_id)).size,
  };
}

function renderProvider() {
  const live = Boolean(state.config?.live_generation);
  document.querySelector("#provider-card").classList.toggle("is-demo", !live);
  document.querySelector("#provider-label").textContent = live ? `${state.config.model} · 已连接` : "本地演示引擎";
  document.querySelector("#generate-mode-copy").textContent = live ? `使用 ${state.config.model}，结果会进入评审队列` : "离线演示模式 · 生成结果保存在本机";
}
function renderProjectSwitcher() {
  const select = document.querySelector("#project-switcher");
  select.innerHTML = state.projects.map((project) => `<option value="${escapeHtml(project.id)}">${escapeHtml(project.name)}</option>`).join("");
  select.value = getActiveProject()?.id || "";
}
function renderProjectHero() {
  const project = getActiveProject(); if (!project) return;
  const metrics = projectMetrics(project); const total = project.poem_ids.length;
  const progress = total ? Math.round(metrics.completedPoems / total * 100) : 0;
  document.querySelector("#active-project-name").textContent = project.name;
  document.querySelector("#active-project-brief").textContent = `${project.purpose} · ${total} 首诗 · ${getStyle(project.style_id)?.name || "待定风格"}`;
  document.querySelector("#project-progress-bar").style.width = `${progress}%`;
  document.querySelector("#project-progress-copy").textContent = `${metrics.completedPoems} / ${total} 首完成`;
  document.querySelector("#project-style-name").textContent = getStyle(project.style_id)?.name || "待定风格";
}
function renderStats() {
  const project = getActiveProject(); if (!project) return;
  const metrics = projectMetrics(project);
  document.querySelector("#stat-candidates").textContent = metrics.candidates;
  document.querySelector("#stat-selected").textContent = metrics.selected;
  document.querySelector("#stat-poems").textContent = metrics.covered;
  document.querySelector("#stat-final").textContent = metrics.finals;
}
function renderSop() {
  const project = getActiveProject(); if (!project) return;
  const metrics = projectMetrics(project); const total = project.poem_ids.length;
  const hasBaseline = metrics.selected + metrics.finals > 0;
  const convergedPoems = new Set(metrics.images.filter((item) => item.generation_mode === "converge" && ["selected", "final"].includes(item.decision)).map((item) => item.poem_id)).size;
  const stages = [
    { n: "01", title: "定义项目", output: "用途与边界", done: true, action: "查看项目", view: "projects" },
    { n: "02", title: "建立基线", output: "1 张代表性入选图", done: hasBaseline, action: "评审候选", view: metrics.candidates ? "gallery" : "today" },
    { n: "03", title: "方向探索", output: `${metrics.covered} / ${total} 首已探索`, done: metrics.covered >= total, action: "生成探索", view: "today" },
    { n: "04", title: "收敛迭代", output: `${convergedPoems} / ${total} 首有收敛版本`, done: convergedPoems >= total, action: "开始收敛", view: "gallery" },
    { n: "05", title: "质检交付", output: `${metrics.completedPoems} / ${total} 首可交付`, done: metrics.completedPoems >= total, action: "检查作品", view: "gallery" },
  ];
  const firstIncomplete = stages.findIndex((item) => !item.done);
  const activeIndex = firstIncomplete === -1 ? stages.length - 1 : firstIncomplete;
  document.querySelector("#sop-rail").innerHTML = stages.map((stage, index) => `<article class="sop-step${stage.done ? " is-done" : ""}${index === activeIndex ? " is-current" : ""}"><div class="sop-step-top"><span>${stage.done ? "✓" : stage.n}</span><small>${stage.done ? "已完成" : index === activeIndex ? "当前阶段" : "待开始"}</small></div><h3>${stage.title}</h3><p>${stage.output}</p><button type="button" data-switch-view="${stage.view}" ${index > activeIndex + 1 ? "disabled" : ""}>${stage.action} →</button></article>`).join("");
}

function renderPoemSelect() {
  const project = getActiveProject(); const select = document.querySelector("#poem-select");
  const allowed = project ? state.poems.filter((poem) => project.poem_ids.includes(poem.id)) : state.poems;
  const previous = select.value;
  select.innerHTML = allowed.map((poem) => `<option value="${escapeHtml(poem.id)}">${escapeHtml(poem.title)} · ${escapeHtml(poem.author)}</option>`).join("");
  if (allowed.some((item) => item.id === previous)) select.value = previous;
  updatePoemPreview();
}
function renderStyleSelects() {
  const project = getActiveProject();
  for (const id of ["style-select", "project-style"]) {
    const select = document.querySelector(`#${id}`); if (!select) continue;
    select.innerHTML = state.styles.map((style) => `<option value="${escapeHtml(style.id)}">${escapeHtml(style.name)}</option>`).join("");
  }
  if (project) { document.querySelector("#style-select").value = project.style_id; document.querySelector("#ratio-select").value = project.aspect_ratio; }
  updateDirectionVisual();
}
function updatePoemPreview() {
  const poem = getPoem(document.querySelector("#poem-select").value); if (!poem) return;
  document.querySelector("#poem-preview").innerHTML = `<div class="poem-preview-lines">${poem.lines.map(escapeHtml).join("<br>")}</div><div class="poem-preview-meta"><strong>${escapeHtml(poem.theme)} · ${escapeHtml(poem.mood)}</strong>${escapeHtml(poem.visual_brief)}</div>`;
  document.querySelector("#direction-title").textContent = poem.imagery.slice(0, 2).join(" · ");
  document.querySelector("#direction-copy").textContent = poem.visual_brief;
  document.querySelector("#direction-tags").innerHTML = poem.imagery.slice(0, 4).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
  renderParentOptions();
}
function updateDirectionVisual() {
  const style = getStyle(document.querySelector("#style-select")?.value); const visual = document.querySelector("#art-direction-visual");
  if (!style || !visual) return;
  visual.style.background = `linear-gradient(180deg, ${style.palette[0]} 0%, ${style.palette[1]} 62%, ${style.foreground} 100%)`;
  visual.querySelector(".visual-moon").style.background = style.accent;
  visual.querySelector(".ridge-one").style.background = style.palette[2] || style.foreground;
  visual.querySelector(".ridge-two").style.background = style.foreground;
}
function renderParentOptions(preferredId = null) {
  const poemId = document.querySelector("#poem-select")?.value;
  const options = activeImages().filter((image) => image.poem_id === poemId && ["selected", "final"].includes(image.decision) && (!state.config?.live_generation || /\.(png|jpe?g|webp)$/i.test(image.url)));
  const select = document.querySelector("#parent-image-select"); if (!select) return;
  select.innerHTML = `<option value="">${options.length ? "请选择父候选" : "暂无入选候选，请先完成探索评审"}</option>${options.map((image, index) => `<option value="${image.id}">${decisionNames[image.decision]} · ${image.style_name} · 版本 ${index + 1}</option>`).join("")}`;
  if (preferredId && options.some((item) => item.id === preferredId)) select.value = preferredId;
}
function updateModeUI(mode = state.generationMode) {
  state.generationMode = mode;
  const converge = mode === "converge";
  document.querySelector("#converge-fieldset").hidden = !converge;
  document.querySelector("#settings-step-number").textContent = converge ? "03" : "02";
  document.querySelector("#direction-label").innerHTML = converge ? "明确修改项 <small>必填</small>" : "补充探索要求 <small>可选</small>";
  document.querySelector("#custom-note").placeholder = converge ? "例如：保留人物与光影，只缩小月亮并增加江面留白…" : "例如：同一诗意分别尝试远景、主体叙事与意象留白…";
  document.querySelector("#generate-button-title").textContent = converge ? "生成收敛版本" : "生成探索候选";
  document.querySelector("#art-director-note").textContent = converge ? "收敛阶段只处理已说明的问题，避免同时改变构图、人物与色彩。" : "探索阶段应优先拉开构图差异，不追求微小细节变化。";
}

function artCardMarkup(image) {
  const mode = image.generation_mode === "converge" ? "收敛" : "探索";
  return `<article class="art-card decision-${escapeHtml(image.decision || "candidate")}" data-image-id="${image.id}"><button class="art-card-image" type="button" data-open-image="${image.id}" aria-label="评审${escapeHtml(image.poem_title)}候选"><span class="art-card-badge">${mode} · ${escapeHtml(image.style_name)}</span><img src="${escapeHtml(image.url)}" alt="${escapeHtml(image.poem_title)}插图" loading="lazy" /></button><div class="decision-strip"><span>${decisionNames[image.decision] || "待评审"}</span>${image.parent_image_id ? "<small>衍生版本</small>" : "<small>初始候选</small>"}</div><div class="art-card-copy"><div><h3>${escapeHtml(image.poem_title)}</h3><p>${escapeHtml(image.author)} · ${escapeHtml(image.project_name || "默认项目")}</p></div><time>${formatDate(image.created_at)}</time></div></article>`;
}
function renderRecent() {
  const images = activeImages().filter((item) => item.decision === "candidate").slice(0, 6);
  document.querySelector("#recent-grid").innerHTML = images.length ? images.map(artCardMarkup).join("") : `<div class="inline-empty">当前没有待评审候选。可以开始新一轮探索，或去画廊查看已入选作品。</div>`;
}
function renderGallery() {
  const activeProjectId = getActiveProject()?.id;
  const images = state.images.filter((image) => {
    if (image.hidden) return false;
    if (state.projectFilter === "active" && image.project_id !== activeProjectId) return false;
    if (state.projectFilter !== "active" && state.projectFilter !== "all" && image.project_id !== state.projectFilter) return false;
    if (state.decisionFilter !== "all" && image.decision !== state.decisionFilter) return false;
    if (state.styleFilter !== "all" && image.style_id !== state.styleFilter) return false;
    return true;
  });
  document.querySelector("#gallery-grid").innerHTML = images.map(artCardMarkup).join("");
  document.querySelector("#gallery-empty").hidden = images.length > 0;
  const counts = Object.fromEntries(Object.keys(decisionNames).map((key) => [key, images.filter((item) => item.decision === key).length]));
  document.querySelector("#review-summary").innerHTML = `<span>当前结果 <strong>${images.length}</strong></span><span>待评审 <strong>${counts.candidate}</strong></span><span>已入选 <strong>${counts.selected}</strong></span><span>可交付 <strong>${counts.final}</strong></span>`;
}
function renderGalleryFilters() {
  document.querySelector("#gallery-project-filter").innerHTML = `<option value="active">当前项目</option><option value="all">全部项目</option>${state.projects.map((project) => `<option value="${project.id}">${escapeHtml(project.name)}</option>`).join("")}`;
  document.querySelector("#gallery-project-filter").value = state.projectFilter;
  document.querySelector("#gallery-style-filter").innerHTML = `<option value="all">全部风格</option>${state.styles.map((style) => `<option value="${style.id}">${escapeHtml(style.short_name)}</option>`).join("")}`;
  document.querySelector("#gallery-style-filter").value = state.styleFilter;
}
function renderPoemLibrary(query = "") {
  const project = getActiveProject(); const normalized = query.trim().toLowerCase();
  const images = activeImages();
  const poems = state.poems.filter((poem) => !normalized || [poem.title, poem.author, poem.theme, poem.mood, ...poem.imagery].join(" ").toLowerCase().includes(normalized));
  document.querySelector("#poem-library").innerHTML = poems.map((poem, index) => {
    const poemImages = images.filter((image) => image.poem_id === poem.id); const final = poemImages.some((image) => image.decision === "final");
    const inProject = project?.poem_ids.includes(poem.id);
    return `<article class="poem-card"><div class="poem-card-content"><span class="poem-card-number">POEM ${String(index + 1).padStart(2, "0")} · ${inProject ? (final ? "已完成" : `${poemImages.length} 张候选`) : "未加入项目"}</span><h3>${escapeHtml(poem.title)}</h3><p class="poem-card-author">${escapeHtml(poem.dynasty)} · ${escapeHtml(poem.author)}</p><div class="poem-card-lines">${poem.lines.slice(0, 4).map(escapeHtml).join("<br>")}</div><div class="poem-card-footer"><span class="theme-tag">${escapeHtml(poem.theme)}</span><button class="poem-create" type="button" data-create-poem="${poem.id}" ${inProject ? "" : "disabled"}>${inProject ? "为此诗作画 →" : "不在当前项目"}</button></div></div><div class="poem-visual" aria-hidden="true"></div></article>`;
  }).join("");
}
function renderStyleLibrary() {
  document.querySelector("#style-library").innerHTML = state.styles.map((style) => {
    const anchor = state.images.find((image) => image.style_id === style.id && image.provider === "sample") || state.images.find((image) => image.style_id === style.id);
    const visual = anchor ? `<div class="style-library-visual has-anchor"><img src="${escapeHtml(anchor.url)}" alt="${escapeHtml(style.name)}风格锚点" /></div>` : `<div class="style-library-visual" style="--style-bg:${style.background};--style-fg:${style.foreground};--style-accent:${style.accent};--style-mid:${style.palette[1]}"><span class="style-visual-ridge"></span></div>`;
    return `<article class="style-library-card">${visual}<div class="style-library-copy"><div class="style-title-row"><h3>${escapeHtml(style.name)}</h3>${getActiveProject()?.style_id === style.id ? "<span>当前基线</span>" : ""}</div><p>${escapeHtml(style.description)}</p><div class="style-specs"><span>色彩 ${style.paper === "night" ? "冷夜矿物色" : "低饱和"}</span><span>材质 ${escapeHtml(style.paper)} 纸感</span><span>禁用 文字与现代物</span></div><div class="palette-row">${style.palette.map((color) => `<span style="--color:${color}" title="${color}"></span>`).join("")}</div><button class="style-card-action" type="button" data-create-style="${style.id}">用此基线探索 <span>→</span></button></div></article>`;
  }).join("");
}
function renderProjects() {
  document.querySelector("#project-list").innerHTML = state.projects.map((project) => {
    const metrics = projectMetrics(project); const total = project.poem_ids.length; const pct = total ? Math.round(metrics.completedPoems / total * 100) : 0;
    return `<article class="project-card${project.id === getActiveProject()?.id ? " is-active" : ""}"><div class="project-card-main"><div class="project-card-head"><span>${project.id === getActiveProject()?.id ? "当前项目" : "系列项目"}</span><small>${formatDate(project.updated_at)}</small></div><h3>${escapeHtml(project.name)}</h3><p>${escapeHtml(project.purpose)} · ${total} 首诗 · ${escapeHtml(getStyle(project.style_id)?.short_name || "未定风格")}</p><div class="project-card-metrics"><span><strong>${metrics.candidates}</strong> 待评审</span><span><strong>${metrics.selected}</strong> 已入选</span><span><strong>${metrics.finals}</strong> 成品</span></div><div class="project-progress"><span style="width:${pct}%"></span></div></div><div class="project-card-side"><strong>${pct}%</strong><small>系列完成度</small><button type="button" data-activate-project="${project.id}">${project.id === getActiveProject()?.id ? "继续创作" : "切换项目"} →</button></div></article>`;
  }).join("");
}
function renderProjectForm() {
  document.querySelector("#project-style").innerHTML = state.styles.map((style) => `<option value="${style.id}">${escapeHtml(style.name)}</option>`).join("");
  document.querySelector("#project-poem-picker").innerHTML = `<span>选择诗词范围</span><div>${state.poems.map((poem) => `<label><input type="checkbox" value="${poem.id}" checked /><span>${escapeHtml(poem.title)}</span></label>`).join("")}</div>`;
}
function renderJobs() {
  const active = state.jobs.filter((job) => ["queued", "running"].includes(job.status));
  document.querySelector("#active-job-count").textContent = active.length; document.querySelector("#jobs-button").classList.toggle("has-active", active.length > 0);
  document.querySelector("#job-list").innerHTML = state.jobs.length ? state.jobs.map((job) => `<article class="job-card"><div class="job-card-head"><strong>${escapeHtml(job.poem_title)}</strong><span class="job-status ${job.status}">${statusNames[job.status] || job.status}</span></div><div class="job-card-meta"><span>${job.generation_mode === "converge" ? "收敛迭代" : "方向探索"} · ${escapeHtml(job.style_name)} · ${job.count} 张</span><span>${formatDate(job.created_at)}</span></div><div class="job-progress"><span style="--progress:${job.progress || 0}%"></span></div>${job.error ? `<p class="job-error">${escapeHtml(job.error)}</p>` : ""}</article>`).join("") : `<div class="job-list-empty">还没有生成任务。<br />从创作台开始第一轮探索吧。</div>`;
}
function renderAll() {
  renderProvider(); renderProjectSwitcher(); renderProjectHero(); renderStats(); renderSop(); renderPoemSelect(); renderStyleSelects(); renderRecent(); renderGalleryFilters(); renderGallery(); renderPoemLibrary(document.querySelector("#poem-search")?.value || ""); renderStyleLibrary(); renderProjects(); renderProjectForm(); renderJobs();
}

function switchView(view, options = {}) {
  if (!viewMeta[view]) view = "today";
  document.querySelectorAll("[data-view-panel]").forEach((panel) => panel.classList.toggle("is-active", panel.dataset.viewPanel === view));
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("is-active", button.dataset.view === view));
  document.querySelector("#page-eyebrow").textContent = viewMeta[view].eyebrow; document.querySelector("#page-title").textContent = viewMeta[view].title;
  if (!options.preserveHash) history.replaceState(null, "", `#${view}`); window.scrollTo({ top: 0, behavior: "auto" });
}
function jumpToGenerator(poemId, styleId, options = {}) {
  switchView("today");
  if (poemId && getPoem(poemId)) { document.querySelector("#poem-select").value = poemId; updatePoemPreview(); }
  if (styleId && getStyle(styleId)) { document.querySelector("#style-select").value = styleId; updateDirectionVisual(); }
  if (options.mode) { const radio = document.querySelector(`input[name="generation_mode"][value="${options.mode}"]`); if (radio) radio.checked = true; updateModeUI(options.mode); }
  if (options.parentImageId) renderParentOptions(options.parentImageId);
  requestAnimationFrame(() => document.querySelector("#generator").scrollIntoView({ behavior: "smooth", block: "start" }));
}
function activateProject(projectId) {
  if (!getProject(projectId)) return;
  state.activeProjectId = projectId; state.projectFilter = "active"; renderAll(); showToast(`已切换到「${getActiveProject().name}」`);
}

async function patchImage(imageId, updates, successMessage = "评审已保存") {
  const payload = await api(`/api/images/${imageId}`, { method: "PATCH", body: JSON.stringify(updates) });
  const index = state.images.findIndex((item) => item.id === imageId); if (index >= 0) state.images[index] = payload.image;
  renderProjectHero(); renderStats(); renderSop(); renderRecent(); renderGallery(); renderProjects();
  if (state.currentImageId === imageId && document.querySelector("#art-dialog").open) populateImageDialog(payload.image);
  showToast(successMessage); return payload.image;
}
function decisionBadgeMarkup(image) { return `${decisionNames[image.decision] || "待评审"}`; }
function populateImageDialog(image) {
  const poem = getPoem(image.poem_id); if (!poem) return;
  document.querySelector("#dialog-image").src = image.url; document.querySelector("#dialog-image").alt = `${image.poem_title}的${image.style_name}插图`;
  document.querySelector("#dialog-style").textContent = `${escapeHtml(image.project_name)} · ${escapeHtml(image.style_name)}`;
  document.querySelector("#dialog-title").textContent = image.poem_title; document.querySelector("#dialog-author").textContent = `${poem.dynasty} · ${image.author}`;
  document.querySelector("#dialog-poem").innerHTML = poem.lines.map(escapeHtml).join("<br>");
  const badge = document.querySelector("#dialog-decision"); badge.textContent = decisionBadgeMarkup(image); badge.className = `decision-badge decision-${image.decision}`;
  const children = state.images.filter((item) => item.parent_image_id === image.id).length;
  document.querySelector("#dialog-lineage").innerHTML = image.parent_image_id ? `<span>衍生版本</span><strong>来自已入选父候选</strong>` : `<span>初始候选</span><strong>${children ? `已有 ${children} 个衍生版本` : "尚未创建衍生版本"}</strong>`;
  document.querySelector("#dialog-meta").innerHTML = `<span>${image.generation_mode === "converge" ? "收敛迭代" : "方向探索"}</span><span>${image.provider === "openai" ? "AI 生成" : image.provider === "sample" ? "AI 风格样图" : "演示渲染"}</span><span>${escapeHtml(image.aspect_ratio)}</span><span>${formatDate(image.created_at)}</span>`;
  document.querySelector("#feedback-options").innerHTML = feedbackLabels.map((label) => `<label><input type="checkbox" value="${label}" ${(image.feedback_tags || []).includes(label) ? "checked" : ""} /><span>${label}</span></label>`).join("");
  document.querySelector("#review-note").value = image.review_note || "";
  document.querySelector("#qc-list").innerHTML = Object.entries(qcLabels).map(([key, label]) => `<label><input type="checkbox" data-qc-key="${key}" ${image.qc?.[key] ? "checked" : ""} /><span>${label}</span></label>`).join("");
  const allQc = Object.keys(qcLabels).every((key) => image.qc?.[key]); const finalButton = document.querySelector("#finalize-image");
  finalButton.disabled = !allQc && image.decision !== "final"; finalButton.textContent = image.decision === "final" ? "✓ 已通过质检，可交付" : allQc ? "标记为可交付成品" : `完成 ${Object.keys(qcLabels).filter((key) => image.qc?.[key]).length} / 5 项质检`;
  document.querySelector("#dialog-prompt").textContent = image.prompt;
  const download = document.querySelector("#dialog-download"); download.href = image.url; download.download = `${image.poem_title}-${image.style_name}.${image.url.split(".").pop()}`;
}
function openImageDialog(imageId) {
  const image = state.images.find((item) => item.id === imageId); if (!image) return;
  state.currentImageId = imageId; populateImageDialog(image); document.querySelector("#art-dialog").showModal();
}
async function saveReview() {
  if (!state.currentImageId) return;
  const feedback_tags = [...document.querySelectorAll("#feedback-options input:checked")].map((item) => item.value);
  const review_note = document.querySelector("#review-note").value;
  try { await patchImage(state.currentImageId, { feedback_tags, review_note }, "评审意见已保存"); } catch (error) { showToast(error.message, "error"); }
}
async function setDecision(decision) {
  if (!state.currentImageId) return;
  try { await patchImage(state.currentImageId, { decision }, decision === "selected" ? "已入选，可继续收敛" : "候选已淘汰"); } catch (error) { showToast(error.message, "error"); }
}
async function saveQc() {
  if (!state.currentImageId) return;
  const qc = {}; document.querySelectorAll("#qc-list input").forEach((item) => { qc[item.dataset.qcKey] = item.checked; });
  try { await patchImage(state.currentImageId, { qc }, "质检进度已保存"); } catch (error) { showToast(error.message, "error"); }
}
async function finalizeCurrentImage() {
  if (!state.currentImageId) return;
  const image = state.images.find((item) => item.id === state.currentImageId); if (!image) return;
  if (!Object.keys(qcLabels).every((key) => image.qc?.[key])) { showToast("请先完成全部五项质检。", "error"); return; }
  try { await patchImage(image.id, { decision: "final" }, "已标记为可交付成品"); } catch (error) { showToast(error.message, "error"); }
}
function iterateCurrentImage() {
  const image = state.images.find((item) => item.id === state.currentImageId); if (!image) return;
  document.querySelector("#art-dialog").close(); jumpToGenerator(image.poem_id, image.style_id, { mode: "converge", parentImageId: image.id });
  if (image.review_note) document.querySelector("#custom-note").value = image.review_note;
}
async function exportPoemCard() {
  const image = state.images.find((item) => item.id === state.currentImageId); const poem = image && getPoem(image.poem_id); if (!image || !poem) return;
  const source = new Image(); source.src = image.url; await source.decode();
  const canvas = document.createElement("canvas"); canvas.width = 1400; canvas.height = 2000; const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#f3eee3"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  const imageBox = { x: 90, y: 90, w: 1220, h: 1280 }; const scale = Math.min(imageBox.w / source.width, imageBox.h / source.height); const w = source.width * scale; const h = source.height * scale;
  ctx.drawImage(source, imageBox.x + (imageBox.w - w) / 2, imageBox.y + (imageBox.h - h) / 2, w, h);
  ctx.fillStyle = "#1d2926"; ctx.font = "600 74px KaiTi, STKaiti, serif"; ctx.fillText(poem.title, 110, 1510);
  ctx.fillStyle = "#7f837c"; ctx.font = "32px system-ui"; ctx.fillText(`${poem.dynasty} · ${poem.author}`, 112, 1570);
  ctx.fillStyle = "#354c46"; ctx.font = "42px KaiTi, STKaiti, serif"; poem.lines.forEach((line, index) => ctx.fillText(line, 112, 1660 + index * 62));
  ctx.fillStyle = "#b24a3b"; ctx.fillRect(1240, 1490, 55, 55); ctx.fillStyle = "#fff"; ctx.font = "36px KaiTi"; ctx.fillText("绘", 1249, 1532);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png")); const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `${poem.title}-诗画卡.png`; link.click(); URL.revokeObjectURL(link.href); showToast("诗画卡已导出");
}

function openJobs() { const drawer = document.querySelector("#jobs-drawer"); const scrim = document.querySelector("#drawer-scrim"); scrim.hidden = false; requestAnimationFrame(() => { drawer.classList.add("is-open"); drawer.setAttribute("aria-hidden", "false"); scrim.classList.add("is-visible"); }); }
function closeJobs() { const drawer = document.querySelector("#jobs-drawer"); const scrim = document.querySelector("#drawer-scrim"); drawer.classList.remove("is-open"); drawer.setAttribute("aria-hidden", "true"); scrim.classList.remove("is-visible"); window.setTimeout(() => { if (!scrim.classList.contains("is-visible")) scrim.hidden = true; }, 190); }
async function refreshImages() { const payload = await api("/api/images"); state.images = payload.images; renderProjectHero(); renderStats(); renderSop(); renderRecent(); renderGallery(); renderProjects(); }
async function refreshJobs() {
  try { const payload = await api("/api/jobs"); state.jobs = payload.jobs; let refresh = false;
    for (const job of state.jobs) if (["completed", "failed"].includes(job.status) && !state.knownTerminalJobs.has(job.id)) { state.knownTerminalJobs.add(job.id); if (job.status === "completed") { refresh = true; showToast(`${job.poem_title} · ${job.generation_mode === "converge" ? "收敛版本" : "探索候选"}已完成`); } else showToast(`${job.poem_title}生成失败：${job.error || "未知错误"}`, "error"); }
    renderJobs(); if (refresh) await refreshImages(); schedulePolling();
  } catch (error) { showToast(`任务状态更新失败：${error.message}`, "error"); }
}
function schedulePolling() { if (state.pollTimer) window.clearTimeout(state.pollTimer); const active = state.jobs.some((job) => ["queued", "running"].includes(job.status)); state.pollTimer = window.setTimeout(refreshJobs, active ? 1200 : 6000); }
async function submitGeneration(event) {
  event.preventDefault(); const button = document.querySelector("#generate-button"); const customNote = document.querySelector("#custom-note").value.trim(); const parentId = document.querySelector("#parent-image-select").value;
  if (state.generationMode === "converge" && !parentId) { showToast("收敛迭代必须选择一张已入选父候选。", "error"); return; }
  if (state.generationMode === "converge" && !customNote) { showToast("请明确填写本轮需要修改的问题。", "error"); return; }
  const payload = { project_id: getActiveProject().id, poem_id: document.querySelector("#poem-select").value, style_id: document.querySelector("#style-select").value, aspect_ratio: document.querySelector("#ratio-select").value, count: Number(document.querySelector("#count-select").value), custom_note: customNote, generation_mode: state.generationMode, parent_image_id: parentId || null, preserve: [...document.querySelectorAll("#preserve-options input:checked")].map((item) => item.value) };
  button.disabled = true; try { const result = await api("/api/generate", { method: "POST", body: JSON.stringify(payload) }); state.jobs.unshift(result.job); renderJobs(); openJobs(); showToast(state.generationMode === "converge" ? "收敛任务已创建" : "探索任务已创建"); schedulePolling(); } catch (error) { showToast(error.message, "error"); } finally { button.disabled = false; }
}
async function submitProject(event) {
  event.preventDefault(); const poem_ids = [...document.querySelectorAll("#project-poem-picker input:checked")].map((item) => item.value); if (!poem_ids.length) { showToast("请至少选择一首诗。", "error"); return; }
  try { const result = await api("/api/projects", { method: "POST", body: JSON.stringify({ name: document.querySelector("#project-name").value, purpose: document.querySelector("#project-purpose").value, aspect_ratio: document.querySelector("#project-ratio").value, style_id: document.querySelector("#project-style").value, poem_ids }) }); state.projects.push(result.project); state.activeProjectId = result.project.id; document.querySelector("#project-dialog").close(); event.currentTarget.reset(); renderAll(); switchView("today"); showToast("项目已创建，先用代表诗建立风格基线"); } catch (error) { showToast(error.message, "error"); }
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  document.querySelectorAll("[data-jump-to-generator]").forEach((button) => button.addEventListener("click", () => jumpToGenerator()));
  document.addEventListener("click", (event) => {
    const open = event.target.closest("[data-open-image]"); if (open) openImageDialog(open.dataset.openImage);
    const poem = event.target.closest("[data-create-poem]"); if (poem && !poem.disabled) jumpToGenerator(poem.dataset.createPoem);
    const style = event.target.closest("[data-create-style]"); if (style) jumpToGenerator(null, style.dataset.createStyle);
    const view = event.target.closest("[data-switch-view]"); if (view && !view.disabled) switchView(view.dataset.switchView);
    const project = event.target.closest("[data-activate-project]"); if (project) { activateProject(project.dataset.activateProject); switchView("today"); }
    const decision = event.target.closest("[data-set-decision]"); if (decision) setDecision(decision.dataset.setDecision);
  });
  document.querySelector("#project-switcher").addEventListener("change", (event) => activateProject(event.target.value));
  document.querySelector("#poem-select").addEventListener("change", updatePoemPreview); document.querySelector("#style-select").addEventListener("change", updateDirectionVisual);
  document.querySelectorAll('input[name="generation_mode"]').forEach((radio) => radio.addEventListener("change", () => updateModeUI(radio.value)));
  document.querySelector("#generator-form").addEventListener("submit", submitGeneration); document.querySelector("#poem-search").addEventListener("input", (event) => renderPoemLibrary(event.target.value));
  document.querySelector("#gallery-project-filter").addEventListener("change", (event) => { state.projectFilter = event.target.value; renderGallery(); }); document.querySelector("#gallery-decision-filter").addEventListener("change", (event) => { state.decisionFilter = event.target.value; renderGallery(); }); document.querySelector("#gallery-style-filter").addEventListener("change", (event) => { state.styleFilter = event.target.value; renderGallery(); });
  document.querySelector("#jobs-button").addEventListener("click", openJobs); document.querySelector("#close-jobs").addEventListener("click", closeJobs); document.querySelector("#drawer-scrim").addEventListener("click", closeJobs);
  document.querySelector("#dialog-close").addEventListener("click", () => document.querySelector("#art-dialog").close()); document.querySelector("#art-dialog").addEventListener("click", (event) => { if (event.target === event.currentTarget) event.currentTarget.close(); });
  document.querySelector("#save-review").addEventListener("click", saveReview); document.querySelector("#qc-list").addEventListener("change", saveQc); document.querySelector("#finalize-image").addEventListener("click", finalizeCurrentImage); document.querySelector("#dialog-iterate").addEventListener("click", iterateCurrentImage); document.querySelector("#dialog-export-card").addEventListener("click", exportPoemCard);
  document.querySelector("#new-project-button").addEventListener("click", () => document.querySelector("#project-dialog").showModal()); document.querySelector("#project-dialog-close").addEventListener("click", () => document.querySelector("#project-dialog").close()); document.querySelector("#project-form").addEventListener("submit", submitProject);
  document.querySelector("#open-help").addEventListener("click", () => document.querySelector("#help-dialog").showModal()); document.querySelector("#help-close").addEventListener("click", () => document.querySelector("#help-dialog").close());
  window.addEventListener("hashchange", () => switchView(location.hash.slice(1), { preserveHash: true }));
  window.addEventListener("keydown", (event) => { if (event.key === "Escape") closeJobs(); const dialog = document.querySelector("#art-dialog"); if (!dialog.open || ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) return; if (event.key === "1") setDecision("selected"); if (event.key === "2") setDecision("rejected"); });
}
async function initialize() {
  try { const payload = await api("/api/bootstrap"); Object.assign(state, { poems: payload.poems, styles: payload.styles, projects: payload.projects, images: payload.images, jobs: payload.jobs, config: payload.config }); state.activeProjectId = payload.projects[0]?.id || null; for (const job of state.jobs) if (["completed", "failed"].includes(job.status)) state.knownTerminalJobs.add(job.id); renderAll(); bindEvents(); updateModeUI("explore"); switchView(location.hash.slice(1) || "today", { preserveHash: true }); schedulePolling(); }
  catch (error) { document.body.innerHTML = `<main style="max-width:720px;margin:12vh auto;padding:32px;font-family:system-ui;color:#1d2926"><h1>无法连接唐诗绘卷本地服务</h1><p>${escapeHtml(error.message)}</p><p>请确认已运行 <code>python3 server.py</code>。</p></main>`; }
}
initialize();
