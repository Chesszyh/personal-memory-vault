# 本地更新与工作台

## 一键增量更新

在仓库根目录运行，显式指定自己的 vault 和账号范围：

```bash
bash scripts/update-chatgpt.sh /path/to/unpacked-export --date 2026-10-07 \
  --vault-root "$HOME/personal-memory-vault-data" --identity-scope my-chatgpt-account
bash scripts/update-chatgpt.sh /path/to/export.zip --date 2026-10-07 \
  --vault-root "$HOME/personal-memory-vault-data" --identity-scope my-chatgpt-account
```

不传目录时，选择 `--export-root` 下最新的日期目录，且目录必须包含 `conversations.json` 或 `conversations-*.json`。也可显式传 ZIP，支持根目录或单层包装目录中的官方导出；文件名为日期时可省略 `--date`。ZIP 解压结果保存在 vault 的 `evidence/zip/`，相同输入重跑复用已有目录。默认路径与账号范围以 `src/personal_vault/update_export.py` 的 `main` 参数为准；默认账号范围为 `new-chatgpt-account`。旧账号使用 `--identity-scope legacy-chatgpt-account`，已有批次重跑时用 `--source-id` 保持其原名称。

脚本显示来源清单、导出结构检查、增量导入、归档验收等阶段进度，并更新记忆候选。完成后报告新增、更新、未变和本次未出现的会话数量，具体会话清单写入 `*-changes.json`。本次未出现不等于删除。重跑相同导出返回 `no_op`，不重复创建快照或候选，不改变已有审核决定。相同日期有另一份导出时，放在独立目录，并用 `--date` 和 `--source-id` 指定独立快照名称。

运行只使用本地 Python 和 SQLite，不调用模型，不消耗 Codex 额度。大导出需要数分钟校验。失败返回非零退出码并显示原因；修正问题后可重跑。结束后刷新浏览器，Pi 检索直接读取新档案。

报告保存在 vault 的 `derived/reports/updates/`，私人数据不写入实现仓库。

## 启动工作台

```bash
PERSONAL_VAULT_ROOT="$HOME/personal-memory-vault-data" bash scripts/workbench.sh
```

此脚本需要 Linux 和 systemd 用户会话，并使用系统 `/usr/bin/python3`。它复用同名的已有服务；已有服务的 vault 路径由该服务配置决定，环境变量仅用于首次创建临时服务。

脚本通过 systemd 用户服务启动：

- 阅读工作台：`http://127.0.0.1:8767/`。
- 记忆审核工作台：`http://127.0.0.1:8766/`。

关闭浏览器不会停止服务；重启机器后重新运行脚本。运行状态可用 `systemctl --user status personal-vault-reader personal-vault-review` 查看。修改 Python 后端后，用 `systemctl --user restart personal-vault-reader personal-vault-review` 重载。

不使用 systemd 时，在两个终端分别运行：

```bash
personal-vault read serve "$HOME/personal-memory-vault-data" --port 8767
personal-vault memory serve "$HOME/personal-memory-vault-data" --port 8766
```

## 阅读能力

默认“全部会话”跨账号、跨导出浏览，每个会话采用最后一次观察到的版本；未在新导出出现的旧会话仍保留。下拉菜单可切换单份导出。列表按时间排列并分页加载，搜索覆盖标题和消息正文。

正文支持标题、段落、强调、列表、引用、表格、代码块、链接与代码复制，提供用户消息气泡、深浅主题和手机布局。会话还保留替代分支、附件下载、来源定位、导出批次与统计洞察。群聊和洞察按单份导出查看。

记忆审核页支持按批次筛选、查看来源上下文、编辑候选表述、批量决定和生成已确认画像。审核不阻塞 Pi 检索历史。

图片附件在正文中预览，支持已解析的 PNG、JPEG、GIF、WebP 和 AVIF；原件仍可下载。公式通过随包分发的 KaTeX 渲染为 MathML，支持 `$…$`、`$$…$$`、`\(…\)` 和 `\[…\]`；无效公式保留原文，代码块不作公式处理。

滚动位置自动保存，再次打开同一会话恢复；明确的消息引用优先于旧阅读位置。每条消息可“收藏”到自定专题，侧栏“收藏与专题”支持筛选和移除。“纠正”支持标记过时、引用材料、以后不使用、更正、恢复，以及适用日期、说明和替代消息 ID。这些操作只写独立标注库，不修改 ChatGPT 原始归档。

“Pi 会话”入口显示本地自动归档的新对话，消息也支持短来源链接。阅读器不调用 ChatGPT 继续对话；原始 HTML 以文字呈现。
