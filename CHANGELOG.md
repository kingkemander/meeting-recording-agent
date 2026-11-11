# 变更记录

## v1.0.2

- 录音与纪要同名配对上传（`.wav` + `.md`），音频先传；`kb-upload/` 用硬链接不占双份空间
- `upload_kb.py` 默认带音频，新增 `--no-audio`；补充大文件 `processing` 不重复上传的说明
- `export_meetings.py` 在 `kb-upload/` 生成同主名音频
- 修正默认策略：此前默认只传 Markdown，现改为「原始录音 + 纪要」一起入库

## v1.0.1

- 新增 `set_topic.py`：上传前按「内容主题 · 源文件名」命名
- 新增 `upload_kb.py`：按文件名查重、必须指定权限域、幂等上传
- `export_meetings.py`：`--force` 不再重复下载音频；待办从原始 JSON 改为「内容（负责人）」
- 主流程固化为六步（登录态 → 导出 → 命名 → 上传 → 校验 → 汇报）
- 新增知识库坑位说明：KB ID 失效、Key 读写分离、`target_domain` 必填、无改名接口

## v1.0.0

- 首版：三条登录态取证路径、归档、Markdown 纪要、知识库上传
