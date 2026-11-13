'use strict';

// Preload for the official Quark CLI. It preserves os.homedir and the original
// SDK, blocks Node filesystem mutations on C:, and can hand an authorization
// URL to the Chrome controller without opening the system default browser.
const fs = require('node:fs');
const path = require('node:path');
const childProcess = require('node:child_process');
const { fileURLToPath } = require('node:url');
const originalRealpath = fs.realpathSync.native || fs.realpathSync;
const originalLstat = fs.lstatSync;
const originalReadlink = fs.readlinkSync;
const blockC = process.env.SANMOU_QUARK_BLOCK_C_WRITES !== '0';

function isCDrive(value) {
  return /^(?:\\\\[?.]\\|\\\?\?\\)?c:/i.test(value);
}

function actualPath(value) {
  let target = value;
  let tail = [];
  for (let depth = 0; depth < 128; depth += 1) {
    try {
      return path.join(originalRealpath(target), ...tail.reverse());
    } catch (error) {
      if (!['ENOENT', 'ENOTDIR'].includes(error.code)) throw error;
      try {
        if (originalLstat(target).isSymbolicLink()) {
          target = path.resolve(path.dirname(target), originalReadlink(target));
          if (isCDrive(target)) return target;
          continue;
        }
      } catch (linkError) {
        if (!['ENOENT', 'ENOTDIR'].includes(linkError.code)) throw linkError;
      }
      const parent = path.dirname(target);
      if (parent === target) return value;
      tail.push(path.basename(target));
      target = parent;
    }
  }
  throw new Error('Filesystem path contains too many unresolved links');
}

function checkWrite(value) {
  if (typeof value === 'number' || value == null) return;
  if (value instanceof URL) value = fileURLToPath(value);
  if (Buffer.isBuffer(value)) value = value.toString();
  const target = path.resolve(String(value));
  if (blockC && (isCDrive(target) || isCDrive(actualPath(target)))) {
    const error = new Error('Quark runtime storage policy blocks writes to C:; choose an F-drive path.');
    error.code = 'EACCES';
    throw error;
  }
}

const mutationArguments = {
  appendFile: [0], writeFile: [0], mkdir: [0], mkdtemp: [0], rm: [0],
  rmdir: [0], unlink: [0], truncate: [0], chmod: [0], chown: [0], utimes: [0],
  rename: [0, 1], link: [0, 1], symlink: [1], copyFile: [1], cp: [1],
};
for (const [name, positions] of Object.entries(mutationArguments)) {
  for (const suffix of ['', 'Sync']) {
    const method = name + suffix;
    if (typeof fs[method] !== 'function') continue;
    const original = fs[method];
    fs[method] = function (...args) {
      for (const position of positions) checkWrite(args[position]);
      return Reflect.apply(original, this, args);
    };
  }
  if (typeof fs.promises[name] === 'function') {
    const original = fs.promises[name];
    fs.promises[name] = async function (...args) {
      for (const position of positions) checkWrite(args[position]);
      return Reflect.apply(original, this, args);
    };
  }
}

function writable(flags) {
  return typeof flags === 'number'
    ? Boolean(flags & (fs.constants.O_WRONLY | fs.constants.O_RDWR | fs.constants.O_CREAT |
        fs.constants.O_TRUNC | fs.constants.O_APPEND))
    : /[wa+]/.test(String(flags || 'r'));
}
for (const name of ['open', 'openSync']) {
  const original = fs[name];
  fs[name] = function (...args) {
    if (writable(args[1])) checkWrite(args[0]);
    return Reflect.apply(original, this, args);
  };
}
const originalOpen = fs.promises.open;
fs.promises.open = async function (...args) {
  if (writable(args[1])) checkWrite(args[0]);
  return Reflect.apply(originalOpen, this, args);
};
const originalStream = fs.createWriteStream;
fs.createWriteStream = function (...args) {
  checkWrite(args[0]);
  return Reflect.apply(originalStream, this, args);
};

const originalExec = childProcess.exec;
childProcess.exec = function (command, ...rest) {
  const handoffFile = process.env.SANMOU_QUARK_AUTH_URL_FILE;
  const match = handoffFile && typeof command === 'string' &&
    /^start "" "(https:\/\/[^"\r\n]+)"$/.exec(command);
  if (!match) return Reflect.apply(originalExec, this, [command, ...rest]);
  const url = new URL(match[1]);
  if (!/(^|\.)quark\.cn$/i.test(url.hostname)) {
    throw new Error('Unexpected Quark authorization host; browser handoff refused.');
  }
  if ([...url.searchParams.keys()].some(key => /^(?:access_token|refresh_token|id_token)$/i.test(key))) {
    throw new Error('A credential-bearing URL cannot be used for browser handoff.');
  }
  if (!path.isAbsolute(handoffFile)) {
    throw new Error('SANMOU_QUARK_AUTH_URL_FILE must be an absolute path.');
  }
  const output = path.resolve(handoffFile);
  if (blockC && isCDrive(output)) {
    throw new Error('SANMOU_QUARK_AUTH_URL_FILE cannot be on C: while storage protection is enabled.');
  }
  checkWrite(output);
  fs.mkdirSync(path.dirname(output), { recursive: true, mode: 0o700 });
  const temporary = output + '.' + process.pid + '.tmp';
  try {
    fs.writeFileSync(temporary,
      JSON.stringify({ url: match[1], created_at: new Date().toISOString() }),
      { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    fs.renameSync(temporary, output);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
  // The SDK continues its own authorization polling; the user grants access
  // in Chrome. The URL/nonce and account credentials never enter stdout.
  process.stderr.write('Quark authorization is ready for Chrome handoff.\n');
  const callback = rest.find(value => typeof value === 'function');
  if (callback) setImmediate(() => callback(null, '', ''));
  return { pid: undefined, kill: () => false };
};
