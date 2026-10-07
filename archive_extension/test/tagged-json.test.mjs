import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { Blob as NodeBlob, File as NodeFile } from "node:buffer";
import test from "node:test";

import {
  TAGGED_GRAPH_CODEC,
  TaggedSerializationError,
  deterministicJson,
  encodeTaggedGraph
} from "../tagged-json.js";

if (typeof globalThis.Blob === "undefined") globalThis.Blob = NodeBlob;
if (typeof globalThis.File === "undefined") globalThis.File = NodeFile;

function propertiesOf(node) {
  return Object.fromEntries(node.properties);
}

function extensionIdFromKey(base64Der) {
  const digest = createHash("sha256").update(Buffer.from(base64Der, "base64")).digest();
  const alphabet = "abcdefghijklmnop";
  let id = "";
  for (const byte of digest.subarray(0, 16)) {
    id += alphabet[byte >>> 4];
    id += alphabet[byte & 0x0f];
  }
  return id;
}

test("encodes every non-JSON primitive without coercion", async () => {
  const encoded = await encodeTaggedGraph({
    undefinedValue: undefined,
    bigintValue: 9007199254740993123456789n,
    nanValue: NaN,
    positiveInfinity: Infinity,
    negativeInfinity: -Infinity,
    negativeZero: -0,
    ordinary: 42,
    nullValue: null
  });

  assert.equal(encoded.codec, TAGGED_GRAPH_CODEC);
  const root = encoded.data.nodes[encoded.data.root.$ref];
  const properties = propertiesOf(root);
  assert.deepEqual(properties.undefinedValue, { $type: "Undefined" });
  assert.deepEqual(properties.bigintValue, {
    $type: "BigInt",
    value: "9007199254740993123456789"
  });
  assert.deepEqual(properties.nanValue, { $type: "Number", value: "NaN" });
  assert.deepEqual(properties.positiveInfinity, { $type: "Number", value: "Infinity" });
  assert.deepEqual(properties.negativeInfinity, { $type: "Number", value: "-Infinity" });
  assert.deepEqual(properties.negativeZero, { $type: "Number", value: "-0" });
  assert.equal(properties.ordinary, 42);
  assert.equal(properties.nullValue, null);
  assert.doesNotThrow(() => deterministicJson(encoded));
});

test("preserves cycles and shared object identity", async () => {
  const shared = { marker: "same object" };
  const source = { left: shared, right: shared };
  source.self = source;

  const encoded = await encodeTaggedGraph(source);
  const rootId = encoded.data.root.$ref;
  const rootProperties = propertiesOf(encoded.data.nodes[rootId]);
  assert.deepEqual(rootProperties.self, { $ref: rootId });
  assert.deepEqual(rootProperties.left, rootProperties.right);
  assert.notEqual(rootProperties.left.$ref, rootId);
});

test("preserves sparse arrays, null prototypes, Date, RegExp, Map, and Set", async () => {
  const sparse = [];
  sparse.length = 4;
  sparse[0] = "first";
  sparse[2] = undefined;
  sparse.note = "custom";
  const nullPrototype = Object.create(null);
  nullPrototype.answer = 42;
  const map = new Map();
  map.set("self", map);
  const set = new Set(["a", 2]);

  const encoded = await encodeTaggedGraph({
    sparse,
    nullPrototype,
    date: new Date("2026-08-10T12:34:56.789Z"),
    invalidDate: new Date(NaN),
    regexp: /archive/giu,
    map,
    set
  });
  const root = propertiesOf(encoded.data.nodes[encoded.data.root.$ref]);

  const arrayNode = encoded.data.nodes[root.sparse.$ref];
  assert.equal(arrayNode.length, 4);
  assert.deepEqual(arrayNode.entries, [[0, "first"], [2, { $type: "Undefined" }]]);
  assert.deepEqual(arrayNode.properties, [["note", "custom"]]);

  const nullNode = encoded.data.nodes[root.nullPrototype.$ref];
  assert.equal(nullNode.prototype, "null");
  assert.deepEqual(nullNode.properties, [["answer", 42]]);

  const dateNode = encoded.data.nodes[root.date.$ref];
  assert.equal(dateNode.milliseconds, 1786365296789);
  const invalidDateNode = encoded.data.nodes[root.invalidDate.$ref];
  assert.deepEqual(invalidDateNode.milliseconds, { $type: "Number", value: "NaN" });

  const regexpNode = encoded.data.nodes[root.regexp.$ref];
  assert.equal(regexpNode.source, "archive");
  assert.equal(regexpNode.flags, "giu");

  const mapNode = encoded.data.nodes[root.map.$ref];
  assert.deepEqual(mapNode.entries[0], ["self", { $ref: root.map.$ref }]);
  const setNode = encoded.data.nodes[root.set.$ref];
  assert.deepEqual(setNode.values, ["a", 2]);
});

test("tags RegExp lastIndex special values instead of relying on JSON coercion", async () => {
  const negativeZero = /zero/g;
  negativeZero.lastIndex = -0;
  const bigint = /bigint/g;
  bigint.lastIndex = 12n;
  const encoded = await encodeTaggedGraph({ negativeZero, bigint });
  const root = propertiesOf(encoded.data.nodes[encoded.data.root.$ref]);
  assert.deepEqual(encoded.data.nodes[root.negativeZero.$ref].lastIndex, {
    $type: "Number",
    value: "-0"
  });
  assert.deepEqual(encoded.data.nodes[root.bigint.$ref].lastIndex, {
    $type: "BigInt",
    value: "12"
  });
});

test("inlines small ArrayBuffers and preserves shared typed-array buffers", async () => {
  const buffer = Uint8Array.from([0, 1, 2, 3, 254, 255]).buffer;
  const view = new Uint16Array(buffer, 2, 2);
  const dataView = new DataView(buffer, 1, 3);
  const encoded = await encodeTaggedGraph({ buffer, view, dataView }, {
    externalBinaryThreshold: 1024
  });
  const root = propertiesOf(encoded.data.nodes[encoded.data.root.$ref]);
  const bufferNode = encoded.data.nodes[root.buffer.$ref];
  assert.equal(bufferNode.data.inline.encoding, "base64");
  assert.equal(bufferNode.data.inline.value, "AAECA/7/");
  assert.deepEqual(encoded.data.nodes[root.view.$ref].buffer, root.buffer);
  assert.deepEqual(encoded.data.nodes[root.dataView.$ref].buffer, root.buffer);
  assert.equal(encoded.data.nodes[root.view.$ref].name, "Uint16Array");
  assert.equal(encoded.data.nodes[root.view.$ref].byteOffset, 2);
  assert.equal(encoded.data.nodes[root.dataView.$ref].byteLength, 3);
  assert.deepEqual(encoded.binaryParts, []);
});

test("externalizes a large shared ArrayBuffer exactly once", async () => {
  const buffer = Uint8Array.from([1, 2, 3, 4, 5, 6]).buffer;
  const writes = [];
  const encoded = await encodeTaggedGraph({ first: buffer, second: buffer }, {
    externalBinaryThreshold: 4,
    context: { record: 7 },
    writeBinary: async (payload) => {
      writes.push(payload);
      return { path: "binary/record-7-value-1.bin", byteLength: payload.byteLength };
    }
  });

  assert.equal(writes.length, 1);
  assert.equal(writes[0].kind, "ArrayBuffer");
  assert.equal(writes[0].context.record, 7);
  assert.equal(writes[0].byteLength, 6);
  const root = propertiesOf(encoded.data.nodes[encoded.data.root.$ref]);
  assert.deepEqual(root.first, root.second);
  const node = encoded.data.nodes[root.first.$ref];
  assert.deepEqual(node.data.external, {
    path: "binary/record-7-value-1.bin",
    byteLength: 6
  });
  assert.deepEqual(encoded.binaryParts, [{
    nodeId: root.first.$ref,
    kind: "ArrayBuffer",
    byteLength: 6,
    path: "binary/record-7-value-1.bin"
  }]);
});

test("preserves a resizable ArrayBuffer but rejects views with unobservable tracking mode", async () => {
  const buffer = new ArrayBuffer(8, { maxByteLength: 32 });
  if (!buffer.resizable) return;

  const encoded = await encodeTaggedGraph(buffer);
  const bufferNode = encoded.data.nodes[encoded.data.root.$ref];
  assert.equal(bufferNode.resizable, true);
  assert.equal(bufferNode.maxByteLength, 32);

  await assert.rejects(
    encodeTaggedGraph(new Uint8Array(buffer)),
    (error) => error instanceof TaggedSerializationError && /length-tracking mode/.test(error.message)
  );
  await assert.rejects(
    encodeTaggedGraph(new DataView(buffer)),
    (error) => error instanceof TaggedSerializationError && /length-tracking mode/.test(error.message)
  );
});

test("externalizes Blob and File bytes while retaining metadata", async () => {
  const blob = new Blob([Uint8Array.from([7, 8, 9])], { type: "application/octet-stream" });
  const file = new File(["hello"], "greeting.txt", {
    type: "text/plain",
    lastModified: 123456789
  });
  const writes = [];
  const encoded = await encodeTaggedGraph({ blob, file }, {
    writeBinary: async (payload) => {
      const bytes = new Uint8Array(await payload.value.arrayBuffer());
      writes.push({ kind: payload.kind, bytes: [...bytes], fileName: payload.fileName });
      return {
        path: `binary/${payload.kind.toLowerCase()}-${payload.nodeId}.bin`,
        byteLength: payload.byteLength
      };
    }
  });

  assert.deepEqual(writes, [
    { kind: "Blob", bytes: [7, 8, 9], fileName: null },
    { kind: "File", bytes: [104, 101, 108, 108, 111], fileName: "greeting.txt" }
  ]);
  const root = propertiesOf(encoded.data.nodes[encoded.data.root.$ref]);
  const blobNode = encoded.data.nodes[root.blob.$ref];
  const fileNode = encoded.data.nodes[root.file.$ref];
  assert.equal(blobNode.mediaType, "application/octet-stream");
  assert.equal(fileNode.name, "greeting.txt");
  assert.equal(fileNode.lastModified, 123456789);
  assert.equal(fileNode.mediaType, "text/plain");
});

test("hard-fails on unsupported or lossy values", async () => {
  class UnsupportedClass {
    constructor() {
      this.value = 1;
    }
  }

  await assert.rejects(
    encodeTaggedGraph({ nested: new UnsupportedClass() }),
    (error) => error instanceof TaggedSerializationError &&
      error.path === '$["nested"]' &&
      error.type === "[object Object]"
  );
  await assert.rejects(
    encodeTaggedGraph({ nested: () => 1 }),
    (error) => error instanceof TaggedSerializationError && error.type === "function"
  );
  const symbolKeyed = {};
  symbolKeyed[Symbol("secret")] = "not silently dropped";
  await assert.rejects(
    encodeTaggedGraph(symbolKeyed),
    (error) => error instanceof TaggedSerializationError && /symbol keys/.test(error.message)
  );
  await assert.rejects(
    encodeTaggedGraph(new Blob(["binary-without-writer"])),
    (error) => error instanceof TaggedSerializationError && /external binary writer/.test(error.message)
  );
});

test("rejects unsafe or inconsistent external binary references", async () => {
  const large = new Uint8Array(8).buffer;
  await assert.rejects(
    encodeTaggedGraph(large, {
      externalBinaryThreshold: 0,
      writeBinary: async () => ({ path: "../escape.bin", byteLength: 8 })
    }),
    (error) => error instanceof TaggedSerializationError && /unsafe relative path/.test(error.message)
  );
  await assert.rejects(
    encodeTaggedGraph(large, {
      externalBinaryThreshold: 0,
      writeBinary: async () => ({ path: "binary/value.bin", byteLength: 7 })
    }),
    (error) => error instanceof TaggedSerializationError && /mismatched byte length/.test(error.message)
  );
});

test("emits deterministic JSON for repeated encodes", async () => {
  const source = { a: new Map([["x", 1]]), b: new Set([2, 3]) };
  const first = deterministicJson(await encodeTaggedGraph(source));
  const second = deterministicJson(await encodeTaggedGraph(source));
  assert.equal(first, second);
});

test("preserves boxed BigInt and AggregateError evidence fields", async () => {
  const error = new AggregateError([new Error("first"), "second"], "aggregate", {
    cause: 7n
  });
  const encoded = await encodeTaggedGraph({ boxed: Object(9n), error });
  const root = propertiesOf(encoded.data.nodes[encoded.data.root.$ref]);
  const boxedNode = encoded.data.nodes[root.boxed.$ref];
  assert.equal(boxedNode.$type, "BoxedBigInt");
  assert.equal(boxedNode.value, "9");
  const errorNode = encoded.data.nodes[root.error.$ref];
  assert.equal(errorNode.$type, "Error");
  assert.deepEqual(errorNode.cause, { $type: "BigInt", value: "7" });
  assert.equal(encoded.data.nodes[errorNode.errors.$ref].$type, "Array");
});

test("manifest derives the fixed extension ID and exposes no privileged surfaces", async () => {
  const manifestUrl = new URL("../manifest.json", import.meta.url);
  const manifest = JSON.parse(await readFile(manifestUrl, "utf8"));
  assert.equal(extensionIdFromKey(manifest.key), "ainoobmdpanhopangobnggdkpljnpmgl");
  for (const forbiddenField of [
    "permissions",
    "optional_permissions",
    "host_permissions",
    "optional_host_permissions",
    "background",
    "content_scripts",
    "action",
    "web_accessible_resources"
  ]) {
    assert.equal(Object.hasOwn(manifest, forbiddenField), false, forbiddenField);
  }
  const csp = manifest.content_security_policy.extension_pages;
  assert.match(csp, /default-src 'none'/);
  assert.match(csp, /connect-src 'none'/);
  assert.match(csp, /script-src 'self'/);

  const exportJs = await readFile(new URL("../export.js", import.meta.url), "utf8");
  assert.doesNotMatch(exportJs, /\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\b/);
  assert.doesNotMatch(
    exportJs,
    /\.transaction\s*\([^;\n]*,\s*["'](?:readwrite|versionchange)["']/
  );
  assert.doesNotMatch(exportJs, /\.getAll\s*\(/);
  assert.match(exportJs, /browser_export_complete: false/);
  assert.match(exportJs, /extraction_complete: false/);
  assert.match(exportJs, /pending_host_hash_and_replay_validation/);
  assert.doesNotMatch(exportJs, /manifest\.extraction_complete\s*=\s*true/);
});
