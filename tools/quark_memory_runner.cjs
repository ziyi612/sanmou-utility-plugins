'use strict';

// Run the unchanged official CLI against memfs. Only its existing account
// configuration can touch the real filesystem; report bytes stay in pipes/RAM.
const path = require('node:path');
const fs = require('node:fs');
const realPromises = require('node:fs/promises');
const { syncBuiltinESMExports } = require('node:module');
const crypto = require('node:crypto');
const http = require('node:http');
const https = require('node:https');
const { EventEmitter, errorMonitor } = require('node:events');
const { installMemoryHttpsBridge } = require('./quark_memory_https.cjs');
const { Volume, createFsFromVolume } = require(path.join(__dirname, '../.runtime/quark-memory/node_modules/memfs'));
const { patchFs } = require(path.join(__dirname, '../.runtime/quark-memory/node_modules/fs-monkey'));

const original = {};
for (const key of Reflect.ownKeys(fs)) {
  if (typeof fs[key] === 'function') original[key] = fs[key].bind(fs);
}
const originalPromises = {};
for (const key of Reflect.ownKeys(realPromises)) {
  if (typeof realPromises[key] === 'function') originalPromises[key] = realPromises[key].bind(realPromises);
}
const output = process.stdout.write.bind(process.stdout);
const exit = process.exit.bind(process);
const input = JSON.parse(original.readFileSync(0, 'utf8'));
const runtime = path.resolve(process.argv[2]);
const sdkRoot = path.dirname(path.dirname(runtime));
const account = path.join(sdkRoot, 'codex', 'config.json');
const accountDirectory = path.dirname(account);
const cwd = process.cwd();
const ramRoot = path.join(cwd, '.ram-quark');
const downloadRoot = path.join(ramRoot, 'download');
const limit = Number(input.max_bytes || 16 * 1024 * 1024);
if (!Number.isSafeInteger(limit) || limit < 1 || limit > 64 * 1024 * 1024) throw new Error('Invalid in-memory object limit');
// Install below the audit wrapper, so allowlisted Python HTTPS operations still
// record only the same bounded, non-sensitive request metadata.
const pythonHttps = installMemoryHttpsBridge();
const norm = value => path.resolve(String(value)).toLowerCase();
const accountKey = norm(account);
const accountDirKey = norm(accountDirectory);
const isAccount = value => {
  if (typeof value === 'number') return false;
  const candidate = norm(value);
  return candidate === accountKey || candidate === accountKey + '.' + process.pid + '.tmp';
};
const physicalDescriptors = new Set();
const descriptorPaths = new Map();
const audit = { report_disk_writes: 0, physical_writes: [], ram_write_calls: 0,
  backend: 'memfs@4.80.1 + fs-monkey@1.1.0', hash_worker: 'official in-process fallback',
  https_adapter: pythonHttps ? 'urllib-allowlist' : 'native', network: [] };

const endpointCategories = new Map([
  ['/open/v1/file/get_download_url', 'download-url'],
  ['/open/v1/file/multi_get_download_url', 'download-url-batch'],
  ['/open/v1/file/upload_pre', 'upload-pre'],
  ['/open/v1/file/update/hash', 'upload-hash'],
  ['/open/v1/file/get_upload_urls', 'upload-urls'],
  ['/open/v1/file/upload_finish', 'upload-finish'],
  ['/open/v1/file/list', 'file-list'],
  ['/open/v1/user/info', 'account-info'],
  ['/open/v1/user/get_vip_info', 'account-info'],
  ['/agent/v1/oauth/access_token/rotate', 'credential-refresh'],
]);
function describeNetwork(moduleProtocol, args) {
  let parsed, options = {};
  const first = args[0];
  if (typeof first === 'string' || first instanceof URL) {
    try { parsed = new URL(first); } catch {}
    if (args[1] && typeof args[1] === 'object') options = args[1];
  } else if (first && typeof first === 'object') options = first;
  let hostname = String(options.hostname || options.host || parsed?.hostname || '');
  // Never allow a malformed host to smuggle URL userinfo, paths or queries.
  if (/[@/?#\\\s]/.test(hostname) || hostname.length > 253) hostname = '[invalid-host]';
  else {
    try { hostname = new URL(moduleProtocol + '//' + hostname).hostname; }
    catch { hostname = '[invalid-host]'; }
  }
  const protocolValue = options.protocol || parsed?.protocol || options.agent?.protocol || moduleProtocol;
  const protocol = ['http:', 'https:'].includes(protocolValue) ? protocolValue : '[other-protocol]';
  const selectedPort = options.port || parsed?.port || options.agent?.defaultPort || (protocol === 'https:' ? 443 : 80);
  const port = /^\d{1,5}$/.test(String(selectedPort)) && Number(selectedPort) <= 65535 ? Number(selectedPort) : null;
  const methodValue = String(options.method || 'GET').toUpperCase();
  const method = ['GET', 'POST', 'PUT', 'HEAD', 'DELETE', 'PATCH', 'OPTIONS', 'CONNECT'].includes(methodValue) ? methodValue : '[other-method]';
  // Path values are used solely for exact classification; none are retained.
  let pathname = String(options.path || parsed?.pathname || '').split('?')[0];
  if (/^https?:\/\//.test(pathname)) {
    try { pathname = new URL(pathname).pathname; } catch { pathname = ''; }
  }
  let endpoint = endpointCategories.get(pathname);
  if (!endpoint) {
    if (hostname.endsWith('.effirst.com') || hostname === 'effirst.com' || hostname === 'px.wpk.quark.cn' || hostname.endsWith('.wpk.quark.cn')) endpoint = 'telemetry';
    else if (hostname === 'open-api-drive.quark.cn') endpoint = 'other-drive-api';
    else if (input.action === 'download') endpoint = 'download-content';
    else if (input.action === 'upload') endpoint = 'upload-content';
    else endpoint = 'other-host';
  }
  return { transport_protocol: moduleProtocol, protocol, hostname, port, method, endpoint };
}
function networkErrorCode(error) {
  const code = error?.code;
  if (typeof code === 'number' && Number.isFinite(code)) return code;
  return typeof code === 'string' && /^[A-Z0-9_]{1,64}$/.test(code) ? code : null;
}
function observedRequest(moduleProtocol, delegate, receiver, args) {
  const record = audit.network.length < 20 ? describeNetwork(moduleProtocol, args) : null;
  if (record) audit.network.push(record);
  try {
    const request = Reflect.apply(delegate, receiver, args);
    // errorMonitor observes without consuming an otherwise unhandled error.
    if (record && typeof request?.on === 'function') request.on(errorMonitor, error => { record.error_code = networkErrorCode(error); });
    if (record && typeof request?.on === 'function') request.on('quark-memory-https-phase', value => {
      record.lifecycle ||= [];
      if (record.lifecycle.length < 16) record.lifecycle.push(value);
    });
    return request;
  } catch (error) {
    if (record) record.error_code = networkErrorCode(error);
    throw error;
  }
}
for (const [transport, protocol] of [[http, 'http:'], [https, 'https:']]) {
  const delegate = transport.request;
  transport.request = function (...args) { return observedRequest(protocol, delegate, this, args); };
}
syncBuiltinESMExports();
const volume = new Volume();
const memory = createFsFromVolume(volume);
memory.mkdirSync(path.dirname(runtime), { recursive: true });
memory.mkdirSync(accountDirectory, { recursive: true });
memory.mkdirSync(downloadRoot, { recursive: true });
memory.mkdirSync(path.join(ramRoot, 'browse'), { recursive: true });
memory.writeFileSync(runtime, original.readFileSync(runtime));
// The optional official hash-worker is intentionally not mounted. The pinned
// CLI supports an in-process hash fallback, which uses this same RAM volume.
if (input.action === 'upload') {
  const name = input.name;
  if (typeof name !== 'string' || !name || path.basename(name) !== name || /[\\/:\0]/.test(name)) throw new Error('Invalid object name');
  const data = Buffer.from(input.body_base64 || '', 'base64');
  if (data.length > limit) throw new Error('Upload exceeds the in-memory object limit');
  memory.mkdirSync(path.join(ramRoot, 'input'), { recursive: true });
  memory.writeFileSync(path.join(ramRoot, 'input', name), data);
}

const writeMethods = new Set(['appendFile', 'appendFileSync', 'writeFile', 'writeFileSync',
  'write', 'writeSync', 'writev', 'writevSync', 'truncate', 'truncateSync', 'ftruncate', 'ftruncateSync',
  'rename', 'renameSync', 'unlink', 'unlinkSync', 'rm', 'rmSync', 'rmdir', 'rmdirSync',
  'mkdir', 'mkdirSync', 'mkdtemp', 'mkdtempSync', 'copyFile', 'copyFileSync', 'cp', 'cpSync',
  'chmod', 'chmodSync', 'fchmod', 'fchmodSync', 'chown', 'chownSync', 'fchown', 'fchownSync',
  'utimes', 'utimesSync', 'futimes', 'futimesSync', 'lutimes', 'lutimesSync',
  'link', 'linkSync', 'symlink', 'symlinkSync', 'createWriteStream']);

function physical(method, args) {
  if (typeof args[0] === 'number') return physicalDescriptors.has(args[0]);
  if (method === 'mkdir' || method === 'mkdirSync') return norm(args[0]) === accountDirKey;
  if (method === 'rename' || method === 'renameSync') return isAccount(args[0]) && isAccount(args[1]);
  return isAccount(args[0]);
}
function notePhysical(method, args) {
  if (writeMethods.has(method) || method.startsWith('open')) {
    audit.physical_writes.push({ method, account_configuration_only: true });
  }
}
function wrapped(method) {
  return function (...args) {
    const real = physical(method, args);
    const implementation = real ? original[method] : memory[method];
    if (typeof implementation !== 'function') throw new Error('RAM filesystem does not support ' + method);
    if (real) notePhysical(method, args);
    else if (writeMethods.has(method)) audit.ram_write_calls++;
    if (!real && ['writeSync', 'write'].includes(method)) {
      const target = descriptorPaths.get(args[0]);
      if (target && norm(target).startsWith(norm(downloadRoot) + path.sep)) {
        const data = args[1];
        const options = args[2] && typeof args[2] === 'object' ? args[2] : null;
        const length = typeof data === 'string' ? Buffer.byteLength(data) :
          Number(options?.length ?? args[3] ?? data.byteLength);
        const position = options?.position ?? args[4];
        const end = (typeof position === 'number' ? position : memory.fstatSync(args[0]).size) + length;
        if (end > limit) throw new Error('Downloaded object exceeds the in-memory limit');
      }
    }
    if (!real && ['writeFileSync', 'writeFile', 'appendFileSync', 'appendFile'].includes(method) &&
        typeof args[0] !== 'number' && norm(args[0]).startsWith(norm(downloadRoot) + path.sep) &&
        Buffer.byteLength(args[1]) > limit) throw new Error('Downloaded object exceeds the in-memory limit');
    if (method === 'openSync') {
      const fd = implementation(...args);
      if (real) physicalDescriptors.add(fd);
      descriptorPaths.set(fd, args[0]);
      return fd;
    }
    if (method === 'open') {
      const callback = args[args.length - 1];
      args[args.length - 1] = (error, fd) => {
        if (!error && real) physicalDescriptors.add(fd);
        if (!error) descriptorPaths.set(fd, args[0]);
        callback(error, fd);
      };
    }
    const result = implementation(...args);
    if (method === 'close' || method === 'closeSync') {
      physicalDescriptors.delete(args[0]); descriptorPaths.delete(args[0]);
    }
    return result;
  };
}

const overlay = {};
for (const key of Reflect.ownKeys(memory)) overlay[key] = memory[key];
for (const key of Reflect.ownKeys(original)) overlay[key] = wrapped(key);
const promiseOverlay = {};
for (const key of Reflect.ownKeys(originalPromises)) {
  promiseOverlay[key] = async (...args) => {
    if (physical(key, args)) {
      notePhysical(key, args);
      return originalPromises[key](...args);
    }
    if (typeof memory.promises[key] !== 'function') throw new Error('RAM promises filesystem does not support ' + key);
    if (writeMethods.has(key)) audit.ram_write_calls++;
    if (['writeFile', 'appendFile'].includes(key) && typeof args[0] !== 'number' &&
        norm(args[0]).startsWith(norm(downloadRoot) + path.sep) && Buffer.byteLength(args[1]) > limit)
      throw new Error('Downloaded object exceeds the in-memory limit');
    return memory.promises[key](...args);
  };
}
overlay.promises = promiseOverlay;
// Node 24 exposes constants as non-writable properties; keep the identical
// built-in constant values rather than asking fs-monkey to reassign them.
for (const key of Reflect.ownKeys(overlay)) {
  const descriptor = Object.getOwnPropertyDescriptor(fs, key);
  if (key !== 'promises' && descriptor &&
      ((Object.hasOwn(descriptor, 'writable') && !descriptor.writable) ||
       (Object.hasOwn(descriptor, 'get') && !descriptor.set))) delete overlay[key];
}
patchFs(overlay, fs);
// fs-monkey's method list predates some newer Node fs APIs. Every exposed
// function must still use the overlay or fail closed, never retain disk I/O.
for (const key of Reflect.ownKeys(original)) {
  if (['Stats', 'Dir', 'Dirent', 'ReadStream', 'WriteStream', 'FileReadStream', 'FileWriteStream', 'FSWatcher', 'StatWatcher'].includes(key)) {
    if (memory[key]) fs[key] = memory[key];
    continue;
  }
  const descriptor = Object.getOwnPropertyDescriptor(fs, key);
  if (!descriptor || descriptor.writable) fs[key] = overlay[key];
}
fs.realpathSync.native = fs.realpathSync;
fs.realpath.native = fs.realpath;
// fs/promises may already have been imported by a preload or another module.
for (const [key, value] of Object.entries(promiseOverlay)) realPromises[key] = value;
syncBuiltinESMExports();

const lines = [];
let pending = '';
process.stdout.write = function (chunk, encoding, callback) {
  pending += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
  let index;
  while ((index = pending.indexOf('\n')) >= 0) {
    const line = pending.slice(0, index); pending = pending.slice(index + 1);
    try {
      const value = JSON.parse(line);
      if (value && ['result', 'artifact', 'list'].includes(value.type)) lines.push(value);
    } catch {}
  }
  if (typeof encoding === 'function') encoding();
  if (typeof callback === 'function') callback();
  return true;
};
process.stderr.write = function (_chunk, encoding, callback) {
  if (typeof encoding === 'function') encoding();
  if (typeof callback === 'function') callback();
  return true;
};

function readDownload() {
  const files = [];
  function walk(directory) {
    for (const entry of memory.readdirSync(directory, { withFileTypes: true })) {
      const current = path.join(directory, entry.name);
      if (entry.isDirectory()) walk(current);
      else if (entry.isFile()) files.push(current);
    }
  }
  walk(downloadRoot);
  if (files.length !== 1) throw new Error('Expected one RAM download, received ' + files.length);
  const size = memory.statSync(files[0]).size;
  if (size > limit) throw new Error('Downloaded object exceeds the in-memory limit');
  return memory.readFileSync(files[0]).toString('base64');
}
let finished = false;
let fatalError;
function finish(code) {
  if (finished) return;
  finished = true;
  let envelope;
  try {
    const results = lines.filter(item => item.type === 'result');
    const result = results[results.length - 1];
    envelope = { ok: Boolean(Number(code || 0) === 0 && result && [0, '0', undefined].includes(result.code)), result, audit };
    if (fatalError) envelope.error = fatalError;
    if (envelope.ok && input.action === 'download') envelope.body_base64 = readDownload();
    if (envelope.ok && input.action === 'list') {
      const artifact = lines.find(item => item.type === 'artifact');
      const name = result.data?.file_path || artifact?.data?.file_path;
      if (!name || !memory.existsSync(name)) throw new Error('Complete RAM browse artifact is missing');
      envelope.items = memory.readFileSync(name, 'utf8').split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
    }
    if (input.action === 'contract') envelope = { ok: true, result: { code: 0 }, audit, contract: input.contract_result };
  } catch (error) { envelope = { ok: false, error: String(error.message), audit }; }
  output(JSON.stringify(envelope) + '\n');
}
process.exit = code => { finish(code); exit(code); };
process.on('exit', code => finish(code));
process.on('uncaughtException', error => { fatalError = String(error.message); finish(1); exit(1); });
process.on('unhandledRejection', error => { fatalError = String(error?.message || error); finish(1); exit(1); });

if (input.action === 'contract') {
  const marker = path.join(ramRoot, 'contract-not-on-disk.bin');
  const bytes = Buffer.from('RAM contract bytes\u0000保持完整', 'utf8');
  fs.writeFileSync(marker, bytes);
  const fd = fs.openSync(marker, 'r+');
  fs.fsyncSync(fd); fs.closeSync(fd);
  fs.renameSync(marker, marker + '.renamed');
  fs.promises.readFile(marker + '.renamed').then(async received => {
    const asyncPath = marker + '.async';
    await require('node:fs/promises').writeFile(asyncPath, received);
    const chunks = [];
    for await (const chunk of fs.createReadStream(asyncPath)) chunks.push(chunk);
    let forwarded = true;
    for (const spec of input.network_contract || []) {
      const args = spec.url ? [spec.url, spec.options || {}] : [spec.options || {}];
      const request = new EventEmitter();
      request.on('error', () => {});
      const delegate = function (...actual) { forwarded = forwarded && actual.length === args.length && actual.every((item, index) => item === args[index]); return request; };
      const response = observedRequest(spec.transport || 'https:', delegate, null, args);
      if (spec.error_code) response.emit('error', Object.assign(new Error(spec.error_message || ''), { code: spec.error_code }));
    }
    input.contract_result = { exact: Buffer.concat(chunks).equals(bytes), marker, hash_worker_hidden: !fs.existsSync(path.join(path.dirname(runtime), 'hash-worker.cjs')), network_arguments_preserved: forwarded };
    process.exit(0);
  }).catch(() => process.exit(1));
} else {
  const argumentsFor = {
    upload: ['upload', path.join(ramRoot, 'input', input.name || ''), '--parent-fid', String(input.parent_fid)],
    download: ['download', '--fid', String(input.fid), '--output-dir', downloadRoot, '--overwrite'],
    list: ['browse', '--parent-fid', String(input.parent_fid), '--all', '--page-size', '100', '--task-dir', path.join(ramRoot, 'browse')],
    mkdir: ['create-folder', '--dir-path', String(input.name), '--parent-fid', String(input.parent_fid)]
  };
  const args = argumentsFor[input.action];
  if (!args) throw new Error('Unknown memory operation');
  process.argv = [process.execPath, runtime, ...args];
  require(runtime);
}
