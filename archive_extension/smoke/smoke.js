import {
  DATABASE_NAME,
  DATABASE_VERSION,
  EXPECTED_SCHEMA,
  exportStore,
  inspectDatabaseSchema,
  openExistingDatabase,
  performExport,
  readCursorBatch,
  runPreflight,
  validateSchema
} from "../export.js";
import { MemoryDirectoryHandle } from "./fake-fsa.js";

const logElement = document.querySelector("#smoke-log");
const errorElement = document.querySelector("#smoke-error");

const SYNTHETIC_RECOVERY_PROVENANCE = Object.freeze({
  replay_id: "replay-1",
  challenge: "1".repeat(64),
  working_copy_evidence_tree_sha256: "2".repeat(64),
  source_payload_sha256: "3".repeat(64),
  production_bundle_sha256: "4".repeat(64),
  preparation_sha256: "5".repeat(64),
  recovery_state_sha256: "6".repeat(64),
  browser_flavor: "chrome-for-testing",
  browser_version: "151.0.7922.34",
  browser_binary_sha256: "7".repeat(64)
});

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function assertEqual(actual, expected, message) {
  if (!Object.is(actual, expected)) {
    throw new Error(`${message}: expected ${String(expected)}, observed ${String(actual)}`);
  }
}

function logPass(message) {
  const item = document.createElement("li");
  item.textContent = message;
  logElement.append(item);
}

async function check(name, operation) {
  await operation();
  logPass(name);
}

async function expectReject(operation, pattern) {
  try {
    await operation();
  } catch (error) {
    if (pattern && !pattern.test(error?.message ?? String(error))) {
      throw new Error(`Expected rejection matching ${pattern}, received: ${error?.message ?? String(error)}`);
    }
    return error;
  }
  throw new Error("Expected operation to reject");
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("Synthetic IndexedDB request failed"));
  });
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("Synthetic transaction failed"));
    transaction.onabort = () => reject(transaction.error ?? new Error("Synthetic transaction aborted"));
  });
}

async function databaseEntries() {
  return indexedDB.databases();
}

function deleteSyntheticDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.deleteDatabase(DATABASE_NAME);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error ?? new Error("Synthetic database deletion failed"));
    request.onblocked = () => reject(new Error("Synthetic database deletion was blocked"));
  });
}

function createSyntheticDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      for (const [storeName, definition] of Object.entries(EXPECTED_SCHEMA)) {
        const store = database.createObjectStore(storeName, {
          keyPath: definition.keyPath,
          autoIncrement: definition.autoIncrement
        });
        for (const [indexName, index] of Object.entries(definition.indexes)) {
          store.createIndex(indexName, index.keyPath, {
            unique: index.unique,
            multiEntry: index.multiEntry
          });
        }
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("Synthetic database creation failed"));
    request.onblocked = () => reject(new Error("Synthetic database creation was blocked"));
  });
}

function deterministicBytes(size, seed = 17) {
  const bytes = new Uint8Array(size);
  for (let index = 0; index < size; index += 1) bytes[index] = (index * 31 + seed) & 0xff;
  return bytes;
}

async function populateSyntheticDatabase(database) {
  const storeNames = Object.keys(EXPECTED_SCHEMA);
  const transaction = database.transaction(storeNames, "readwrite");

  const events = transaction.objectStore("turn-events");
  for (let index = 0; index < 260; index += 1) {
    events.add({
      version: 1,
      eventId: `event-${String(index).padStart(3, "0")}`,
      turnId: `turn-${Math.floor(index / 2)}`,
      conversationId: "conversation-synthetic",
      timestamp: 1_786_365_600_000 + index,
      type: index % 2 === 0 ? "assistant_delta" : "tool_event",
      payload: { index }
    });
  }

  transaction.objectStore("turn-records").put({
    recordId: "turn:conversation-synthetic:user-1:assistant-1",
    turnId: "turn-1",
    conversationId: "conversation-synthetic",
    nativeUserMessageId: "user-1",
    nativeAssistantMessageId: "assistant-1",
    startedAt: 1_786_365_600_000,
    updatedAt: 1_786_365_600_500,
    status: "completed",
    visible: { prompt: "synthetic prompt", output: "synthetic output", reasoning: "" },
    schemaVersion: 2
  });
  transaction.objectStore("conversation-records").put({
    conversationId: "conversation-synthetic",
    title: "Synthetic browser smoke",
    createdAt: 1_786_365_600_000,
    updatedAt: 1_786_365_600_500,
    turnRecordIds: ["turn:conversation-synthetic:user-1:assistant-1"],
    rawArtifactIds: ["artifact-success"],
    isArchived: false,
    isProjectConversation: false,
    projectId: null,
    schemaVersion: 2
  });
  transaction.objectStore("daily-aggregates").put({
    date: "2026-08-10",
    timezone: "Asia/Shanghai",
    inputTokens: 10,
    outputTokens: 20,
    reasoningTokens: 0,
    totalTokens: 30,
    turns: 1,
    tools: 0,
    activeMinutes: 1,
    updatedAt: 1_786_365_600_500,
    schemaVersion: 2
  });
  transaction.objectStore("raw-artifacts").put({
    artifactId: "artifact-success",
    kind: "live_turn",
    conversationId: "conversation-synthetic",
    turnId: "turn-1",
    createdAt: 1_786_365_600_000,
    updatedAt: 1_786_365_600_500,
    payload: {
      largeBuffer: deterministicBytes(70 * 1024).buffer,
      blob: new Blob(["synthetic blob bytes"], { type: "text/plain" })
    },
    captureSchemaVersion: 1,
    schemaVersion: 2
  });
  transaction.objectStore("sync-outbox").put({
    mutationId: "turn:synthetic:1",
    recordId: "turn:conversation-synthetic:user-1:assistant-1",
    kind: "turn",
    revision: 1_786_365_600_500,
    createdAt: 1_786_365_600_500,
    attemptCount: 0,
    lastError: null,
    schemaVersion: 1
  });
  const meta = transaction.objectStore("meta");
  meta.put({ key: "recordsMaterializedV2", value: true });
  meta.put({ key: Uint8Array.from([0, 1, 254, 255]).buffer, value: "binary-primary-key" });

  await transactionDone(transaction);
}

async function addFailingArtifact(database) {
  const cryptoKey = await crypto.subtle.generateKey(
    { name: "AES-GCM", length: 128 },
    true,
    ["encrypt", "decrypt"]
  );
  const transaction = database.transaction("raw-artifacts", "readwrite");
  transaction.objectStore("raw-artifacts").put({
    artifactId: "zz-artifact-unsupported-crypto-key",
    kind: "live_turn",
    conversationId: "conversation-synthetic",
    turnId: "turn-failure",
    createdAt: 1_786_365_601_000,
    updatedAt: 1_786_365_601_500,
    payload: {
      largeBufferWrittenBeforeFailure: deterministicBytes(70 * 1024, 91).buffer,
      unsupportedCryptoKey: cryptoKey
    },
    captureSchemaVersion: 1,
    schemaVersion: 2
  });
  await transactionDone(transaction);

  const readTransaction = database.transaction("raw-artifacts", "readonly");
  const stored = await requestResult(
    readTransaction.objectStore("raw-artifacts").get("zz-artifact-unsupported-crypto-key")
  );
  await transactionDone(readTransaction);
  assertEqual(
    Object.prototype.toString.call(stored.payload.unsupportedCryptoKey),
    "[object CryptoKey]",
    "IndexedDB must preserve the unsupported synthetic CryptoKey type"
  );
}

function parseNdjson(file) {
  const text = file.text().trim();
  if (!text) return [];
  return text.split("\n").map((line) => JSON.parse(line));
}

function parseManifest(runDirectory) {
  return JSON.parse(runDirectory.file("manifest.json").text());
}

function schemaFor(observedSchema, name) {
  const schema = observedSchema.find((store) => store.name === name);
  assert(schema, `Observed schema is missing ${name}`);
  return schema;
}

async function runSmoke() {
  await deleteSyntheticDatabase();
  let database = null;
  try {
    await check("missing database preflight refuses without opening it", async () => {
      await expectReject(
        () => runPreflight({ requireDirectoryPicker: false }),
        /不存在|does not exist/i
      );
      const entries = await databaseEntries();
      assert(!entries.some((entry) => entry.name === DATABASE_NAME), "Preflight created the missing database");
    });

    await check("open without a database triggers observable upgrade abort and leaves no database", async () => {
      await expectReject(openExistingDatabase, /onupgradeneeded/);
      const entries = await databaseEntries();
      assert(!entries.some((entry) => entry.name === DATABASE_NAME), "Upgrade abort left an empty database");
    });

    database = await createSyntheticDatabase();
    await populateSyntheticDatabase(database);
    const preflight = await runPreflight({ requireDirectoryPicker: false });
    const observedSchema = inspectDatabaseSchema(database);

    await check("synthetic database is v2 with all seven expected stores and indexes", async () => {
      assertEqual(preflight.observedVersion, 2, "Preflight database version");
      assertEqual(observedSchema.length, 7, "Observed store count");
      const errors = validateSchema(observedSchema).filter((issue) => issue.severity === "error");
      assertEqual(errors.length, 0, "Schema error count");
    });

    await check("readonly cursor crosses 128-row batches without gaps or duplicates", async () => {
      const pageSizes = [];
      const keys = [];
      let lowerBound = null;
      while (true) {
        const batch = await readCursorBatch(database, "turn-events", lowerBound, 128);
        if (batch.rows.length === 0) break;
        pageSizes.push(batch.rows.length);
        keys.push(...batch.rows.map((row) => row.primaryKey));
        lowerBound = batch.rows.at(-1).primaryKey;
        if (batch.reachedEnd) break;
      }
      assertEqual(JSON.stringify(pageSizes), JSON.stringify([128, 128, 4]), "Cursor page sizes");
      assertEqual(keys.length, 260, "Cursor key count");
      assertEqual(new Set(keys).size, 260, "Unique cursor key count");
      assertEqual(keys[0], 1, "First auto-increment key");
      assertEqual(keys.at(-1), 260, "Last auto-increment key");
    });

    await check("memory FSA commits and aborts transactionally", async () => {
      const root = new MemoryDirectoryHandle();
      const file = await root.getFileHandle("transaction.txt", { create: true });
      const committed = await file.createWritable({ keepExistingData: false });
      await committed.write("committed");
      await committed.close();
      const aborted = await file.createWritable({ keepExistingData: false });
      await aborted.write("discarded");
      await aborted.abort();
      assertEqual(file.text(), "committed", "Abort must preserve the last committed bytes");
      assertEqual(file.commitCount, 1, "FSA commit count");
      assertEqual(file.abortCount, 1, "FSA abort count");
    });

    await check("composite and binary primary keys survive the production store exporter", async () => {
      const runDirectory = new MemoryDirectoryHandle("key-export");
      const dailySchema = schemaFor(observedSchema, "daily-aggregates");
      const dailyIndex = observedSchema.indexOf(dailySchema);
      const dailyResult = await exportStore(
        database,
        runDirectory,
        "synthetic-key-smoke",
        dailySchema,
        dailyIndex
      );
      assert(dailyResult.output_committed, "Composite-key store output was not committed");
      const dailyRows = parseNdjson(runDirectory.resolveFile(dailyResult.output_file));
      const rootNode = dailyRows[0].source_primary_key.data.nodes[
        dailyRows[0].source_primary_key.data.root.$ref
      ];
      assertEqual(rootNode.$type, "Array", "Composite key graph root type");
      assertEqual(rootNode.length, 2, "Composite key length");

      const metaSchema = schemaFor(observedSchema, "meta");
      const metaIndex = observedSchema.indexOf(metaSchema);
      const metaResult = await exportStore(
        database,
        runDirectory,
        "synthetic-key-smoke",
        metaSchema,
        metaIndex
      );
      const metaRows = parseNdjson(runDirectory.resolveFile(metaResult.output_file));
      assert(
        metaRows.some((row) => row.source_primary_key.data.nodes.some((node) => node?.$type === "ArrayBuffer")),
        "No binary primary key was present in the exported meta rows"
      );
    });

    await check("Blob and large ArrayBuffer stream to independent binary files", async () => {
      const runDirectory = new MemoryDirectoryHandle("binary-export");
      const rawSchema = schemaFor(observedSchema, "raw-artifacts");
      const rawIndex = observedSchema.indexOf(rawSchema);
      const result = await exportStore(
        database,
        runDirectory,
        "synthetic-binary-smoke",
        rawSchema,
        rawIndex
      );
      assert(result.output_committed, "Raw artifact NDJSON was not committed");
      assertEqual(result.external_binary_part_count, 2, "External binary part count");
      assertEqual(result.failed_external_binary_part_count, 0, "Failed external binary count");
      for (const part of result.external_binary_parts) {
        const file = runDirectory.resolveFile(part.path);
        assertEqual(file.bytes.byteLength, part.byte_length, `Binary size for ${part.path}`);
        assertEqual(file.commitCount, 1, `Binary commit count for ${part.path}`);
      }
    });

    await check("full production export writes a pending-host-validation manifest", async () => {
      const parent = new MemoryDirectoryHandle("success-parent");
      const outcome = await performExport("synthetic-full-success", {
        parentDirectory: parent,
        preflight,
        recoveryProvenance: SYNTHETIC_RECOVERY_PROVENANCE
      });
      const runDirectory = parent.directory(outcome.runDirectoryName);
      const manifest = parseManifest(runDirectory);
      assertEqual(manifest.status, "complete", "Browser manifest status");
      assertEqual(manifest.browser_export_complete, true, "Browser export completion");
      assertEqual(manifest.extraction_complete, false, "Authoritative extraction completion");
      assertEqual(
        manifest.extraction_complete_status,
        "pending_host_hash_and_replay_validation",
        "Host validation status"
      );
      assertEqual(manifest.stores.length, 7, "Manifest store count");
      assert(manifest.stores.every((store) => store.status === "complete"), "A success store is incomplete");
    });

    await addFailingArtifact(database);
    await check("unsupported structured clone produces a partial failed store manifest", async () => {
      const parent = new MemoryDirectoryHandle("failure-parent");
      await expectReject(
        () => performExport("synthetic-expected-failure", {
          parentDirectory: parent,
          preflight,
          recoveryProvenance: SYNTHETIC_RECOVERY_PROVENANCE
        }),
        /CryptoKey|structured-clone|Store export failed/
      );
      const runDirectory = parent.onlyDirectory();
      const manifest = parseManifest(runDirectory);
      assertEqual(manifest.status, "failed", "Failure manifest status");
      assertEqual(manifest.browser_export_complete, false, "Failure browser completion");
      assertEqual(manifest.extraction_complete, false, "Failure extraction completion");
      assertEqual(manifest.extraction_complete_status, "failed", "Failure extraction status");
      const rawStore = manifest.stores.find((store) => store.name === "raw-artifacts");
      assert(rawStore, "Failure manifest is missing raw-artifacts");
      assertEqual(rawStore.status, "failed", "Raw artifact failure status");
      assertEqual(rawStore.output_committed, false, "Failed NDJSON must not commit");
      assert(rawStore.encoded_row_count >= 1, "No successful row preceded the synthetic failure");
      assert(rawStore.unencoded_cursor_row_count >= 1, "Failed cursor row was not counted");
      assert(rawStore.external_binary_attempt_count >= 3, "Partial binary attempts were not recorded");
      for (const part of rawStore.external_binary_parts) {
        assertEqual(part.status, "complete", `Unexpected binary attempt status for ${part.path}`);
        assertEqual(
          runDirectory.resolveFile(part.path).bytes.byteLength,
          part.byte_length,
          `Partial binary size for ${part.path}`
        );
      }
      const ndjson = runDirectory.resolveFile(rawStore.output_file);
      assertEqual(ndjson.bytes.byteLength, 0, "Aborted NDJSON retained uncommitted bytes");
      assertEqual(ndjson.abortCount, 1, "Failed NDJSON abort count");
    });
  } finally {
    database?.close();
    await deleteSyntheticDatabase();
  }
}

try {
  await runSmoke();
  document.body.dataset.smokeStatus = "pass";
  document.title = "PMV archive extension smoke: PASS";
  globalThis.__PMV_ARCHIVE_SMOKE__ = { status: "pass" };
} catch (error) {
  document.body.dataset.smokeStatus = "fail";
  document.title = "PMV archive extension smoke: FAIL";
  errorElement.textContent = error?.stack ?? String(error);
  globalThis.__PMV_ARCHIVE_SMOKE__ = {
    status: "fail",
    error: error?.message ?? String(error)
  };
}
