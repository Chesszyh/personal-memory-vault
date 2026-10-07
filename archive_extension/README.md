# Archive-only IndexedDB reader

这个目录是扩展源码与测试夹具，**不得直接加载到真实恢复 profile**。主机准备流程会从这里复制并验证一个只读 production bundle；该 bundle 严格只有五个静态文件：

```text
manifest.json
export.html
export.css
export.js
tagged-json.js
```

`package.json`、`test/` 和 `smoke/` 只用于测试，绝不进入 production bundle。尤其是 `smoke/` 会创建和删除完全合成的同名数据库；它只能存在于断网、一次性的合成 profile 中。真实 argv 只允许加载每个 replay 内已固定 SHA-256 的 `runtime/archive-extension/`，不能加载本源码目录。

## 安全边界

- 固定 manifest public key；预期扩展 ID 是 `ainoobmdpanhopangobnggdkpljnpmgl`。页面会在运行时再次核对 ID。
- manifest 没有 permissions、host permissions、background、content scripts、action、sync 或 web-accessible resources。
- CSP 使用 `default-src 'none'` 和 `connect-src 'none'`；代码不包含任何网络客户端。
- 页面先调用 `indexedDB.databases()`。目标数据库不存在或版本不是 2 时，不会调用 `indexedDB.open()`。
- 真正打开时不传 version；若仍触发 `onupgradeneeded`，立即 abort。
- 所有数据事务均为 `readonly`。导出使用分批 cursor；不会调用 `getAll()`。
- `ExtensionStorage` 不读取、不复制、不导出。
- 只能针对 Recovery Working Copy 使用；禁止把冻结 master 或日常 ChatGPT profile 暴露给此流程。

这个扩展没有 toolbar action。生产导出页只能由主机恢复编排器在专用临时
profile 中打开；下面这个不带来源绑定参数的裸 URL 会被页面拒绝：

```text
chrome-extension://ainoobmdpanhopangobnggdkpljnpmgl/export.html
```

编排器必须在 query string 中精确提供以下十个参数，不能缺少、重复或增加字段：

```text
replay_id
challenge
working_copy_evidence_tree_sha256
source_payload_sha256
production_bundle_sha256
preparation_sha256
recovery_state_sha256
browser_flavor
browser_version
browser_binary_sha256
```

规范化后的 `recovery_provenance` 结构为：

```json
{
  "replay_id": "replay-1",
  "challenge": "<64 lowercase hex>",
  "working_copy_evidence_tree_sha256": "<sha256>",
  "source_payload_sha256": "<sha256>",
  "production_bundle_sha256": "<sha256>",
  "preparation_sha256": "<sha256>",
  "recovery_state_sha256": "<sha256>",
  "browser_flavor": "chrome-for-testing",
  "browser_version": "151.0.7922.34",
  "browser_binary_sha256": "<sha256>"
}
```

`replay_id` 只能是 `replay-1` 或 `replay-2`；`challenge` 和所有 SHA-256
字段都是 64 位小写十六进制；`browser_flavor` 只能是
`chrome-for-testing` 或 `chromium`。页面在 IndexedDB 预检、目录选择和创建
输出目录之前验证这些值，并把原样规范化后的对象写入每一次成功或失败的
browser manifest。主机验收必须将它逐字段匹配到对应 replay 的准备记录、恢复
状态、生产 bundle 和实际浏览器二进制；URL 中的自述本身不构成独立证明。

首次创建临时 profile 时，页面应报告数据库不存在。必须完全退出该 Chrome 后，外部恢复脚本才能把 **工作副本** 的 LevelDB 和 blob origin 目录安装到临时 profile。不要加载历史扩展、`realtime-only` 扩展或 `ExtensionStorage`。

核心 Python API 与 `personal-vault extension-recovery` CLI 已负责五文件 bundle、两份 replay、Chrome for Testing/Chromium 身份、专属输出目录、来源哈希、隔离启动、等待 Chrome 完全退出和主机验收绑定。不要绕过 CLI 手工复制 master、拼接 Chrome 参数或加载本源码目录。

## 导出行为

用户点击按钮后，页面通过 File System Access API 选择输出父目录，并为本次运行创建一个全新子目录。输出包含：

```text
<run>/
├── manifest.json
├── stores/
│   └── <ordinal>-<store>.ndjson
└── binary/
    └── <ordinal>-<store>/...
```

预期数据库是 `chatgpt-web-usage-observatory` v2，含七个 store：

```text
turn-events
turn-records
conversation-records
daily-aggregates
raw-artifacts
sync-outbox
meta
```

扩展会动态枚举现场的所有 store；额外 store 仍会导出。任何预期 store、keyPath 或 index 缺失，任一 `count()` 与 cursor/编码数量不一致，或者某个值无法无损编码时，`manifest.json` 都保持 `failed`。

即使浏览器侧顺利完成，manifest 也只设置 `browser_export_complete=true` 和 `status=complete`；`extraction_complete` 仍为 `false`，状态是 `pending_host_hash_and_replay_validation`。只有 Chrome 完全退出后，主机补齐逐行确定性哈希、全文件 SHA-256，并完成第二份工作副本的重放对账，外层验收报告才能声明 `Extraction Complete`。

每个 browser manifest 都包含严格的 `recovery_provenance`。两次 replay 的
`replay_id` 和 `challenge` 必须不同，其他来源、bundle 和浏览器身份哈希必须与
主机为各自运行签发的期望值一致。复制同一次输出到另一个目录不能满足这个条件。

`raw-artifacts` 每批只从 IndexedDB 取一条；其他 store 每批最多 128 条。事务完成后才进行文件写入，因此不会在等待 File System Access I/O 时错误地保持或恢复一个 IndexedDB transaction。

每行是 `pmv.extension-idb-row.v1` envelope，包含 source/snapshot、store、cursor ordinal、primary key、完整 value、schema version 和二进制引用。它是浏览器解码后的 structured-clone 图，不冒充 LevelDB/V8 原始字节：

- 循环和共享引用使用 graph node ID；
- 明确标记 `undefined`、BigInt、NaN、正负 Infinity 和 `-0`；
- 保留 Date、RegExp、Map、Set、普通/空原型对象、稀疏 Array；
- 保留 ArrayBuffer、TypedArray、DataView 的 buffer identity、offset 和长度；Resizable ArrayBuffer 本身可保存，但由于 JS 无法反射 view 的 fixed/length-tracking 内部模式，其上的 view 会硬失败；
- Blob、File 及超过 64 KiB 的 ArrayBuffer 流式写入 `binary/`；
- 未知对象类型、函数、symbol 或不安全的二进制路径会硬失败。

### Tagged graph contract

Primary key 和 value 都使用同一个 `tagged-structured-clone-v1` codec：

```json
{
  "codec": "tagged-structured-clone-v1",
  "data": {
    "root": { "$ref": 0 },
    "nodes": [
      {
        "$type": "Object",
        "prototype": "Object",
        "properties": [["example", { "$type": "Undefined" }]]
      }
    ]
  }
}
```

JSON 原生可无损表达的 `null`、boolean、string 和普通有限 number 直接内联；其他 primitive 使用 `$type`：

```text
Undefined
BigInt(value)
Number(value = NaN | Infinity | -Infinity | -0)
Absent（只用于缺少的 Error cause/errors）
```

所有有 identity 的值都存入 `data.nodes`，引用统一为 `{ "$ref": <node id> }`。当前 node 类型是：

```text
Object(prototype, properties)
Array(length, entries, properties)
Date(milliseconds, properties)
RegExp(source, flags, lastIndex, properties)
Map(entries, properties)
Set(values, properties)
ArrayBuffer(byteLength, resizable, maxByteLength, data, properties)
TypedArray(name, buffer, byteOffset, length, properties)
DataView(buffer, byteOffset, byteLength, properties)
Blob(size, mediaType, data, properties)
File(size, mediaType, name, lastModified, webkitRelativePath, data, properties)
BoxedBoolean / BoxedNumber / BoxedString / BoxedBigInt
Error(name, message, stack, cause, errors, properties)
DOMException(name, message, code, stack, properties)
```

`properties` 是 `[key, tagged-value]` 数组，不用 JSON object 承载用户 key，因此 `__proto__` 等名称没有特殊行为。Array 的 `entries` 只列出真实存在的 index，能够区分 hole 和 `undefined`。Map/Set 保留迭代顺序；graph node ID 保留循环和共享 identity。

ArrayBuffer 的 `data` 是 base64 inline，或者 `{ "external": { "path", "byteLength" } }`。Blob/File 始终 external。每行 envelope 的 `binary_parts` 汇总外置引用；manifest 还记录每次 binary 写入尝试及其状态，因此失败运行中的部分文件也可对账。

主机补写 `decoded_value_sha256` 时，哈希目标是该行 `value` 字段的 UTF-8 deterministic JSON，而不是整个 envelope、LevelDB value 或 V8 原始字节。原始存储完整性由 Recovery Working Copy 的文件清单单独证明。

浏览器不计算最终文件 SHA-256。必须在 Chrome 完全退出后，由主机工具对 manifest、NDJSON 和 binary 文件统一计算校验值，并完成第二次独立导出的确定性对账。

## 单元测试

测试只使用 Node 内置模块，不读取 vault、IndexedDB 或 Chrome profile，也不访问网络：

```bash
node --test archive_extension/test/*.test.mjs
```

测试覆盖 tagged graph 类型、循环/共享引用、大二进制外置、Blob/File、拒绝有损类型、固定扩展 ID、manifest 权限面、代码中的网络/写事务禁令，以及 recovery provenance 的严格 query/manifest 契约。

## 纯合成浏览器冒烟

`smoke/` 不被生产 manifest 引用，只用于正式接触 Recovery Working Copy 前的集成验收。它在全新的临时 profile 内创建一个完全合成的 IndexedDB v2，不读取 vault、备份、日常浏览器 profile 或 ExtensionStorage。

```bash
bash archive_extension/smoke/run-browser-smoke.sh
```

runner 使用本机 Python Playwright 驱动 Chrome for Testing 或无品牌 Chromium，并强制套入 `bwrap --unshare-net`：主机文件系统只读，只有一次性 profile 可写，且不会使用 `--no-sandbox`。Google Chrome 从 137 起不再接受命令行 `--load-extension`；runner 会明确拒绝该二进制，而不是把 `ERR_BLOCKED_BY_CLIENT` 误报成扩展失败。可通过 `PMV_CHROME_BIN=/path/to/chrome-for-testing` 指定兼容二进制。

它通过真实 Chrome IndexedDB 覆盖：

- 缺少数据库时预检不调用 open；
- 无版本 open 对空库触发并回滚 `onupgradeneeded`；
- 七个 v2 store/index；
- readonly cursor 跨越 `128 + 128 + 4` 行；
- composite key 与 binary key；
- Blob 和超过 64 KiB 的 ArrayBuffer；
- 完整成功 manifest 仍等待主机哈希/重放；
- 不支持的 synthetic CryptoKey 触发 store failure，并保留 partial binary manifest；
- 内存 fake FSA 的 commit/abort 语义。

测试使用 production `export.js` 的同一组函数；依赖注入只替换目录选择器并显式传入一组固定的假 provenance，不复制导出实现。该假 provenance 只验证 browser manifest 的数据流，不得用于真实恢复或主机验收。Playwright 的调试管道只存在于这个纯合成、断网的一次性 profile，绝不用于 Recovery Working Copy。需要保留失败时的一次性目录用于诊断，可设置 `PMV_KEEP_SMOKE=1`。该目录只含合成数据。
