# 提交前检查清单

## 已完成

- [x] 已依据原 PDF 逐条核对章节标题与范围；
- [x] 已生成 `manual_review/human_section_review_confirmed.csv`；
- [x] 每条确认记录均填写了 PDF 证据和人工判断理由；
- [x] 已确认 `manual_additions.csv` 中两条人工新增参考章节；
- [x] 复核人填写为 `lzr`，复核日期为 `2026-07-13`；
- [x] 已运行最终脚本，未使用 `--allow-pending`；
- [x] `human_review_summary.json` 中 `pending_or_unchecked` 为空；
- [x] 最终结果已保存到 `outputs/final/`。

## GitHub 应包含

- [x] 自动定位代码；
- [x] 规则配置；
- [x] 大模型 prompt 和原始建议；
- [x] 人工确认表与人工复核笔记；
- [x] 人工新增参考章节；
- [x] 自动与人工差异表；
- [x] 运行日志和 README。

## 上传前最后检查

- [ ] 将原始 PDF 放到仓库外或使用 Git LFS，避免普通 GitHub 仓库文件过大；
- [ ] 在 GitHub 页面确认 CSV 中文编码显示正常；
- [ ] 确认提交的是 `outputs/final/`，不是 `example_output/final_preview/`。
