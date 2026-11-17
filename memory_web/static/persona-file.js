const wordNamespaces = new Set([
  "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
  "http://purl.oclc.org/ooxml/wordprocessingml/main",
]);
const documentPart = "word/document.xml";
// The browser parses this one XML part in memory. Embedded media stays in the ZIP.
const maxDocumentBytes = 32 * 1024 * 1024;

function invalidDocx() {
  return new Error("无法读取 .docx 正文，请确认文件可在 Word 中正常打开。");
}

async function docxEntry(file) {
  // APPNOTE 4.3.16: search only the fixed end record and the maximum ZIP comment.
  const tailStart = Math.max(0, file.size - 22 - 0xffff);
  const tail = new DataView(await file.slice(tailStart).arrayBuffer());
  let end = tail.byteLength - 22;
  while (end >= 0 && (
    tail.getUint32(end, true) !== 0x06054b50 ||
    end + 22 + tail.getUint16(end + 20, true) !== tail.byteLength
  )) end--;
  if (end < 0 || tail.getUint32(end + 4, true) !== 0) throw invalidDocx();

  // The central directory has the sizes even when the local header uses a data descriptor.
  const directorySize = tail.getUint32(end + 12, true);
  const directoryStart = tail.getUint32(end + 16, true);
  if (directoryStart + directorySize > tailStart + end) throw invalidDocx();
  const bytes = await file.slice(directoryStart, directoryStart + directorySize).arrayBuffer();
  const directory = new DataView(bytes);
  const decoder = new TextDecoder();
  for (let entry = 0; entry + 46 <= directorySize;) {
    if (directory.getUint32(entry, true) !== 0x02014b50) throw invalidDocx();
    const nameLength = directory.getUint16(entry + 28, true);
    const nameStart = entry + 46;
    const next = nameStart + nameLength + directory.getUint16(entry + 30, true) +
      directory.getUint16(entry + 32, true);
    if (next > directorySize) throw invalidDocx();
    if (decoder.decode(new Uint8Array(bytes, nameStart, nameLength)) === documentPart) {
      const method = directory.getUint16(entry + 10, true);
      const encrypted = directory.getUint16(entry + 8, true) & 1;
      const compressedSize = directory.getUint32(entry + 20, true);
      const size = directory.getUint32(entry + 24, true);
      const localStart = directory.getUint32(entry + 42, true);
      if (encrypted || ![0, 8].includes(method) || localStart + 30 > directoryStart) throw invalidDocx();
      if (size > maxDocumentBytes) throw new Error("Word 正文结构过大，请拆分文档后再导入。");
      const header = new DataView(await file.slice(localStart, localStart + 30).arrayBuffer());
      if (header.getUint32(0, true) !== 0x04034b50) throw invalidDocx();
      const dataStart = localStart + 30 + header.getUint16(26, true) + header.getUint16(28, true);
      if (dataStart + compressedSize > directoryStart) throw invalidDocx();
      return {data: file.slice(dataStart, dataStart + compressedSize), method, size};
    }
    entry = next;
  }
  throw invalidDocx();
}

async function docxText(file) {
  const {data, method, size} = await docxEntry(file);
  let stream = data.stream();
  if (method === 8) {
    try {
      stream = stream.pipeThrough(new DecompressionStream("deflate-raw"));
    } catch {
      throw new Error("当前浏览器不支持解压 .docx，请另存为 UTF-8 的 .txt 后导入。");
    }
  }
  const bytes = new Uint8Array(size);
  const reader = stream.getReader();
  let offset = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      if (offset + value.length > size) {
        await reader.cancel();
        throw invalidDocx();
      }
      bytes.set(value, offset);
      offset += value.length;
    }
  } catch {
    throw invalidDocx();
  } finally {
    reader.releaseLock();
  }
  if (offset !== size) throw invalidDocx();

  let encoding = "utf-8";
  if (bytes[0] === 0xff && bytes[1] === 0xfe) encoding = "utf-16le";
  if (bytes[0] === 0xfe && bytes[1] === 0xff) encoding = "utf-16be";
  let xml;
  try {
    const text = new TextDecoder(encoding, {fatal: true}).decode(bytes);
    xml = new DOMParser().parseFromString(text, "application/xml");
  } catch {
    throw invalidDocx();
  }
  const root = xml.documentElement;
  const ns = root?.namespaceURI;
  if (xml.doctype || xml.querySelector("parsererror") || root?.localName !== "document" || !wordNamespaces.has(ns)) {
    throw invalidDocx();
  }
  const body = xml.getElementsByTagNameNS(ns, "body")[0];
  if (!body) throw invalidDocx();
  const separators = new Map([["tab", "\t"], ["br", "\n"], ["cr", "\n"]]);
  return Array.from(body.getElementsByTagNameNS(ns, "p"), (paragraph) => {
    const runs = xml.createTreeWalker(paragraph, NodeFilter.SHOW_ELEMENT, (element) => {
      if (element.namespaceURI !== ns) return NodeFilter.FILTER_SKIP;
      // A nested text box may have its own paragraphs. Handle each only once.
      if (element.localName === "p") return NodeFilter.FILTER_REJECT;
      return element.localName === "t" || separators.has(element.localName)
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_SKIP;
    });
    let text = "";
    for (let run = runs.nextNode(); run; run = runs.nextNode()) {
      text += separators.get(run.localName) ?? run.textContent;
    }
    return text;
  }).join("\n");
}

export async function readPersonaFile(file, limit) {
  const extension = file.name.match(/\.[^.]+$/)?.[0].toLowerCase();
  const tooLong = () => new Error("导入内容超过 10 万字，人设未替换。");
  let text;
  if (extension === ".docx") {
    text = await docxText(file);
  } else if (extension === ".txt" || extension === ".md") {
    if (file.size > limit * 4 + 3) throw tooLong();
    try {
      text = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
    } catch (error) {
      if (!(error instanceof TypeError)) throw error;
      throw new Error("请将文件另存为 UTF-8 编码后再导入。");
    }
  } else {
    throw new Error("请在 Word 里另存为 .docx 再导入");
  }
  text = text.replace(/\r\n?/g, "\n");
  if (text.length > limit) throw tooLong();
  return text;
}
