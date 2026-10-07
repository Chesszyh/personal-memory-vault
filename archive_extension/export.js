import {
  TaggedSerializationError,
  deterministicJson,
  encodeTaggedGraph
} from "./tagged-json.js";

export const EXPECTED_EXTENSION_ID = "ainoobmdpanhopangobnggdkpljnpmgl";
export const DATABASE_NAME = "chatgpt-web-usage-observatory";
export const DATABASE_VERSION = 2;
const MANIFEST_SCHEMA = "pmv.extension-idb-export-manifest.v1";
const ROW_ENVELOPE_SCHEMA = "pmv.extension-idb-row.v1";
const SOURCE_KIND = "extension_indexeddb_state_snapshot";
const EXTERNAL_BINARY_THRESHOLD = 64 * 1024;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const BROWSER_VERSION_PATTERN = /^\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9._-]+)?$/;

export const RECOVERY_PROVENANCE_FIELDS = Object.freeze([
  "replay_id",
  "challenge",
  "working_copy_evidence_tree_sha256",
  "source_payload_sha256",
  "production_bundle_sha256",
  "preparation_sha256",
  "recovery_state_sha256",
  "browser_flavor",
  "browser_version",
  "browser_binary_sha256"
]);

const RECOVERY_PROVENANCE_SHA256_FIELDS = new Set([
  "challenge",
  "working_copy_evidence_tree_sha256",
  "source_payload_sha256",
  "production_bundle_sha256",
  "preparation_sha256",
  "recovery_state_sha256",
  "browser_binary_sha256"
]);

export function parseRecoveryProvenance(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Recovery provenance must be an object");
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) {
    throw new Error("Recovery provenance must be a plain object");
  }

  const observedFields = Reflect.ownKeys(value);
  for (const field of observedFields) {
    if (typeof field !== "string") {
      throw new Error("Recovery provenance has unexpected symbol field");
    }
  }
  for (const field of RECOVERY_PROVENANCE_FIELDS) {
    if (!Object.hasOwn(value, field)) {
      throw new Error(`Recovery provenance is missing ${field}`);
    }
  }
  for (const field of observedFields) {
    if (!RECOVERY_PROVENANCE_FIELDS.includes(field)) {
      throw new Error(`Recovery provenance has unexpected field ${field}`);
    }
  }

  const result = {};
  for (const field of RECOVERY_PROVENANCE_FIELDS) {
    const descriptor = Object.getOwnPropertyDescriptor(value, field);
    if (!descriptor || !descriptor.enumerable || !("value" in descriptor)) {
      throw new Error(`Recovery provenance ${field} must be an enumerable data property`);
    }
    const candidate = descriptor.value;
    if (typeof candidate !== "string") {
      throw new Error(`Recovery provenance ${field} must be a string`);
    }
    result[field] = candidate;
  }

  if (!/^replay-[12]$/.test(result.replay_id)) {
    throw new Error("Recovery provenance replay_id must be replay-1 or replay-2");
  }
  for (const field of RECOVERY_PROVENANCE_SHA256_FIELDS) {
    if (!SHA256_PATTERN.test(result[field])) {
      throw new Error(`Recovery provenance ${field} must be a lowercase SHA-256 value`);
    }
  }
  if (!new Set(["chrome-for-testing", "chromium"]).has(result.browser_flavor)) {
    throw new Error(
      "Recovery provenance browser_flavor must be chrome-for-testing or chromium"
    );
  }
  if (!BROWSER_VERSION_PATTERN.test(result.browser_version)) {
    throw new Error("Recovery provenance browser_version is not canonical");
  }
  return Object.freeze(result);
}

export function recoveryProvenanceFromSearch(search) {
  if (typeof search !== "string") {
    throw new Error("Recovery provenance query must be a string");
  }
  const parameters = new URLSearchParams(search);
  const candidate = {};
  for (const [field, value] of parameters) {
    if (!RECOVERY_PROVENANCE_FIELDS.includes(field)) {
      throw new Error(`Recovery provenance query has unexpected field ${field}`);
    }
    if (Object.hasOwn(candidate, field)) {
      throw new Error(`Recovery provenance query has duplicate ${field}`);
    }
    candidate[field] = value;
  }
  return parseRecoveryProvenance(candidate);
}

export const EXPECTED_SCHEMA = {
  "turn-events": {
    keyPath: "sequence",
    autoIncrement: true,
    indexes: {
      timestamp: { keyPath: "timestamp", unique: false, multiEntry: false },
      turnId: { keyPath: "turnId", unique: false, multiEntry: false },
      type: { keyPath: "type", unique: false, multiEntry: false },
      eventId: { keyPath: "eventId", unique: false, multiEntry: false },
      conversationId: { keyPath: "conversationId", unique: false, multiEntry: false }
    }
  },
  "turn-records": {
    keyPath: "recordId",
    autoIncrement: false,
    indexes: {
      turnId: { keyPath: "turnId", unique: false, multiEntry: false },
      conversationId: { keyPath: "conversationId", unique: false, multiEntry: false },
      startedAt: { keyPath: "startedAt", unique: false, multiEntry: false },
      updatedAt: { keyPath: "updatedAt", unique: false, multiEntry: false },
      status: { keyPath: "status", unique: false, multiEntry: false },
      conversationUser: {
        keyPath: ["conversationId", "nativeUserMessageId"],
        unique: false,
        multiEntry: false
      }
    }
  },
  "conversation-records": {
    keyPath: "conversationId",
    autoIncrement: false,
    indexes: {
      updatedAt: { keyPath: "updatedAt", unique: false, multiEntry: false },
      projectId: { keyPath: "projectId", unique: false, multiEntry: false },
      isArchived: { keyPath: "isArchived", unique: false, multiEntry: false }
    }
  },
  "daily-aggregates": {
    keyPath: ["date", "timezone"],
    autoIncrement: false,
    indexes: {}
  },
  "raw-artifacts": {
    keyPath: "artifactId",
    autoIncrement: false,
    indexes: {
      conversationId: { keyPath: "conversationId", unique: false, multiEntry: false },
      turnId: { keyPath: "turnId", unique: false, multiEntry: false },
      updatedAt: { keyPath: "updatedAt", unique: false, multiEntry: false }
    }
  },
  "sync-outbox": {
    keyPath: "mutationId",
    autoIncrement: false,
    indexes: {
      createdAt: { keyPath: "createdAt", unique: false, multiEntry: false },
      recordId: { keyPath: "recordId", unique: false, multiEntry: false }
    }
  },
  meta: {
    keyPath: "key",
    autoIncrement: false,
    indexes: {}
  }
};

const statusBadge = document.querySelector("#status-badge");
const preflightDetails = document.querySelector("#preflight-details");
const preflightMessage = document.querySelector("#preflight-message");
const snapshotIdInput = document.querySelector("#snapshot-id");
const exportButton = document.querySelector("#export-button");
const exportMessage = document.querySelector("#export-message");
const exportProgress = document.querySelector("#export-progress");
const textEncoder = new TextEncoder();

let preflightResult = null;
let exportRunning = false;
let productionPageInitialized = false;
let productionRecoveryProvenance = null;

export class StoreExportFailure extends Error {
  constructor(storeResult, cause) {
    super(`Store export failed: ${storeResult.name}: ${cause?.message ?? String(cause)}`, { cause });
    this.name = "StoreExportFailure";
    this.storeResult = storeResult;
    this.serializationPath = cause instanceof TaggedSerializationError ? cause.path : null;
    this.serializationType = cause instanceof TaggedSerializationError ? cause.type : null;
  }
}

function isoNow() {
  return new Date().toISOString();
}

function displayDetail(label, value) {
  if (!preflightDetails) return;
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = String(value);
  preflightDetails.append(term, description);
}

function setBadge(kind, label) {
  if (!statusBadge) return;
  statusBadge.className = `badge badge-${kind}`;
  statusBadge.textContent = label;
}

function setExportMessage(message, kind = null) {
  if (!exportMessage) return;
  exportMessage.className = kind ? `message message-${kind}` : "message";
  exportMessage.textContent = message;
}

function errorSummary(error) {
  return {
    name: typeof error?.name === "string" ? error.name : "Error",
    message: typeof error?.message === "string" ? error.message : String(error),
    path: error instanceof TaggedSerializationError
      ? error.path
      : error instanceof StoreExportFailure
        ? error.serializationPath
        : null,
    value_type: error instanceof TaggedSerializationError
      ? error.type
      : error instanceof StoreExportFailure
        ? error.serializationType
        : null
  };
}

function copyKeyPath(keyPath) {
  if (Array.isArray(keyPath)) return [...keyPath];
  if (keyPath === null || typeof keyPath === "string") return keyPath;
  return Array.from(keyPath);
}

function sameKeyPath(left, right) {
  return deterministicJson(left) === deterministicJson(right);
}

function listNames(domStringList) {
  return Array.from(domStringList);
}

function stableStoreSlug(name) {
  const slug = name
    .normalize("NFKD")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
  return slug || "store";
}

function stableSnapshotSlug(snapshotId) {
  const slug = snapshotId
    .normalize("NFKD")
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 72);
  return slug || "snapshot";
}

function compactTimestamp() {
  return new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

function randomHex(byteCount = 4) {
  const bytes = new Uint8Array(byteCount);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function runPreflight({ requireDirectoryPicker = true } = {}) {
  const runtimeId = chrome.runtime.id;
  const extensionVersion = chrome.runtime.getManifest().version;
  if (runtimeId !== EXPECTED_EXTENSION_ID) {
    throw new Error(
      `扩展 ID 不匹配：实际 ${runtimeId}，预期 ${EXPECTED_EXTENSION_ID}。不要继续迁移数据库目录。`
    );
  }
  if (typeof indexedDB.databases !== "function") {
    throw new Error("当前 Chrome 不支持 indexedDB.databases()，为避免创建空库，导出已禁用。");
  }
  if (requireDirectoryPicker && typeof window.showDirectoryPicker !== "function") {
    throw new Error("当前环境不支持 File System Access API，无法进行流式目录导出。");
  }

  const databases = await indexedDB.databases();
  const matching = databases.filter((entry) => entry.name === DATABASE_NAME);
  if (matching.length !== 1) {
    throw new Error(
      matching.length === 0
        ? `数据库 ${DATABASE_NAME} 不存在；已拒绝调用 indexedDB.open()。`
        : `发现 ${matching.length} 个同名数据库条目；已拒绝继续。`
    );
  }
  const observedVersion = matching[0].version;
  if (observedVersion !== DATABASE_VERSION) {
    throw new Error(`数据库版本为 ${observedVersion}，预期为 ${DATABASE_VERSION}；不会创建或升级。`);
  }

  return {
    runtimeId,
    extensionVersion,
    observedVersion,
    databaseNames: databases.map((entry) => entry.name ?? null)
  };
}

export function openExistingDatabase() {
  return new Promise((resolve, reject) => {
    let settled = false;
    const request = indexedDB.open(DATABASE_NAME);
    const rejectOnce = (error) => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    request.onupgradeneeded = () => {
      try {
        request.transaction?.abort();
      } finally {
        try {
          request.result.close();
        } finally {
          rejectOnce(new Error("打开数据库触发了 onupgradeneeded；事务已中止，未创建或升级数据库。"));
        }
      }
    };
    request.onerror = () => rejectOnce(request.error ?? new Error("打开 IndexedDB 失败"));
    request.onblocked = () => rejectOnce(new Error("数据库打开被其他页面阻塞"));
    request.onsuccess = () => {
      if (settled) {
        request.result.close();
        return;
      }
      settled = true;
      const database = request.result;
      if (database.version !== DATABASE_VERSION) {
        const observedVersion = database.version;
        database.close();
        reject(new Error(`打开后的数据库版本为 ${observedVersion}，预期为 ${DATABASE_VERSION}`));
        return;
      }
      database.onversionchange = () => database.close();
      resolve(database);
    };
  });
}

function transactionResult(transaction, request) {
  return new Promise((resolve, reject) => {
    let requestValue;
    let requestFinished = false;
    request.onsuccess = () => {
      requestValue = request.result;
      requestFinished = true;
    };
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
    transaction.oncomplete = () => {
      if (!requestFinished) {
        reject(new Error("IndexedDB transaction completed without a request result"));
        return;
      }
      resolve(requestValue);
    };
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
  });
}

function countStore(database, storeName) {
  const transaction = database.transaction(storeName, "readonly");
  const request = transaction.objectStore(storeName).count();
  return transactionResult(transaction, request);
}

export function readCursorBatch(database, storeName, lowerBound, batchSize) {
  return new Promise((resolve, reject) => {
    const rows = [];
    let reachedEnd = false;
    let settled = false;
    const transaction = database.transaction(storeName, "readonly");
    const store = transaction.objectStore(storeName);
    const range = lowerBound === null ? null : IDBKeyRange.lowerBound(lowerBound, true);
    const request = store.openCursor(range, "next");

    const rejectOnce = (error) => {
      if (settled) return;
      settled = true;
      reject(error);
    };
    request.onerror = () => rejectOnce(request.error ?? new Error(`Cursor failed for ${storeName}`));
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) {
        reachedEnd = true;
        return;
      }
      rows.push({ primaryKey: cursor.primaryKey, value: cursor.value });
      if (rows.length < batchSize) cursor.continue();
    };
    transaction.onerror = () => rejectOnce(transaction.error ?? new Error(`Transaction failed for ${storeName}`));
    transaction.onabort = () => rejectOnce(transaction.error ?? new Error(`Transaction aborted for ${storeName}`));
    transaction.oncomplete = () => {
      if (settled) return;
      settled = true;
      resolve({ rows, reachedEnd });
    };
  });
}

export function inspectDatabaseSchema(database) {
  const storeNames = listNames(database.objectStoreNames);
  if (storeNames.length === 0) return [];
  const transaction = database.transaction(storeNames, "readonly");
  return storeNames.map((storeName) => {
    const store = transaction.objectStore(storeName);
    const indexes = listNames(store.indexNames).map((indexName) => {
      const index = store.index(indexName);
      return {
        name: indexName,
        key_path: copyKeyPath(index.keyPath),
        unique: index.unique,
        multi_entry: index.multiEntry
      };
    });
    return {
      name: storeName,
      key_path: copyKeyPath(store.keyPath),
      auto_increment: store.autoIncrement,
      indexes
    };
  });
}

export function validateSchema(observedStores) {
  const issues = [];
  const observedByName = new Map(observedStores.map((store) => [store.name, store]));

  for (const [storeName, expected] of Object.entries(EXPECTED_SCHEMA)) {
    const observed = observedByName.get(storeName);
    if (!observed) {
      issues.push({ severity: "error", code: "missing_expected_store", store_name: storeName });
      continue;
    }
    if (!sameKeyPath(observed.key_path, expected.keyPath)) {
      issues.push({
        severity: "error",
        code: "key_path_mismatch",
        store_name: storeName,
        expected: expected.keyPath,
        observed: observed.key_path
      });
    }
    if (observed.auto_increment !== expected.autoIncrement) {
      issues.push({
        severity: "error",
        code: "auto_increment_mismatch",
        store_name: storeName,
        expected: expected.autoIncrement,
        observed: observed.auto_increment
      });
    }

    const observedIndexes = new Map(observed.indexes.map((index) => [index.name, index]));
    for (const [indexName, expectedIndex] of Object.entries(expected.indexes)) {
      const observedIndex = observedIndexes.get(indexName);
      if (!observedIndex) {
        issues.push({
          severity: "error",
          code: "missing_expected_index",
          store_name: storeName,
          index_name: indexName
        });
        continue;
      }
      if (
        !sameKeyPath(observedIndex.key_path, expectedIndex.keyPath) ||
        observedIndex.unique !== expectedIndex.unique ||
        observedIndex.multi_entry !== expectedIndex.multiEntry
      ) {
        issues.push({
          severity: "error",
          code: "index_definition_mismatch",
          store_name: storeName,
          index_name: indexName,
          expected: {
            key_path: expectedIndex.keyPath,
            unique: expectedIndex.unique,
            multi_entry: expectedIndex.multiEntry
          },
          observed: observedIndex
        });
      }
    }
    for (const observedIndex of observed.indexes) {
      if (!(observedIndex.name in expected.indexes)) {
        issues.push({
          severity: "error",
          code: "unexpected_index",
          store_name: storeName,
          index_name: observedIndex.name
        });
      }
    }
  }

  for (const observed of observedStores) {
    if (!(observed.name in EXPECTED_SCHEMA)) {
      issues.push({ severity: "error", code: "unexpected_store", store_name: observed.name });
    }
  }
  return issues;
}

async function createFreshRunDirectory(parent, snapshotId) {
  const name = `${stableSnapshotSlug(snapshotId)}-${compactTimestamp()}-${randomHex()}`;
  try {
    await parent.getDirectoryHandle(name, { create: false });
  } catch (error) {
    if (error?.name === "NotFoundError") {
      return { name, handle: await parent.getDirectoryHandle(name, { create: true }) };
    }
    throw error;
  }
  throw new Error(`输出目录 ${name} 已存在；为避免覆盖，导出已中止。`);
}

async function directoryAt(root, parts) {
  let current = root;
  for (const part of parts) current = await current.getDirectoryHandle(part, { create: true });
  return current;
}

async function writeTextFile(directory, name, text) {
  const file = await directory.getFileHandle(name, { create: true });
  const writable = await file.createWritable({ keepExistingData: false });
  try {
    await writable.write(text);
    await writable.close();
  } catch (error) {
    try {
      await writable.abort(error);
    } catch {
      // Preserve the original write error.
    }
    throw error;
  }
}

function manifestJson(manifest) {
  return `${JSON.stringify(manifest, null, 2)}\n`;
}

async function checkpointManifest(runDirectory, manifest) {
  await writeTextFile(runDirectory, "manifest.json", manifestJson(manifest));
}

function sourceVersion(value, field) {
  if (!value || typeof value !== "object") return null;
  const candidate = value[field];
  if (typeof candidate === "string") return candidate;
  return typeof candidate === "number" && Number.isFinite(candidate) ? candidate : null;
}

async function writeExternalBinary(runDirectory, storeStem, cursorOrdinal, role, payload) {
  const binaryDirectory = await directoryAt(runDirectory, ["binary", storeStem]);
  const fileName = binaryFileName(cursorOrdinal, role, payload.nodeId);
  const fileHandle = await binaryDirectory.getFileHandle(fileName, { create: true });
  const writable = await fileHandle.createWritable({ keepExistingData: false });
  try {
    if (payload.value instanceof ArrayBuffer) {
      await writable.write(new Uint8Array(payload.value));
    } else if (typeof Blob !== "undefined" && payload.value instanceof Blob) {
      await writable.write(payload.value);
    } else {
      throw new Error(`Unsupported external binary source: ${payload.kind}`);
    }
    await writable.close();
  } catch (error) {
    try {
      await writable.abort(error);
    } catch {
      // Preserve the original write error.
    }
    throw error;
  }
  return {
    path: `binary/${storeStem}/${fileName}`,
    byteLength: payload.byteLength
  };
}

function binaryFileName(cursorOrdinal, role, nodeId) {
  return `${String(cursorOrdinal).padStart(12, "0")}-${role}-${String(nodeId).padStart(6, "0")}.bin`;
}

function binaryRelativePath(storeStem, cursorOrdinal, role, nodeId) {
  return `binary/${storeStem}/${binaryFileName(cursorOrdinal, role, nodeId)}`;
}

export async function exportStore(database, runDirectory, snapshotId, storeSchema, storeIndex) {
  const storeName = storeSchema.name;
  const storeStem = `${String(storeIndex).padStart(3, "0")}-${stableStoreSlug(storeName)}`;
  const outputName = `${storeStem}.ndjson`;
  const batchSize = storeName === "raw-artifacts" ? 1 : 128;
  let expectedCount = null;
  let lowerBound = null;
  let cursorRowsRead = 0;
  let encodedCount = 0;
  let outputBytes = 0;
  let binaryPartCount = 0;
  let binaryBytes = 0;
  const externalBinaryParts = [];
  let outputCommitted = false;
  let writable = null;

  const result = (status, error = null) => ({
    ...storeSchema,
    output_file: `stores/${outputName}`,
    cursor_batch_size: batchSize,
    count_before_export: expectedCount,
    cursor_row_count: cursorRowsRead,
    encoded_row_count: encodedCount,
    failed_row_count: error && cursorRowsRead > encodedCount ? 1 : 0,
    unencoded_cursor_row_count: Math.max(0, cursorRowsRead - encodedCount),
    attempted_output_bytes: outputBytes,
    output_committed: outputCommitted,
    external_binary_part_count: binaryPartCount,
    external_binary_bytes: binaryBytes,
    external_binary_attempt_count: externalBinaryParts.length,
    failed_external_binary_part_count: externalBinaryParts.filter((part) => part.status === "failed").length,
    external_binary_parts: externalBinaryParts,
    count_matches: expectedCount !== null &&
      expectedCount === cursorRowsRead &&
      cursorRowsRead === encodedCount,
    status,
    error: error ? errorSummary(error) : null
  });

  try {
    expectedCount = await countStore(database, storeName);
    const storesDirectory = await directoryAt(runDirectory, ["stores"]);
    const outputFile = await storesDirectory.getFileHandle(outputName, { create: true });
    writable = await outputFile.createWritable({ keepExistingData: false });
    while (true) {
      const batch = await readCursorBatch(database, storeName, lowerBound, batchSize);
      if (batch.rows.length === 0) break;
      cursorRowsRead += batch.rows.length;
      for (const row of batch.rows) {
        const cursorOrdinal = encodedCount;
        const writeTrackedBinary = async (role, payload) => {
          const part = {
            path: binaryRelativePath(storeStem, cursorOrdinal, role, payload.nodeId),
            cursor_ordinal: cursorOrdinal,
            role,
            node_id: payload.nodeId,
            kind: payload.kind,
            byte_length: payload.byteLength,
            status: "in_progress"
          };
          externalBinaryParts.push(part);
          try {
            const reference = await writeExternalBinary(
              runDirectory,
              storeStem,
              cursorOrdinal,
              role,
              payload
            );
            part.status = "complete";
            binaryPartCount += 1;
            binaryBytes += payload.byteLength;
            return reference;
          } catch (error) {
            part.status = "failed";
            throw error;
          }
        };
        const keyGraph = await encodeTaggedGraph(row.primaryKey, {
          externalBinaryThreshold: EXTERNAL_BINARY_THRESHOLD,
          context: { storeName, cursorOrdinal, role: "primary-key" },
          writeBinary: (payload) => writeTrackedBinary("primary-key", payload)
        });
        const valueGraph = await encodeTaggedGraph(row.value, {
          externalBinaryThreshold: EXTERNAL_BINARY_THRESHOLD,
          context: { storeName, cursorOrdinal, role: "value" },
          writeBinary: (payload) => writeTrackedBinary("value", payload)
        });
        const binaryParts = [...keyGraph.binaryParts, ...valueGraph.binaryParts];
        const encodedPrimaryKey = { codec: keyGraph.codec, data: keyGraph.data };
        const encodedValue = { codec: valueGraph.codec, data: valueGraph.data };
        const envelope = {
          envelope_schema: ROW_ENVELOPE_SCHEMA,
          snapshot_id: snapshotId,
          source_kind: SOURCE_KIND,
          database_name: DATABASE_NAME,
          database_version: DATABASE_VERSION,
          store_name: storeName,
          source_primary_key: encodedPrimaryKey,
          cursor_ordinal: cursorOrdinal,
          source_record_schema_version: sourceVersion(row.value, "schemaVersion"),
          capture_schema_version: sourceVersion(row.value, "captureSchemaVersion"),
          value: encodedValue,
          decoded_value_sha256: null,
          decoded_value_hash_status: "deferred_to_host",
          decode_status: "ok",
          decoder_warnings: [],
          binary_parts: binaryParts
        };
        const line = `${deterministicJson(envelope)}\n`;
        await writable.write(line);
        outputBytes += textEncoder.encode(line).byteLength;
        encodedCount += 1;
        lowerBound = row.primaryKey;
        if (encodedCount % 100 === 0) {
          setExportMessage(`正在导出 ${storeName}：${encodedCount} / ${expectedCount}`);
        }
      }
      if (batch.reachedEnd) break;
    }
    await writable.close();
    outputCommitted = true;
  } catch (error) {
    if (writable && !outputCommitted) {
      try {
        await writable.abort(error);
      } catch {
        // Preserve the original export error.
      }
    }
    throw new StoreExportFailure(result("failed", error), error);
  }

  const completed = result("complete");
  if (!completed.count_matches) completed.status = "failed";
  return completed;
}

export function makeManifest(
  snapshotId,
  runDirectoryName,
  observedPreflight = preflightResult,
  recoveryProvenance
) {
  if (!observedPreflight) throw new Error("Preflight result is required to create an export manifest");
  const validatedRecoveryProvenance = parseRecoveryProvenance(recoveryProvenance);
  return {
    manifest_schema: MANIFEST_SCHEMA,
    status: "in_progress",
    browser_export_complete: false,
    extraction_complete: false,
    extraction_complete_status: "pending_browser_export",
    snapshot_id: snapshotId,
    source_kind: SOURCE_KIND,
    started_at: isoNow(),
    completed_at: null,
    output_directory_name: runDirectoryName,
    database: {
      name: DATABASE_NAME,
      expected_version: DATABASE_VERSION,
      observed_version: observedPreflight.observedVersion
    },
    recovery_extension: {
      expected_id: EXPECTED_EXTENSION_ID,
      runtime_id: observedPreflight.runtimeId,
      version: observedPreflight.extensionVersion,
      manifest_version: 3
    },
    recovery_provenance: { ...validatedRecoveryProvenance },
    browser: {
      user_agent: navigator.userAgent
    },
    preflight: {
      database_names_observed: observedPreflight.databaseNames,
      passed: true
    },
    expected_store_names: Object.keys(EXPECTED_SCHEMA),
    observed_schema: [],
    schema_issues: [],
    stores: [],
    error: null,
    excluded_operational_sources: ["ExtensionStorage"],
    historical_versions_may_have_been_overwritten: true,
    checksums: {
      status: "deferred_to_host_after_chrome_exit",
      algorithm: "sha256"
    }
  };
}

export async function performExport(
  snapshotId,
  {
    parentDirectory = null,
    preflight = preflightResult,
    recoveryProvenance
  } = {}
) {
  const validatedRecoveryProvenance = parseRecoveryProvenance(recoveryProvenance);
  const selectedParent = parentDirectory ?? await window.showDirectoryPicker({ mode: "readwrite" });
  const run = await createFreshRunDirectory(selectedParent, snapshotId);
  const manifest = makeManifest(
    snapshotId,
    run.name,
    preflight,
    validatedRecoveryProvenance
  );
  let database = null;
  await checkpointManifest(run.handle, manifest);

  try {
    database = await openExistingDatabase();
    const observedSchema = inspectDatabaseSchema(database);
    manifest.observed_schema = observedSchema;
    manifest.schema_issues = validateSchema(observedSchema);
    await checkpointManifest(run.handle, manifest);

    if (exportProgress) {
      exportProgress.hidden = false;
      exportProgress.max = Math.max(observedSchema.length, 1);
      exportProgress.value = 0;
    }

    for (let index = 0; index < observedSchema.length; index += 1) {
      const store = observedSchema[index];
      setExportMessage(`正在导出 ${store.name}……`);
      const storeStem = `${String(index).padStart(3, "0")}-${stableStoreSlug(store.name)}`;
      const manifestStoreIndex = manifest.stores.length;
      manifest.stores.push({
        ...store,
        output_file: `stores/${storeStem}.ndjson`,
        cursor_batch_size: store.name === "raw-artifacts" ? 1 : 128,
        status: "in_progress"
      });
      await checkpointManifest(run.handle, manifest);
      try {
        const storeResult = await exportStore(database, run.handle, snapshotId, store, index);
        manifest.stores[manifestStoreIndex] = storeResult;
      } catch (error) {
        if (error instanceof StoreExportFailure) {
          manifest.stores[manifestStoreIndex] = error.storeResult;
        } else {
          manifest.stores[manifestStoreIndex].status = "failed";
          manifest.stores[manifestStoreIndex].error = errorSummary(error);
        }
        await checkpointManifest(run.handle, manifest);
        throw error;
      }
      if (exportProgress) exportProgress.value = index + 1;
      await checkpointManifest(run.handle, manifest);
    }

    const schemaErrors = manifest.schema_issues.filter((issue) => issue.severity === "error");
    const storesClosed = manifest.stores.every((store) =>
      store.count_matches &&
      store.output_committed &&
      store.failed_external_binary_part_count === 0 &&
      store.status === "complete"
    );
    manifest.browser_export_complete = schemaErrors.length === 0 && storesClosed;
    manifest.status = manifest.browser_export_complete ? "complete" : "failed";
    manifest.extraction_complete = false;
    manifest.extraction_complete_status = manifest.browser_export_complete
      ? "pending_host_hash_and_replay_validation"
      : "failed";
    manifest.completed_at = isoNow();
    if (!manifest.browser_export_complete) {
      manifest.error = {
        name: "BrowserExportIncomplete",
        message: "Browser schema validation, store count closure, or output commit failed",
        path: null,
        value_type: null
      };
    }
    await checkpointManifest(run.handle, manifest);

    if (!manifest.browser_export_complete) {
      throw new Error(`导出文件已保留，但浏览器导出未闭合。请检查 ${run.name}/manifest.json。`);
    }
    setExportMessage(
      `浏览器导出完成：${run.name}\n退出 Chrome 后由主机计算 SHA-256、补逐行哈希并判定 Extraction Complete。`,
      "ok"
    );
    return { runDirectoryName: run.name, manifest };
  } catch (error) {
    manifest.status = "failed";
    manifest.browser_export_complete = false;
    manifest.extraction_complete = false;
    manifest.extraction_complete_status = "failed";
    manifest.completed_at = isoNow();
    manifest.error = errorSummary(error);
    try {
      await checkpointManifest(run.handle, manifest);
    } catch (manifestError) {
      throw new AggregateError([error, manifestError], "导出失败，且 failed manifest 写入失败");
    }
    throw error;
  } finally {
    database?.close();
  }
}

function normalizeSnapshotId(value) {
  const snapshotId = value.trim();
  if (!snapshotId) throw new Error("Snapshot ID 不能为空。");
  if (snapshotId.length > 200) throw new Error("Snapshot ID 不能超过 200 个字符。");
  if (/[\u0000-\u001f\u007f]/.test(snapshotId)) throw new Error("Snapshot ID 不能包含控制字符。");
  return snapshotId;
}

async function handleExportClick() {
  if (exportRunning || !preflightResult || !snapshotIdInput || !exportButton) return;
  let snapshotId;
  try {
    snapshotId = normalizeSnapshotId(snapshotIdInput.value);
  } catch (error) {
    setExportMessage(error.message, "error");
    return;
  }

  exportRunning = true;
  exportButton.disabled = true;
  snapshotIdInput.disabled = true;
  setExportMessage("等待选择输出父目录……");
  try {
    await performExport(snapshotId, {
      recoveryProvenance: productionRecoveryProvenance
    });
  } catch (error) {
    if (error?.name === "AbortError") {
      setExportMessage("用户取消了目录选择；未写入任何导出数据。", "error");
    } else {
      setExportMessage(`导出失败：${error?.message ?? String(error)}`, "error");
    }
  } finally {
    exportRunning = false;
    exportButton.disabled = !preflightResult;
    snapshotIdInput.disabled = false;
  }
}

export async function initializeProductionPage(
  { search = globalThis.location?.search ?? "" } = {}
) {
  if (productionPageInitialized) return;
  productionPageInitialized = true;
  try {
    productionRecoveryProvenance = recoveryProvenanceFromSearch(search);
  } catch (error) {
    preflightResult = null;
    productionRecoveryProvenance = null;
    setBadge("error", "已拒绝");
    if (preflightMessage) {
      preflightMessage.classList.add("message-error");
      preflightMessage.textContent = `恢复来源绑定无效：${error?.message ?? String(error)}`;
    }
    if (exportButton) exportButton.disabled = true;
    throw error;
  }
  if (!exportButton || !snapshotIdInput) {
    throw new Error("Production export page is missing required controls");
  }
  exportButton.addEventListener("click", handleExportClick);

  try {
    preflightResult = await runPreflight();
    setBadge("ok", "可读取");
    if (preflightMessage) {
      preflightMessage.textContent = "只读预检通过。数据库尚未打开；点击导出后才会以无版本参数打开。";
    }
    displayDetail("扩展 ID", preflightResult.runtimeId);
    displayDetail("数据库", DATABASE_NAME);
    displayDetail("版本", preflightResult.observedVersion);
    displayDetail("预期 stores", Object.keys(EXPECTED_SCHEMA).length);
    displayDetail("Replay", productionRecoveryProvenance.replay_id);
    exportButton.disabled = false;
  } catch (error) {
    preflightResult = null;
    setBadge("error", "已拒绝");
    if (preflightMessage) {
      preflightMessage.classList.add("message-error");
      preflightMessage.textContent = error?.message ?? String(error);
    }
    displayDetail("扩展 ID", chrome.runtime.id);
    displayDetail("预期扩展 ID", EXPECTED_EXTENSION_ID);
    exportButton.disabled = true;
  }
}

if (document.documentElement.dataset.pmvPage === "archive-export") {
  await initializeProductionPage();
}
