# Personal Memory Vault

这是一个厂商无关的个人对话证据与记忆工具集。支持 ChatGPT 官方导出增量导入、归档阅读器、HTML/PDF、记忆审核与迁移，以及不依赖逐条审核的 Pi 历史检索、语义重排、来源纠错和新会话归档。IndexedDB 恢复暂缓，其他厂商和微信/QQ 目前只有统一适配器契约。

项目采用 [MIT 许可证](LICENSE)。功能范围见 [开发范围](TODO.md)，检索与 Pi 回答对照见 [评测说明](docs/recall-evaluation.md)。

## 当前状态

| 能力 | 状态 | 边界 |
|---|---|---|
| Evidence Manifest（来源文件清单）创建与校验 | 已实现并有合成测试 | 清单证明指定目录在扫描时的文件集合、类型、大小和哈希；它不是签名或可信时间戳 |
| ChatGPT 官方导出 doctor/import | 已实现并有合成测试 | 支持 JSON 会话图、跨快照版本、Source Absence、group chat、附件 CAS 与 SQLite FTS5（全文检索）；代码存在不等于某份私人语料已经通过完整验收 |
| IndexedDB recovery/export | 暂缓 | 主机端编排和五文件只读扩展已有合成测试；真实备份因旧扩展 origin 与恢复扩展 ID 不一致而暂不进入主线 |
| 本地 Reading Archive | 已实现本地服务 | 支持普通会话的当前/替代分支与全文搜索，也可切换阅读和搜索 ChatGPT group chat；含附件、来源诊断和确定性统计；只监听 `127.0.0.1` |
| 静态 HTML、PDF | 已实现初版 | 可导出单个普通会话或整份快照的普通会话 HTML；HTML 复制已解析附件并保留替代分支；单会话 PDF 需要可选 Playwright 依赖和兼容 Chromium；尚无 group chat/年度/专题阅读合集 |
| 确定性分析 | 已实现初版 | 输出 JSON/Markdown；不使用 LLM、不回写画像，不把源缺席解释为删除 |
| 个人画像与记忆审核 | 已实现交互式审核工作台 | 只扫描当前主线 user 消息；可筛选、多选、查看上下文、编辑表述、比较潜在重复/冲突并自动保存；confirmed 条目进入 profile |
| Pi 历史检索 | 按需检索与上下文读取 | 无需先审核；按会话取最后一次观察到的当前分支，返回角色、时间与来源；支持终端和 RPC |
| ChatGPT 迁移包 | 已实现初版 | 只包含 confirmed 记忆、来源引用、档案覆盖索引和回忆测试；不会恢复账号内部状态 |
| 其他来源适配 | 契约已实现 | 可验证 source-neutral JSONL；Gemini、DeepSeek、Claude、微信和 QQ 的实际解析器尚未实现 |

重要：`archive_extension/` 是源码与测试夹具，包含会创建、删除合成数据库的 smoke harness，**绝不能直接加载到真实恢复 profile**。`extension_recovery.py` 和 `extension-recovery` CLI 只允许构建经过绑定的五文件 bundle。真实备份目前因 Chromium 扩展 origin 不一致而暂缓，保留证据但不继续进入主线。

在完整 Git checkout 中，领域词汇见 [`CONTEXT.md`](CONTEXT.md)，架构决定见 [`docs/adr/`](docs/adr/)，当前 SQLite 实现边界见 [`docs/schema-v1.md`](docs/schema-v1.md)。这些仓库文档不在 Python wheel/sdist 内。

## 数据与代码边界

私人数据保存在代码仓库之外，例如：

```text
~/personal-memory-vault-data/
```

这是示例路径。`personal-vault import ...` 的 `output` 是必填参数。Evidence root 与输出目录应保持分离；不要把输出目录设为 Evidence root 或其子目录。

Python 包包含 CLI（命令行接口）、数据库 schema、阅读器和审核界面的离线资源，以及随包第三方组件的许可证。完整文档、Pi 集成、辅助脚本和浏览器恢复扩展从 Git 仓库获取；打包范围由 `pyproject.toml` 和 `MANIFEST.in` 定义。

## 安装与快速开始

需要 Python 3.11 或更新版本，以及支持 FTS5 trigram（SQLite 全文检索）的 SQLite。先克隆仓库并创建虚拟环境：

```bash
git clone https://github.com/Chesszyh/personal-memory-vault.git
cd personal-memory-vault
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
personal-vault --help
```

导入已解压的 ChatGPT 官方导出，并启动阅读器：

```bash
bash scripts/update-chatgpt.sh /path/to/unpacked-export \
  --date 2026-10-07 \
  --vault-root "$HOME/personal-memory-vault-data" \
  --identity-scope my-chatgpt-account
personal-vault read serve "$HOME/personal-memory-vault-data"
```

打开 `http://127.0.0.1:8765/`。同一账号后续导出继续使用相同的 `--identity-scope`，不同账号使用不同值。

ZIP 导入、工作台后台运行和目录选择见 [本地更新与工作台](docs/local-workbench.md)。命令不依赖 RTK；测试与打包见 [贡献指南](CONTRIBUTING.md)。

## IndexedDB 双重重放恢复

恢复流程只接受经过 v2 Evidence Manifest 验证的 Recovery Working Copy，并要求显式列出不可触碰的 master 与日常 Chrome profile。它为两个 replay 分别创建全新 profile、只读五文件扩展 bundle 和专属输出目录：

```bash
personal-vault extension-recovery prepare \
  WORKING_COPY WORKING_COPY_MANIFEST NEW_REPLAY_ROOT \
  archive_extension/manifest.json \
  --chrome-binary /path/to/chrome-for-testing \
  --master-root IMMUTABLE_MASTER \
  --active-profile-root DAILY_CHROME_PROFILE
```

每个 replay 的阶段顺序固定为：

```text
launch -> confirm-preflight -> install -> launch -> validate
```

`launch` 只断开该临时 Chrome 的网络并等待窗口完全退出，不影响整机联网。第一次窗口只做“目标数据库不存在”预检；第二次窗口需要填写稳定 Snapshot ID，选择该 replay 的专属输出目录并导出。两个 replay 均验证后执行：

```bash
personal-vault extension-recovery compare REPLAY_1 REPLAY_2
```

只有来源、浏览器、bundle、逐 store/row/binary 哈希和两次独立 challenge 全部闭合时，比较结果才会声明 `extraction_complete`。具体页面操作与安全边界见 [`archive_extension/README.md`](archive_extension/README.md)。

## Evidence Manifest

创建清单时，输出文件必须位于被扫描目录之外。`--captured-at` 接受 `YYYY-MM-DD`，也接受带时区的 ISO 8601 时间：

```bash
personal-vault manifest create SOURCE_ROOT MANIFEST_JSON \
  --source-id chatgpt-official-2026-07-15 \
  --source-kind chatgpt_official_export \
  --captured-at 2026-07-15

personal-vault manifest verify SOURCE_ROOT MANIFEST_JSON
```

`manifest verify` 只核对该清单描述的证据树，不解析聊天语义。

## ChatGPT 官方导出

先只读检查一个已经有 v2 Evidence Manifest 的快照：

```bash
personal-vault doctor chatgpt-official SOURCE_ROOT MANIFEST_JSON
```

再导入到单独的 vault。属于同一账号连续快照的 `--identity-scope` 必须保持相同，并建议按时间从旧到新导入：

```bash
personal-vault import chatgpt-official \
  SOURCE_ROOT \
  MANIFEST_JSON \
  VAULT_ROOT \
  --identity-scope legacy-chatgpt-account
```

导入器会在 `VAULT_ROOT` 下创建或复用：

```text
canonical/archive.sqlite
decoded/chatgpt-official/...
assets/sha256/...
```

日后处理一个新解压快照时，推荐使用高层增量命令，一次完成 Evidence Manifest、doctor、幂等导入、全库独立验收和元数据报告：

```bash
personal-vault update chatgpt-official \
  SOURCE_ROOT VAULT_ROOT \
  --identity-scope legacy-chatgpt-account \
  --source-id chatgpt-official-2026-08-11 \
  --captured-at 2026-08-11
```

默认 manifest 写到 `VAULT_ROOT/manifests/sources/SOURCE_ID/manifest.json`，更新报告写到 `VAULT_ROOT/derived/reports/updates/SOURCE_ID.json`。同一来源树再次运行会验证并复用 manifest；目录内容发生漂移时会在导入前拒绝。

重复导入相同快照会走 no-op 检查，但 CLI 返回 `no_op` 不能替代独立验收。完整验收还应覆盖 SQLite integrity/foreign keys、投影一致性、真实 FTS5 索引、NDJSON 哈希、CAS 文件集合及导入前后逻辑指纹。命令的普通 JSON 输出是计数和路径等元数据；验收脚本也不应打印消息正文或 `user.json` 内容。

## 本地归档阅读器

读取 vault 中已经存在的 canonical archive：

```bash
personal-vault read serve /path/to/personal-memory-vault
```

默认地址是 `http://127.0.0.1:8765/`。使用 `--port 0` 可让系统分配空闲端口。ChatGPT 归档数据库以 SQLite `mode=ro` 和 `query_only` 打开，附件在返回前按 CAS SHA-256 校验；纠错、收藏与阅读位置的 POST 请求写入独立标注库。停止服务使用 `Ctrl-C`。工作台脚本使用 8767，详见 [工作台](docs/local-workbench.md)。

侧栏的“阅读 / 群聊 / 洞察”用于切换普通会话、ChatGPT `group_chats.json` 的规范化线性消息记录和确定性统计。群聊附件继续显示 `resolved / unresolved / ambiguous / external`，不会把缺失二进制伪装成已恢复。

## 静态 HTML 与 PDF

将单条会话导出为可搬运目录，附件从 CAS 校验后复制到局部 `assets/`：

```bash
personal-vault export conversation-html \
  VAULT_ROOT SNAPSHOT_ID CONVERSATION_IDENTITY_KEY OUTPUT_DIRECTORY
```

将一个快照的全部普通会话导出为带总索引的离线网页档案：

```bash
personal-vault export snapshot-html \
  VAULT_ROOT SNAPSHOT_ID OUTPUT_DIRECTORY

personal-vault export verify-snapshot-html OUTPUT_DIRECTORY
```

每个会话仍是独立可搬运目录，因此同一附件被多个会话引用时会在各目录分别复制；输出空间应按 manifest 中的 `asset_bytes` 预留。验证命令会重算根索引、逐会话页面和附件哈希，核对文件清单、CSP 与活动标签，并且不打印标题或正文。

PDF 使用同一份 HTML；安装可选依赖 `pip install -e '.[pdf]'`，并显式绑定 Chromium：

```bash
personal-vault export conversation-pdf \
  OUTPUT_DIRECTORY/index.html conversation.pdf \
  --chrome-binary /path/to/chrome-for-testing
```

PDF renderer 使用 Playwright 隔离 context，只允许 `file:`、`data:` 和 `blob:` 子资源。

## 接入 Pi

从完整 Git checkout 启动：

```bash
PERSONAL_VAULT_ROOT="$HOME/personal-memory-vault-data" bash integrations/pi/launch.sh
```

入口复用本机 Pi 的模型和认证配置，只加载本仓库的历史检索扩展。会话保存在 vault 的 `runtime/pi/sessions/`，可加 `--continue` 接续。通过 `PERSONAL_VAULT_ROOT` 指定其他 vault。

使用与效果测试、桌宠 RPC 接法见 [Pi 使用指南](docs/pi-recall.md)。检索直接使用对话证据，审核队列不阻塞使用；画像与迁移包继续只采用 confirmed 条目。

## 分析、记忆审核和迁移包

确定性快照分析不会使用 LLM：

```bash
personal-vault analyze snapshot VAULT_ROOT SNAPSHOT_ID REPORT_DIRECTORY
```

记忆扫描只检查选中主线的 user 消息，命中结果全部为 pending：

```bash
personal-vault memory scan VAULT_ROOT --snapshot-id SNAPSHOT_ID
personal-vault memory review VAULT_ROOT
personal-vault memory decide VAULT_ROOT CANDIDATE_ID --status confirmed
personal-vault memory profile VAULT_ROOT
```

启动真正可写入的本地审核工作台：

```bash
personal-vault memory serve VAULT_ROOT
```

默认地址为 `http://127.0.0.1:8766/`。界面可按来源快照切换候选批次，并支持确认、拒绝、暂缓、编辑候选表述、按状态/类型/日期/关键词筛选、多选批量处理、来源消息上下文、潜在重复/冲突线索和审核历史。每次决定或编辑都会立即写入 `memory.sqlite`；右上角可按当前 confirmed 集合重建 `profile.md` 与新的 ChatGPT 迁移包。原始 `statement` 永不被编辑覆盖，审核后的表述单独保存。

候选不是由模型生成的画像结论。当前确定性提取器只扫描指定快照中、`current_node` 选中主线上的 user 消息；正文至少 8 个字符，并命中“我是/我叫”“我喜欢/希望”“我打算/正在”“不要/不需要”等身份、偏好、计划或边界表达后才进入待审核队列。每条消息最多归入一个类型，固定初始置信度为 `0.55`；assistant/tool/system、未选中分支和未命中内容不会进入队列。置信度只表示“规则命中”，不表示事实正确。

审核时建议先看上下文，再把表述改成脱离原会话也能理解的一条记忆：本人直接表达、当前仍成立、未来协作可能用得上时接受；单次任务要求、引用/假设、误命中、已完成或已失效的计划、被后文推翻的内容，以及重复候选应拒绝；需要更多上下文或尚未判断时效/冲突时暂缓。拒绝只是不让它进入当前画像，不会删除原消息证据。

`memory review` 仍保留为脚本无关、只读的 HTML 快照。`profile.md` 和迁移包只使用 confirmed 条目：

```bash
personal-vault migration chatgpt VAULT_ROOT OUTPUT_DIRECTORY
```

其他来源适配器应先输出 `pmv.interchange-event.v1` JSONL，并通过：

```bash
personal-vault adapter validate-jsonl EVENTS.jsonl
```

契约见 [`docs/interchange-v1.md`](docs/interchange-v1.md)。

## 测试与打包

开发依赖和完整步骤见 [CONTRIBUTING.md](CONTRIBUTING.md)。

Python 测试只使用合成 fixture：

```bash
env PYTHONPATH=src python -W error::ResourceWarning \
  -m unittest discover -s tests -v
```

archive extension 的 Node 测试也不读取 vault：

```bash
node --test archive_extension/test/*.test.mjs
```

构建 Python wheel 和 source distribution：

```bash
python -m pip install build
python -m build
```

纯合成浏览器冒烟的范围和依赖见 [`archive_extension/README.md`](archive_extension/README.md)。它不是对真实 Recovery Working Copy 的验收。

## 完整性声明的解释

- `Source Complete` 只针对一个已经冻结并由清单覆盖的 Source Snapshot，不表示账号历史完整。
- `Extraction Complete` 必须明确解码范围；官方 JSON 导入与未来 IndexedDB 恢复不能共用未经限定的结论。
- `Reconciliation Complete` 需要独立的跨来源/跨快照报告，不能由“没有 unresolved warning”单独证明。
- `Account Complete` 永远不声明。

这些术语的规范定义以 [`CONTEXT.md`](CONTEXT.md) 为准。

## 许可证

项目代码和文档采用 [MIT](LICENSE)。随包分发的 Marked 和 KaTeX 保留各自 MIT 版权声明，见 [第三方组件](src/personal_vault/reader_static/vendor/README.md)。
