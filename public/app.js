const state = {
  poems: [],
  styles: [],
  images: [],
  jobs: [],
  config: null,
  selectedStyleId: null,
  currentImageId: null,
  favoriteOnly: false,
  styleFilter: "all",
  knownTerminalJobs: new Set(),
  pollTimer: null,
};

const viewMeta = {
  today: { eyebrow: "今日工作台", title: "把诗意，变成一套画" },
  poems: { eyebrow: "内容中心", title: "从诗句中寻找画面" },
  styles: { eyebrow: "视觉资产", title: "建立属于唐诗的风格语言" },
  gallery: { eyebrow: "作品资产", title: "让灵感有迹可循" },
};

const statusNames = {
  queued: "等待生成",
  running: "生成中",
  completed: "已完成",
  failed: "生成失败",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `请求失败（${response.status}）`);
  }
  return payload;
}

function getPoem(id) {
  return state.poems.find((poem) => poem.id === id);
}

function getStyle(id) {
  return state.styles.find((style) => style.id === id);
}

function formatDate(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "刚刚";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function showToast(message, type = "success") {
  const region = document.querySelector("#toast-region");
  const toast = document.createElement("div");
  toast.className = `toast${type === "error" ? " is-error" : ""}`;
  toast.textContent = message;
  region.append(toast);
  window.setTimeout(() => {
    toast.classList.add("is-leaving");
    window.setTimeout(() => toast.remove(), 200);
  }, 3600);
}

function renderProvider() {
  const live = Boolean(state.config?.live_generation);
  const label = document.querySelector("#provider-label");
  const card = document.querySelector("#provider-card");
  const modeCopy = document.querySelector("#generate-mode-copy");
  card.classList.toggle("is-demo", !live);
  label.textContent = live ? `${state.config.model} · 已连接` : "本地演示引擎";
  modeCopy.textContent = live
    ? `使用 ${state.config.model} 真实生成，可能产生 API 费用`
    : "当前为离线演示模式，不产生 API 费用";
}

function renderStats() {
  const visible = state.images.filter((image) => !image.hidden);
  const coveredPoems = new Set(visible.map((image) => image.poem_id));
  const favorites = visible.filter((image) => image.favorite).length;
  document.querySelector("#stat-images").textContent = visible.length;
  document.querySelector("#stat-poems").textContent = coveredPoems.size;
  document.querySelector("#stat-styles").textContent = state.styles.length;
  document.querySelector("#stat-favorites").textContent = favorites;
}

function renderPoemSelect() {
  const select = document.querySelector("#poem-select");
  select.innerHTML = state.poems
    .map(
      (poem) =>
        `<option value="${escapeHtml(poem.id)}">${escapeHtml(poem.title)} · ${escapeHtml(poem.author)}</option>`,
    )
    .join("");
  updatePoemPreview();
}

function updatePoemPreview() {
  const poemId = document.querySelector("#poem-select").value;
  const poem = getPoem(poemId);
  if (!poem) return;
  const preview = document.querySelector("#poem-preview");
  preview.innerHTML = `
    <div class="poem-preview-lines">${poem.lines.map(escapeHtml).join("<br>")}</div>
    <div class="poem-preview-meta">
      <strong>${escapeHtml(poem.theme)} · ${escapeHtml(poem.mood)}</strong>
      ${escapeHtml(poem.visual_brief)}
    </div>`;
  document.querySelector("#direction-title").textContent = poem.imagery.slice(0, 2).join(" · ");
  document.querySelector("#direction-copy").textContent = poem.visual_brief;
  document.querySelector("#direction-tags").innerHTML = poem.imagery
    .slice(0, 4)
    .map((tag) => `<span>${escapeHtml(tag)}</span>`)
    .join("");
}

function renderStylePicker() {
  if (!state.selectedStyleId && state.styles.length) {
    state.selectedStyleId = state.styles[0].id;
  }
  const picker = document.querySelector("#style-picker");
  picker.innerHTML = state.styles
    .map(
      (style) => `
      <div class="style-option">
        <input
          type="radio"
          name="style_id"
          id="style-${escapeHtml(style.id)}"
          value="${escapeHtml(style.id)}"
          ${style.id === state.selectedStyleId ? "checked" : ""}
        />
        <label for="style-${escapeHtml(style.id)}">
          <span
            class="style-swatch"
            style="--swatch-bg:${style.background};--swatch-fg:${style.foreground};--swatch-accent:${style.accent}"
            aria-hidden="true"
          ></span>
          <span class="style-option-copy">
            <strong>${escapeHtml(style.short_name)}</strong>
            <small>${escapeHtml(style.palette.slice(0, 2).join(" · "))}</small>
          </span>
        </label>
      </div>`,
    )
    .join("");
  updateDirectionVisual();
}

function updateDirectionVisual() {
  const style = getStyle(state.selectedStyleId);
  const visual = document.querySelector("#art-direction-visual");
  if (!style || !visual) return;
  visual.style.background = `linear-gradient(180deg, ${style.palette[0]} 0%, ${style.palette[1]} 62%, ${style.foreground} 100%)`;
  visual.querySelector(".visual-moon").style.background = style.accent;
  visual.querySelector(".ridge-one").style.background = style.palette[2] || style.foreground;
  visual.querySelector(".ridge-two").style.background = style.foreground;
}

function artCardMarkup(image) {
  const providerLabel =
    image.provider === "openai" ? "AI 生成" : image.provider === "sample" ? "AI 风格样图" : "演示渲染";
  return `
    <article class="art-card" data-image-id="${escapeHtml(image.id)}">
      <button class="art-card-image" type="button" data-open-image="${escapeHtml(image.id)}" aria-label="查看${escapeHtml(image.poem_title)}大图">
        <span class="art-card-badge">${providerLabel} · ${escapeHtml(image.style_name)}</span>
        <img src="${escapeHtml(image.url)}" alt="${escapeHtml(image.poem_title)}的${escapeHtml(image.style_name)}插图" loading="lazy" />
      </button>
      <button class="card-favorite${image.favorite ? " is-favorite" : ""}" type="button" data-favorite-image="${escapeHtml(image.id)}" aria-label="${image.favorite ? "取消收藏" : "收藏"}">${image.favorite ? "♥" : "♡"}</button>
      <div class="art-card-copy">
        <div>
          <h3>${escapeHtml(image.poem_title)}</h3>
          <p>${escapeHtml(image.author)} · ${escapeHtml(image.style_name)}</p>
        </div>
        <time>${formatDate(image.created_at)}</time>
      </div>
    </article>`;
}

function renderRecent() {
  const images = state.images.filter((image) => !image.hidden).slice(0, 6);
  document.querySelector("#recent-grid").innerHTML = images.map(artCardMarkup).join("");
}

function renderGallery() {
  const images = state.images.filter((image) => {
    if (image.hidden) return false;
    if (state.favoriteOnly && !image.favorite) return false;
    if (state.styleFilter !== "all" && image.style_id !== state.styleFilter) return false;
    return true;
  });
  document.querySelector("#gallery-grid").innerHTML = images.map(artCardMarkup).join("");
  document.querySelector("#gallery-empty").hidden = images.length > 0;
}

function renderGalleryFilters() {
  const select = document.querySelector("#gallery-style-filter");
  select.innerHTML = `<option value="all">全部风格</option>${state.styles
    .map((style) => `<option value="${escapeHtml(style.id)}">${escapeHtml(style.short_name)}</option>`)
    .join("")}`;
  select.value = state.styleFilter;
}

function renderPoemLibrary(query = "") {
  const normalized = query.trim().toLowerCase();
  const poems = state.poems.filter((poem) => {
    const haystack = [poem.title, poem.author, poem.theme, poem.mood, ...poem.imagery]
      .join(" ")
      .toLowerCase();
    return !normalized || haystack.includes(normalized);
  });
  document.querySelector("#poem-library").innerHTML = poems
    .map(
      (poem, index) => `
      <article class="poem-card">
        <div class="poem-card-content">
          <span class="poem-card-number">POEM ${String(index + 1).padStart(2, "0")}</span>
          <h3>${escapeHtml(poem.title)}</h3>
          <p class="poem-card-author">${escapeHtml(poem.dynasty)} · ${escapeHtml(poem.author)}</p>
          <div class="poem-card-lines">${poem.lines.slice(0, 4).map(escapeHtml).join("<br>")}</div>
          <div class="poem-card-footer">
            <span class="theme-tag">${escapeHtml(poem.theme)}</span>
            <button class="poem-create" type="button" data-create-poem="${escapeHtml(poem.id)}">为此诗作画 →</button>
          </div>
        </div>
        <div class="poem-visual" aria-hidden="true"></div>
      </article>`,
    )
    .join("");
}

function renderStyleLibrary() {
  document.querySelector("#style-library").innerHTML = state.styles
    .map(
      (style) => `
      <article class="style-library-card">
        <div
          class="style-library-visual"
          style="--style-bg:${style.background};--style-fg:${style.foreground};--style-accent:${style.accent};--style-mid:${style.palette[1]}"
          aria-hidden="true"
        ><span class="style-visual-ridge"></span></div>
        <div class="style-library-copy">
          <h3>${escapeHtml(style.name)}</h3>
          <p>${escapeHtml(style.description)}</p>
          <div class="palette-row" aria-label="风格色板">
            ${style.palette.map((color) => `<span style="--color:${color}" title="${color}"></span>`).join("")}
          </div>
          <button class="style-card-action" type="button" data-create-style="${escapeHtml(style.id)}">
            使用此风格创作 <span>→</span>
          </button>
        </div>
      </article>`,
    )
    .join("");
}

function renderJobs() {
  const activeJobs = state.jobs.filter((job) => ["queued", "running"].includes(job.status));
  const button = document.querySelector("#jobs-button");
  document.querySelector("#active-job-count").textContent = activeJobs.length;
  button.classList.toggle("has-active", activeJobs.length > 0);
  const list = document.querySelector("#job-list");
  if (!state.jobs.length) {
    list.innerHTML = `<div class="job-list-empty">还没有生成任务。<br>从工作台创建第一组插图吧。</div>`;
    return;
  }
  list.innerHTML = state.jobs
    .map(
      (job) => `
      <article class="job-card">
        <div class="job-card-head">
          <strong>${escapeHtml(job.poem_title)}</strong>
          <span class="job-status ${escapeHtml(job.status)}">${statusNames[job.status] || escapeHtml(job.status)}</span>
        </div>
        <div class="job-card-meta">
          <span>${escapeHtml(job.style_name)} · ${job.count} 张</span>
          <span>${formatDate(job.created_at)}</span>
        </div>
        <div class="job-progress" aria-label="任务进度 ${job.progress || 0}%"><span style="--progress:${job.progress || 0}%"></span></div>
        ${job.error ? `<p class="job-error">${escapeHtml(job.error)}</p>` : ""}
      </article>`,
    )
    .join("");
}

function renderAll() {
  renderProvider();
  renderStats();
  renderRecent();
  renderGallery();
  renderPoemLibrary(document.querySelector("#poem-search")?.value || "");
  renderStyleLibrary();
  renderGalleryFilters();
  renderJobs();
}

function switchView(view, options = {}) {
  if (!viewMeta[view]) view = "today";
  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.viewPanel === view);
  });
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === view);
  });
  document.querySelector("#page-eyebrow").textContent = viewMeta[view].eyebrow;
  document.querySelector("#page-title").textContent = viewMeta[view].title;
  if (!options.preserveHash) history.replaceState(null, "", `#${view}`);
  window.scrollTo({ top: 0, behavior: "auto" });
}

function jumpToGenerator(poemId, styleId) {
  switchView("today");
  if (poemId && getPoem(poemId)) {
    document.querySelector("#poem-select").value = poemId;
    updatePoemPreview();
  }
  if (styleId && getStyle(styleId)) {
    state.selectedStyleId = styleId;
    renderStylePicker();
  }
  requestAnimationFrame(() => {
    document.querySelector("#generator").scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

async function toggleFavorite(imageId) {
  const image = state.images.find((item) => item.id === imageId);
  if (!image) return;
  const nextValue = !image.favorite;
  image.favorite = nextValue;
  renderStats();
  renderRecent();
  renderGallery();
  if (state.currentImageId === imageId) updateDialogFavorite(image);
  try {
    await api(`/api/images/${imageId}`, {
      method: "PATCH",
      body: JSON.stringify({ favorite: nextValue }),
    });
    showToast(nextValue ? "已收藏这张灵感" : "已取消收藏");
  } catch (error) {
    image.favorite = !nextValue;
    renderStats();
    renderRecent();
    renderGallery();
    if (state.currentImageId === imageId) updateDialogFavorite(image);
    showToast(error.message, "error");
  }
}

function updateDialogFavorite(image) {
  const button = document.querySelector("#dialog-favorite");
  button.classList.toggle("is-favorite", image.favorite);
  button.textContent = image.favorite ? "♥ 已收藏" : "♡ 收藏灵感";
}

function openImageDialog(imageId) {
  const image = state.images.find((item) => item.id === imageId);
  const poem = image ? getPoem(image.poem_id) : null;
  if (!image || !poem) return;
  state.currentImageId = imageId;
  document.querySelector("#dialog-image").src = image.url;
  document.querySelector("#dialog-image").alt = `${image.poem_title}的${image.style_name}插图`;
  document.querySelector("#dialog-style").textContent = image.style_name;
  document.querySelector("#dialog-title").textContent = image.poem_title;
  document.querySelector("#dialog-author").textContent = `${poem.dynasty} · ${image.author}`;
  document.querySelector("#dialog-poem").innerHTML = poem.lines.map(escapeHtml).join("<br>");
  document.querySelector("#dialog-meta").innerHTML = `
    <span>${image.provider === "openai" ? "OpenAI 生成" : image.provider === "sample" ? "内置 AI 风格样图" : "本地演示渲染"}</span>
    <span>${escapeHtml(image.aspect_ratio)}</span>
    <span>${formatDate(image.created_at)}</span>`;
  document.querySelector("#dialog-prompt").textContent = image.prompt;
  const download = document.querySelector("#dialog-download");
  download.href = image.url;
  download.download = `${image.poem_title}-${image.style_name}.${image.url.split(".").pop()}`;
  updateDialogFavorite(image);
  document.querySelector("#art-dialog").showModal();
}

function openJobs() {
  const drawer = document.querySelector("#jobs-drawer");
  const scrim = document.querySelector("#drawer-scrim");
  scrim.hidden = false;
  requestAnimationFrame(() => {
    drawer.classList.add("is-open");
    drawer.setAttribute("aria-hidden", "false");
    scrim.classList.add("is-visible");
  });
}

function closeJobs() {
  const drawer = document.querySelector("#jobs-drawer");
  const scrim = document.querySelector("#drawer-scrim");
  drawer.classList.remove("is-open");
  drawer.setAttribute("aria-hidden", "true");
  scrim.classList.remove("is-visible");
  window.setTimeout(() => {
    if (!scrim.classList.contains("is-visible")) scrim.hidden = true;
  }, 190);
}

async function refreshImages() {
  const payload = await api("/api/images");
  state.images = payload.images;
  renderStats();
  renderRecent();
  renderGallery();
}

async function refreshJobs() {
  try {
    const payload = await api("/api/jobs");
    state.jobs = payload.jobs;
    let needsImageRefresh = false;
    for (const job of state.jobs) {
      if (["completed", "failed"].includes(job.status) && !state.knownTerminalJobs.has(job.id)) {
        state.knownTerminalJobs.add(job.id);
        if (job.status === "completed") {
          needsImageRefresh = true;
          showToast(`${job.poem_title} · ${job.style_name} 已完成`);
        } else {
          showToast(`${job.poem_title}生成失败：${job.error || "未知错误"}`, "error");
        }
      }
    }
    renderJobs();
    if (needsImageRefresh) await refreshImages();
    schedulePolling();
  } catch (error) {
    showToast(`任务状态更新失败：${error.message}`, "error");
  }
}

function schedulePolling() {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  const active = state.jobs.some((job) => ["queued", "running"].includes(job.status));
  state.pollTimer = window.setTimeout(refreshJobs, active ? 1200 : 6000);
}

async function submitGeneration(event) {
  event.preventDefault();
  const button = document.querySelector("#generate-button");
  const checkedStyle = document.querySelector('input[name="style_id"]:checked');
  if (!checkedStyle) {
    showToast("请先选择一种美术风格。", "error");
    return;
  }
  const payload = {
    poem_id: document.querySelector("#poem-select").value,
    style_id: checkedStyle.value,
    aspect_ratio: document.querySelector("#ratio-select").value,
    count: Number(document.querySelector("#count-select").value),
    custom_note: document.querySelector("#custom-note").value,
  };
  button.disabled = true;
  try {
    const result = await api("/api/generate", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.jobs.unshift(result.job);
    renderJobs();
    openJobs();
    showToast("生成任务已创建，可以继续浏览其他内容。");
    schedulePolling();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function bindEvents() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.view));
  });
  document.querySelectorAll("[data-jump-to-generator]").forEach((button) => {
    button.addEventListener("click", () => jumpToGenerator());
  });
  document.addEventListener("click", (event) => {
    const openTarget = event.target.closest("[data-open-image]");
    if (openTarget) openImageDialog(openTarget.dataset.openImage);
    const favoriteTarget = event.target.closest("[data-favorite-image]");
    if (favoriteTarget) toggleFavorite(favoriteTarget.dataset.favoriteImage);
    const poemTarget = event.target.closest("[data-create-poem]");
    if (poemTarget) jumpToGenerator(poemTarget.dataset.createPoem, null);
    const styleTarget = event.target.closest("[data-create-style]");
    if (styleTarget) jumpToGenerator(null, styleTarget.dataset.createStyle);
    const viewTarget = event.target.closest("[data-switch-view]");
    if (viewTarget) switchView(viewTarget.dataset.switchView);
  });
  document.querySelector("#poem-select").addEventListener("change", updatePoemPreview);
  document.querySelector("#style-picker").addEventListener("change", (event) => {
    if (event.target.matches('input[name="style_id"]')) {
      state.selectedStyleId = event.target.value;
      updateDirectionVisual();
    }
  });
  document.querySelector("#generator-form").addEventListener("submit", submitGeneration);
  document.querySelector("#poem-search").addEventListener("input", (event) => renderPoemLibrary(event.target.value));
  document.querySelector("#gallery-style-filter").addEventListener("change", (event) => {
    state.styleFilter = event.target.value;
    renderGallery();
  });
  document.querySelector("#favorite-filter").addEventListener("click", (event) => {
    state.favoriteOnly = !state.favoriteOnly;
    event.currentTarget.classList.toggle("is-active", state.favoriteOnly);
    event.currentTarget.textContent = state.favoriteOnly ? "♥ 只看收藏" : "♡ 只看收藏";
    renderGallery();
  });
  document.querySelector("#jobs-button").addEventListener("click", openJobs);
  document.querySelector("#close-jobs").addEventListener("click", closeJobs);
  document.querySelector("#drawer-scrim").addEventListener("click", closeJobs);
  document.querySelector("#dialog-close").addEventListener("click", () => document.querySelector("#art-dialog").close());
  document.querySelector("#dialog-favorite").addEventListener("click", () => {
    if (state.currentImageId) toggleFavorite(state.currentImageId);
  });
  document.querySelector("#art-dialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
  document.querySelector("#open-help").addEventListener("click", () => document.querySelector("#help-dialog").showModal());
  document.querySelector("#help-close").addEventListener("click", () => document.querySelector("#help-dialog").close());
  document.querySelector("#help-dialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
  window.addEventListener("hashchange", () => switchView(location.hash.slice(1), { preserveHash: true }));
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeJobs();
  });
}

async function initialize() {
  try {
    const payload = await api("/api/bootstrap");
    state.poems = payload.poems;
    state.styles = payload.styles;
    state.images = payload.images;
    state.jobs = payload.jobs;
    state.config = payload.config;
    for (const job of state.jobs) {
      if (["completed", "failed"].includes(job.status)) state.knownTerminalJobs.add(job.id);
    }
    renderPoemSelect();
    renderStylePicker();
    renderAll();
    bindEvents();
    switchView(location.hash.slice(1) || "today", { preserveHash: true });
    schedulePolling();
  } catch (error) {
    document.body.innerHTML = `
      <main style="max-width:720px;margin:12vh auto;padding:32px;font-family:system-ui;color:#1d2926">
        <h1>无法连接唐诗绘卷本地服务</h1>
        <p>${escapeHtml(error.message)}</p>
        <p>请确认已运行 <code>python3 server.py</code>，并通过终端显示的地址访问。</p>
      </main>`;
  }
}

initialize();
