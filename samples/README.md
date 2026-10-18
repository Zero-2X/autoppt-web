# 金标准样例集

为联调准备的**虚构**测试材料（每份文件开头都标明“测试样例：内容和数据均为虚构”），用于每次升级 Worker 模板后回归：

    python scripts/run_samples.py samples --jobs-root D:/autoppt-jobs/samples --template <模板目录> --profiles <profiles.json>

| 样例 | 场景 | 档位 | 资料 |
| --- | --- | --- | --- |
| `thesis-small` | 硕士论文答辩 | 原生可编辑 | DOCX（含对比表） |
| `nsfc-application` | 国自然申请答辩 | 规划包 | DOCX 节选 |
| `group-meeting` | 组会进展 | 图片版 | Markdown + XLSX |
| `description-only` | 本科毕设答辩 | 原生可编辑 | 只有描述，没有文档 |
| `vectorize-image-deck` | 矢量化 | 原生可编辑 | 3 页图片版 PPTX |

实测记录：[p1-real-run-masters-reconstruct.md](../docs/p1-real-run-masters-reconstruct.md)（thesis-small）、
[p1-sample-runs-20261018.md](../docs/p1-sample-runs-20261018.md)（其余四份）。
