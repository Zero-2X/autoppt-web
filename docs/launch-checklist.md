# 内测与上线清单（P8）

只列上线前必须由人确认或操作的事项；每项完成时写上日期和负责人。代码侧的状态见 [开发清单](development-checklist.md)。

## 1. 内测前（技术）

- [ ] 在 GitHub 仓库设置里添加 secret `AUTOPPT_REPO_TOKEN`（对 `Zero-2X/autoppt` 只读的 fine-grained token），让 CI 能拉取工作流仓库；确认 CI 通过
- [ ] Worker 机器：专用 Windows 账号自动登录；运行 `scripts/install_worker.ps1`；`worker_check` 全部通过（含 office 能力）
- [ ] 完成 Codex elevated 沙箱安装并配置 `deny_read`，重跑[安全模型](security-model.md)第 1 节探针，确认读取也被拦截
- [ ] Worker 出网白名单（防火墙）
- [ ] 生产配置：`AUTOPPT_API_SECRET_KEY`、`AUTOPPT_API_STORAGE_KEY`（静态加密）、PostgreSQL、对象存储、SMTP；`alembic upgrade head`
- [ ] 计划任务：`python -m autoppt_api.maintenance all` 每 5 分钟（通知、过期、对账、告警、账本核对、清理）
- [ ] 补全 `autoppt_service/templates/redactions.json` 后构建 Worker 模板；处理模板清单里列出的“代码中含案例名”的脚本
- [ ] 原生重建阶段（P1/P5 未完成的部分）上线前，只开放“规划包”和“图片版”两个档位

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
