import assert from "node:assert/strict";
import test from "node:test";

globalThis.document = {
  documentElement: { dataset: {} },
  querySelector() {
    return null;
  }
};

const {
  EXPECTED_SCHEMA,
  initializeProductionPage,
  makeManifest,
  parseRecoveryProvenance,
  performExport,
  recoveryProvenanceFromSearch,
  validateSchema
} = await import("../export.js");

const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const SHA_C = "c".repeat(64);
const SHA_D = "d".repeat(64);
const SHA_E = "e".repeat(64);
const SHA_F = "f".repeat(64);

function validProvenance() {
  return {
    replay_id: "replay-1",
    challenge: SHA_A,
    working_copy_evidence_tree_sha256: SHA_B,
    source_payload_sha256: SHA_C,
    production_bundle_sha256: SHA_D,
    preparation_sha256: SHA_E,
    recovery_state_sha256: SHA_F,
    browser_flavor: "chrome-for-testing",
    browser_version: "151.0.7922.34",
    browser_binary_sha256: SHA_A
  };
}

function asSearch(value) {
  return `?${new URLSearchParams(value).toString()}`;
}

test("production recovery provenance accepts only the complete canonical contract", () => {
  const expected = validProvenance();
  assert.deepEqual(parseRecoveryProvenance(expected), expected);

  for (const field of Object.keys(expected)) {
    const incomplete = { ...expected };
    delete incomplete[field];
    assert.throws(
      () => parseRecoveryProvenance(incomplete),
      new RegExp(`missing.*${field}`, "i")
    );
  }

  assert.throws(
    () => parseRecoveryProvenance({ ...expected, unbound_note: "ignored" }),
    /unexpected.*unbound_note/i
  );
  assert.throws(
    () => parseRecoveryProvenance({ ...expected, replay_id: "replay-3" }),
    /replay_id/i
  );
  assert.throws(
    () => parseRecoveryProvenance({ ...expected, source_payload_sha256: "A".repeat(64) }),
    /source_payload_sha256/i
  );
  assert.throws(
    () => parseRecoveryProvenance({ ...expected, browser_flavor: "google-chrome" }),
    /browser_flavor/i
  );

  const symbolExtended = { ...expected };
  symbolExtended[Symbol("hidden")] = "not bound";
  assert.throws(() => parseRecoveryProvenance(symbolExtended), /unexpected.*symbol/i);

  const accessorBacked = { ...expected };
  Object.defineProperty(accessorBacked, "challenge", {
    enumerable: true,
    get() {
      return SHA_A;
    }
  });
  assert.throws(
    () => parseRecoveryProvenance(accessorBacked),
    /challenge.*data property/i
  );
});

test("production query fails closed when provenance is absent, malformed, duplicated, or extended", () => {
  const expected = validProvenance();
  assert.deepEqual(recoveryProvenanceFromSearch(asSearch(expected)), expected);

  assert.throws(() => recoveryProvenanceFromSearch(""), /missing.*replay_id/i);
  assert.throws(
    () => recoveryProvenanceFromSearch(asSearch({ ...expected, challenge: "short" })),
    /challenge/i
  );
  assert.throws(
    () => recoveryProvenanceFromSearch(`${asSearch(expected)}&replay_id=replay-2`),
    /duplicate.*replay_id/i
  );
  assert.throws(
    () => recoveryProvenanceFromSearch(`${asSearch(expected)}&extra=value`),
    /unexpected.*extra/i
  );
});

test("browser export manifest contains the exact validated recovery provenance", () => {
  const provenance = validProvenance();
  const manifest = makeManifest(
    "synthetic-snapshot",
    "synthetic-output",
    {
      runtimeId: "ainoobmdpanhopangobnggdkpljnpmgl",
      extensionVersion: "0.1.0",
      observedVersion: 2,
      databaseNames: ["chatgpt-web-usage-observatory"]
    },
    provenance
  );

  assert.deepEqual(manifest.recovery_provenance, provenance);
  assert.notEqual(manifest.recovery_provenance, provenance);
});

test("performExport rejects missing provenance before touching an output directory", async () => {
  let directoryTouched = false;
  const parentDirectory = {
    async getDirectoryHandle() {
      directoryTouched = true;
      throw new Error("output directory must not be touched");
    }
  };

  await assert.rejects(
    performExport("synthetic-snapshot", {
      parentDirectory,
      preflight: {
        runtimeId: "ainoobmdpanhopangobnggdkpljnpmgl",
        extensionVersion: "0.1.0",
        observedVersion: 2,
        databaseNames: ["chatgpt-web-usage-observatory"]
      }
    }),
    /recovery provenance/i
  );
  assert.equal(directoryTouched, false);
});

test("production page rejects missing query provenance before checking page controls", async () => {
  await assert.rejects(
    initializeProductionPage({ search: "" }),
    /recovery provenance.*missing.*replay_id/i
  );
});

test("schema validation treats every unexpected store and index as an error", () => {
  const observed = Object.entries(EXPECTED_SCHEMA).map(([name, definition]) => ({
    name,
    key_path: definition.keyPath,
    auto_increment: definition.autoIncrement,
    indexes: Object.entries(definition.indexes).map(([indexName, index]) => ({
      name: indexName,
      key_path: index.keyPath,
      unique: index.unique,
      multi_entry: index.multiEntry
    }))
  }));
  observed[0].indexes.push({
    name: "unexpected-index",
    key_path: "unexpected",
    unique: false,
    multi_entry: false
  });
  observed.push({
    name: "unexpected-store",
    key_path: null,
    auto_increment: false,
    indexes: []
  });

  const issues = validateSchema(observed);
  assert.equal(
    issues.find((issue) => issue.code === "unexpected_index")?.severity,
    "error"
  );
  assert.equal(
    issues.find((issue) => issue.code === "unexpected_store")?.severity,
    "error"
  );
});
