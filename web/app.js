// AutoPPT single-page front end.  No build step and no third-party code.
// Every value from the server is inserted as text (never innerHTML), so
// uploaded or generated content cannot inject markup.

import { api, auth, ApiError, setAccessReason, uploadFile } from "./api.js";
import { L, lang, setLang, stateLabel, t, tr } from "./i18n.js";

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
    items.map((item) => h("option", { value: item.id, selected: String(item.id) === String(value) ? "selected" : null }, L(item))));
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
  const links = [["#/", t("home")], ["#/cases", tr("案例", "Case study")], ["#/pricing", t("pricing")]];
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
    h("span", {}, tr("AutoPPT · 内容准确性由用户最终负责；原生可编辑版不承诺与母版逐像素一致。", "AutoPPT · You are responsible for the accuracy of the content; the native editable deck is not pixel-identical to its masters.")),
  );
}

const ROUTES = [
  [/^#?\/?$/, pageHome],
  [/^#\/pricing$/, pagePricing],
  [/^#\/cases$/, pageCases],
  [/^#\/login(?:\?.*)?$/, pageLogin],
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
    const needsLogin = ![pageHome, pagePricing, pageCases, pageLogin].includes(page);
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

// The 2026-10-17/18 end-to-end run on a fictional thesis made for testing (content and data invented,
// no third-party rights); figures from docs/p1-real-run-masters-reconstruct.md.
const CASE_SLIDES = [
  { file: "S05-side-by-side.jpg",
    zh: "S05 方法：三步流程图重建为原生文本框和形状，示意图保留为有边界的图片。",
    en: "S05 method: the three-step diagram rebuilt as native text boxes and shapes; illustrations stay bounded pictures." },
  { file: "S07-side-by-side.jpg",
    zh: "S07 结果：表格数字来自论文表 1，并配了带工作簿的原生图表（表格下的数据条，作为差异披露）。",
    en: "S07 results: the numbers come from the thesis’s table 1, with native charts that carry their workbook (the bars under the table are a disclosed difference)." },
  { file: "S08-side-by-side.jpg",
    zh: "S08 本人贡献：母版里的“不同模型设置的预警效果对比”柱状图是 ImageGen 画的示意，资料里没有数据。现在母版审核会把这类无数据来源的图表单独报出来。",
    en: "S08 contribution: the master’s ablation bar chart was drawn by ImageGen with no data in the sources. Master review now flags charts like this one." },
];

async function pageCases() {
  const base = "/cases/j-plan-real-001/";
  const facts = lang() === "en" ? [
    ["Source", "a 9-slide master’s defense from one DOCX (fictional test thesis)"],
    ["Reviews", "outline approved; masters: S09 redone (it showed an invented “2.6×”); native preview approved by the user after 5 rebuild rounds"],
    ["Delivered deck", "190 native text boxes, 507 native shapes, 64 bounded pictures, 3 native charts with workbooks"],
    ["Effort", "about 5.1 h of agent time and 44 M input tokens (93% cached) end to end"],
  ] : [
    ["资料", "一份 DOCX 论文（虚构的测试论文），9 页硕士答辩"],
    ["审核", "大纲通过；母版重做 S09（出现资料没有的“提升 2.6 倍”）；原生重建 5 轮后由用户本人批准原生预览"],
    ["交付", "190 个原生文本框、507 个原生形状、64 张有边界的图片、3 个带工作簿的原生图表"],
    ["投入", "全流程代理约 5.1 小时、输入 4,400 万 token（93% 为缓存）"],
  ];
  return h("div", { class: "stack" },
    card(tr("案例：城市内涝预警硕士答辩（测试样例）", "Case: a master’s defense on flood warning (test sample)"),
      notice(tr("这是为联调准备的虚构论文，内容和数据均为虚构；展示的是流程和交付物的真实样子，不是质量承诺。",
        "A fictional thesis made for testing; its content and data are invented. It shows what the process and the deliverables really look like, not a quality promise."), "info"),
      h("table", { class: "table" }, h("tbody", {}, facts.map(([k, v]) => h("tr", {}, h("th", {}, k), h("td", {}, v))))),
      h("img", { class: "contact", src: `${base}contact-sheet.jpg`, alt: tr("9 页原生版联系表", "Contact sheet of the 9 native slides"), loading: "lazy" })),
    card(tr("母版（左）与原生可编辑版（右）", "Master (left) and native editable slide (right)"),
      CASE_SLIDES.map((slide) => h("figure", { class: "compare" },
        h("img", { src: base + slide.file, alt: slide.file.slice(0, 3), loading: "lazy" }),
        h("figcaption", {}, tr(slide.zh, slide.en))))),
    card(tr("如实披露的差异", "Differences disclosed"),
      h("ul", {},
        h("li", {}, tr("字体由母版字形换成微软雅黑，局部图形有差异；用户看过逐页对比图后接受。", "Lettering becomes Microsoft YaHei and some graphics differ; the user accepted after looking at every comparison.")),
        h("li", {}, tr("S08 的示意柱状图没有数据来源（见上）。", "S08’s illustrative bar chart has no source data (see above).")),
        h("li", {}, tr("S08“雷达外推模块”标题字号比母版略小。", "S08’s “radar extrapolation” heading is slightly smaller than in the master.")))),
  );
}

async function pageHome() {
  const opts = await getOptions();
  return h("div", {},
    h("section", { class: "hero" },
      h("h1", {}, t("heroTitle")),
      h("p", {}, t("heroText")),
      h("a", { class: "button primary", href: auth.loggedIn() ? "#/new" : "#/login" }, t("start"))),
    card(t("tiers"), h("div", { class: "grid three" }, opts.delivery_tiers.map((tier) =>
      h("div", { class: "tile" }, h("h3", {}, L(tier)), h("p", { class: "muted" }, tierText(tier.id)))))),
    card(tr("使用场景", "Scenarios"), h("div", { class: "grid three" }, opts.scenarios.map((s) =>
      h("div", { class: "tile" }, h("h3", {}, L(s)), h("p", {}, L(s, "use_when")),
        h("small", { class: "muted" }, tr(`建议 ${s.default_slide_range[0]}–${s.default_slide_range[1]} 页`, `${s.default_slide_range[0]}–${s.default_slide_range[1]} slides suggested`)))))),
    card(t("limits"), h("ul", {}, (lang() === "en" && opts.notices_en ? opts.notices_en : opts.notices).map((n) => h("li", {}, n)))),
  );
}

function tierText(id) {
  return {
    planning_package: tr("大纲、逐页内容、证据对照、精确文字清单和逐页出图提示词。", "Outline, slide-by-slide content, evidence map, exact on-slide text and an image prompt per slide."),
    image_deck: tr("规划包 + 每页一张 AI 生成的完整母版，组装成图片版 PPTX；需审核母版。", "Planning package + one AI-generated master per slide, assembled into an image PPTX; you review the masters."),
    native_editable: tr("图片版 + 文字、形状、图表原生可改；照片等为可替换图片；附逐页对比证据。必须由你批准原生预览。", "Image deck + native, editable text, shapes and charts; photos stay replaceable pictures; slide-by-slide comparison included. You must approve the native preview."),
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
          h("tr", {}, h("th", {}, tr("基础费（解析与规划）", "Base fee (reading and planning)")), h("td", {}, `${r.base} ${tr("积分", "credits")}`)),
          h("tr", {}, h("th", {}, tr("图片版 / 页", "Image deck / slide")), h("td", {}, `${r.per_slide.image_deck} ${tr("积分", "credits")}`)),
          h("tr", {}, h("th", {}, tr("原生可编辑版 / 页", "Native editable / slide")), h("td", {}, `${r.per_slide.native_editable} ${tr("积分", "credits")}`)),
          h("tr", {}, h("th", {}, tr("讲稿 / 预测问题", "Speaker notes / likely questions")), h("td", {}, `${r.addons.speaker_notes} / ${r.addons.anticipated_questions} ${tr("积分", "credits")}`)),
          h("tr", {}, h("th", {}, tr("母版单页重做", "Redo one master")), h("td", {}, tr(`${r.redo_slide} 积分 / 页`, `${r.redo_slide} credits / slide`))),
          h("tr", {}, h("th", {}, tr(`超出 ${r.free_rounds} 轮的返修`, `Revisions beyond ${r.free_rounds} rounds`)), h("td", {}, tr(`${r.extra_round} 积分 / 轮`, `${r.extra_round} credits / round`))))),
      h("p", { class: "muted" }, tr("下单时按页数上限冻结积分，每个阶段完成后结算；取消时只结算已完成的阶段，系统失败自动退还未完成部分。", "Credits for the maximum number of slides are frozen when you order and settled as each stage finishes; cancelling settles only finished stages, and a system failure returns the rest.")),
      h("p", { class: "muted" }, tr("测试期不接在线支付：积分通过兑换码或管理员充值获得。", "No online payment during the beta: credits come from redeem codes or an administrator."))));
}

function accountBody(value) {
  // One field for both: an address with "@" is an email, anything else a mainland mobile number.
  const text = value.trim();
  return text.includes("@") ? { email: text } : { phone: text };
}

async function pageLogin() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const ticket = params.get("wechat_ticket");
  if (ticket) {
    try {
      auth.set(await api.post("/api/auth/wechat/exchange", { ticket }));
      me = null;
      location.hash = "#/jobs";
      return card(t("login"), notice(t("signIn")));
    } catch (error) { return card(t("login"), errorBox(error)); }
  }
  const options = await api.get("/api/auth/options").catch(() => ({ wechat: false }));
  const account = h("input", { required: true, autocomplete: "username", placeholder: t("accountHint") });
  const code = h("input", { inputmode: "numeric", maxlength: 6, autocomplete: "one-time-code" });
  const status = h("div");
  const send = async () => {
    fill(status, );
    try {
      const reply = await api.post("/api/auth/code", accountBody(account.value));
      const sent = reply.channel === "sms" ? t("codeSentSms") : t("codeSent");
      fill(status, notice(reply.dev_code ? tr(`${sent}（开发模式验证码：${reply.dev_code}）`, `${sent} (development code: ${reply.dev_code})`) : sent));
      code.focus();
    } catch (error) { fill(status, errorBox(error)); }
  };
  const login = async (event) => {
    event.preventDefault();
    try {
      auth.set(await api.post("/api/auth/login", { ...accountBody(account.value), code: code.value }));
      me = null;
      location.hash = "#/jobs";
    } catch (error) { fill(status, errorBox(error)); }
  };
  const wechat = async () => {
    try { location.href = (await api.get("/api/auth/wechat/start")).url; } catch (error) { fill(status, errorBox(error)); }
  };
  return card(t("login"),
    h("form", { class: "stack narrow", onsubmit: login },
      field(t("account"), account),
      h("div", { class: "row" }, field(t("code"), code), h("button", { type: "button", onclick: send }, t("sendCode"))),
      h("button", { class: "primary", type: "submit" }, t("signIn")),
      options.wechat ? h("button", { type: "button", class: "secondary", onclick: wechat }, t("wechatLogin")) : null,
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
    style: scenario.default_style, styleExplicit: false, density: "medium_high", audience: tr("答辩委员会", "Defense committee"), language: lang() === "en" ? "en" : "zh-CN",
    cover: { title: "", subtitle: "", author: "", advisor: "", institution: "", date: "" },
    mustInclude: "", mustAvoid: "", charts: "native_with_workbook", fontAccepted: false, aspect: "16:9",
    outlineCheckpoint: true, masterCheckpoint: true, speakerNotes: false, questions: false,
    retention: 7, caseStudy: false, logoRights: false, deadline: "",
    vecPages: "", vecDepth: "text_shapes_charts", vecKeepPhotos: true,
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
  else {
    if (w.slidesCount) request.slides = { mode: "fixed", count: Number(w.slidesCount) };
    request.vectorize = { depth: w.vecDepth, keep_photos_as_images: w.vecKeepPhotos };
    if (w.vecPages.trim()) request.vectorize.pages = w.vecPages.trim();
  }
  if (w.deadline) request.deadline = new Date(w.deadline).toISOString();
  return request;
}

function nonSpace(text) { return [...text].filter((c) => !/\s/.test(c)).length; }

async function pageNewJob() {
  const opts = await getOptions();
  let w = loadWizard() || freshWizard(opts);
  const root = h("div", { class: "wizard" });
  const set = (patch, rerender = false) => { w = { ...w, ...patch }; saveWizard(w); if (rerender) draw(); };
  const steps = [tr("服务与档位", "Service and tier"), tr("资料与描述", "Sources and description"), tr("场景、页数与风格", "Scenario, slides and style"), tr("高级选项", "Advanced"), tr("估价与确认", "Estimate and confirm")];

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
    return card(tr("① 服务类型与交付档位", "① Service and delivery tier"),
      h("div", { class: "grid two" }, opts.services.map((s) => h("button", {
        class: `choice ${w.service === s.id ? "selected" : ""}`,
        onclick: () => set({ service: s.id, tier: s.id === "vectorize" ? "native_editable" : w.tier }, true),
      }, h("strong", {}, L(s)), h("small", {}, s.id === "vectorize" ? tr("把图片版 PPT/截图/PDF 转成可编辑 PPTX（测试中）", "Turn an image deck, screenshots or a PDF into an editable PPTX (beta)") : tr("用论文、报告或描述生成答辩 PPT", "Build a defense deck from a thesis, report or description"))))),
      h("h3", {}, t("tiers")),
      h("div", { class: "grid three" }, opts.delivery_tiers.map((tier) => h("button", {
        class: `choice ${w.tier === tier.id ? "selected" : ""}`,
        disabled: w.service === "vectorize" && tier.id !== "native_editable",
        onclick: () => set({ tier: tier.id }, true),
      }, h("strong", {}, L(tier)), h("small", {}, tierText(tier.id))))),
      nav());
  }

  function stepFiles() {
    const list = h("div", { class: "files" });
    const roles = opts.file_roles;
    const uploading = () => w.files.some((f) => !f.sha256 && !f.failed);
    const navBar = nav(true);
    const syncNext = () => { const next = navBar.querySelector("button.primary"); if (next) next.disabled = uploading(); };
    const drawFiles = () => { syncNext(); updateHints(); fill(list, ...w.files.map((f, index) => h("div", { class: "file-row" },
      h("span", { class: "file-name" }, f.name),
      h("span", { class: "muted" }, f.status || ""),
      select(roles, f.role, (role) => { w.files[index].role = role; saveWizard(w); }),
      h("button", { class: "link", onclick: () => { w.files.splice(index, 1); saveWizard(w); drawFiles(); } }, tr("移除", "Remove"))))); };
    const input = h("input", { type: "file", multiple: true, onchange: async (event) => {
      for (const file of event.target.files) {
        const entry = { file_id: `f_${String(w.files.length + 1).padStart(2, "0")}`, name: file.name, role: guessRole(file.name, w.service), status: tr("上传中 0%", "uploading 0%") };
        w.files.push(entry); drawFiles();
        try {
          const done = await uploadFile(file, (sent, total) => { entry.status = tr(`上传中 ${Math.round((100 * sent) / total)}%`, `uploading ${Math.round((100 * sent) / total)}%`); drawFiles(); });
          Object.assign(entry, { sha256: done.sha256, status: tr(`已上传（${done.kind}）`, `uploaded (${done.kind})`) });
        } catch (error) { Object.assign(entry, { failed: true, status: `${tr("失败：", "failed: ")}${error.message}` }); }
        saveWizard(w); drawFiles();
      }
      event.target.value = "";
    } });
    const hasDocs = () => w.files.some((f) => ["primary_source", "supplementary", "chart_data"].includes(f.role) && f.sha256);
    const counter = h("small", { class: "hint" });
    const hints = h("ul", { class: "hints" });
    const updateHints = () => {
      const count = nonSpace(w.description);
      counter.textContent = hasDocs() ? tr(`${count} 字（有文档时作为重点说明）`, `${count} characters (with documents, this says what to stress)`) : tr(`${count} / ${opts.limits.min_description_chars_without_documents} 字（未上传文档时必填）`, `${count} / ${opts.limits.min_description_chars_without_documents} characters (required without documents)`);
      const tips = [];
      if (!hasDocs()) {
        if (count < opts.limits.min_description_chars_without_documents) tips.push(tr("没有文档时，请写清主题、目的、核心结论和关键数据。", "Without documents, state the topic, the purpose, the main conclusions and the key data."));
        if (!/\d/.test(w.description)) tips.push(tr("没有数字：只凭描述生成的内容会标注为“框架或示意”，不会编造数据。", "No numbers: content built only from a description is marked as a framework or illustration; no data is invented."));
      }
      fill(hints, ...tips.map((tip) => h("li", {}, tip)));
    };
    const description = h("textarea", { rows: 8, value: w.description, "aria-label": tr("描述文字", "Description"), oninput: (e) => { set({ description: e.target.value }); updateHints(); } });
    drawFiles();
    return card(tr("② 资料与描述", "② Sources and description"),
      h("p", { class: "muted" }, tr("为每个文件选择用途：主资料、补充资料、图表数据、图片素材、原图保留（不让 AI 重绘）、Logo、风格参考、待矢量化文件。支持 DOCX、PDF、PPTX、XLSX、CSV、MD/TXT/TEX、PNG/JPG/WebP/HEIC、ZIP。", "Choose a role for each file: main source, supplementary material, chart data, images, keep as is (never redrawn by AI), logo, style reference, or pages to make editable. DOCX, PDF, PPTX, XLSX, CSV, MD/TXT/TEX, PNG/JPG/WebP/HEIC and ZIP are accepted.")),
      input, list,
      field(tr("描述文字", "Description"), description, null), counter, hints,
      navBar);
  }

  function stepScenario() {
    const scenario = opts.scenarios.find((s) => s.id === w.scenario);
    const style = opts.styles.find((s) => s.id === w.style);
    const slides = w.slidesMode === "fixed"
      ? field(tr("页数", "Slides"), h("input", { type: "number", min: 1, max: opts.limits.max_slides, value: w.slidesCount, oninput: (e) => set({ slidesCount: e.target.value }) }))
      : w.slidesMode === "range"
        ? h("div", { class: "row" },
          field(tr("最少", "At least"), h("input", { type: "number", min: 1, max: opts.limits.max_slides, value: w.slidesMin, oninput: (e) => set({ slidesMin: e.target.value }) })),
          field(tr("最多", "At most"), h("input", { type: "number", min: 1, max: opts.limits.max_slides, value: w.slidesMax, oninput: (e) => set({ slidesMax: e.target.value }) })))
        : null;
    return card(tr("③ 场景、页数与风格", "③ Scenario, slides and style"),
      w.service === "new_deck" ? [
        h("h3", {}, tr("使用场景", "Scenario")),
        h("div", { class: "grid three" }, opts.scenarios.map((s) => h("button", {
          class: `choice ${w.scenario === s.id ? "selected" : ""}`,
          onclick: () => set({ scenario: s.id, slidesMin: s.default_slide_range[0], slidesMax: s.default_slide_range[1], style: w.styleExplicit ? w.style : s.default_style }, true),
        }, h("strong", {}, L(s)), h("small", {}, L(s, "use_when"))))),
        h("h3", {}, tr("页数", "Slides")),
        h("div", { class: "row" },
          select(opts.slide_modes, w.slidesMode, (mode) => set({ slidesMode: mode }, true)),
          slides,
          field(tr("演讲时长（分钟）", "Talk length (minutes)"), h("input", { type: "number", min: 1, max: 180, value: w.talkMinutes, oninput: (e) => set({ talkMinutes: e.target.value }) }),
            scenario ? tr(`本场景约每页 ${scenario.default_duration_seconds} 秒`, `about ${scenario.default_duration_seconds} s per slide in this scenario`) : null)),
      ] : h("div", { class: "stack" },
        h("div", { class: "row" },
          field(tr("要转换的页数", "Pages to convert"), h("input", { type: "number", min: 1, max: opts.limits.max_slides, value: w.slidesCount, oninput: (e) => set({ slidesCount: e.target.value }) })),
          field(tr("页码选择（可选）", "Pages (optional)"), h("input", { placeholder: tr("例如 1-5,8", "e.g. 1-5,8"), value: w.vecPages, oninput: (e) => set({ vecPages: e.target.value }) }), tr("留空表示全部页", "empty = all pages")),
          field(tr("可编辑深度", "Editable depth"), select([
            { id: "text", label: tr("只做文字", "Text only") }, { id: "text_shapes", label: tr("文字 + 形状", "Text + shapes") }, { id: "text_shapes_charts", label: tr("文字 + 形状 + 图表", "Text + shapes + charts") },
          ], w.vecDepth, (v) => set({ vecDepth: v })))),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.vecKeepPhotos, onchange: (e) => set({ vecKeepPhotos: e.target.checked }) }),
          tr("照片和复杂插图保留为可移动的图片", "Keep photos and complex artwork as movable pictures")),
        notice(tr("上传的页面会作为“用户提供的母版”冻结，不经过 AI 重绘；识别出的文字会先交给你核对。", "Your pages are frozen as user-supplied masters and never redrawn by AI; the recognised text comes to you for checking first."), "info")),
      h("h3", {}, tr("风格", "Style")),
      h("div", { class: "grid three" }, opts.styles.map((s) => h("button", {
        class: `choice ${w.style === s.id ? "selected" : ""}`,
        onclick: () => set({ style: s.id, styleExplicit: false }, true),
      }, h("strong", {}, L(s), s.is_default ? tr("（默认）", " (default)") : ""), h("small", {}, L(s, "use_when")),
        s.sample_image ? h("img", { class: "style-sample", src: s.sample_image, alt: tr(`${s.label} 示例页`, `${L(s)} sample slide`), loading: "lazy" }) : swatches(s.palette)))),
      style && style.requires_explicit_choice ? h("label", { class: "check" },
        h("input", { type: "checkbox", checked: w.styleExplicit, onchange: (e) => set({ styleExplicit: e.target.checked }) }),
        tr(`我确认选择“${style.label}”（${style.dark ? "深色" : "极简"}风格需要主动确认）`, `I choose “${L(style)}” (a ${style.dark ? "dark" : "minimal"} style needs an explicit choice)`)) : null,
      h("div", { class: "row" },
        field(tr("受众", "Audience"), h("input", { list: "audiences", value: w.audience, oninput: (e) => set({ audience: e.target.value }) })),
        h("datalist", { id: "audiences" }, (lang() === "en" && opts.audiences_en ? opts.audiences_en : opts.audiences).map((a) => h("option", { value: a }))),
        field(tr("语言", "Language"), select(opts.languages, w.language, (v) => set({ language: v }))),
        field(tr("信息密度", "Information density"), select(opts.densities, w.density, (v) => set({ density: v })))),
      nav());
  }

  function stepAdvanced() {
    const coverField = (key, label) => field(label, h("input", { value: w.cover[key], oninput: (e) => { w.cover[key] = e.target.value; saveWizard(w); } }));
    const usesLogo = w.files.some((f) => f.role === "logo");
    return card(tr("④ 高级选项", "④ Advanced"),
      h("h3", {}, tr("封面信息（按原文进入页面）", "Cover (used exactly as typed)")),
      h("div", { class: "grid three" }, coverField("title", tr("标题", "Title")), coverField("subtitle", tr("副标题", "Subtitle")), coverField("author", tr("汇报人", "Presenter")),
        coverField("advisor", tr("导师", "Supervisor")), coverField("institution", tr("单位", "Institution")), coverField("date", tr("日期", "Date"))),
      h("div", { class: "grid two" },
        field(tr("必须包含（每行一条）", "Must include (one per line)"), h("textarea", { rows: 4, value: w.mustInclude, oninput: (e) => set({ mustInclude: e.target.value }) })),
        field(tr("必须避免（每行一条）", "Must avoid (one per line)"), h("textarea", { rows: 4, value: w.mustAvoid, oninput: (e) => set({ mustAvoid: e.target.value }) }))),
      h("div", { class: "row" },
        field(tr("画幅", "Aspect ratio"), select(opts.aspect_ratios, w.aspect, (v) => set({ aspect: v }))),
        w.tier === "native_editable" ? field(tr("图表", "Charts"), select(opts.chart_modes.filter((c) => c.id !== "in_image"), w.charts, (v) => set({ charts: v }))) : null,
        field(tr("数据保留", "Data retention"), select(opts.retention_days.map((d) => ({ id: d, label: tr(`${d} 天后删除`, `delete after ${d} days`) })), w.retention, (v) => set({ retention: Number(v) }))),
        field(tr("截止时间", "Deadline"), h("input", { type: "datetime-local", value: w.deadline, oninput: (e) => set({ deadline: e.target.value }) }))),
      w.tier === "native_editable" ? h("label", { class: "check" },
        h("input", { type: "checkbox", checked: w.fontAccepted, onchange: (e) => set({ fontAccepted: e.target.checked }) }),
        tr("接受用 Office 字体（微软雅黑/等线）替代母版字形（原生可编辑版必须勾选）", "Accept Office fonts (Microsoft YaHei / DengXian) in place of the masters’ lettering (required for the native editable deck)")) : null,
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.outlineCheckpoint, onchange: (e) => set({ outlineCheckpoint: e.target.checked }) }), tr("大纲确认（CP1）", "Outline review (CP1)")),
      w.tier !== "planning_package" ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.masterCheckpoint, onchange: (e) => set({ masterCheckpoint: e.target.checked }) }), tr("母版审核（CP2）", "Master review (CP2)")) : null,
      w.tier === "native_editable" ? h("label", { class: "check disabled" }, h("input", { type: "checkbox", checked: true, disabled: true }), tr("原生预览批准（CP3，不能关闭）", "Native preview approval (CP3, always on)")) : null,
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.speakerNotes, onchange: (e) => set({ speakerNotes: e.target.checked }) }), tr("讲稿（单独计费）", "Speaker notes (charged separately)")),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.questions, onchange: (e) => set({ questions: e.target.checked }) }), tr("预测问题（单独计费）", "Likely questions (charged separately)")),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.caseStudy, onchange: (e) => set({ caseStudy: e.target.checked }) }), tr("允许脱敏后作为案例展示（默认不允许）", "Allow use as an anonymised showcase (off by default)")),
      usesLogo ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: w.logoRights, onchange: (e) => set({ logoRights: e.target.checked }) }), tr("我确认拥有所上传 Logo 的使用权", "I confirm I may use the uploaded logo")) : null,
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
          quote.provisional ? notice(tr("测试期价格，P1 实测后调整。", "Beta prices, to be adjusted after measured runs."), "warn") : null,
          h("table", { class: "table" }, h("tbody", {}, rows,
            h("tr", { class: "total" }, h("th", {}, t("total")), h("td", {}, `${quote.total} ${tr("积分", "credits")}`)))),
          h("p", {}, tr(`按 ${quote.slides_for_hold} 页冻结 ${quote.hold} 积分；当前可用 ${credits.available}。每个阶段完成后结算，取消时只结算已完成阶段。`, `${quote.hold} credits frozen for ${quote.slides_for_hold} slides; ${credits.available} available. Each stage settles when it finishes; cancelling settles only finished stages.`)),
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
            } }, `${t("submit")} (${quote.total} ${tr("积分", "credits")})`)),
          credits.available < quote.total ? notice(tr("积分不足：请先在“积分”页使用兑换码。", "Not enough credits: redeem a code on the Credits page first."), "error") : null);
      } catch (error) {
        fill(box, errorBox(error), h("div", { class: "row end" }, h("button", { onclick: () => set({ step: 4 }, true) }, t("back"))));
      }
    })();
    return card(tr("⑤ 估价与确认", "⑤ Estimate and confirm"), box);
  }

  draw();
  return h("div", {}, h("div", { class: "row space" }, h("h1", {}, t("newJob")),
    h("button", { class: "link", onclick: () => { sessionStorage.removeItem(DRAFT_KEY); w = freshWizard(opts); draw(); } }, tr("重新开始", "Start over"))), root);
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
  return (lang() === "en" ? { intake: "Reading", planning: "Planning", masters: "Masters", reconstruct: "Native rebuild", package: "Packaging and add-ons" }
    : { intake: "解析", planning: "规划", masters: "母版", reconstruct: "原生重建", package: "打包与附加项" })[phase] || phase;
}

// ---------------------------------------------------------------- jobs

async function pageJobs() {
  const jobs = await api.get("/api/jobs");
  if (!jobs.length) return card(t("myJobs"), h("p", {}, t("empty")), h("a", { class: "button primary", href: "#/new" }, t("newJob")));
  return card(t("myJobs"), h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, tr("标题", "Title")), h("th", {}, t("state")), h("th", {}, t("credits")), h("th", {}, t("created")))),
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
        job.hold ? h("span", { class: "muted" }, tr(`已结算 ${job.hold.amount - job.hold.remaining} / 冻结 ${job.hold.amount}`, `settled ${job.hold.amount - job.hold.remaining} / frozen ${job.hold.amount}`)) : null),
      job.state === "blocked" ? notice(`${tr("任务暂停：", "Paused: ")}${job.blocked_reason}`, "error") : null,
      progressBar(job),
      h("div", { class: "row" },
        !["delivered", "failed", "cancelled", "draft", "estimated"].includes(job.state) ? h("button", { class: "danger", onclick: async () => {
          if (!confirm(tr("取消后只结算已完成的阶段，未完成部分的冻结积分会释放。确定取消？", "Only finished stages are charged and the rest of the frozen credits is released. Cancel the job?"))) return;
          await api.post(`/api/jobs/${jobId}/cancel`); me = null; await draw(); await renderNav();
        } }, t("cancel")) : null,
        ["delivered", "failed", "cancelled", "draft", "estimated"].includes(job.state) ? h("button", { class: "link", onclick: async () => {
          if (!confirm(tr("删除后任务资料和产物无法恢复。确定删除？", "The job’s files and outputs cannot be recovered after deletion. Delete?"))) return;
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

  try {
    await draw();
  } catch (error) {
    if (error instanceof ApiError && error.status === 403 && String(error.message).includes("X-Access-Reason")) {
      const reason = prompt("查看其他用户的任务需要填写原因（会记入审计日志）：");
      if (reason && reason.trim()) { setAccessReason(jobId, reason.trim()); return pageJob(jobId); }
    }
    throw error;
  }
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
    if (charge > 0 && !confirm(tr(`这一步将扣除 ${charge} 积分，确认继续？`, `This step costs ${charge} credits. Continue?`))) return;
    try {
      await api.post(`/api/jobs/${job.id}/checkpoint/${cp.checkpoint_id}/response`, { ...body, charge_confirmed: charge > 0 });
      me = null; await redraw(); await renderNav();
    } catch (error) { fill(status, errorBox(error)); }
  };
  const extraRound = () => (cp.round >= (cp.pricing.free_rounds || 2) ? cp.pricing.extra_round || 0 : 0);
  const kinds = lang() === "en" ? { outline: "Outline", masters: "Masters", native_preview: "Native preview", vectorize_text: "Text check" }
    : { outline: "大纲确认", masters: "母版审核", native_preview: "原生预览批准", vectorize_text: "文字核对" };
  const title = `${t("review")} · ${kinds[cp.type]}${tr(`（第 ${cp.round} 轮）`, ` (round ${cp.round})`)}`;
  if (cp.type === "vectorize_text") return card(title, cp.summary ? h("p", {}, cp.summary) : null, await textReview(cp, send), status);
  if (cp.type === "outline") return card(title, cp.summary ? h("p", {}, cp.summary) : null, await outlineReview(cp, send, extraRound), status);
  if (cp.type === "masters") return card(title, cp.summary ? h("p", {}, cp.summary) : null, mastersReview(cp, send, extraRound), status);
  return card(title, nativeReview(cp, send, extraRound), status);
}

async function outlineReview(cp, send, extraRound) {
  const documentPreview = cp.previews.find((p) => p.kind === "document");
  const text = documentPreview && documentPreview.url ? await (await fetch(documentPreview.url)).text() : "";
  const slideIds = [...text.matchAll(/^##\s+(S\d{2})/gm)].map((m) => m[1]);
  const notes = Object.fromEntries(slideIds.map((id) => [id, ""]));
  const general = h("textarea", { rows: 3, placeholder: tr("整体意见（可选）", "Overall comments (optional)") });
  return h("div", { class: "stack" },
    markdownView(text),
    slideIds.length ? h("details", {}, h("summary", {}, tr("逐页批注", "Comments per slide")), slideIds.map((id) =>
      field(id, h("input", { oninput: (e) => { notes[id] = e.target.value; } })))) : null,
    general,
    h("div", { class: "row end" },
      h("button", { onclick: () => {
        const annotations = Object.entries(notes).filter(([, v]) => v.trim()).map(([slide_id, v]) => ({ slide_id, text: v.trim() }));
        if (!annotations.length && !general.value.trim()) { alert(tr("请写出要修改的地方", "Say what should change")); return; }
        send({ decision: "revise", user_reply: general.value, annotations }, extraRound());
      } }, t("revise")),
      h("button", { class: "primary", onclick: () => send({ decision: "approve", user_reply: general.value || "通过" }, 0) }, t("approve"))));
}

async function textReview(cp, send) {
  const documentPreview = cp.previews.find((p) => p.kind === "document");
  const review = documentPreview && documentPreview.url ? await (await fetch(documentPreview.url)).json() : { slides: [] };
  const images = Object.fromEntries(cp.previews.filter((p) => p.kind === "slide").map((p) => [p.slide_id, p.url]));
  const corrections = {};
  const added = {};
  const pages = review.slides.map((slide) => h("div", { class: "text-review" },
    images[slide.slide_id] ? h("img", { src: images[slide.slide_id], alt: slide.slide_id, loading: "lazy" }) : null,
    h("div", { class: "stack" },
      h("strong", {}, slide.slide_id),
      slide.lines.map((line) => {
        const record = (value) => { (corrections[slide.slide_id] = corrections[slide.slide_id] || {})[line.line_id] = value; };
        const input = h("input", {
          value: line.text, "aria-label": `${slide.slide_id} ${line.line_id}`, class: line.suspect ? "suspect" : "",
          oninput: (e) => record(e.target.value),
        });
        // OCR sometimes splits one character into its components (涝 -> 氵劳); offer the fix, never apply it silently.
        const hint = line.suggestion ? h("button", { class: "link", type: "button", onclick: () => { input.value = line.suggestion; record(line.suggestion); } },
          tr(`采用建议：${line.suggestion}`, `Use suggestion: ${line.suggestion}`)) : line.suspect ? h("small", { class: "hint" }, tr("这一行可能识别有误，请核对", "This line may be misread; please check it")) : null;
        return h("div", { class: "stack tight" }, input, hint);
      }),
      h("textarea", { rows: 2, placeholder: tr("识别漏掉的文字（每行一条，可选）", "Text the recognition missed (one per line, optional)"),
        oninput: (e) => { added[slide.slide_id] = e.target.value.split("\n").map((s) => s.trim()).filter(Boolean); } }),
      h("small", { class: "muted" }, tr("清空一行即删除该行。", "Empty a line to delete it.")))));
  return h("div", { class: "stack" },
    notice(tr("请逐页核对识别出的文字。核对后的文字会作为最终页面文字使用，之后不会再被识别结果覆盖。", "Check the recognised text slide by slide. The checked text becomes the final slide text and is never overwritten by recognition again."), "warn"),
    pages,
    h("div", { class: "row end" }, h("button", { class: "primary", onclick: () => send({
      decision: "approve", user_reply: "文字已核对", answers: { text_corrections: corrections, added_lines: added },
    }, 0) }, tr("确认文字，开始重建", "Confirm the text and rebuild"))));
}

function mastersReview(cp, send, extraRound) {
  const chosen = new Set();
  const reason = h("textarea", { rows: 3, placeholder: tr("重做原因（勾选的页）", "Why redo the ticked slides") });
  const slides = cp.previews.filter((p) => p.kind === "slide");
  const sheet = cp.previews.find((p) => p.kind === "contact_sheet");
  return h("div", { class: "stack" },
    sheet && sheet.url ? h("img", { class: "contact", src: sheet.url, alt: tr("联系表", "Contact sheet") }) : null,
    h("div", { class: "gallery" }, slides.map((p) => h("figure", {},
      h("a", { href: p.url, target: "_blank", rel: "noopener" }, h("img", { src: p.url, alt: p.slide_id, loading: "lazy" })),
      h("figcaption", {}, h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => (e.target.checked ? chosen.add(p.slide_id) : chosen.delete(p.slide_id)) }), tr(`${p.slide_id} 重做`, `redo ${p.slide_id}`)))))),
    reason,
    h("div", { class: "row end" },
      h("button", { onclick: () => {
        if (!chosen.size) { alert(tr("请先勾选要重做的页", "Tick the slides to redo first")); return; }
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
  const reply = h("textarea", { rows: 3, placeholder: tr("请用一句话写下你的决定，例如：接受预览中的非文字差异，优先真实可编辑", "Write your decision in one sentence, e.g. I accept the non-text differences in the preview; real editability first") });
  const scope = cp.editability_scope || {};
  return h("div", { class: "stack" },
    notice(tr("原生可编辑版与母版之间会有字体和局部图形差异。请逐页查看对比图和差异热图；你的原话会记录在验收记录里。", "The native editable deck differs from the masters in fonts and some graphics. Look at every comparison and heat map; your own words go into the acceptance record."), "warn"),
    h("p", {}, tr(`原生可编辑：${(scope.native || []).join("、") || "—"}；以图片保留：${(scope.image || []).join("、") || "—"}`, `Native and editable: ${(scope.native || []).join(", ") || "—"}; kept as pictures: ${(scope.image || []).join(", ") || "—"}`)),
    sides.map((p) => h("figure", { class: "compare" },
      h("img", { src: p.url, alt: tr(`${p.slide_id} 对比`, `${p.slide_id} comparison`), loading: "lazy" }),
      heat[p.slide_id] && heat[p.slide_id].url ? h("details", {}, h("summary", {}, tr(`${p.slide_id} 差异热图`, `${p.slide_id} difference heat map`)), h("img", { src: heat[p.slide_id].url, alt: tr(`${p.slide_id} 差异`, `${p.slide_id} differences`), loading: "lazy" })) : null,
      h("figcaption", {}, p.slide_id))),
    reply,
    h("div", { class: "row end" },
      h("button", { onclick: () => {
        if (!reply.value.trim()) { alert(tr("请写出需要返修的问题", "Say what needs fixing")); return; }
        send({ decision: "revise", user_reply: reply.value }, extraRound());
      } }, t("revise")),
      h("button", { class: "primary", onclick: () => {
        if (!reply.value.trim()) { alert(tr("批准原生预览需要你写一句话说明接受的范围", "Approving the native preview needs one sentence saying what you accept")); return; }
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
  const orderStatus = lang() === "en" ? { pending: "Awaiting payment", paid: "Paid", refunded: "Refunded", expired: "Expired" }
    : { pending: "待支付", paid: "已支付", refunded: "已退款", expired: "已过期" };
  const buy = async (pkg) => {
    try {
      const order = await api.post("/api/orders", { package: pkg.id, provider: shop.providers[0] });
      fill(status, notice(tr(`订单已创建（${(order.amount_cents / 100).toFixed(2)} 元）：${order.payment.instructions || "请按支付页面提示完成支付"}`, `Order created (CNY ${(order.amount_cents / 100).toFixed(2)}): ${order.payment.instructions || "follow the payment page"}`)));
    } catch (error) { fill(status, errorBox(error)); }
  };
  return h("div", { class: "stack" },
    card(tr("积分包", "Credit packs"),
      shop.providers.length ? null : notice(tr("在线支付尚未开通（测试期）：请使用兑换码或联系管理员充值。", "Online payment is not open yet (beta): use a redeem code or ask an administrator."), "warn"),
      h("div", { class: "grid three" }, shop.packages.map((pkg) => h("div", { class: "tile" },
        h("strong", { class: "big" }, (lang() === "en" ? pkg.label_en : pkg.label) || `${pkg.credits} ${tr("积分", "credits")}`),
        h("p", { class: "muted" }, tr(`${(pkg.price_cents / 100).toFixed(2)} 元`, `CNY ${(pkg.price_cents / 100).toFixed(2)}`)),
        h("button", { class: "primary", disabled: !shop.providers.length, onclick: () => buy(pkg) }, tr("购买", "Buy")))))),
    orders.length ? card(tr("订单", "Orders"), h("table", { class: "table" }, h("tbody", {}, orders.map((o) => h("tr", {},
      h("td", {}, fmtTime(o.created_at)), h("td", {}, `${o.credits} ${tr("积分", "credits")}`), h("td", {}, tr(`${(o.amount_cents / 100).toFixed(2)} 元`, `CNY ${(o.amount_cents / 100).toFixed(2)}`)),
      h("td", {}, orderStatus[o.status] || o.status, o.refunded_credits ? tr(`（已退 ${o.refunded_credits} 积分）`, ` (${o.refunded_credits} credits refunded)`) : "")))))) : null,
    card(t("credits"),
      h("div", { class: "grid three" },
        h("div", { class: "tile" }, h("small", {}, t("balance")), h("strong", { class: "big" }, data.balance)),
        h("div", { class: "tile" }, h("small", {}, t("frozen")), h("strong", { class: "big" }, data.frozen)),
        h("div", { class: "tile" }, h("small", {}, t("available")), h("strong", { class: "big" }, data.available))),
      h("form", { class: "row", onsubmit: async (e) => {
        e.preventDefault();
        try { const r = await api.post("/api/credits/redeem", { code: code.value }); me = null; fill(status, notice(tr(`已到账 ${r.credited} 积分`, `${r.credited} credits added`))); setTimeout(route, 800); }
        catch (error) { fill(status, errorBox(error)); }
      } }, field(tr("兑换码", "Redeem code"), code), h("button", { class: "primary", type: "submit" }, tr("兑换", "Redeem"))),
      status),
    card(tr("积分流水", "Credit history"), data.statement.length ? h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", {}, tr("时间", "Time")), h("th", {}, tr("类型", "Type")), h("th", {}, t("credits")), h("th", {}, tr("说明", "Note")))),
      h("tbody", {}, data.statement.map((row) => h("tr", {},
        h("td", {}, fmtTime(row.created_at)), h("td", {}, (lang() === "en" ? { topup: "Top-up", grant: "Grant", redeem: "Redeem", settle: "Settled", refund: "Refund", adjust: "Adjustment", expire: "Expired" }
          : { topup: "充值", grant: "赠送", redeem: "兑换", settle: "结算", refund: "退款", adjust: "调整", expire: "过期" })[row.kind] || row.kind),
        h("td", { class: row.amount < 0 ? "neg" : "pos" }, row.amount > 0 ? `+${row.amount}` : row.amount),
        h("td", {}, row.memo || (row.job_id ? h("a", { href: `#/jobs/${row.job_id}` }, tr("任务", "Job")) : "")))))) : h("p", {}, t("empty"))));
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
      } }, "恢复") : null,
      h("button", { class: "secondary", onclick: async () => {
        const credits = Number(prompt("退回多少积分给用户？（不超过该任务已结算的积分）"));
        const reason = credits > 0 ? prompt("原因（会通知用户并记入审计）") : "";
        if (!(credits > 0) || !reason) return;
        try {
          const result = await api.post(`/api/admin/jobs/${job.id}/refund`, { credits, reason }, { "Idempotency-Key": crypto.randomUUID() });
          alert(`已退回 ${credits} 积分；该任务还可退 ${result.refundable} 积分`);
        } catch (error) { alert(error.message); }
      } }, "退积分")))))));
}

async function adminUsers() {
  const search = h("input", { placeholder: "邮箱" });
  const table = h("div");
  const load = async () => {
    const users = await api.get(`/api/admin/users?email=${encodeURIComponent(search.value)}`);
    fill(table, h("table", { class: "table" }, h("tbody", {}, users.map((u) => h("tr", {},
      h("td", {}, u.email || u.phone || (u.wechat ? "微信用户" : u.id.slice(0, 8))), h("td", {}, `${t("balance")} ${u.balance} / ${t("available")} ${u.available}`),
      h("td", {}, h("button", { onclick: async () => {
        const who = u.email || u.phone;
        if (!who) { alert("这个账号只绑定了微信，暂不能按邮箱或手机号调整"); return; }
        const credits = Number(prompt(`给 ${who} 调整多少积分？（负数为扣减）`));
        const memo = credits ? prompt("原因（会记入审计）") : "";
        if (!credits || !memo) return;
        try { await api.post("/api/admin/credits/adjust", { email: who, credits, memo }, { "Idempotency-Key": `adj-${u.id}-${Date.now()}` }); load(); }
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
