const fs = require('fs'), vm = require('vm'), path = require('path');
const file = process.argv[2];
const out = process.argv[3] || file.replace(/\.js$/, '.deob.js');
let src = fs.readFileSync(file, 'utf8');
function makeStub() {
  const f = function () { return p; };
  const p = new Proxy(f, {
    get(t, k) {
      if (k === Symbol.toPrimitive) return () => '';
      if (k === 'then' || k === Symbol.iterator) return undefined;
      if (k === 'length') return 0;
      return p;
    },
    apply() { return p; },
    construct() { return p; },
    has() { return true; },
  });
  return p;
}
const sandbox = {
  require: () => makeStub(),
  module: { exports: {} }, exports: {},
  console: { log() {}, error() {}, warn() {} },
  __dirname: '/tmp', __filename: file, process: { env: {}, argv: [], platform: 'darwin' },
  setTimeout, setInterval, clearTimeout, Buffer, URL, TextEncoder, TextDecoder,
};
const ctx = vm.createContext(sandbox);
try { vm.runInContext(src, ctx); } catch (e) { /* partial ok */ }
const alias = {};
const aliasRe = /(_0x[0-9a-f]+)\s*=\s*(a0_0x[0-9a-f]+)\s*[,;]/g;
let m;
while ((m = aliasRe.exec(src))) alias[m[1]] = m[2];
let ch = true;
while (ch) { ch = false; for (const k in alias) if (alias[alias[k]]) { alias[k] = alias[alias[k]]; ch = true; } }
let replaced = 0, missed = 0;
const res = src.replace(/\b(a0_0x[0-9a-f]+|_0x[0-9a-f]+)\((0x[0-9a-f]+)\)/g, (mm, fn, num) => {
  const target = alias[fn] || fn;
  try {
    const v = vm.runInContext(`typeof ${target}==='function' ? ${target}(${num}) : undefined`, ctx);
    if (typeof v === 'string') { replaced++; return JSON.stringify(v); }
  } catch (e) {}
  missed++;
  return mm;
});
fs.writeFileSync(out, res);
console.log(`${path.basename(file)}: strings=${replaced} unresolved=${missed}`);
