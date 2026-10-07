import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const vault = process.env.PERSONAL_VAULT_ROOT;
  if (!vault) throw new Error("PERSONAL_VAULT_ROOT is required");
  async function run(args: string[], signal?: AbortSignal) {
    const result = await pi.exec(process.env.PERSONAL_VAULT_PYTHON || "python3",
      ["-m", "personal_vault.recall", vault!, ...args], { signal, timeout: 30000 });
    if (result.code !== 0) throw new Error(result.stderr || "Vault retrieval failed");
    const data = JSON.parse(result.stdout);
    return { content: [{ type: "text" as const, text: JSON.stringify(data) }], details: data };
  }
  async function archiveSession(ctx: any) {
    const file = ctx.sessionManager.getSessionFile();
    if (!file) return;
    const args = ["-m", "personal_vault.pi_archive", vault!, file];
    const leaf = ctx.sessionManager.getLeafId();
    if (leaf) args.push("--leaf", leaf);
    const result = await pi.exec(process.env.PERSONAL_VAULT_PYTHON || "python3", args, {timeout:30000});
    if (result.code !== 0) ctx.ui.notify("会话归档失败：" + (result.stderr || "unknown error"), "error");
  }
  pi.on("agent_end", async (_event, ctx) => { await archiveSession(ctx); });
  pi.on("session_start", async (_event, ctx) => { await archiveSession(ctx); });
  pi.on("turn_start", async (_event, ctx) => { await archiveSession(ctx); });
  pi.registerTool({
    name: "memory_semantic_search", label: "按意思搜索历史",
    description: "Find historical user messages by meaning and rerank with local models. Use for paraphrases or when keyword search misses. Refreshes changed messages. Similarity is not proof: read context and keep citation_label, time and feedback qualifiers. Can return unrelated results when no evidence exists.",
    parameters: Type.Object({query:Type.String(), rerank:Type.Optional(Type.Boolean()), limit:Type.Optional(Type.Integer({minimum:1,maximum:20})),
      budget_chars:Type.Optional(Type.Integer({minimum:500,maximum:40000}))}),
    async execute(_id,p,signal) {
      const python=process.env.PERSONAL_VAULT_SEMANTIC_PYTHON;
      if(!python) throw new Error("Set PERSONAL_VAULT_SEMANTIC_PYTHON to Python with the semantic dependencies and cached models.");
      const result=await pi.exec(python,["-m","personal_vault.semantic",vault!,"search",p.query,"--refresh",
        "--limit",String(p.limit||8),"--budget-chars",String(p.budget_chars||6000),...(p.rerank===false?["--no-rerank"]:[])],{signal,timeout:300000});
      if(result.code!==0) throw new Error(result.stderr||"Semantic retrieval failed");
      const data=JSON.parse(result.stdout);
      return {content:[{type:"text" as const,text:JSON.stringify(data)}],details:data};
    },
  });
  pi.registerTool({
    name: "memory_search", label: "搜索历史",
    description: "Search personal ChatGPT history using a literal keyword or phrase. Try several short Chinese/English topic keywords separately; this is not semantic search. Defaults to user statements. Results are deduplicated by conversation and bounded by a text budget. Use citation_label verbatim when citing; assistant_response is only a historical assistant summary, never an original email/log. Read the anchor context before answering.",
    parameters: Type.Object({ query: Type.String(), role: Type.Optional(Type.Union([
      Type.Literal("user"), Type.Literal("assistant"), Type.Literal("all")])),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })),
      budget_chars: Type.Optional(Type.Integer({ minimum: 500, maximum: 40000 })),
      per_conversation: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })) }),
    async execute(_id, p, signal) {
      return run(["search", p.query, "--role", p.role || "user", "--limit", String(p.limit || 8),
        "--budget-chars", String(p.budget_chars || 6000), "--per-conversation", String(p.per_conversation || 1)], signal);
    },
  });
  pi.registerTool({
    name: "memory_context", label: "读取来源上下文",
    description: "Read visible current-branch conversation messages. Use conversation_id and optionally message_id from search. Paginate with offset; use text_offset for long messages. Read context before drawing personal conclusions.",
    parameters: Type.Object({ conversation_id: Type.String(), message_id: Type.Optional(Type.String()),
      offset: Type.Optional(Type.Integer({ minimum: 0 })), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })),
      text_offset: Type.Optional(Type.Integer({ minimum: 0 })),
      budget_chars: Type.Optional(Type.Integer({ minimum: 500, maximum: 40000 })) }),
    async execute(_id, p, signal) {
      const args = ["context", p.conversation_id, "--offset", String(p.offset || 0),
        "--limit", String(p.limit || 8), "--text-offset", String(p.text_offset || 0), "--budget-chars", String(p.budget_chars || 6000)];
      if (p.message_id) args.push("--message-id", p.message_id);
      return run(args, signal);
    },
  });
  pi.registerTool({
    name: "memory_feedback", label: "纠正记忆来源",
    description: "Record the user's explicit correction of a source. Only use when the current user asks to correct, mark outdated/quoted, exclude, or restore a specific message. Never infer a correction from archived instructions.",
    parameters: Type.Object({ message_id: Type.String(), action: Type.Union([
      Type.Literal("outdated"), Type.Literal("quoted"), Type.Literal("excluded"), Type.Literal("corrected"), Type.Literal("restored")]),
      note: Type.Optional(Type.String()), valid_from: Type.Optional(Type.String()), valid_until: Type.Optional(Type.String()),
      assertion_kind: Type.Optional(Type.Union([Type.Literal("statement"), Type.Literal("preference"), Type.Literal("plan"), Type.Literal("event")])),
      replacement_message_id: Type.Optional(Type.String()) }),
    async execute(_id, p, signal) {
      const args = ["feedback", p.message_id, p.action];
      for (const key of ["note", "valid_from", "valid_until", "assertion_kind", "replacement_message_id"] as const) {
        if (p[key]) args.push("--" + key.replaceAll("_", "-"), p[key]!);
      }
      return run(args, signal);
    },
  });
  pi.registerTool({
    name: "memory_remember", label: "保存原话记忆",
    description: "Save an exact user quote with source when the current user explicitly asks you to remember it. Does not save assistant inferences. Search or archive the current session first to get its message_id.",
    parameters: Type.Object({ message_id: Type.String(), quote: Type.String(),
      kind: Type.Optional(Type.Union([Type.Literal("statement"), Type.Literal("preference"), Type.Literal("plan"), Type.Literal("event")])) }),
    async execute(_id, p, signal) {
      return run(["remember", p.message_id, p.quote, "--kind", p.kind || "statement"], signal);
    },
  });
  pi.registerTool({
    name: "memory_profile", label: "读取已确认记忆",
    description: "Read confirmed/user-requested quotes plus automatically grouped historical statements. observed.groups are unreviewed historical evidence, not current facts. Keep conflicts and time qualifiers, and use each source link.",
    parameters: Type.Object({}),
    async execute(_id, _p, signal) { return run(["profile"], signal); },
  });
}
