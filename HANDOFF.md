# 维护入口

安装与使用从 [README](README.md) 开始；开发检查见 [CONTRIBUTING](CONTRIBUTING.md)。功能边界见 [TODO](TODO.md)，领域词汇见 [CONTEXT](CONTEXT.md)。

## 实现导航

| 功能 | 主要模块 | 使用文档 |
|---|---|---|
| 导入与增量更新 | `chatgpt_export.py`、`workflow.py`、`update_export.py` | [导入与工作台](docs/local-workbench.md) |
| 阅读与导出 | `reader.py`、`reader_static/`、`export_archive.py` | [README](README.md) |
| 关键词与来源上下文 | `recall.py` | [Pi 检索](docs/pi-recall.md) |
| 本地语义检索 | `semantic.py` | [语义环境](docs/pi-recall.md#本地语义检索) |
| 纠错、时间与画像 | `annotations.py`、`profile.py`、`memory_store.py` | [Pi 检索](docs/pi-recall.md) |
| Pi 会话与通信 | `pi_archive.py`、`pi_rpc.py`、`integrations/pi/` | [Pi 集成](docs/pi-recall.md) |
| 评测 | `evaluate_recall.py`、`evaluate_pi.py` | [评测说明](docs/recall-evaluation.md) |

Python 模块均位于 `src/personal_vault/`。ChatGPT 归档、标注、Pi 会话和语义索引分库保存，具体职责见 [数据库说明](docs/schema-v1.md)。真实导出、运行状态和评测结果保存在使用者自己的 vault，不属于仓库文档。
