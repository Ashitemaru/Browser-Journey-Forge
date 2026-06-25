#!/usr/bin/env node
// Dependency-free zip: archive a directory tree, preserving unix permissions
// (so the .app launcher keeps its executable bit). No `zip` binary needed —
// the CI image only guarantees Node.
//
//   node packaging/zip.mjs <srcDir> <out.zip>
// Archives the CONTENTS of <srcDir> (top-level entries become archive roots).

import { createWriteStream, readdirSync, readFileSync, statSync } from 'node:fs';
import { deflateRawSync } from 'node:zlib';
import { join, relative } from 'node:path';

const [srcDir, outZip] = process.argv.slice(2);
if (!srcDir || !outZip) {
  console.error('usage: node zip.mjs <srcDir> <out.zip>');
  process.exit(2);
}

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    const st = statSync(full);
    if (st.isDirectory()) out.push(...walk(full));
    else if (st.isFile()) out.push(full);
  }
  return out;
}

function crc32(buf) {
  let c = ~0;
  for (let i = 0; i < buf.length; i++) {
    c ^= buf[i];
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return (~c) >>> 0;
}

const files = walk(srcDir).sort();
const central = [];
const chunks = [];
let offset = 0;

for (const full of files) {
  const arc = relative(srcDir, full).split('\\').join('/');
  const data = readFileSync(full);
  const mode = statSync(full).mode & 0o7777;        // preserve perms (exec bit!)
  const nameBuf = Buffer.from(arc, 'utf8');
  const crc = crc32(data);
  const comp = deflateRawSync(data);

  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0);
  local.writeUInt16LE(20, 4);
  local.writeUInt16LE(0, 6);
  local.writeUInt16LE(8, 8);          // deflate
  local.writeUInt16LE(0, 10);
  local.writeUInt16LE(0x21, 12);
  local.writeUInt32LE(crc, 14);
  local.writeUInt32LE(comp.length, 18);
  local.writeUInt32LE(data.length, 22);
  local.writeUInt16LE(nameBuf.length, 26);
  local.writeUInt16LE(0, 28);
  chunks.push(local, nameBuf, comp);

  const cd = Buffer.alloc(46);
  cd.writeUInt32LE(0x02014b50, 0);
  cd.writeUInt16LE(0x031e, 4);        // version made by: 3 (unix) << 8 | 30
  cd.writeUInt16LE(20, 6);
  cd.writeUInt16LE(0, 8);
  cd.writeUInt16LE(8, 10);
  cd.writeUInt16LE(0, 12);
  cd.writeUInt16LE(0x21, 14);
  cd.writeUInt32LE(crc, 16);
  cd.writeUInt32LE(comp.length, 20);
  cd.writeUInt32LE(data.length, 24);
  cd.writeUInt16LE(nameBuf.length, 28);
  cd.writeUInt16LE(0, 30);
  cd.writeUInt16LE(0, 32);
  cd.writeUInt16LE(0, 34);
  cd.writeUInt16LE(0, 36);
  cd.writeUInt32LE(0, 38);
  cd.writeUInt32LE((mode << 16) >>> 0, 38);  // external attrs = unix mode << 16
  cd.writeUInt32LE(offset, 42);
  central.push(cd, nameBuf);

  offset += local.length + nameBuf.length + comp.length;
}

const localBuf = Buffer.concat(chunks);
const cdBuf = Buffer.concat(central);
const end = Buffer.alloc(22);
end.writeUInt32LE(0x06054b50, 0);
end.writeUInt16LE(0, 4);
end.writeUInt16LE(0, 6);
end.writeUInt16LE(files.length, 8);
end.writeUInt16LE(files.length, 10);
end.writeUInt32LE(cdBuf.length, 12);
end.writeUInt32LE(localBuf.length, 16);
end.writeUInt16LE(0, 20);

const ws = createWriteStream(outZip);
ws.write(localBuf); ws.write(cdBuf); ws.write(end); ws.end();
ws.on('close', () => console.log(`zip: ${outZip} (${files.length} files)`));
