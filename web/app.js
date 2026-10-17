// AutoPPT single-page front end.  No build step and no third-party code.
// Every value from the server is inserted as text (never innerHTML), so
// uploaded or generated content cannot inject markup.

import { api, auth, ApiError, uploadFile } from "./api.js";
import { lang, setLang, stateLabel, t } from "./i18n.js";

// ---------------------------------------------------------------- tiny DOM helpers

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "class") el.className = value;
    else if (key === "value") el.value = value;
    else if (key === "checked") el.checked = !!value;
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// replaceChildren() would render null as the text "null"; skip empty parts like h() does.
function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

const view = () => document.getElementById("view");
let options = null;
let me = null;

async function getOptions() {
  options = options || (await api.get("/api/options"));
  return options;
}

function notice(text, kind = "info") {
  return h("div", { class: `notice ${kind}`, role: kind === "error" ? "alert" : "status" }, text);
}

function errorBox(error) {
  const lines = [error.message || t("error")];
  const details = error.body && error.body.details;
  if (details && details.errors) for (const e of details.errors) lines.push(`${e.path || ""} ${e.message}`);
  if (details && details.issues) lines.push(...details.issues);
  if (details && details.needed !== undefined) lines.push(`需要 ${details.needed}，可用 ${details.available}`);
  return h("div", { class: "notice error", role: "alert" }, lines.map((line) => h("div", {}, line)));
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(lang() === "zh" ? "zh-CN" : "en-GB", { hour12: false });
}

function stateBadge(state) {
  return h("span", { class: `badge state-${state}` }, stateLabel(state));
}

function card(title, ...content) {
  return h("section", { class: "card" }, title ? h("h2", {}, title) : null, ...content);
}

function field(label, control, hint) {
  if (control instanceof Element && ["INPUT", "SELECT", "TEXTAREA"].includes(control.tagName) && !control.getAttribute("aria-label")) {
    control.setAttribute("aria-label", label);
  }
  return h("label", { class: "field" }, h("span", { class: "label" }, label), control, hint ? h("small", { class: "hint" }, hint) : null);
}

function select(items, value, onChange) {
  return h("select", { onchange: (e) => onChange(e.target.value) },
    items.map((item) => h("option", { value: item.id, selected: String(item.id) === String(value) ? "selected" : null }, item.label)));
}

// Minimal, safe Markdown view for outline previews: headings and bullet lists only.
function markdownView(text) {
  const root = h("div", { class: "markdown" });
  let list = null;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trimEnd();
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    const bullet = line.match(/^(\s*)-\s+(.*)$/);
    if (heading) { list = null; root.append(h(`h${heading[1].length + 2}`, {}, heading[2])); }
    else if (bullet) {
      if (!list) { list = h("ul"); root.append(list); }
      list.append(h("li", { class: bullet[1].length ? "nested" : "" }, bullet[2]));
    } else if (line.trim()) { list = null; root.append(h("p", {}, line)); }
    else list = null;
  }
  return root;
}

// ---------------------------------------------------------------- shell

async function renderNav() {
  const nav = document.getElementById("nav");
  fill(nav, );
  if (auth.loggedIn() && !me) {
    try { me = await api.get("/api/me"); } catch { me = null; }
  }
  const links = [["#/", t("home")], ["#/pricing", t("pricing")]];
  if (auth.loggedIn()) {
    links.push(["#/new", t("newJob")], ["#/jobs", t("myJobs")], ["#/credits", t("credits")]);
    if (me && me.is_admin) links.push(["#/admin", t("admin")]);
  }
  for (const [href, label] of links) nav.append(h("a", { href, class: location.hash === href ? "active" : "" }, label));
  if (auth.loggedIn()) {
    if (me) nav.append(h("span", { class: "balance" }, `${t("available")} ${me.credits.available}`));
    nav.append(h("button", { class: "link", onclick: async () => {
      const tokens = auth.get();
      if (tokens) await api.post("/api/auth/logout", { refresh_token: tokens.refresh_token }).catch(() => {});
      auth.clear(); me = null; location.hash = "#/";
    } }, t("logout")));
  } else nav.append(h("a", { href: "#/login" }, t("login")));
  nav.append(h("button", { class: "link", onclick: () => { setLang(lang() === "zh" ? "en" : "zh"); route(); } }, t("language")));
  fill(document.getElementById("footer"), 
    h("span", {}, "AutoPPT · 内容准确性由用户最终负责；原生可编辑版不承诺与母版逐像素一致。"),
  );
}

const ROUTES = [
  [/^#?\/?$/, pageHome],
  [/^#\/pricing$/, pagePricing],
  [/^#\/login$/, pageLogin],
  [/^#\/new$/, pageNewJob],
  [/^#\/jobs$/, pageJobs],
  [/^#\/jobs\/([0-9a-f]{32})$/, pageJob],
  [/^#\/credits$/, pageCredits],
  [/^#\/admin(?:\/(\w+))?$/, pageAdmin],
];

let routeToken = 0;
async function route() {
  const token = ++routeToken;
  stopPolling();
  await renderNav();
  const hash = location.hash || "#/";
  for (const [pattern, page] of ROUTES) {
    const match = hash.match(pattern);
    if (!match) continue;
    const needsLogin = ![pageHome, pagePricing, pageLogin].includes(page);
    if (needsLogin && !auth.loggedIn()) { location.hash = "#/login"; return; }
    fill(view(), h("p", { class: "muted" }, t("loading")));
    try {
      const content = await page(...match.slice(1));
      if (token === routeToken) fill(view(), content);
    } catch (error) {
      if (token === routeToken) fill(view(), errorBox(error));
    }
    window.scrollTo(0, 0);
    return;
  }
  fill(view(), notice("404"));
}

let pollTimer = null;
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

// ---------------------------------------------------------------- public pages

async function pageHome() {
  const opts = await getOptions();
  return h("div", {},
    h("section", { class: "hero" },
      h("h1", {}, t("heroTitle")),
      h("p", {}, t("heroText")),
      h("a", { class: "button primary", href: auth.loggedIn() ? "#/new" : "#/login" }, t("start"))),
    card(t("tiers"), h("div", { class: "grid three" }, opts.delivery_tiers.map((tier) =>
      h("div", { class: "tile" }, h("h3", {}, tier.label), h("p", { class: "muted" }, tierText(tier.id)))))),
    card("使用场景", h("div", { class: "grid three" }, opts.scenarios.map((s) =>
      h("div", { class: "tile" }, h("h3", {}, s.label), h("p", {}, s.use_when),
        h("small", { class: "muted" }, `建议 ${s.default_slide_range[0]}–${s.default_slide_range[1]} 页`))))),
    card(t("limits"), h("ul", {}, opts.notices.map((n) => h("li", {}, n)))),
  );
}

function tierText(id) {
  return {
    planning_package: "大纲、逐页内容、证据对照、精确文字清单和逐页出图提示词。",
    image_deck: "规划包 + 每页一张 AI 生成的完整母版，组装成图片版 PPTX；需审核母版。",
    native_editable: "图片版 + 文字、形状、图表原生可改；照片等为可替换图片；附逐页对比证据。必须由你批准原生预览。",
  }[id] || "";
}

async function pagePricing() {
  const pricing = await api.get("/api/pricing");
  const r = pricing.rules;
  return h("div", {},
    card(t("pricing"),
      pricing.note ? notice(pricing.note, "warn") : null,
      h("table", { class: "table" },
        h("tbody", {},
          h("tr", {}, h("th", {}, "基础费（解析与规划）"), h("td", {}, `${r.base} 积分`)),
          h("tr", {}, h("th", {}, "图片版 / 页"), h("td", {}, `${r.per_slide.image_deck} 积分`)),
          h("tr", {}, h("th", {}, "原生可编辑版 / 页"), h("td", {}, `${r.per_slide.native_editable} 积分`)),
          h("tr", {}, h("th", {}, "讲稿 / 预测问题"), h("td", {}, `${r.addons.speaker_notes} / ${r.addons.anticipated_questions} 积分`)),
          h("tr", {}, h("th", {}, "母版单页重做"), h("td", {}, `${r.redo_slide} 积分 / 页`)),
          h("tr", {}, h("th", {}, `超出 ${r.free_rounds} 轮的返修`), h("td", {}, `${r.extra_round} 积分 / 轮`)))),
      h("p", { class: "muted" }, "下单时按页数上限冻结积分，每个阶段完成后结算；取消时只结算已完成的阶段，系统失败自动退还未完成部分。"),
      h("p", { class: "muted" }, "测试期不接在线支付：积分通过兑换码或管理员充值获得。")));
}

async function pageLogin() {
  const email = h("input", { type: "email", required: true, autocomplete: "email" });
  const code = h("input", { inputmode: "numeric", maxlength: 6, autocomplete: "one-time-code" });
  const status = h("div");
  const send = async () => {
    fill(status, );
    try {
      const reply = await api.post("/api/auth/code", { email: email.value });
      fill(status, notice(reply.dev_code ? `${t("codeSent")}（开发模式验证码：${reply.dev_code}）` : t("codeSent")));
      code.focus();
    } catch (error) { fill(status, errorBox(error)); }
  };
  const login = async (event) => {
    event.preventDefault();
    try {
      auth.set(await api.post("/api/auth/login", { email: email.value, code: code.value }));
      me = null;
      location.hash = "#/jobs";
    } catch (error) { fill(status, errorBox(error)); }
  };
  return card(t("login"),
    h("form", { class: "stack narrow", onsubmit: login },
      field(t("email"), email),
      h("div", { class: "row" }, field(t("code"), code), h("button", { type: "button", onclick: send }, t("sendCode"))),
      h("button", { class: "primary", type: "submit" }, t("signIn")),
      status));
}

// ---------------------------------------------------------------- new job wizard

const DRAFT_KEY = "autoppt.wizard";

function loadWizard() {
  try { return JSON.parse(sessionStorage.getItem(DRAFT_KEY)) || null; } catch { return null; }
}

function saveWizard(state) { sessionStorage.setItem(DRAFT_KEY, JSON.stringify(state)); }

function freshWizard(opts) {
  const scenario = opts.scenarios.find((s) => s.id === "thesis_defense") || opts.scenarios[0];
  return {
    step: 1, jobId: null,
    service: "new_deck", tier: "native_editable",
    files: [], description: "",
    scenario: scenario.id, slidesMode: "range", slidesMin: scenario.default_slide_range[0], slidesMax: scenario.default_slide_range[1],
    slidesCount: scenario.default_slide_range[1], talkMinutes: "",
    style: scenario.default_style, styleExplicit: false, density: "medium_high", audience: "答辩委员会", language: "zh-CN",
    cover: { title: "", subtitle: "", author: "", advisor: "", institution: "", date: "" },
    mustInclude: "", mustAvoid: "", charts: "native_with_workbook", fontAccepted: false, aspect: "16:9",
    outlineCheckpoint: true, masterCheckpoint: true, speakerNotes: false, questions: false,
    retention: 7, caseStudy: false, logoRights: false, deadline: "",
  };
}

function buildRequest(w) {
  const lines = (text) => text.split("\n").map((s) => s.trim()).filter(Boolean);
  const slides = w.slidesMode === "fixed" ? { mode: "fixed", count: Number(w.slidesCount) }
    : w.slidesMode === "range" ? { mode: "range", min: Number(w.slidesMin), max: Number(w.slidesMax) }
    : { mode: "duration", talk_minutes: Number(w.talkMinutes) };
  if (w.slidesMode !== "duration" && w.talkMinutes) slides.talk_minutes = Number(w.talkMinutes);
  const cover = Object.fromEntries(Object.entries(w.cover).filter(([, v]) => v.trim()));
  const delivery = { tier: w.tier, aspect_ratio: w.aspect, charts: w.tier === "native_editable" ? w.charts : "in_image",
    speaker_notes: w.speakerNotes, anticipated_questions: w.questions };
  if (w.tier === "native_editable") { if (w.fontAccepted) delivery.font_policy = "office_native_yahei"; }
  const request = {
    schema_version: "autoppt-job-request-v1",
    service: w.service,
    inputs: {
      files: w.files.filter((f) => f.sha256).map((f) => ({ file_id: f.file_id, role: f.role, name: f.name, sha256: f.sha256 })),
      description: w.description, must_include: lines(w.mustInclude), must_avoid: lines(w.mustAvoid),
      logo_rights_confirmed: w.logoRights,
    },
    language: w.language, audience: w.audience,
    style: { profile: w.style, explicit_choice: w.styleExplicit, density: w.density, reference_file_ids: w.files.filter((f) => f.role === "style_reference" && f.sha256).map((f) => f.file_id) },
    cover, delivery,
    review: { outline_checkpoint: w.outlineCheckpoint, master_checkpoint: w.masterCheckpoint, native_preview_checkpoint: true },
    privacy: { retention_days: Number(w.retention), allow_case_study: w.caseStudy },
  };
  if (w.service === "new_deck") { request.scenario = w.scenario; request.slides = slides; }
  else if (w.slidesCount) request.slides = { mode: "fixed", count: Number(w.slidesCount) };
  if (w.deadline) request.deadline = new Date(w.deadline).toISOString();
  return request;
}

function nonSpace(text) { return [...text].filter((c) => !/\s/.test(c)).length; }

async function pageNewJob() {
  const opts = await getOptions();
  let w = loadWizard() || freshWizard(opts);
  const root = h("div", { class: "wizard" });
  const set = (patch, rerender = false) => { w = { ...w, ...patch }; saveWizard(w); if (rerender) draw(); };
  const steps = ["服务与档位", "资料与描述", "场景、页数与风格", "高级选项", "估价与确认"];

  function draw() {
    fill(root, 
      h("ol", { class: "steps" }, steps.map((label, i) => h("li", { class: i + 1 === w.step ? "current" : i + 1 < w.step ? "done" : "" }, label))),
      [null, stepService, stepFiles, stepScenario, stepAdvanced, stepEstimate][w.step](),
    );
  }

  const nav = (canNext = true, nextLabel = t("next")) => h("div", { class: "row end" },
    w.step > 1 ? h("button", { onclick: () => set({ step: w.step - 1 }, true) }, t("back")) : null,
    w.step < 5 ? h("button", { class: "primary", disabled: !canNext, onclick: () => set({ step: w.step + 1 }, true) }, nextLabel) : null);

  function stepService() {
    return card("① 服务类型与交付档位",
      h("div", { class: "grid two" }, opts.services.map((s) => h("button", {
        class: `choice ${w.service === s.id ? "selected" : ""}`,
        onclick: () => set({ service: s.id, tier: s.id === "vectorize" ? "native_editable" : w.tier }, true),
      }, h("strong", {}, s.label), h("small", {}, s.id === "vectorize" ? "把图片版 PPT/截图/PDF 转成可编辑 PPTX（测试中）" : "用论文、报告或描述生成答辩 PPT")))),
      h("h3", {}, "交付档位"),
      h("div", { class: "grid three" }, opts.delivery_tiers.map((tier) => h("button", {
        class: `choice ${w.tier === tier.id ? "selected" : ""}`,
        disabled: w.service === "vectorize" && tier.id !== "native_editable",
        onclick: () => set({ tier: tier.id }, true),
      }, h("strong", {}, tier.label), h("small", {}, tierText(tier.id))))),
      nav());
  }

  function stepFiles() {
    const list = h("div", { class: "files" });
    const roles = opts.file_roles;
    const uploading = () => w.files.some((f) => !f.sha256 && !String(f.status).startsWith("失败"));
    const navBar = nav(true);
    const syncNext = () => { const next = navBar.querySelector("button.primary"); if (next) next.disabled = uploading(); };
    const drawFiles = () => { syncNext(); updateHints(); fill(list, ...w.files.map((f, index) => h("div", { class: "file-row" },
      h("span", { class: "file-name" }, f.name),
      h("span", { class: "muted" }, f.status || ""),
      select(roles.map((r) => ({ id: r.id, label: r.label })), f.role, (role) => { w.files[index].role = role; saveWizard(w); }),
      h("button", { class: "link", onclick: () => { w.files.splice(index, 1); saveWizard(w); drawFiles(); } }, "移除")))); };
    const input = h("input", { type: "file", multiple: true, onchange: async (event) => {
      for (const file of event.target.files) {
        const entry = { file_id: `f_${String(w.files.length + 1).padStart(2, "0")}`, name: file.name, role: guessRole(file.name, w.service), status: "上传中 0%" };
        w.files.push(entry); drawFiles();
        try {
          const done = await uploadFile(file, (sent, total) => { entry.status = `上传中 ${Math.round((100 * sent) / total)}%`; drawFiles(); });
          Object.assign(entry, { sha256: done.sha256, status: `已上传（${done.kind}）` });
        } catch (error) { entry.status = `失败：${error.message}`; }
        saveWizard(w); drawFiles();
      }
      event.target.value = "";
    } });
    const hasDocs = () => w.files.some((f) => ["primary_source", "supplementary", "chart_data"].includes(f.role) && f.sha256);
    const counter = h("small", { class: "hint" });
    const hints = h("ul", { class: "hints" });
    const updateHints = () => {
      const count = nonSpace(w.description);
      counter.textContent = hasDocs() ? `${count} 字（有文档时作为重点说明）` : `${count} / ${opts.limits.min_description_chars_without_documents} 字（未上传文档时必填）`;
      const tips = [];
      if (!hasDocs()) {
        if (count < opts.limits.min_description_chars_without_documents) tips.push("没有文档时，请写清主题、目的、核心结论和关键数据。");
        if (!/\d/.test(w.description)) tips.push("没有数字：只凭描述生成的内容会标注为“框架或示意”，不会编造数据。");
      }
      fill(hints, ...tips.map((tip) => h("li", {}, tip)));
    };
    const description = h("textarea", { rows: 8, value: w.description, "aria-label": "描述文字", oninput: (e) => { set({ description: e.target.value }); updateHints(); } });
    drawFiles();
    return card("② 资料与描述",
      h("p", { class: "muted" }, "为每个文件选择用途：主资料、补充资料、图表数据、图片素材、原图保留（不让 AI 重绘）、Logo、风格参考、待矢量化文件。支持 DOCX、PDF、PPTX、XLSX、CSV、MD/TXT/TEX、PNG/JPG/WebP/HEIC、ZIP。"),
      input, list,
      field("描述文字", description, null), counter, hints,
      navBar);
  }

  function stepScenario() {
    const scenario = opts.scenarios.find((s) => s.id === w.scenario);
    const style = opts.styles.find((s) => s.id === w.style);
    const slides = w.slidesMode === "fixed"
      ? field("页数", h("input", { type: "number", min: 1, max: opts.limits.max_slides, value: w.slidesCount, oninput: (e) => set({ slidesCount: e.target.value }) }))
      : w.slidesMode === "range"
        ? h("div", { class: "row" },
          field("最少", h("input", { type: "number", min: 1, max: opts.limits.max_slides, value: w.slidesMin, oninput: (e) => set({ slidesMin: e.target.value }) })),
          field("最多", h("input", { type: "number", min: 1, max: opts.limits.max_slides, value: w.slidesMax, oninput: (e) => set({ slidesMax: e.target.value }) })))
        : null;
    return card("③ 场景、页数与风格",
      w.service === "new_deck" ? [
        h("h3", {}, "使用场景"),
        h("div", { class: "grid three" }, opts.scenarios.map((s) => h("button", {
          class: `choice ${w.scenario === s.id ? "selected" : ""}`,
          onclick: () => set({ scenario: s.id, slidesMin: s.default_slide_range[0], slidesMax: s.default_slide_range[1], style: w.styleExplicit ? w.style : s.default_style }, true),
        }, h("strong", {}, s.label), h("small", {}, s.use_when)))),
        h("h3", {}, "页数"),
        h("div", { class: "row" },
          select(opts.slide_modes.map((m) => ({ id: m.id, label: m.label })), w.slidesMode, (mode) => set({ slidesMode: mode }, true)),
          slides,
          field("演讲时长（分钟）", h("input", { type: "number", min: 1, max: 180, value: w.talkMinutes, oninput: (e) => set({ talkMinutes: e.target.value }) }),
            scenario ? `本场景约每页 ${scenario.default_duration_seconds} 秒` : null)),
      ] : field("要转换的页数", h("input", { type: "number", min: 1, max: opts.limits.max_slides, value: w.slidesCount, oninput: (e) => set({ slidesCount: e.target.value }) })),
      h("h3", {}, "风格"),
      h("div", { class: "grid three" }, opts.styles.map((s) => h("button", {
        class: `choice ${w.style === s.id ? "selected" : ""}`,
        onclick: () => set({ style: s.id, styleExplicit: false }, true),
      }, h("strong", {}, s.label, s.is_default ? "（默认）" : ""), h("small", {}, s.use_when), swatches(s.palette)))),
      style && style.requires_explicit_choice ? h("label", { class: "check" },
        h("input", { type: "checkbox", checked: w.styleExplicit, onchange: (e) => set({ styleExplicit: e.target.checked }) }),
        `我确认选择“${style.label}”（${style.dark ? "深色" : "极简"}风格需要主动确认）`) : null,
      h("div", { class: "row" },
        field("受众", h("input", { list: "audiences", value: w.audience, oninput: (e) => set({ audience: e.target.value }) })),
        h("datalist", { id: "audiences" }, opts.audiences.map((a) => h("option", { value: a }))),
        field("语言", select(opts.languages.map((l) => ({ id: l.id, label: l.label })), w.language, (v) => set({ language: v }))),
        field("信息密度", select(opts.densities.map((d) => ({ id: d.id, label: d.label })), w.density, (v) => set({ density: v })))),
      nav());
  }

  function stepAdvanced() {
    const coverField = (key, label) => field(label, h("input", { value: w.cover[key], oninput: (e) => { w.cover[key] = e.target.value; saveWizard(w); } }));
    const usesLogo = w.files.some((f) => f.role === "logo");
    return card("④ 高级选项",
      h("h3", {}, "封面信息（按原文进入页面）"),
      h("div", { class: "grid three" }, coverField("title", "标题"), coverField("subtitle", "副标题"), coverField("author", "汇报人"),
        coverField("advisor", "导师"), coverField("institution", "单位"), coverField("date", "日期")),
      h("div", { class: "grid two" },
        field("必须包含（每行一条）", h("textarea", { rows: 4, value: w.mustInclude, oninput: (e) => set({ mustInclude: e.target.value }) })),
        field("必须避免（每行一条）", h("textarea", { rows: 4, value: w.mustAvoid, oninput: (e) => set({ mustAvoid: e.target.value }) }))),
      h("div", { class: "row" },
        field("画幅", select(opts.aspect_ratios.map((a) => ({ id: a.id, label: a.label })), w.aspect, (v) => set({ aspect: v }))),
        w.tier === "native_editable" ? field("图表", select(opts.chart_modes.filter((c) => c.id !== "in_image").map((c) => ({ id: c.id, label: c.label })), w.charts, (v) => set({ charts: v }))) : null,
        field("数据保留", select(opts.retention_days.map((d) => ({ id: d, label: `${d} 天后删除` })), w.retention, (v) => set({ retention: Number(v) }))),
        field("截止时间", h("input", { type: "datetime-local", value: w.deadline, oninput: (e) => set({ deadline: e.target.value }) }))),
      w.tier === "native_editable" ? h("label", { class: "check" },
        h("input", { type: "checkbox", checked: w.fontAccepted, onchange: (e) => set({ fontAccepted: e.target.checked }) }),
        "接受用 Office 字体（微软雅黑/等线）替代母版字形（原生可编辑版必须勾选）") : null,
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.outlineCheckpoint, onchange: (e) => set({ outlineCheckpoint: e.target.checked }) }), "大纲确认（CP1）"),
      w.tier !== "planning_package" ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.masterCheckpoint, onchange: (e) => set({ masterCheckpoint: e.target.checked }) }), "母版审核（CP2）") : null,
      w.tier === "native_editable" ? h("label", { class: "check disabled" }, h("input", { type: "checkbox", checked: true, disabled: true }), "原生预览批准（CP3，不能关闭）") : null,
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.speakerNotes, onchange: (e) => set({ speakerNotes: e.target.checked }) }), "讲稿（单独计费）"),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.questions, onchange: (e) => set({ questions: e.target.checked }) }), "预测问题（单独计费）"),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.caseStudy, onchange: (e) => set({ caseStudy: e.target.checked }) }), "允许脱敏后作为案例展示（默认不允许）"),
      usesLogo ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.logoRights, onchange: (e) => set({ logoRights: e.target.checked }) }), "我确认拥有所上传 Logo 的使用权") : null,
      nav());
  }

  function stepEstimate() {
    const box = h("div", { class: "stack" }, h("p", { class: "muted" }, t("loading")));
    (async () => {
      try {
        const request = buildRequest(w);
        const job = w.jobId ? await api.put(`/api/jobs/${w.jobId}`, { request }) : await api.post("/api/jobs", { request });
        set({ jobId: job.id });
        const quote = await api.post(`/api/jobs/${job.id}/estimate`);
        const credits = await api.get("/api/credits");
        const rows = Object.entries(quote.phases).filter(([, v]) => v).map(([phase, v]) => h("tr", {}, h("th", {}, phaseLabel(phase)), h("td", {}, `${v}`)));
        fill(box, 
          quote.provisional ? notice("测试期价格，P1 实测后调整。", "warn") : null,
          h("table", { class: "table" }, h("tbody", {}, rows,
            h("tr", { class: "total" }, h("th", {}, t("total")), h("td", {}, `${quote.total} 积分`)))),
          h("p", {}, `按 ${quote.slides_for_hold} 页冻结 ${quote.hold} 积分；当前可用 ${credits.available}。每个阶段完成后结算，取消时只结算已完成阶段。`),
          quote.warnings.length ? h("div", { class: "notice warn" }, quote.warnings.map((x) => h("div", {}, x.message))) : null,
          quote.derived.content_notice ? notice(quote.derived.content_notice, "warn") : null,
          h("p", { class: "muted" }, tierText(w.tier)),
          h("div", { class: "row end" },
            h("button", { onclick: () => set({ step: 4 }, true) }, t("back")),
            h("button", { class: "primary", disabled: credits.available < quote.total, onclick: async (e) => {
              e.target.disabled = true;
              try {
                await api.post(`/api/jobs/${job.id}/submit`, { confirm_total: quote.total }, { "Idempotency-Key": `submit-${job.id}` });
                sessionStorage.removeItem(DRAFT_KEY);
                me = null;
                location.hash = `#/jobs/${job.id}`;
              } catch (error) { box.append(errorBox(error)); e.target.disabled = false; }
            } }, `${t("submit")}（${quote.total} 积分）`)),
          credits.available < quote.total ? notice("积分不足：请先在“积分”页使用兑换码。", "error") : null);
      } catch (error) {
        fill(box, errorBox(error), h("div", { class: "row end" }, h("button", { onclick: () => set({ step: 4 }, true) }, t("back"))));
      }
    })();
    return card("⑤ 估价与确认", box);
  }

  draw();
  return h("div", {}, h("div", { class: "row space" }, h("h1", {}, t("newJob")),
    h("button", { class: "link", onclick: () => { sessionStorage.removeItem(DRAFT_KEY); w = freshWizard(opts); draw(); } }, "重新开始")), root);
}

function guessRole(name, service) {
  const ext = name.toLowerCase().split(".").pop();
  if (service === "vectorize") return "vectorize_source";
  if (["xlsx", "csv"].includes(ext)) return "chart_data";
  if (["png", "jpg", "jpeg", "webp", "heic", "heif"].includes(ext)) return "image_asset";
  return "primary_source";
}

function swatches(palette) {
  const row = h("span", { class: "swatches" });
  for (const colours of Object.values(palette || {})) {
    const swatch = h("span", { class: "swatch" });
    swatch.style.backgroundColor = colours[0];
    row.append(swatch);
  }
  return row;
}

function phaseLabel(phase) {
  return { intake: "解析", planning: "规划", masters: "母版", reconstruct: "原生重建", package: "打包与附加项" }[phase] || phase;
}

// ---------------------------------------------------------------- jobs

async function pageJobs() {
  const jobs = await api.get("/api/jobs");
  if (!jobs.length) return card(t("myJobs"), h("p", {}, t("empty")), h("a", { class: "button primary", href: "#/new" }, t("newJob")));
  return card(t("myJobs"), h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "标题"), h("th", {}, t("state")), h("th", {}, "积分"), h("th", {}, t("created")))),
    h("tbody", {}, jobs.map((job) => h("tr", {},
      h("td", {}, h("a", { href: `#/jobs/${job.id}` }, job.title || job.id.slice(0, 8))),
      h("td", {}, stateBadge(job.state)),
      h("td", {}, job.hold ? `${job.hold.amount - job.hold.remaining} / ${job.hold.amount}` : job.estimate ? `≈${job.estimate.total}` : "—"),
      h("td", {}, fmtTime(job.created_at)))))));
}

async function pageJob(jobId) {
  const root = h("div", { class: "stack" });
  let lastEvent = 0;
  const timeline = h("ol", { class: "timeline" });

  async function refreshEvents() {
    const events = await api.get(`/api/jobs/${jobId}/events?after=${lastEvent}`);
    for (const e of events) {
      lastEvent = e.id;
      if (e.kind === "lease") continue;
      timeline.prepend(h("li", {}, h("time", {}, fmtTime(e.at)), " ", eventText(e)));
    }
    return events.length;
  }

  async function draw() {
    const job = await api.get(`/api/jobs/${jobId}`);
    const head = card(job.title || job.id,
      h("div", { class: "row" }, stateBadge(job.state),
        job.hold ? h("span", { class: "muted" }, `已结算 ${job.hold.amount - job.hold.remaining} / 冻结 ${job.hold.amount}`) : null),
      job.state === "blocked" ? notice(`任务暂停：${job.blocked_reason}`, "error") : null,
      progressBar(job),
      h("div", { class: "row" },
        !["delivered", "failed", "cancelled", "draft", "estimated"].includes(job.state) ? h("button", { class: "danger", onclick: async () => {
          if (!confirm("取消后只结算已完成的阶段，未完成部分的冻结积分会释放。确定取消？")) return;
          await api.post(`/api/jobs/${jobId}/cancel`); me = null; await draw(); await renderNav();
        } }, t("cancel")) : null,
        ["delivered", "failed", "cancelled", "draft", "estimated"].includes(job.state) ? h("button", { class: "link", onclick: async () => {
          if (!confirm("删除后任务资料和产物无法恢复。确定删除？")) return;
          await api.del(`/api/jobs/${jobId}`); location.hash = "#/jobs";
        } }, t("del")) : null));
    const parts = [head];
    if (job.open_checkpoint) parts.push(await reviewPanel(job, draw));
    if (job.deliverables.length) parts.push(card(t("deliverables"), h("ul", { class: "downloads" }, job.deliverables.map((d) =>
      h("li", {}, h("a", { href: d.url, download: "" }, d.path.split("/").pop()), h("small", { class: "muted" }, ` ${(d.size / 1024).toFixed(0)} KB`))))));
    parts.push(card(t("events"), timeline));
    fill(root, ...parts);
    return job;
  }

  await draw();
  await refreshEvents();
  pollTimer = setInterval(async () => {
    try { if ((await refreshEvents()) > 0) { await draw(); me = null; await renderNav(); } } catch { /* keep polling */ }
  }, 5000);
  return root;
}

function eventText(e) {
  if (e.kind === "state") return `${stateLabel(e.data.from || "")} → ${stateLabel(e.data.to)}${e.message ? `（${e.message}）` : ""}`;
  if (e.kind === "progress") return `${e.message}${e.data.current_slide ? ` · ${e.data.current_slide}` : ""}`;
  return e.message || e.kind;
}

function progressBar(job) {
  const plan = ["queued", "intake", "planning", "masters", "reconstructing", "packaging", "delivered"];
  const index = Math.max(0, plan.findIndex((s) => job.state === s || job.state.startsWith(s)));
  const bar = h("div", { class: "progress" }, h("div", { class: "fill" }));
  bar.firstChild.style.width = `${job.state === "delivered" ? 100 : Math.round((100 * index) / (plan.length - 1))}%`;
  return bar;
}

// ---------------------------------------------------------------- checkpoint review

async function reviewPanel(job, redraw) {
  const cp = await api.get(`/api/jobs/${job.id}/checkpoint`);
  const status = h("div");
  const send = async (body, charge) => {
    if (charge > 0 && !confirm(`这一步将扣除 ${charge} 积分，确认继续？`)) return;
    try {
      await api.post(`/api/jobs/${job.id}/checkpoint/${cp.checkpoint_id}/response`, { ...body, charge_confirmed: charge > 0 });
      me = null; await redraw(); await renderNav();
    } catch (error) { fill(status, errorBox(error)); }
  };
  const extraRound = () => (cp.round >= (cp.pricing.free_rounds || 2) ? cp.pricing.extra_round || 0 : 0);
  const title = `${t("review")} · ${{ outline: "大纲确认", masters: "母版审核", native_preview: "原生预览批准", vectorize_text: "文字核对" }[cp.type]}（第 ${cp.round} 轮）`;
  if (cp.type === "outline" || cp.type === "vectorize_text") return card(title, cp.summary ? h("p", {}, cp.summary) : null, await outlineReview(cp, send, extraRound), status);
  if (cp.type === "masters") return card(title, cp.summary ? h("p", {}, cp.summary) : null, mastersReview(cp, send, extraRound), status);
  return card(title, nativeReview(cp, send, extraRound), status);
}

async function outlineReview(cp, send, extraRound) {
  const documentPreview = cp.previews.find((p) => p.kind === "document");
  const text = documentPreview && documentPreview.url ? await (await fetch(documentPreview.url)).text() : "";
  const slideIds = [...text.matchAll(/^##\s+(S\d{2})/gm)].map((m) => m[1]);
  const notes = Object.fromEntries(slideIds.map((id) => [id, ""]));
  const general = h("textarea", { rows: 3, placeholder: "整体意见（可选）" });
  return h("div", { class: "stack" },
    markdownView(text),
    slideIds.length ? h("details", {}, h("summary", {}, "逐页批注"), slideIds.map((id) =>
      field(id, h("input", { oninput: (e) => { notes[id] = e.target.value; } })))) : null,
    general,
    h("div", { class: "row end" },
      h("button", { onclick: () => {
        const annotations = Object.entries(notes).filter(([, v]) => v.trim()).map(([slide_id, v]) => ({ slide_id, text: v.trim() }));
        if (!annotations.length && !general.value.trim()) { alert("请写出要修改的地方"); return; }
        send({ decision: "revise", user_reply: general.value, annotations }, extraRound());
      } }, t("revise")),
      h("button", { class: "primary", onclick: () => send({ decision: "approve", user_reply: general.value || "通过" }, 0) }, t("approve"))));
}

function mastersReview(cp, send, extraRound) {
  const chosen = new Set();
  const reason = h("textarea", { rows: 3, placeholder: "重做原因（勾选的页）" });
  const slides = cp.previews.filter((p) => p.kind === "slide");
  const sheet = cp.previews.find((p) => p.kind === "contact_sheet");
  return h("div", { class: "stack" },
    sheet && sheet.url ? h("img", { class: "contact", src: sheet.url, alt: "联系表" }) : null,
    h("div", { class: "gallery" }, slides.map((p) => h("figure", {},
      h("a", { href: p.url, target: "_blank", rel: "noopener" }, h("img", { src: p.url, alt: p.slide_id, loading: "lazy" })),
      h("figcaption", {}, h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => (e.target.checked ? chosen.add(p.slide_id) : chosen.delete(p.slide_id)) }), `${p.slide_id} 重做`))))),
    reason,
    h("div", { class: "row end" },
      h("button", { onclick: () => {
        if (!chosen.size) { alert("请先勾选要重做的页"); return; }
        const ids = [...chosen];
        const charge = (cp.pricing.redo_slide || 0) * ids.length + extraRound();
        send({ decision: "redo_slides", redo_slide_ids: ids, user_reply: reason.value || `重做 ${ids.join("、")}`,
          annotations: reason.value ? ids.map((slide_id) => ({ slide_id, text: reason.value })) : [] }, charge);
      } }, t("redo")),
      h("button", { class: "primary", onclick: () => send({ decision: "approve", user_reply: "母版通过" }, 0) }, t("approve"))));
}

function nativeReview(cp, send, extraRound) {
  const sides = cp.previews.filter((p) => p.kind === "side_by_side");
  const heat = Object.fromEntries(cp.previews.filter((p) => p.kind === "diff_heatmap").map((p) => [p.slide_id, p]));
  const reply = h("textarea", { rows: 3, placeholder: "请用一句话写下你的决定，例如：接受预览中的非文字差异，优先真实可编辑" });
  const scope = cp.editability_scope || {};
  return h("div", { class: "stack" },
    notice("原生可编辑版与母版之间会有字体和局部图形差异。请逐页查看对比图和差异热图；你的原话会记录在验收记录里。", "warn"),
    h("p", {}, `原生可编辑：${(scope.native || []).join("、") || "—"}；以图片保留：${(scope.image || []).join("、") || "—"}`),
    sides.map((p) => h("figure", { class: "compare" },
      h("img", { src: p.url, alt: `${p.slide_id} 对比`, loading: "lazy" }),
      heat[p.slide_id] && heat[p.slide_id].url ? h("details", {}, h("summary", {}, `${p.slide_id} 差异热图`), h("img", { src: heat[p.slide_id].url, alt: `${p.slide_id} 差异`, loading: "lazy" })) : null,
      h("figcaption", {}, p.slide_id))),
    reply,
    h("div", { class: "row end" },
      h("button", { onclick: () => {
        if (!reply.value.trim()) { alert("请写出需要返修的问题"); return; }
        send({ decision: "revise", user_reply: reply.value }, extraRound());
      } }, t("revise")),
      h("button", { class: "primary", onclick: () => {
        if (!reply.value.trim()) { alert("批准原生预览需要你写一句话说明接受的范围"); return; }
        send({ decision: "approve", user_reply: reply.value, reviewed_previews: sides.map((p) => p.path) }, 0);
      } }, t("accept"))));
}

// ---------------------------------------------------------------- credits

async function pageCredits() {
  const data = await api.get("/api/credits");
  const shop = await api.get("/api/packages");
  const orders = await api.get("/api/orders");
  const code = h("input", { placeholder: "XXXX-XXXX-XXXX-XXXX" });
  const status = h("div");
  const orderStatus = { pending: "待支付", paid: "已支付", refunded: "已退款", expired: "已过期" };
  const buy = async (pkg) => {
    try {
      const order = await api.post("/api/orders", { package: pkg.id, provider: shop.providers[0] });
      fill(status, notice(`订单已创建（${(order.amount_cents / 100).toFixed(2)} 元）：${order.payment.instructions || "请按支付页面提示完成支付"}`));
    } catch (error) { fill(status, errorBox(error)); }
  };
  return h("div", { class: "stack" },
    card("积分包",
      shop.providers.length ? null : notice("在线支付尚未开通（测试期）：请使用兑换码或联系管理员充值。", "warn"),
      h("div", { class: "grid three" }, shop.packages.map((pkg) => h("div", { class: "tile" },
        h("strong", { class: "big" }, pkg.label || `${pkg.credits} 积分`),
        h("p", { class: "muted" }, `${(pkg.price_cents / 100).toFixed(2)} 元`),
        h("button", { class: "primary", disabled: !shop.providers.length, onclick: () => buy(pkg) }, "购买"))))),
    orders.length ? card("订单", h("table", { class: "table" }, h("tbody", {}, orders.map((o) => h("tr", {},
      h("td", {}, fmtTime(o.created_at)), h("td", {}, `${o.credits} 积分`), h("td", {}, `${(o.amount_cents / 100).toFixed(2)} 元`),
      h("td", {}, orderStatus[o.status] || o.status, o.refunded_credits ? `（已退 ${o.refunded_credits} 积分）` : "")))))) : null,
    card(t("credits"),
      h("div", { class: "grid three" },
        h("div", { class: "tile" }, h("small", {}, t("balance")), h("strong", { class: "big" }, data.balance)),
        h("div", { class: "tile" }, h("small", {}, t("frozen")), h("strong", { class: "big" }, data.frozen)),
        h("div", { class: "tile" }, h("small", {}, t("available")), h("strong", { class: "big" }, data.available))),
      h("form", { class: "row", onsubmit: async (e) => {
        e.preventDefault();
        try { const r = await api.post("/api/credits/redeem", { code: code.value }); me = null; fill(status, notice(`已到账 ${r.credited} 积分`)); setTimeout(route, 800); }
        catch (error) { fill(status, errorBox(error)); }
      } }, field("兑换码", code), h("button", { class: "primary", type: "submit" }, "兑换")),
      status),
    card("积分流水", data.statement.length ? h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, "时间"), h("th", {}, "类型"), h("th", {}, "积分"), h("th", {}, "说明"))),
      h("tbody", {}, data.statement.map((row) => h("tr", {},
        h("td", {}, fmtTime(row.created_at)), h("td", {}, { topup: "充值", grant: "赠送", redeem: "兑换", settle: "结算", refund: "退款", adjust: "调整", expire: "过期" }[row.kind] || row.kind),
        h("td", { class: row.amount < 0 ? "neg" : "pos" }, row.amount > 0 ? `+${row.amount}` : row.amount),
        h("td", {}, row.memo || (row.job_id ? h("a", { href: `#/jobs/${row.job_id}` }, "任务") : "")))))) : h("p", {}, t("empty"))));
}

// ---------------------------------------------------------------- admin

async function pageAdmin(tab = "jobs") {
  const tabs = [["jobs", "任务监控"], ["users", "用户与积分"], ["codes", "兑换码"], ["workers", "Worker"], ["pricing", "定价"], ["lessons", "经验候选"], ["ledger", "账本与审计"]];
  const body = await ({ jobs: adminJobs, users: adminUsers, codes: adminCodes, workers: adminWorkers, pricing: adminPricing, lessons: adminLessons, ledger: adminLedger }[tab] || adminJobs)();
  return h("div", { class: "stack" },
    h("nav", { class: "tabs" }, tabs.map(([id, label]) => h("a", { href: `#/admin/${id}`, class: id === tab ? "active" : "" }, label))),
    body);
}

async function adminJobs() {
  const filter = new URLSearchParams(location.hash.split("?")[1] || "").get("state") || "";
  const jobs = await api.get(`/api/admin/jobs${filter ? `?state=${encodeURIComponent(filter)}` : ""}`);
  return card("任务监控", h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "任务"), h("th", {}, t("state")), h("th", {}, "待运行"), h("th", {}, "Worker"), h("th", {}, "操作"))),
    h("tbody", {}, jobs.map((job) => h("tr", {},
      h("td", {}, h("a", { href: `#/jobs/${job.id}` }, job.title || job.id.slice(0, 8))),
      h("td", {}, stateBadge(job.state), job.blocked_reason ? h("small", { class: "muted block" }, job.blocked_reason) : null),
      h("td", {}, job.pending_phase || "—"), h("td", {}, job.lease_worker_id ? job.lease_worker_id.slice(0, 8) : "—"),
      h("td", {}, job.state === "blocked" ? h("button", { onclick: async () => {
        const to = prompt("恢复到哪个状态？（例如 planning）");
        if (to) { await api.post(`/api/admin/jobs/${job.id}/resume?to_state=${encodeURIComponent(to)}`); route(); }
      } }, "恢复") : null))))));
}

async function adminUsers() {
  const search = h("input", { placeholder: "邮箱" });
  const table = h("div");
  const load = async () => {
    const users = await api.get(`/api/admin/users?email=${encodeURIComponent(search.value)}`);
    fill(table, h("table", { class: "table" }, h("tbody", {}, users.map((u) => h("tr", {},
      h("td", {}, u.email), h("td", {}, `${t("balance")} ${u.balance} / ${t("available")} ${u.available}`),
      h("td", {}, h("button", { onclick: async () => {
        const credits = Number(prompt(`给 ${u.email} 调整多少积分？（负数为扣减）`));
        const memo = credits ? prompt("原因（会记入审计）") : "";
        if (!credits || !memo) return;
        try { await api.post("/api/admin/credits/adjust", { email: u.email, credits, memo }, { "Idempotency-Key": `adj-${u.id}-${Date.now()}` }); load(); }
        catch (error) { alert(error.message); }
      } }, "调整积分")))))));
  };
  await load();
  return card("用户与积分", h("div", { class: "row" }, search, h("button", { onclick: load }, "搜索")), table);
}

async function adminCodes() {
  const credits = h("input", { type: "number", value: 100, min: 1 });
  const count = h("input", { type: "number", value: 5, min: 1, max: 500 });
  const note = h("input", { placeholder: "备注" });
  const out = h("pre", { class: "codes" });
  return card("兑换码", h("div", { class: "row" }, field("每个积分", credits), field("数量", count), field("备注", note),
    h("button", { class: "primary", onclick: async () => {
      const r = await api.post("/api/admin/redeem-codes", { credits: Number(credits.value), count: Number(count.value), note: note.value });
      out.textContent = r.codes.join("\n");
    } }, "生成")), h("p", { class: "muted" }, "兑换码只显示一次，系统只保存哈希。"), out);
}

async function adminWorkers() {
  const workers = await api.get("/api/admin/workers");
  const name = h("input", { placeholder: "名称" });
  const caps = h("input", { value: "intake,codex,package" });
  const out = h("pre", { class: "codes" });
  return card("Worker",
    h("table", { class: "table" }, h("tbody", {}, workers.map((w) => h("tr", {},
      h("td", {}, w.name), h("td", {}, w.status), h("td", {}, w.capabilities.join(", ")), h("td", {}, w.template_version || "—"),
      h("td", {}, fmtTime(w.last_heartbeat_at)))))),
    h("div", { class: "row" }, field("名称", name), field("能力", caps), h("button", { class: "primary", onclick: async () => {
      const r = await api.post("/api/admin/workers", { name: name.value, capabilities: caps.value.split(",").map((s) => s.trim()).filter(Boolean) });
      out.textContent = `token（只显示一次）：${r.token}`;
    } }, "添加")), out);
}

async function adminPricing() {
  const pricing = await api.get("/api/pricing");
  const editor = h("textarea", { rows: 18, class: "mono", value: JSON.stringify(pricing.rules, null, 2) });
  const status = h("div");
  return card(`定价（当前版本 ${pricing.version}）`, editor, h("div", { class: "row end" }, h("button", { class: "primary", onclick: async () => {
    try { const r = await api.put("/api/admin/pricing", { rules: JSON.parse(editor.value), note: "admin edit" }); fill(status, notice(`已发布版本 ${r.version}`)); }
    catch (error) { fill(status, errorBox(error)); }
  } }, "发布新版本")), status);
}

async function adminLessons() {
  const lessons = await api.get("/api/admin/lessons");
  return card("经验候选（审核后才进入主仓库）", lessons.length ? lessons.map((l) => h("div", { class: "tile" },
    h("pre", { class: "mono" }, JSON.stringify(l.payload, null, 2)),
    h("div", { class: "row end" },
      h("button", { onclick: async () => { await api.post(`/api/admin/lessons/${l.id}`, { status: "rejected" }); route(); } }, "拒绝"),
      h("button", { class: "primary", onclick: async () => { await api.post(`/api/admin/lessons/${l.id}`, { status: "approved" }); route(); } }, "通过")))) : h("p", {}, t("empty")));
}

async function adminLedger() {
  const check = await api.get("/api/admin/ledger/check");
  const audit = await api.get("/api/admin/audit?limit=100");
  return h("div", { class: "stack" },
    card("账本核对", check.ok ? notice("分录平衡，余额与流水一致。") : h("div", { class: "notice error" }, check.issues.map((i) => h("div", {}, i)))),
    card("审计日志", h("table", { class: "table" }, h("tbody", {}, audit.map((a) => h("tr", {},
      h("td", {}, fmtTime(a.at)), h("td", {}, `${a.actor_type}:${a.actor_id.slice(0, 8)}`), h("td", {}, a.action), h("td", {}, a.target.slice(0, 8))))))));
}

// ---------------------------------------------------------------- start

window.addEventListener("hashchange", route);
document.documentElement.lang = lang() === "zh" ? "zh-CN" : "en";
route();

export { buildRequest, freshWizard, markdownView, h };
