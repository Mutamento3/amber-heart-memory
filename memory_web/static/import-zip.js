const UINT32_MAX = 0xffffffff;
const LOCAL_HEADER = 0x04034b50;
const CENTRAL_HEADER = 0x02014b50;
const ZIP_END = 0x06054b50;
const ZIP64_LOCATOR = 0x07064b50;
const ENCRYPTED = 0x2041;

const wanted = {
  chatgpt: (_, basename) => /^conversations(?:-\d+)?\.json$/.test(basename),
  claude: (_, basename) => basename === "conversations.json",
  gemini: (_, basename) => basename.toLowerCase() === "myactivity.json",
  deepseek: name => name === "manifest.json" || name === "chat_session_index.json"
    || /^chat_sessions\/[^/]+\.json$/.test(name),
  kelivo: name => name === "database/kelivo.db" || name === "settings.json",
  operit: (name, basename) => /\.json$/i.test(basename) && !basename.startsWith("._")
    && !name.split("/").includes("__MACOSX"),
};
const u16 = (view, offset) => view.getUint16(offset, true);
const u32 = (view, offset) => view.getUint32(offset, true);

async function read(file, start, length) {
  if (start < 0 || start + length > file.size) throw new Error("ZIP range");
  return new DataView(await file.slice(start, start + length).arrayBuffer());
}

function checkExtra(view, offset, length) {
  const end = offset + length;
  while (offset < end) {
    if (offset + 4 > end || u16(view, offset) === 1) throw new Error("ZIP extra");
    offset += 4 + u16(view, offset + 2);
  }
  if (offset !== end) throw new Error("ZIP extra length");
}

async function directory(file) {
  // End record, maximum comment, and room to detect a ZIP64 locator.
  const length = Math.min(file.size, 22 + 65535 + 20);
  const tail = await read(file, file.size - length, length);
  for (let end = length - 22; end >= 0; end--) {
    if (u32(tail, end) !== ZIP_END || end + 22 + u16(tail, end + 20) !== length) continue;
    const count = u16(tail, end + 10);
    const size = u32(tail, end + 12);
    const start = u32(tail, end + 16);
    // Limit the metadata read as well as the entry payload reads.
    if (u16(tail, end + 4) || u16(tail, end + 6) || count !== u16(tail, end + 8) ||
        count === 65535 || start === UINT32_MAX || (end >= 20 && u32(tail, end - 20) === ZIP64_LOCATOR) ||
        start + size !== file.size - length + end || size > 8 * 1024 * 1024) break;
    const view = await read(file, start, size);
    const decoder = new TextDecoder();
    const entries = [];
    let offset = 0;
    for (let index = 0; index < count; index++) {
      if (offset + 46 > size || u32(view, offset) !== CENTRAL_HEADER) throw new Error("ZIP directory");
      const flags = u16(view, offset + 8);
      const compressed = u32(view, offset + 20);
      const uncompressed = u32(view, offset + 24);
      const nameLength = u16(view, offset + 28);
      const extraLength = u16(view, offset + 30);
      const next = offset + 46 + nameLength + extraLength + u16(view, offset + 32);
      const local = u32(view, offset + 42);
      if (next > size || flags & ENCRYPTED || u16(view, offset + 34) ||
          compressed === UINT32_MAX || uncompressed === UINT32_MAX || local >= start) {
        throw new Error("Unsupported ZIP record");
      }
      checkExtra(view, offset + 46 + nameLength, extraLength);
      const header = new Uint8Array(view.buffer, offset, 46 + nameLength);
      const name = decoder.decode(header.subarray(46));
      entries.push({header, name, local, flags, compressed, method: u16(view, offset + 10)});
      offset = next;
    }
    if (offset !== size) break;
    // A selected slice must end before any other entry, including excluded files.
    const byOffset = [...entries].sort((left, right) => left.local - right.local);
    byOffset.forEach((entry, index) => { entry.limit = byOffset[index + 1]?.local ?? start; });
    return entries;
  }
  throw new Error("Unsupported ZIP directory");
}

async function payload(file, entry) {
  const {header, local, flags, compressed, method, limit} = entry;
  if (![0, 8].includes(method) || local + 30 > limit) throw new Error("Unsupported ZIP entry");
  const fixed = await read(file, local, 30);
  const nameLength = u16(fixed, 26);
  const extraLength = u16(fixed, 28);
  const start = local + 30 + nameLength + extraLength;
  if (u32(fixed, 0) !== LOCAL_HEADER || u16(fixed, 6) !== flags || u16(fixed, 8) !== method ||
      nameLength !== header.length - 46 || start + compressed > limit ||
      u32(fixed, 18) === UINT32_MAX || u32(fixed, 22) === UINT32_MAX) throw new Error("ZIP entry mismatch");
  const variable = await read(file, local + 30, nameLength + extraLength);
  checkExtra(variable, nameLength, extraLength);
  const name = new Uint8Array(variable.buffer, 0, nameLength);
  if (name.some((byte, index) => byte !== header[46 + index])) throw new Error("ZIP filename mismatch");
  return file.slice(start, start + compressed);
}

function takeoutProduct(prefix) {
  const start = /^\s*\[\s*([\]{])/.exec(prefix);
  if (!start) return;
  if (start[1] === "]") return "";
  // Scan complete JSON tokens up to the first record's top-level header.
  // A header-shaped string in the message or another object cannot match.
  const token = /\s*("(?:[^"\\]|\\.)*"|[\[\]{}:,]|true|false|null|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/gy;
  token.lastIndex = start[0].length;
  let depth = 1, atKey = true;
  for (let match; (match = token.exec(prefix));) {
    const value = match[1];
    if (depth === 1 && atKey && value.startsWith('"')) {
      if (JSON.parse(value) === "header") {
        if (token.exec(prefix)?.[1] !== ":") return;
        const product = token.exec(prefix)?.[1];
        if (product?.startsWith('"')) return JSON.parse(product);
        return;
      }
      atKey = false;
    }
    if (depth === 1 && value === ",") atKey = true;
    if (value === "{" || value === "[") depth++;
    if ((value === "}" || value === "]") && --depth === 0) return;
  }
}

async function geminiActivity(data, method) {
  // In combined exports, read a bounded prefix of same-named candidates.
  let stream = data.slice(0, 4096).stream();
  if (method === 8) stream = stream.pipeThrough(new DecompressionStream("deflate-raw"));
  const reader = stream.getReader();
  try {
    const decoder = new TextDecoder();
    let prefix = "";
    while (prefix.length < 4096) {
      const {value, done} = await reader.read();
      if (done) break;
      prefix += decoder.decode(value.subarray(0, 4096 - prefix.length), {stream: true});
      const product = takeoutProduct(prefix);
      if (product !== undefined) return /gemini/i.test(product);
    }
    throw new Error("Unrecognized Takeout activity");
  } finally {
    await reader.cancel().catch(() => {});
  }
}

function pack(entries) {
  const local = [], central = [];
  let offset = 0, size = 0;
  for (const {header, data, flags} of entries) {
    const record = header.slice();
    const view = new DataView(record.buffer);
    // CRC and sizes already exist in the central header. Put them in the
    // new local header too, so the original data descriptor can be omitted.
    view.setUint16(8, flags & ~8, true);
    view.setUint16(30, 0, true);
    view.setUint16(32, 0, true);
    view.setUint32(42, offset, true);
    const prefix = new Uint8Array(30 + record.length - 46);
    new DataView(prefix.buffer).setUint32(0, LOCAL_HEADER, true);
    prefix.set(record.subarray(6, 30), 4);
    prefix.set(record.subarray(46), 30);
    local.push(prefix, data);
    central.push(record);
    offset += prefix.byteLength + data.size;
    size += record.byteLength;
  }
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, ZIP_END, true);
  end.setUint16(8, entries.length, true);
  end.setUint16(10, entries.length, true);
  end.setUint32(12, size, true);
  end.setUint32(16, offset, true);
  return new Blob([...local, ...central, end.buffer], {type: "application/zip"});
}

const crcTable = Uint32Array.from({length: 256}, (_, byte) => {
  for (let bit = 0; bit < 8; bit++) byte = (byte >>> 1) ^ (byte & 1 ? 0xedb88320 : 0);
  return byte;
});

async function crc32(file) {
  let crc = UINT32_MAX;
  const reader = file.stream().getReader();
  try {
    for (;;) {
      const {value, done} = await reader.read();
      if (done) return (crc ^ UINT32_MAX) >>> 0;
      for (const byte of value) crc = (crc >>> 8) ^ crcTable[(crc ^ byte) & 0xff];
    }
  } finally {
    reader.releaseLock();
  }
}

export async function packOperitFiles(files) {
  const entries = [];
  const encoder = new TextEncoder();
  for (const [index, file] of files.entries()) {
    // Internal ASCII names avoid encoding issues and duplicate device filenames.
    const name = encoder.encode("operit-" + (index + 1) + ".json");
    const header = new Uint8Array(46 + name.length);
    const view = new DataView(header.buffer);
    view.setUint32(0, CENTRAL_HEADER, true);
    view.setUint16(4, 20, true);
    view.setUint16(6, 20, true);
    view.setUint16(14, 33, true);
    view.setUint32(16, await crc32(file), true);
    view.setUint32(20, file.size, true);
    view.setUint32(24, file.size, true);
    view.setUint16(28, name.length, true);
    header.set(name, 46);
    entries.push({header, data: file, flags: 0});
  }
  return new File([pack(entries)], "operit.zip", {type: "application/zip"});
}

export async function prepareImportUpload(source, file) {
  if (!/\.zip$/i.test(file.name)) return file;
  try {
    if (!Object.hasOwn(wanted, source)) return file;
    const entries = await directory(file);
    // Leave the existing server diagnostic for the retired Kelivo format.
    if (source === "kelivo" && entries.some(entry => entry.name === "chats.json") &&
        !entries.some(entry => entry.name === "database/kelivo.db")) return file;
    const candidates = entries.filter(({name}) => wanted[source](name, name.split("/").at(-1)));
    const selected = [];
    for (const entry of candidates) {
      const {name} = entry;
      if (name.startsWith("/") || name.includes("\\") || name.includes("\0") ||
          name.split("/").includes("..")) return file;
      const data = await payload(file, entry);
      if (source !== "gemini" || candidates.length === 1 || await geminiActivity(data, entry.method)) {
        selected.push({...entry, data});
      }
    }
    return pack(selected);
  } catch {
    return file;
  }
}
