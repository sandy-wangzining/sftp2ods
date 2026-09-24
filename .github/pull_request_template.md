## 改了什么

<!-- 一句话说清；修 bug 时写"原来会怎样、现在怎样" -->

## 为什么

<!-- 复现场景 / 影响的作业；能写清"原来会静默丢数/写错"最好 -->

Closes #

## 怎么验证的

<!-- 列具体的测试或真实作业验证步骤，关键日志片段 -->

- [ ] `python -m unittest discover -s tests` 全绿
- [ ] `ruff check .` 0 告警
- [ ] 涉及数据完整性判定的改动，补了"改之前会失败"的回归用例

## 自检

- [ ] 没提交密钥（`jobs/*.json`、`config.json` 都在 `.gitignore` 里）
- [ ] 日志/异常里没有明文密钥（新增出口过了 `redact()`）
- [ ] 失败路径不会让分区停在一半（写前校验都在 `delete_partition` 之前）
- [ ] 加了配置项/命令行参数 → 同步更新 `README.md` 与 `jobs/_template_full.example.json`
- [ ] 行为变化已写进 `CHANGELOG.md`
