const textEncoder = new TextEncoder();

function notFound(name) {
  return new DOMException(`Entry not found: ${name}`, "NotFoundError");
}

function typeMismatch(name) {
  return new DOMException(`Entry has the wrong kind: ${name}`, "TypeMismatchError");
}

function validateName(name) {
  if (
    typeof name !== "string" ||
    name.length === 0 ||
    name === "." ||
    name === ".." ||
    name.includes("/") ||
    name.includes("\\")
  ) {
    throw new TypeError(`Invalid in-memory FSA entry name: ${String(name)}`);
  }
}

async function chunkBytes(chunk) {
  if (typeof chunk === "string") return textEncoder.encode(chunk);
  if (chunk instanceof ArrayBuffer) return new Uint8Array(chunk.slice(0));
  if (ArrayBuffer.isView(chunk)) {
    return new Uint8Array(chunk.buffer.slice(chunk.byteOffset, chunk.byteOffset + chunk.byteLength));
  }
  if (typeof Blob !== "undefined" && chunk instanceof Blob) {
    return new Uint8Array(await chunk.arrayBuffer());
  }
  throw new TypeError(`Unsupported fake FSA write chunk: ${Object.prototype.toString.call(chunk)}`);
}

function concatenate(chunks) {
  const size = chunks.reduce((total, chunk) => total + chunk.byteLength, 0);
  const output = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return output;
}

class MemoryWritableFileStream {
  constructor(file, keepExistingData) {
    this.file = file;
    this.chunks = keepExistingData ? [file.bytes.slice()] : [];
    this.closed = false;
  }

  async write(chunk) {
    if (this.closed) throw new DOMException("Writable is closed", "InvalidStateError");
    this.chunks.push(await chunkBytes(chunk));
  }

  async close() {
    if (this.closed) throw new DOMException("Writable is closed", "InvalidStateError");
    this.file.bytes = concatenate(this.chunks);
    this.file.commitCount += 1;
    this.closed = true;
  }

  async abort() {
    if (this.closed) return;
    this.file.abortCount += 1;
    this.closed = true;
  }
}

export class MemoryFileHandle {
  constructor(name) {
    this.kind = "file";
    this.name = name;
    this.bytes = new Uint8Array();
    this.commitCount = 0;
    this.abortCount = 0;
  }

  async createWritable({ keepExistingData = false } = {}) {
    return new MemoryWritableFileStream(this, keepExistingData);
  }

  async getFile() {
    return new File([this.bytes], this.name, { type: "application/octet-stream" });
  }

  text() {
    return new TextDecoder().decode(this.bytes);
  }
}

export class MemoryDirectoryHandle {
  constructor(name = "memory-root") {
    this.kind = "directory";
    this.name = name;
    this.directories = new Map();
    this.files = new Map();
  }

  async getDirectoryHandle(name, { create = false } = {}) {
    validateName(name);
    if (this.files.has(name)) throw typeMismatch(name);
    if (this.directories.has(name)) return this.directories.get(name);
    if (!create) throw notFound(name);
    const directory = new MemoryDirectoryHandle(name);
    this.directories.set(name, directory);
    return directory;
  }

  async getFileHandle(name, { create = false } = {}) {
    validateName(name);
    if (this.directories.has(name)) throw typeMismatch(name);
    if (this.files.has(name)) return this.files.get(name);
    if (!create) throw notFound(name);
    const file = new MemoryFileHandle(name);
    this.files.set(name, file);
    return file;
  }

  directory(name) {
    const directory = this.directories.get(name);
    if (!directory) throw notFound(name);
    return directory;
  }

  file(name) {
    const file = this.files.get(name);
    if (!file) throw notFound(name);
    return file;
  }

  onlyDirectory() {
    if (this.directories.size !== 1) {
      throw new Error(`Expected exactly one child directory, observed ${this.directories.size}`);
    }
    return this.directories.values().next().value;
  }

  resolveFile(relativePath) {
    const parts = relativePath.split("/");
    const fileName = parts.pop();
    let directory = this;
    for (const part of parts) directory = directory.directory(part);
    return directory.file(fileName);
  }
}
