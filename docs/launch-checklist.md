# 内测与上线清单（P8）

只列上线前必须由人确认或操作的事项；每项完成时写上日期和负责人。代码侧的状态见 [开发清单](development-checklist.md)。

## 1. 内测前（技术）

- [x] CI 拉取工作流仓库：2026-10-18 Claude 在 `Zero-2X/autoppt` 加了只读 deploy key “autoppt-web CI (read-only)”，私钥存为 secret `AUTOPPT_REPO_SSH_KEY`（不再需要 `AUTOPPT_REPO_TOKEN`）；撤销时两处一起删
- [ ] Worker 机器：专用 Windows 账号自动登录；运行 `scripts/install_worker.ps1`；`worker_check` 全部通过（含 office 能力）
- [ ] 完成 Codex elevated 沙箱安装并配置 `deny_read`，重跑[安全模型](security-model.md)第 1 节探针，确认读取也被拦截
- [ ] Worker 出网白名单：在 Worker 机器上以管理员运行 `scripts/worker_firewall.ps1 -AllowHost <API 与存储主机> -Apply`，并建每小时重跑的计划任务（`-Apply -Quiet`）
- [ ] 在 Worker 机器上跑 48 小时压测：`python scripts/stress_test.py --minutes 2880 --database-url <PostgreSQL>`（mock Worker）以及真实 Office 卡死恢复
- [ ] 生产配置：`AUTOPPT_API_SECRET_KEY`、`AUTOPPT_API_STORAGE_KEY`（静态加密）、PostgreSQL、对象存储、SMTP；`alembic upgrade head`
- [ ] 登录渠道：SMTP（`AUTOPPT_API_SMTP_URL`、`MAIL_SENDER`）；短信中继（`SMS_PROVIDER=webhook`、`SMS_WEBHOOK_URL`、`SMS_WEBHOOK_SECRET`，中继按 `X-AutoPPT-Signature` 验签后调用短信厂商的已审核模板）；微信开放平台网站应用（`WECHAT_APP_ID`、`WECHAT_APP_SECRET`，回调域名填 `PUBLIC_BASE_URL`）
- [ ] 计划任务：`python -m autoppt_api.maintenance all` 每 5 分钟（通知、过期、对账、告警、账本核对、清理）
- [ ] 补全 `autoppt_service/templates/redactions.json` 后构建 Worker 模板；处理模板清单里列出的“代码中含案例名”的脚本
- [ ] 原生重建阶段（P1/P5 未完成的部分）上线前，只开放“规划包”和“图片版”两个档位
- [ ] 确认 Codex 账号仍有带 `image_gen` 的模型（2026-10-18 当前账号的 `gpt-6-luna`、`gpt-5.6-terra` 都没有，新任务会在母版阶段报 `image_gen_unavailable`）；有了以后跑 `python scripts/build_style_samples.py --jobs-root <目录>` 生成风格示例图

## 2. 金标准样例

- [ ] 准备 3–5 份脱敏样例（论文答辩、国自然申请、组会报告、纯描述无文档、矢量化），放在仓库外的样例目录，每份一个 `request.json` 和资料
- [ ] `python scripts/run_samples.py <样例目录> --jobs-root … --template … --profiles …` 实跑，结果写入 `sample-results.jsonl`
- [ ] 每次升级 Worker 模板后重跑，对比质量（人工看大纲和母版）、耗时和 token
- [ ] 据实测结果更新价格表（`/api/admin/pricing`，去掉 `provisional`）

已有实测：1 份虚构论文样例 intake + planning 到 CP1（[记录](p1-real-planning-run.md)）。

## 3. 合规与运营（需要负责人）

- [ ] 拍板 D3（矢量化母版来源）、D4（目标市场）、D7（v66 等参考素材的权属）
- [ ] 账号池条款风险评估（内测期本机 Codex 账号池；商用前按 D2 改用 API key）
- [ ] 用户协议与隐私政策，含[积分结算规则](settlement-rules.md)、学术诚信免责、内容准确性由用户负责、不承诺逐像素一致
- [ ] 中国大陆：ICP 备案、生成式 AI 服务备案或登记、内容安全审核
- [ ] 支付：商户号、回调地址、退款与开票流程（P4 真实渠道）
- [ ] 客服渠道、退款流程、监控值班表

## 4. 内测

- [ ] 10–20 名内测用户，发放兑换码（`/api/admin/redeem-codes`，可设积分有效期）
- [ ] 每单收集满意度和返修原因；每周看 `/api/admin/reports/margin` 和 `/api/admin/health`
- [ ] 目标（上线前定）：交付率、平均返修轮次、单单毛利
