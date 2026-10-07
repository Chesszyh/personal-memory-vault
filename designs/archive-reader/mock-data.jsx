const archiveMockConversations = [
  {
    id: "demo-conversation-01",
    title: "让多年对话真正可读",
    preview: "先保留证据，再给人一条舒服的阅读主线。",
    time: "14:32",
    dateGroup: "今天",
    date: "2026-08-01",
    source: "Chat export · 示例",
    model: "assistant-demo",
    hasAttachment: true,
    hasBranches: true,
    hasAbsence: false,
    tags: ["归档", "阅读器", "附件"],
    summary: "一段用于验证主线、分支、附件与溯源呈现方式的模拟会话。",
    messages: [
      {
        id: "demo-node-u-101",
        role: "user",
        author: "你",
        time: "14:29",
        paragraphs: [
          "我希望本地档案翻起来像熟悉的聊天网页，但需要明确告诉我：现在看到的是哪次快照、哪条主线，以及是否还有替代分支。"
        ]
      },
      {
        id: "demo-node-a-102",
        role: "assistant",
        author: "助手",
        time: "14:30",
        current: true,
        paragraphs: [
          "可以把正文保持安静，把证据状态放在周边：左侧负责找到会话，中间只读当前主线，右侧按需展开来源与快照。",
          "这样默认体验仍然是连续阅读；当你需要核对时，节点标识、源文件与观察时间才进入视野。"
        ],
        bullets: [
          "当前主线默认展开，替代回答折叠在产生分叉的位置。",
          "附件显示可预览状态、内容哈希关联和原文件下载入口。",
          "后续快照未出现某会话时，只标记为“源缺席”，不推断为删除。"
        ],
        branches: [
          {
            label: "替代回答 A",
            time: "14:30",
            copy: "也可以先做一个纯时间线界面，但会弱化会话分支和附件关系，不适合作为默认阅读方式。"
          },
          {
            label: "替代回答 B",
            time: "14:31",
            copy: "另一种方向是把每条消息做成证据卡片；审计更直观，但长对话阅读密度会明显下降。"
          }
        ]
      },
      {
        id: "demo-node-u-103",
        role: "user",
        author: "你",
        time: "14:31",
        paragraphs: [
          "附件不要只剩文件名。像这份演示文档一样，我希望能看出类型、大小、是否已经解析，并且仍能找到原件。"
        ],
        attachment: {
          name: "archive-reader-notes.pdf",
          kind: "PDF",
          size: "3.4 MB",
          status: "原件已关联",
          hash: "sha256:demo…8f2a"
        }
      },
      {
        id: "demo-node-a-104",
        role: "assistant",
        author: "助手",
        time: "14:32",
        current: true,
        paragraphs: [
          "附件卡片会优先回答三个问题：这是什么、还能不能打开、它来自哪里。更深的 MIME 检测、引用节点和内容寻址路径则留在来源抽屉中。"
        ]
      }
    ],
    provenance: {
      conversationId: "conv_demo_a31f",
      selectedNodeId: "node_demo_f27c",
      mappingPath: "mapping / demo-node-a-104",
      sourceFile: "conversations-demo-001.json",
      sourceSnapshot: "snapshot-demo-004",
      observedAt: "2026-08-01 14:35 +08:00",
      importedAt: "2026-08-01 14:42 +08:00",
      payloadHash: "sha256:demo41c9…a702",
      currentBranch: "root → u101 → a102 → u103 → a104"
    },
    snapshots: [
      {
        id: "S-004",
        label: "2026-08-01",
        state: "present",
        detail: "当前观察",
        messages: "4 条主线",
        assets: "1 个附件"
      },
      {
        id: "S-003",
        label: "2026-07-18",
        state: "present",
        detail: "可回溯",
        messages: "3 条主线",
        assets: "1 个附件"
      },
      {
        id: "S-002",
        label: "2026-06-30",
        state: "absent",
        detail: "源缺席",
        messages: "未观察到",
        assets: "不作删除推断"
      }
    ]
  },
  {
    id: "demo-conversation-02",
    title: "离线阅读的偏好",
    preview: "搜索要快，阅读列不要太宽，深色模式要真正耐看。",
    time: "11:08",
    dateGroup: "今天",
    date: "2026-08-01",
    source: "Chat export · 示例",
    model: "assistant-demo",
    hasAttachment: false,
    hasBranches: false,
    hasAbsence: false,
    tags: ["阅读", "主题"],
    summary: "用于展示简洁、无分支会话的模拟记录。",
    messages: [
      {
        id: "demo-node-u-201",
        role: "user",
        author: "你",
        time: "11:05",
        paragraphs: [
          "长对话的阅读列不要铺满屏幕。搜索结果跳转后，最好还能保留我刚才使用的筛选条件。"
        ]
      },
      {
        id: "demo-node-a-202",
        role: "assistant",
        author: "助手",
        time: "11:06",
        current: true,
        paragraphs: [
          "阅读列会限制在舒适宽度，筛选与主题可以保留在本地界面状态中。深色主题单独调节背景层级、边界和文字亮度，不使用机械反相。"
        ]
      },
      {
        id: "demo-node-u-203",
        role: "user",
        author: "你",
        time: "11:08",
        paragraphs: [
          "这正是我想要的默认行为。"
        ]
      }
    ],
    provenance: {
      conversationId: "conv_demo_b84d",
      selectedNodeId: "node_demo_2c11",
      mappingPath: "mapping / demo-node-u-203",
      sourceFile: "conversations-demo-001.json",
      sourceSnapshot: "snapshot-demo-004",
      observedAt: "2026-08-01 11:12 +08:00",
      importedAt: "2026-08-01 14:42 +08:00",
      payloadHash: "sha256:demo8ea0…f92b",
      currentBranch: "root → u201 → a202 → u203"
    },
    snapshots: [
      {
        id: "S-004",
        label: "2026-08-01",
        state: "present",
        detail: "当前观察",
        messages: "3 条主线",
        assets: "无附件"
      },
      {
        id: "S-003",
        label: "2026-07-18",
        state: "present",
        detail: "内容相同",
        messages: "3 条主线",
        assets: "无附件"
      }
    ]
  },
  {
    id: "demo-conversation-03",
    title: "附件匹配验收",
    preview: "同名文件不能替代内容哈希，歧义要显式报告。",
    time: "周四",
    dateGroup: "本周",
    date: "2026-07-30",
    source: "Chat export · 示例",
    model: "assistant-demo",
    hasAttachment: true,
    hasBranches: false,
    hasAbsence: false,
    tags: ["附件", "验收"],
    summary: "用于展示附件筛选和来源字段的模拟记录。",
    messages: [
      {
        id: "demo-node-u-301",
        role: "user",
        author: "你",
        time: "09:15",
        paragraphs: [
          "如果两个附件导出时用了相同显示名，阅读器不能擅自选一个。"
        ]
      },
      {
        id: "demo-node-a-302",
        role: "assistant",
        author: "助手",
        time: "09:16",
        current: true,
        paragraphs: [
          "界面会将这种情况标为匹配歧义，并保留全部候选。只有内容哈希、明确资产指针或其他充分证据一致时，才建立确定关联。"
        ],
        attachment: {
          name: "attachment-match-report.txt",
          kind: "TXT",
          size: "12 KB",
          status: "校验已通过",
          hash: "sha256:demo…c591"
        }
      }
    ],
    provenance: {
      conversationId: "conv_demo_c09a",
      selectedNodeId: "node_demo_31be",
      mappingPath: "mapping / demo-node-a-302",
      sourceFile: "conversations-demo-002.json",
      sourceSnapshot: "snapshot-demo-004",
      observedAt: "2026-07-30 09:20 +08:00",
      importedAt: "2026-08-01 14:42 +08:00",
      payloadHash: "sha256:demo7fd1…62ea",
      currentBranch: "root → u301 → a302"
    },
    snapshots: [
      {
        id: "S-004",
        label: "2026-08-01",
        state: "present",
        detail: "当前观察",
        messages: "2 条主线",
        assets: "1 个附件"
      }
    ]
  },
  {
    id: "demo-conversation-04",
    title: "增量快照不是覆盖更新",
    preview: "本次没有出现，只能说明源缺席。",
    time: "7月18日",
    dateGroup: "更早",
    date: "2026-07-18",
    source: "Chat export · 示例",
    model: "assistant-demo",
    hasAttachment: false,
    hasBranches: true,
    hasAbsence: true,
    tags: ["快照", "源缺席"],
    summary: "用于展示源缺席和历史快照的模拟记录。",
    messages: [
      {
        id: "demo-node-u-401",
        role: "user",
        author: "你",
        time: "17:22",
        paragraphs: [
          "如果新快照里找不到旧会话，界面应该怎样表示？"
        ]
      },
      {
        id: "demo-node-a-402",
        role: "assistant",
        author: "助手",
        time: "17:23",
        current: true,
        paragraphs: [
          "保留旧快照中的证据，并标记该会话在新快照中“源缺席”。除非来源提供明确删除事件，否则不显示成已删除，也不从档案中移除。"
        ],
        branches: [
          {
            label: "替代回答 A",
            time: "17:23",
            copy: "也可以把它放入历史筛选中，但仍需保留源缺席标签，避免把界面隐藏误解为删除。"
          }
        ]
      }
    ],
    provenance: {
      conversationId: "conv_demo_d771",
      selectedNodeId: "node_demo_22af",
      mappingPath: "mapping / demo-node-a-402",
      sourceFile: "conversations-demo-003.json",
      sourceSnapshot: "snapshot-demo-003",
      observedAt: "2026-07-18 17:28 +08:00",
      importedAt: "2026-08-01 14:42 +08:00",
      payloadHash: "sha256:demo0aa2…811c",
      currentBranch: "root → u401 → a402"
    },
    snapshots: [
      {
        id: "S-004",
        label: "2026-08-01",
        state: "absent",
        detail: "源缺席",
        messages: "未观察到",
        assets: "不作删除推断"
      },
      {
        id: "S-003",
        label: "2026-07-18",
        state: "present",
        detail: "最后观察",
        messages: "2 条主线",
        assets: "无附件"
      }
    ]
  }
];

const archiveMockFilterOptions = [
  { id: "attachments", label: "有附件", property: "hasAttachment" },
  { id: "branches", label: "有分支", property: "hasBranches" },
  { id: "absence", label: "源缺席", property: "hasAbsence" }
];

const archiveMockInsights = {
  generatedLabel: "模拟统计快照 S-004",
  scopeLabel: "2024–2026 · 全部演示来源",
  headlineMetrics: [
    { label: "会话", value: "1,284", detail: "4 个可观察快照", icon: "archive" },
    { label: "消息节点", value: "23,946", detail: "主线与替代分支", icon: "layers" },
    { label: "内容资产", value: "1,042", detail: "按内容哈希去重", icon: "paperclip" },
    { label: "来源记录", value: "3", detail: "覆盖声明各自独立", icon: "source" }
  ],
  activity: {
    years: {
      "2024": { seed: 7, activeDays: 186, conversations: 318, monthly: [12, 18, 21, 19, 28, 31, 26, 34, 29, 38, 30, 32] },
      "2025": { seed: 13, activeDays: 249, conversations: 487, monthly: [29, 33, 38, 31, 42, 39, 46, 51, 44, 48, 41, 45] },
      "2026": { seed: 23, activeDays: 201, conversations: 479, monthly: [45, 52, 58, 49, 61, 66, 73, 75, 0, 0, 0, 0] }
    },
    monthLabels: ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"]
  },
  roles: [
    { label: "用户", value: 10472, share: 43.7 },
    { label: "助手", value: 11886, share: 49.6 },
    { label: "工具", value: 1320, share: 5.5 },
    { label: "系统 / 其他", value: 268, share: 1.2 }
  ],
  models: [
    { label: "Model family A", value: 46 },
    { label: "Model family B", value: 31 },
    { label: "Model family C", value: 15 },
    { label: "未识别 / 旧格式", value: 8 }
  ],
  structureRates: [
    { label: "含分支会话", value: 18.6, count: "239 / 1,284", note: "存在同父节点替代项" },
    { label: "重生成节点", value: 7.4, count: "1,772 / 23,946", note: "助手替代响应" },
    { label: "编辑路径", value: 3.1, count: "40 / 1,284", note: "用户消息形成新分支" }
  ],
  attachments: {
    total: 1042,
    resolved: { label: "Resolved", count: 961, share: 92.2 },
    missing: { label: "Missing", count: 61, share: 5.9 },
    ambiguous: { label: "Ambiguous", count: 20, share: 1.9 }
  },
  snapshotDeltas: [
    { id: "S-001", label: "2026-05-12", added: 812, changed: 0, absent: 0 },
    { id: "S-002", label: "2026-06-30", added: 226, changed: 84, absent: 3 },
    { id: "S-003", label: "2026-07-18", added: 117, changed: 53, absent: 5 },
    { id: "S-004", label: "2026-08-01", added: 129, changed: 41, absent: 7 }
  ],
  sourceCoverage: [
    { label: "官方账户导出", coverage: 100, state: "source complete", detail: "清单内文件均已保存并记账" },
    { label: "浏览器增量观察", coverage: 96, state: "extraction pending", detail: "少量记录等待主机复核" },
    { label: "附件内容寻址", coverage: 92, state: "reference health", detail: "缺失与歧义单独报告" }
  ],
  quality: [
    { label: "节点父引用可解析", value: "99.97%", state: "pass" },
    { label: "current_node 可回溯", value: "100%", state: "pass" },
    { label: "附件引用确定匹配", value: "92.2%", state: "review" },
    { label: "未识别 content_type", value: "14", state: "review" }
  ],
  experiments: [
    {
      title: "主题聚类",
      value: "示例主题 A · 示例主题 B",
      detail: "由演示模型对摘要进行聚类；需要逐条核对证据与命名。",
      confidence: "演示置信度 0.81"
    },
    {
      title: "画像候选",
      value: "候选偏好：示例陈述",
      detail: "只能进入候选队列，未经用户确认不得进入 Current Profile。",
      confidence: "未审核"
    },
    {
      title: "情绪趋势",
      value: "中性 → 正向 → 未知",
      detail: "文本分类演示，不代表真实情绪、心理状态或事实判断。",
      confidence: "实验性"
    }
  ]
};

Object.assign(window, {
  archiveMockConversations,
  archiveMockFilterOptions,
  archiveMockInsights
});
