export const TAGGED_GRAPH_CODEC = "tagged-structured-clone-v1";
export const DEFAULT_EXTERNAL_BINARY_THRESHOLD = 64 * 1024;

const objectToString = Object.prototype.toString;
const typedArrayNames = new Map([
  ["[object Int8Array]", "Int8Array"],
  ["[object Uint8Array]", "Uint8Array"],
  ["[object Uint8ClampedArray]", "Uint8ClampedArray"],
  ["[object Int16Array]", "Int16Array"],
  ["[object Uint16Array]", "Uint16Array"],
  ["[object Int32Array]", "Int32Array"],
  ["[object Uint32Array]", "Uint32Array"],
  ["[object Float32Array]", "Float32Array"],
  ["[object Float64Array]", "Float64Array"],
  ["[object Float16Array]", "Float16Array"],
  ["[object BigInt64Array]", "BigInt64Array"],
  ["[object BigUint64Array]", "BigUint64Array"]
]);

export class TaggedSerializationError extends Error {
  constructor(message, { path = "$", type = null, cause = null } = {}) {
    super(message, cause ? { cause } : undefined);
    this.name = "TaggedSerializationError";
    this.path = path;
    this.type = type;
  }
}

function typeLabel(value) {
  if (value === null) return "null";
  if (typeof value !== "object") return typeof value;
  return objectToString.call(value);
}

function pathForProperty(path, key) {
  return `${path}[${JSON.stringify(key)}]`;
}

function pathForIndex(path, index) {
  return `${path}[${index}]`;
}

function numberTag(value) {
  if (Number.isNaN(value)) return { $type: "Number", value: "NaN" };
  if (value === Infinity) return { $type: "Number", value: "Infinity" };
  if (value === -Infinity) return { $type: "Number", value: "-Infinity" };
  if (Object.is(value, -0)) return { $type: "Number", value: "-0" };
  return value;
}

function bytesToBase64(bytes) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  let output = "";
  for (let offset = 0; offset < bytes.length; offset += 3) {
    const first = bytes[offset];
    const hasSecond = offset + 1 < bytes.length;
    const hasThird = offset + 2 < bytes.length;
    const second = hasSecond ? bytes[offset + 1] : 0;
    const third = hasThird ? bytes[offset + 2] : 0;
    const chunk = (first << 16) | (second << 8) | third;
    output += alphabet[(chunk >>> 18) & 63];
    output += alphabet[(chunk >>> 12) & 63];
    output += hasSecond ? alphabet[(chunk >>> 6) & 63] : "=";
    output += hasThird ? alphabet[chunk & 63] : "=";
  }
  return output;
}

function canonicalArrayIndex(key, length) {
  if (!/^(0|[1-9][0-9]*)$/.test(key)) return null;
  const index = Number(key);
  if (!Number.isSafeInteger(index) || index >= length || String(index) !== key) return null;
  return index;
}

function enumerableSymbolKeys(value) {
  return Object.getOwnPropertySymbols(value).filter((symbol) => {
    const descriptor = Object.getOwnPropertyDescriptor(value, symbol);
    return descriptor?.enumerable;
  });
}

function assertNoEnumerableSymbols(value, path) {
  const symbols = enumerableSymbolKeys(value);
  if (symbols.length > 0) {
    throw new TaggedSerializationError(
      `Enumerable symbol keys are not supported (${String(symbols[0])})`,
      { path, type: typeLabel(value) }
    );
  }
}

function normalizeExternalReference(reference, byteLength, path) {
  if (!reference || typeof reference !== "object" || Array.isArray(reference)) {
    throw new TaggedSerializationError("Binary writer returned an invalid reference", {
      path,
      type: typeLabel(reference)
    });
  }
  if (typeof reference.path !== "string" || reference.path.length === 0) {
    throw new TaggedSerializationError("Binary writer reference is missing a path", {
      path,
      type: typeLabel(reference)
    });
  }
  if (
    reference.path.startsWith("/") ||
    reference.path.includes("\\") ||
    reference.path.split("/").some((part) => part === "" || part === "." || part === "..")
  ) {
    throw new TaggedSerializationError("Binary writer returned an unsafe relative path", {
      path,
      type: typeLabel(reference)
    });
  }
  if (reference.byteLength !== undefined && reference.byteLength !== byteLength) {
    throw new TaggedSerializationError("Binary writer reported a mismatched byte length", {
      path,
      type: typeLabel(reference)
    });
  }
  return {
    path: reference.path,
    byteLength
  };
}

function validateOptions(options) {
  const threshold = options.externalBinaryThreshold ?? DEFAULT_EXTERNAL_BINARY_THRESHOLD;
  if (!Number.isSafeInteger(threshold) || threshold < 0) {
    throw new TaggedSerializationError("externalBinaryThreshold must be a non-negative safe integer");
  }
  if (options.writeBinary !== undefined && typeof options.writeBinary !== "function") {
    throw new TaggedSerializationError("writeBinary must be a function when provided");
  }
  return {
    threshold,
    writeBinary: options.writeBinary ?? null,
    context: options.context ?? null
  };
}

/**
 * Encode a browser-decoded structured-clone graph without JSON coercion.
 *
 * The result is JSON-safe and deterministic for a fixed input graph and binary
 * writer. Object identity is represented by numeric node references. Large
 * binary values and every Blob/File are delegated to writeBinary so callers can
 * stream them to independent files.
 */
export async function encodeTaggedGraph(rootValue, options = {}) {
  const { threshold, writeBinary, context } = validateOptions(options);
  const nodes = [];
  const seen = new Map();
  const binaryParts = [];

  async function encodeProperties(value, path, excludedKeys = new Set(), checkSymbols = true) {
    if (checkSymbols) assertNoEnumerableSymbols(value, path);
    const properties = [];
    for (const key of Object.keys(value)) {
      if (excludedKeys.has(key)) continue;
      properties.push([
        key,
        await visit(value[key], pathForProperty(path, key))
      ]);
    }
    return properties;
  }

  async function externalize(value, nodeId, kind, byteLength, path, metadata = {}) {
    if (!writeBinary) {
      throw new TaggedSerializationError(
        `${kind} requires an external binary writer`,
        { path, type: typeLabel(value) }
      );
    }
    let rawReference;
    try {
      rawReference = await writeBinary({
        nodeId,
        kind,
        value,
        byteLength,
        context,
        ...metadata
      });
    } catch (error) {
      if (error instanceof TaggedSerializationError) throw error;
      throw new TaggedSerializationError(`Failed to write external ${kind}`, {
        path,
        type: typeLabel(value),
        cause: error
      });
    }
    const external = normalizeExternalReference(rawReference, byteLength, path);
    binaryParts.push({
      nodeId,
      kind,
      byteLength,
      path: external.path
    });
    return { external };
  }

  async function encodeArrayBuffer(value, nodeId, path) {
    let byteLength;
    let bytes;
    try {
      byteLength = value.byteLength;
      bytes = new Uint8Array(value);
    } catch (error) {
      throw new TaggedSerializationError("Detached ArrayBuffer cannot be archived", {
        path,
        type: typeLabel(value),
        cause: error
      });
    }
    const data = byteLength > threshold
      ? await externalize(value, nodeId, "ArrayBuffer", byteLength, path)
      : { inline: { encoding: "base64", value: bytesToBase64(bytes) } };
    return {
      $type: "ArrayBuffer",
      byteLength,
      resizable: Boolean(value.resizable),
      maxByteLength: typeof value.maxByteLength === "number" ? value.maxByteLength : byteLength,
      data,
      properties: await encodeProperties(value, path)
    };
  }

  async function encodeBlob(value, nodeId, path, isFile) {
    const kind = isFile ? "File" : "Blob";
    const data = await externalize(value, nodeId, kind, value.size, path, {
      mediaType: value.type || "",
      fileName: isFile ? value.name : null,
      lastModified: isFile ? value.lastModified : null
    });
    const node = {
      $type: kind,
      size: value.size,
      mediaType: value.type || "",
      data,
      // Browser/Node Blob implementations may expose engine-internal symbol
      // slots. They are not structured-clone properties; the byte stream and
      // explicit public metadata above are the evidence-bearing state.
      properties: await encodeProperties(value, path, new Set(), false)
    };
    if (isFile) {
      node.name = value.name;
      node.lastModified = value.lastModified;
      node.webkitRelativePath = value.webkitRelativePath || "";
    }
    return node;
  }

  async function visit(value, path) {
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number") return numberTag(value);
    if (typeof value === "undefined") return { $type: "Undefined" };
    if (typeof value === "bigint") return { $type: "BigInt", value: value.toString(10) };
    if (typeof value === "symbol" || typeof value === "function") {
      throw new TaggedSerializationError(`Unsupported value type: ${typeof value}`, {
        path,
        type: typeof value
      });
    }
    if (typeof value !== "object") {
      throw new TaggedSerializationError("Unsupported value", { path, type: typeof value });
    }

    const existingNode = seen.get(value);
    if (existingNode !== undefined) return { $ref: existingNode };

    const nodeId = nodes.length;
    seen.set(value, nodeId);
    nodes.push(null);
    const reference = { $ref: nodeId };
    const tag = typeLabel(value);

    let node;
    if (Array.isArray(value)) {
      assertNoEnumerableSymbols(value, path);
      const entries = [];
      const excluded = new Set();
      for (const key of Object.keys(value)) {
        const index = canonicalArrayIndex(key, value.length);
        if (index === null) continue;
        excluded.add(key);
        entries.push([index, await visit(value[index], pathForIndex(path, index))]);
      }
      node = {
        $type: "Array",
        length: value.length,
        entries,
        properties: await encodeProperties(value, path, excluded)
      };
    } else if (tag === "[object Date]") {
      node = {
        $type: "Date",
        milliseconds: numberTag(value.getTime()),
        properties: await encodeProperties(value, path)
      };
    } else if (tag === "[object RegExp]") {
      node = {
        $type: "RegExp",
        source: value.source,
        flags: value.flags,
        lastIndex: await visit(value.lastIndex, `${path}.<lastIndex>`),
        properties: await encodeProperties(value, path)
      };
    } else if (tag === "[object Map]") {
      const entries = [];
      let index = 0;
      for (const [key, entryValue] of value.entries()) {
        entries.push([
          await visit(key, `${path}.<map-key:${index}>`),
          await visit(entryValue, `${path}.<map-value:${index}>`)
        ]);
        index += 1;
      }
      node = {
        $type: "Map",
        entries,
        properties: await encodeProperties(value, path)
      };
    } else if (tag === "[object Set]") {
      const values = [];
      let index = 0;
      for (const entryValue of value.values()) {
        values.push(await visit(entryValue, `${path}.<set:${index}>`));
        index += 1;
      }
      node = {
        $type: "Set",
        values,
        properties: await encodeProperties(value, path)
      };
    } else if (tag === "[object ArrayBuffer]") {
      node = await encodeArrayBuffer(value, nodeId, path);
    } else if (tag === "[object DataView]") {
      if (value.buffer.resizable) {
        throw new TaggedSerializationError(
          "DataView over a resizable ArrayBuffer has an unobservable fixed/length-tracking mode",
          { path, type: tag }
        );
      }
      node = {
        $type: "DataView",
        buffer: await visit(value.buffer, `${path}.<buffer>`),
        byteOffset: value.byteOffset,
        byteLength: value.byteLength,
        properties: await encodeProperties(value, path)
      };
    } else if (typedArrayNames.has(tag)) {
      if (value.buffer.resizable) {
        throw new TaggedSerializationError(
          "TypedArray over a resizable ArrayBuffer has an unobservable fixed/length-tracking mode",
          { path, type: tag }
        );
      }
      const excluded = new Set();
      for (const key of Object.keys(value)) {
        if (canonicalArrayIndex(key, value.length) !== null) excluded.add(key);
      }
      node = {
        $type: "TypedArray",
        name: typedArrayNames.get(tag),
        buffer: await visit(value.buffer, `${path}.<buffer>`),
        byteOffset: value.byteOffset,
        length: value.length,
        properties: await encodeProperties(value, path, excluded)
      };
    } else if (typeof File !== "undefined" && value instanceof File) {
      node = await encodeBlob(value, nodeId, path, true);
    } else if (typeof Blob !== "undefined" && value instanceof Blob) {
      node = await encodeBlob(value, nodeId, path, false);
    } else if (tag === "[object Boolean]") {
      node = {
        $type: "BoxedBoolean",
        value: value.valueOf(),
        properties: await encodeProperties(value, path)
      };
    } else if (tag === "[object Number]") {
      node = {
        $type: "BoxedNumber",
        value: numberTag(value.valueOf()),
        properties: await encodeProperties(value, path)
      };
    } else if (tag === "[object String]") {
      const excluded = new Set();
      for (let index = 0; index < value.length; index += 1) excluded.add(String(index));
      node = {
        $type: "BoxedString",
        value: value.valueOf(),
        properties: await encodeProperties(value, path, excluded)
      };
    } else if (tag === "[object BigInt]") {
      node = {
        $type: "BoxedBigInt",
        value: value.valueOf().toString(10),
        properties: await encodeProperties(value, path)
      };
    } else if (tag === "[object Error]") {
      const excluded = new Set(["cause", "errors"]);
      node = {
        $type: "Error",
        name: String(value.name),
        message: String(value.message),
        stack: typeof value.stack === "string" ? value.stack : null,
        cause: Object.hasOwn(value, "cause")
          ? await visit(value.cause, `${path}.<cause>`)
          : { $type: "Absent" },
        errors: Object.hasOwn(value, "errors")
          ? await visit(value.errors, `${path}.<errors>`)
          : { $type: "Absent" },
        properties: await encodeProperties(value, path, excluded)
      };
    } else if (tag === "[object DOMException]") {
      node = {
        $type: "DOMException",
        name: String(value.name),
        message: String(value.message),
        code: numberTag(Number(value.code)),
        stack: typeof value.stack === "string" ? value.stack : null,
        properties: await encodeProperties(value, path)
      };
    } else {
      const prototype = Object.getPrototypeOf(value);
      if (prototype !== Object.prototype && prototype !== null) {
        throw new TaggedSerializationError(`Unsupported structured-clone object type: ${tag}`, {
          path,
          type: tag
        });
      }
      node = {
        $type: "Object",
        prototype: prototype === null ? "null" : "Object",
        properties: await encodeProperties(value, path)
      };
    }

    nodes[nodeId] = node;
    return reference;
  }

  const root = await visit(rootValue, "$");
  if (nodes.some((node) => node === null)) {
    throw new TaggedSerializationError("Internal serializer error: unresolved graph node");
  }
  return {
    codec: TAGGED_GRAPH_CODEC,
    data: {
      root,
      nodes
    },
    binaryParts
  };
}

export function deterministicJson(value) {
  return JSON.stringify(value);
}
