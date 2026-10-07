# Pi 历史检索

先通过 `personal-vault update chatgpt-official` 导入导出目录，即可让 Pi 检索普通会话。新导入自动参与检索，无需重建额外索引或逐条审核候选。

## 启动与测试

先安装并配置 [Pi](https://github.com/earendil-works/pi)，确保 `pi` 在 PATH 中且模型认证可用。在实现仓库运行：

```bash
export PERSONAL_VAULT_ROOT="$HOME/personal-memory-vault-data"
bash integrations/pi/launch.sh
bash integrations/pi/launch.sh --continue
bash integrations/pi/launch.sh --print '我之前关于国际象棋相关项目说过什么？请核对原话并给出来源。'
```

`launch.sh` 使用已安装的 `pi`，保留其模型与认证选择，并关闭其他扩展、技能、上下文文件和内置文件工具。本扩展提供关键词与语义检索、来源上下文、画像、纠错和原话记忆工具。会话存放在 `PERSONAL_VAULT_ROOT/runtime/pi/sessions/`；默认 vault 路径由启动脚本定义。可用 Pi 自带的 `--provider`、`--model` 临时指定模型。

效果测试可以围绕三个问题展开：能否找回明确说过的项目；能否区别过去计划和当前事实；没有记录时是否明确说未找到。使用 `--mode json --print '问题'` 可记录工具调用和最终答案，用相同问题、不加载扩展的 Pi 作对照。工具调用成功只证明链路可用，个人偏好是否理解准确仍需实际使用判断。

## 检索行为

固定题目的检索对照与结果解释见 [检索效果评测](recall-evaluation.md)。

`RecallRepository.search` 使用现有全文索引，短于三个字符时逐字匹配。参数是关键词或短语，不是自然语言语义检索；Pi 可换词多次查询。默认搜索用户消息，也可指定 `assistant` 或 `all`。默认每个会话返回一条，每次搜索或上下文的正文总预算为 6000 字符；可通过 `per_conversation`、`budget_chars` 调整。每个会话只读取最后一次导出中选中的分支，来源缺失的会话保留最后一次观察到的版本。不同账号身份不混并。

搜索结果包含角色、消息时间、快照、会话原生 ID 和消息 ID；长正文返回命中附近片段。`memory_context` 可按消息定位上下文，使用 `offset` 翻页，使用 `text_offset` 读取长消息后续部分。翻页时省略 `message_id`，避免重新定位。系统消息、隐藏消息、模型推理和发给工具的消息不进入检索结果。

搜索与上下文中的 `reader_url` 使用简短的 `?source=记录ID`，由同一 vault 的数据库解析为来源快照、会话和消息。打开后，阅读器会定位并高亮该消息；先运行 `scripts/workbench.sh` 启动本地服务。使用不同地址时，设置 `PERSONAL_VAULT_READER_URL` 为阅读器根地址。保留原数据库时链接持续有效，重新导入到全新数据库后应重新取得引用。

`memory_profile` 包含已确认条目、用户明确要求保存的原话，以及自动按相同原话合并的 `observed.groups`。自动分组保留多个来源和陈述时间，是历史证据，不是当前事实。不同陈述保留分开，较新消息不会自动覆盖旧消息。画像不会批量改成 confirmed，也不要求用户先处理审核队列。

`memory_remember` 只保存原消息中实际存在的用户原话；`memory_feedback` 记录用户明确的过时、引用、排除、更正或恢复指令。纠错写入独立的 `memory/annotations.sqlite`，保留完整修改历史及替代消息关系。排除的正文不会再进入关键词检索、语义检索、上下文或画像；归档阅读器仍保留原始证据。

`stated_at` 是消息陈述时间，`valid_from` / `valid_until` 是用户指定的适用日期，`feedback.stated_at` 是纠错发生时间。日期使用 `YYYY-MM-DD`；未知适用日期保持未知。过时或更正条目进入 `historical_items`；未来期间、已到期和历史未验证状态在检索中明确标注。历史计划不自动推定为已完成。

Pi 每轮开始、结束及恢复会话时增量归档到 `canonical/pi.sqlite`，原始 JSONL 仍保留。归档保存完整条目，检索只使用选中分支的用户与助手正文，不收录模型推理和工具输出。新会话可检索旧 Pi 会话；`--continue` 继续同一会话。阅读器的“Pi 会话”入口和消息短链接可以打开这些来源。

当前检索覆盖普通会话文本；附件、群聊、替代分支仍可通过归档阅读器查看，不作为此扩展的检索入口。

## 本地语义检索

安装可选依赖，并将 `Qwen/Qwen3-Embedding-0.6B` 和 `BAAI/bge-reranker-large` 预先缓存到本机。模型加载使用 `local_files_only=True`，查询时不会下载模型。

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[semantic]'
env PYTHONPATH=src .venv/bin/python -m personal_vault.semantic VAULT_ROOT build
env PYTHONPATH=src .venv/bin/python -m personal_vault.semantic VAULT_ROOT search '桌面虚拟伙伴陪伴工作' --refresh
```

索引存放于 `derived/semantic.sqlite`。按消息版本增量切分和编码，删除已失效版本的索引，失败后重跑会跳过已完成消息。语义工具查询前刷新新增/变更的用户消息；关键词检索始终直接读取归档。模型首次加载和增量编码需要时间。

`memory_semantic_search` 将向量候选交给交叉编码模型重排，再合并两种排名，避免完全丢弃原向量排名。默认按会话去重并限制正文预算；`rerank:false` 可只使用向量排名。结果相似不代表确有该事实，无记录问题也可能返回无关来源。用简洁的主题表达查询，跨主题问题分开查询，回答前读取上下文。实际评测与漏检见 [检索效果评测](recall-evaluation.md)。

启动脚本默认使用仓库 `.venv/bin/python` 执行语义工具，可用 `PERSONAL_VAULT_SEMANTIC_PYTHON` 指定其他环境。普通导入和关键词检索不需要这些依赖。

## 桌宠接入

桌宠启动同一入口的 RPC（远程过程调用）模式：

```bash
bash integrations/pi/launch.sh --mode rpc
```

向标准输入写一行 JSON，并持续读取标准输出：

```json
{"id":"turn-1","type":"prompt","message":"我们之前聊过哪些棋类项目？"}
```

`message_update` 中的 `assistantMessageEvent.type=text_delta` 是增量文字；工具进度使用 `tool_execution_start`、`tool_execution_end`；`agent_settled` 表示本轮自动工作结束。`response.success` 仅表示命令被接受。用 `get_state` 检查会话、模型状态，关闭标准输入以结束进程。Pi 会话文件可保留对话，桌宠负责窗口、动画和输入呈现。

`personal_vault.pi_rpc.PiRpc` 提供 Python 子进程客户端：持续读取标准输出，按请求 ID 分发响应，保留工具失败事件，并以 `agent_settled` 判断结束。`cancel()` 先清空待执行队列再取消；`reconnect()` 重新打开原会话文件，不自动重发问题。调用方仍需展示连接错误和超时，并决定下一次用户输入。

```python
from personal_vault.pi_rpc import PiRpc

with PiRpc(["bash", "/path/to/repo/integrations/pi/launch.sh"]) as client:
    events = client.prompt("之前讨论的讲棋目标是什么？")
    session_path = client.session
# 重启应用后传 session=session_path，即可恢复同一会话。
```

协议详情以本机 Pi 随附 `docs/rpc.md` 和 `docs/rpc-commands.md` 为准；上游接口见 [Pi RPC 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)。

## 独立检索接口

其他程序可以直接调用 Python 模块，标准输出为 JSON：

```bash
env PYTHONPATH=src python -m personal_vault.recall VAULT_ROOT search '国际象棋'
env PYTHONPATH=src python -m personal_vault.recall VAULT_ROOT context CONVERSATION_ID --message-id MESSAGE_ID
env PYTHONPATH=src python -m personal_vault.recall VAULT_ROOT profile
```
