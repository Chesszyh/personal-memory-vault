# 检索效果评测

使用固定问题、搜索词和原始证据，对比关键词检索与不提供记忆的结果。真实题目与输出放在私人 vault；仓库测试使用合成档案。

## 运行

在实现仓库运行，替换 `VAULT_ROOT`、`CASES_JSON` 和 `REPORT_JSON`：

```bash
env PYTHONPATH=src python -m personal_vault.evaluate_recall VAULT_ROOT CASES_JSON --output REPORT_JSON
env PYTHONPATH=src python -m personal_vault.evaluate_recall VAULT_ROOT CASES_JSON --mode no-memory --output BASELINE_JSON
```

每次使用新的输出路径。`keyword` 依次执行题目中的固定关键词，给每个不同的命中消息读取一次上下文。`--limit` 控制每次搜索结果数量，`--context-limit` 控制每次上下文消息数量。结果保留参数、快照清单、原始检索结果、上下文、调用次数、返回正文字符数和耗时。字符数按实际返回量累计，包含不同调用之间重复返回的文本，不等于 token 用量。

`no-memory` 不调用检索，返回空证据，作为检索层的空白对照。两种模式均不调用模型；报告中的模型调用与 token 用量均为零。

## 题目文件

```json
{
  "cases": [
    {
      "id": "project-plan",
      "category": "past_plan",
      "question": "我之前对这个项目有什么计划？",
      "queries": ["项目关键词"],
      "answer_guidance": "引用当时计划，不把它说成已经完成。",
      "evidence": [
        {
          "snapshot_key": "snapshot-key",
          "conversation_id": "conversation-identity-key",
          "message_id": "message-identity-key",
          "quote": "支持回答的连续原文"
        }
      ]
    }
  ]
}
```

从 `personal_vault.recall search` 和 `context` 的结果取得来源标识，阅读上下文后选定原话。每个证据消息记录一次；跨会话题列出各自的证据。无记录题使用空 `evidence`。题目类别是自由文本，可覆盖情境偏好、历史项目、计划、跨会话、更正、引用材料和未知问题。

运行前会独立核对证据是否仍在当前检索范围、角色是否为用户、原话是否存在。快照或原文变化时会报错，应核对题目来源后再运行。角色检查不判断内容是否是粘贴材料，也不把历史陈述认定为当前事实。

## 阅读结果

`source_recall` 是该题预期来源消息中被搜索命中的比例；所有预期来源均命中才计入 `all_sources_found`。`search_quote_visible` 和 `context_quote_visible` 分别说明返回的搜索片段、上下文是否包含选定原话。正确消息被命中但关键原话被截断时，两者可以不同。

无记录题的 `source_recall` 为 `null`，单独统计是否返回空结果。空结果仅表示这组搜索词未找到内容，不证明整个档案不存在相关事实。固定关键词的成绩也不等于 Pi 自行选词、多轮搜索的问答成绩。

`answer_review` 初始为 `not_run`，供后续回答评测记录使用。用相同题目分别运行裸 Pi 与接入记忆的 Pi，保存原始输出，再核对是否正确使用证据、是否混淆计划与现状、是否把引用材料当成用户经历。运行方式见 [Pi 启动与测试](pi-recall.md#启动与测试)。自动命中统计不替代回答评判。

## Pi 回答对照

以下命令会实际调用模型。指定同一 provider、model 和 thinking，给每道题分别启动无记忆和有记忆的独立 Pi 进程：

```bash
env PYTHONPATH=src python -m personal_vault.evaluate_pi VAULT_ROOT CASES_JSON --output NEW_REPORT_DIR --provider PROVIDER --model MODEL --thinking medium
```

两组使用相同问题和系统提示，仅有记忆组加载 `integrations/pi/memory.ts`。预期原文、搜索词和判断指南不发送给模型；模型自行选择搜索词。每次使用临时会话，关闭其他扩展、内置文件工具、技能与上下文文件，避免前一题泄露答案。

报告目录保存逐题 JSONL 原始事件、标准错误和 `report.json`。按完成的助手消息累计模型用量，缺失用量保留 `null`，不当成零；token 总数直接采用 provider 返回的 `totalTokens`，不重复加 reasoning。工具返回长度统计整个工具文本，包含 JSON 标识字段，与检索基线的正文字符数口径不同。

进程失败、超时或未生成完整最终回答时，保存当前证据并停止后续调用。默认单次超时可通过 `--timeout` 调整。逐题阅读最终回答及工具返回，在私人目录另存判断记录，注明所依据的消息、未命中及误用；运行完成本身不等于回答正确。

## 语义检索的验收口径

语义测试分别保存向量候选、融合重排结果、目标会话命中、目标消息命中和关键原话可见性。自然语言整句不适合逐字检索，因此同句关键词对照只用于说明检索方式差异，不替代短关键词基线。跨主题问题拆成独立查询。

分别记录目标会话命中和目标消息命中；找到会话不代表直接命中所需原话。为无记录问题保留负例，检查相似候选是否被误当成事实。对上下文预算或会话去重的变更，使用同一题组比较来源命中、原话可见性和返回长度。真实题组和结果保存在自己的 vault 中。
