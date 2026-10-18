// Interface strings in Chinese and English.  Domain labels (scenarios, styles,
// tiers) come from /api/options with *_en twins; L() picks the one for the current language.
// Page text uses tr("中文", "English") next to the code that shows it.

const STRINGS = {
  zh: {
    home: "首页", pricing: "价格", newJob: "新建任务", myJobs: "我的任务", credits: "积分", admin: "管理后台",
    login: "登录", logout: "退出", language: "English",
    heroTitle: "把论文和资料变成答辩 PPT", heroText: "按学术场景规划内容，逐页生成母版，再重建为可编辑的 PowerPoint。每一步都有你来审核。",
    start: "开始新建", tiers: "交付档位", limits: "需要知道的限制",
    email: "邮箱", sendCode: "发送验证码", code: "验证码", signIn: "登录", codeSent: "验证码已发送", account: "邮箱或手机号", accountHint: "name@example.com 或 13800138000", codeSentSms: "短信验证码已发送", wechatLogin: "微信扫码登录",
    next: "下一步", back: "上一步", save: "保存", submit: "确认下单", cancel: "取消任务", del: "删除",
    estimate: "估价", total: "合计", frozen: "冻结", balance: "余额", available: "可用",
    state: "状态", created: "创建时间", events: "进度", deliverables: "下载", review: "待你审核",
    approve: "通过", revise: "提修改意见", redo: "重做所选页", accept: "接受并交付",
    loading: "加载中…", empty: "暂无", error: "出错了", required: "必填",
  },
  en: {
    home: "Home", pricing: "Pricing", newJob: "New job", myJobs: "My jobs", credits: "Credits", admin: "Admin",
    login: "Sign in", logout: "Sign out", language: "中文",
    heroTitle: "Turn your thesis into a defense deck", heroText: "Content planned per academic scenario, one master image per slide, rebuilt as editable PowerPoint. You review every step.",
    start: "Start", tiers: "Delivery tiers", limits: "Limits to know",
    email: "Email", sendCode: "Send code", code: "Code", signIn: "Sign in", codeSent: "Code sent", account: "Email or phone", accountHint: "name@example.com or a mainland mobile number", codeSentSms: "Text message sent", wechatLogin: "Sign in with WeChat",
    next: "Next", back: "Back", save: "Save", submit: "Place order", cancel: "Cancel job", del: "Delete",
    estimate: "Estimate", total: "Total", frozen: "Frozen", balance: "Balance", available: "Available",
    state: "State", created: "Created", events: "Progress", deliverables: "Downloads", review: "Needs your review",
    approve: "Approve", revise: "Request changes", redo: "Redo selected slides", accept: "Accept and deliver",
    loading: "Loading…", empty: "Nothing yet", error: "Something went wrong", required: "required",
  },
};

export const STATE_LABELS = {
  zh: {
    draft: "草稿", estimated: "已估价", paid: "已冻结积分", queued: "排队中", intake: "解析资料", planning: "规划内容",
    cp1_outline: "待确认大纲", masters: "生成母版", cp2_masters: "待审核母版", image_deck_ready: "图片版完成",
    cp_v_text: "待核对文字", reconstructing: "原生重建", office_qa: "PowerPoint 验收", cp3_native_preview: "待批准原生预览",
    packaging: "打包交付", delivered: "已交付", blocked: "暂停（需处理）", failed: "失败", cancelled: "已取消", deleted: "已删除",
  },
  en: {
    draft: "Draft", estimated: "Estimated", paid: "Credits frozen", queued: "Queued", intake: "Reading sources", planning: "Planning",
    cp1_outline: "Outline review", masters: "Masters", cp2_masters: "Master review", image_deck_ready: "Image deck ready",
    cp_v_text: "Text check", reconstructing: "Native rebuild", office_qa: "PowerPoint QA", cp3_native_preview: "Native preview approval",
    packaging: "Packaging", delivered: "Delivered", blocked: "Blocked", failed: "Failed", cancelled: "Cancelled", deleted: "Deleted",
  },
};

let current = localStorage.getItem("autoppt.lang") || (navigator.language.startsWith("zh") ? "zh" : "en");

export function lang() { return current; }
export function setLang(value) { current = value; localStorage.setItem("autoppt.lang", value); document.documentElement.lang = value === "zh" ? "zh-CN" : "en"; }
export function t(key) { return (STRINGS[current] && STRINGS[current][key]) || STRINGS.zh[key] || key; }
export function stateLabel(state) { return (STATE_LABELS[current] || STATE_LABELS.zh)[state] || state; }
export function tr(zh, en) { return current === "en" ? en : zh; }
export function L(item, key = "label") { return (current === "en" && item[`${key}_en`]) || item[key]; }
