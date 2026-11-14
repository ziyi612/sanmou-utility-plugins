'use strict';

// Optional transport only. The official SDK still chooses URLs, API methods,
// headers and signatures; bytes cross private stdio pipes, never temp files.
const https = require('node:https');
const fs = require('node:fs');
const path = require('node:path');
const dns = require('node:dns');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');
const { Readable } = require('node:stream');
const MAX_BYTES = 64 * 1024 * 1024;
const MAX_ENVELOPE = Math.floor(MAX_BYTES * 4 / 3) + 2 * 1024 * 1024;
const NON_GLOBAL = new net.BlockList();
for (const [address, prefix] of [['0.0.0.0',8],['10.0.0.0',8],['100.64.0.0',10],['127.0.0.0',8],
  ['169.254.0.0',16],['172.16.0.0',12],['192.0.0.0',24],['192.0.2.0',24],['192.88.99.0',24],
  ['192.168.0.0',16],['198.18.0.0',15],['198.51.100.0',24],['203.0.113.0',24],['224.0.0.0',4],['240.0.0.0',4]]) {
  NON_GLOBAL.addSubnet(address, prefix, 'ipv4');
}
function isGlobalIPv4(address) {
  return typeof address === 'string' && net.isIP(address) === 4 && !NON_GLOBAL.check(address, 'ipv4');
}

function specification(args) {
  let parsed, options;
  if (typeof args[0] === 'string' || args[0] instanceof URL) {
    try { parsed = new URL(args[0]); } catch { return null; }
    options = args[1] && typeof args[1] === 'object' ? args[1] : {};
  } else options = args[0];
  if (!options || typeof options !== 'object') return null;
  const protocol = options.protocol || parsed?.protocol || 'https:';
  const hostname = String(options.hostname || options.host || parsed?.hostname || '').toLowerCase();
  const port = Number(options.port || parsed?.port || options.agent?.defaultPort || 443);
  if (protocol !== 'https:' || port !== 443 || (parsed && (parsed.username || parsed.password))) return null;
  if (hostname !== 'open-api-drive.quark.cn' && !hostname.endsWith('.open-drive.quark.cn')) return null;
  if (!/^[a-z0-9.-]+$/.test(hostname)) return null;
  if (options.cert || options.key || options.pfx || options.ca || options.socketPath) return null;
  const target = options.path || (parsed ? parsed.pathname + parsed.search : '/');
  if (typeof target !== 'string' || !target.startsWith('/') || /[\r\n]/.test(target)) return null;
  const method = String(options.method || 'GET').toUpperCase();
  if (!['GET', 'POST', 'PUT', 'HEAD', 'DELETE', 'PATCH', 'OPTIONS'].includes(method)) return null;
  const suppliedHeaders = options.headers || {};
  if (Array.isArray(suppliedHeaders) || typeof suppliedHeaders !== 'object') return null;
  const headers = {};
  for (const [key, value] of Object.entries(suppliedHeaders)) {
    if (!['string', 'number'].includes(typeof value)) return null;
    headers[key] = String(value);
  }
  return { url: 'https://' + hostname + target, method, headers, timeout: options.timeout };
}

function bridgeRequest(spec, callback, python, helper, resolverFactory) {
  const request = new EventEmitter();
  const chunks = [];
  let total = 0, child, timer, resolver, ended = false, destroyed = false, settled = false;
  request.requestTimeout = Number(spec.timeout) || 60000;
  let phaseCount = 0;
  const phases = new Set(['helper-spawned', 'stdin-finished', 'stdin-reading', 'stdin-complete', 'opening', 'request-started',
    'request-sent', 'response-headers', 'response-body', 'stdout-complete', 'helper-exit', 'failed', 'sdk-response',
    'timeout', 'destroyed', 'transport-error', 'dns-fallback-start', 'dns-fallback-resolved',
    'dns-fallback-attempt', 'dns-fallback-complete']);
  const sslClasses = new Set(['SSLError', 'SSLEOFError', 'SSLSyscallError', 'SSLZeroReturnError',
    'SSLWantReadError', 'SSLWantWriteError', 'SSLCertVerificationError']);
  function phase(name, values = {}) {
    if (phaseCount >= 48 || !phases.has(name)) return;
    phaseCount++;
    const record = { phase: name };
    for (const key of ['status', 'content_length', 'bytes']) {
      if (Number.isSafeInteger(values[key]) && values[key] >= 0 && values[key] <= MAX_ENVELOPE) record[key] = values[key];
    }
    for (const key of ['attempt', 'candidate_count']) {
      if (Number.isInteger(values[key]) && values[key] >= 0 && values[key] <= 3) record[key] = values[key];
    }
    if (typeof values.error_code === 'string' && /^[A-Z0-9_]{1,64}$/.test(values.error_code)) record.error_code = values.error_code;
    if (name === 'transport-error') {
      if (sslClasses.has(values.ssl_error_class)) record.ssl_error_class = values.ssl_error_class;
      for (const key of ['ssl_library', 'ssl_reason']) {
        if (typeof values[key] === 'string' && /^[A-Z0-9_]{1,96}$/.test(values[key])) record[key] = values[key];
      }
      for (const key of ['ssl_errno', 'verify_code']) {
        if (Number.isInteger(values[key]) && values[key] >= -(2 ** 31) && values[key] < 2 ** 31) record[key] = values[key];
      }
      if (['handshake', 'read', 'write'].includes(values.ssl_operation)) record.ssl_operation = values.ssl_operation;
    }
    request.emit('quark-memory-https-phase', record);
  }
  function clear() {
    if (timer) clearTimeout(timer);
    if (resolver) { try { resolver.cancel(); } catch {} }
  }
  function fail(code) {
    if (settled || destroyed) return;
    settled = true; clear();
    phase('failed', { error_code: code });
    if (child && !child.killed) child.kill();
    const error = new Error('Quark memory HTTPS transport failed: ' + code);
    error.code = code;
    request.emit('error', error); request.emit('close');
  }
  request.setTimeout = function (milliseconds, onTimeout) {
    request.requestTimeout = Number(milliseconds);
    if (typeof onTimeout === 'function') request.once('timeout', onTimeout);
    return request;
  };
  request.write = function (chunk, encoding, done) {
    if (ended || destroyed) return false;
    const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk, typeof encoding === 'string' ? encoding : undefined);
    total += value.length;
    if (total > MAX_BYTES) {
      setImmediate(() => fail('MEMORY_LIMIT'));
      return false;
    }
    chunks.push(value);
    if (typeof encoding === 'function') setImmediate(encoding);
    if (typeof done === 'function') setImmediate(done);
    return true;
  };
  request.destroy = function (error) {
    if (destroyed) return request;
    phase('destroyed');
    destroyed = true; clear();
    if (child && !child.killed) child.kill();
    if (error && !settled) setImmediate(() => request.emit('error', error));
    setImmediate(() => request.emit('close'));
    return request;
  };
  request.end = function (chunk, encoding, done) {
    if (ended || destroyed) return request;
    if (chunk != null && typeof chunk !== 'function') request.write(chunk, encoding);
    ended = true;
    if (total > MAX_BYTES) return request;
    const body = Buffer.concat(chunks);
    const source = { url: spec.url, method: spec.method, headers: spec.headers,
      body_base64: body.toString('base64'), has_body: chunks.length > 0,
      timeout_seconds: Math.max(0.001, request.requestTimeout / 1000) };
    if (Buffer.byteLength(JSON.stringify(source)) > MAX_ENVELOPE) {
      setImmediate(() => fail('MEMORY_LIMIT')); return request;
    }
    const deadline = Date.now() + request.requestTimeout;
    const hostname = new URL(spec.url).hostname;
    let candidates = null, candidateIndex = 0;
    function remaining() { return Math.max(0, deadline - Date.now()); }
    async function fallback(category, state) {
      if (category !== 'EPROTO' || state.requestSent || state.ssl?.ssl_operation !== 'handshake'
        || !sslClasses.has(state.ssl?.ssl_error_class)
        || state.ssl?.ssl_error_class === 'SSLCertVerificationError' || state.ssl?.ssl_reason === 'CERTIFICATE_VERIFY_FAILED'
        || state.ssl?.error_code !== 'EPROTO' || !hostname.endsWith('.open-drive.quark.cn')
        || request.requestTimeout <= 0 || remaining() <= 0) return false;
      if (candidates === null) {
        phase('dns-fallback-start');
        candidates = await new Promise(resolve => {
          let complete = false, dnsTimer;
          const finish = addresses => {
            if (complete) return;
            complete = true; if (dnsTimer) clearTimeout(dnsTimer);
            resolver = undefined;
            resolve(Array.isArray(addresses) ? [...new Set(addresses.filter(isGlobalIPv4))].slice(0, 2) : []);
          };
          try {
            const options = { timeout: Math.max(1, Math.min(1500, remaining())), tries: 1 };
            const lookup = resolverFactory ? resolverFactory(options) : new dns.Resolver(options);
            resolver = lookup;
            lookup.setServers(['223.5.5.5']);
            dnsTimer = setTimeout(() => { try { lookup.cancel(); } catch {} finish([]); }, options.timeout);
            lookup.resolve4(hostname, (error, addresses) => finish(error ? [] : addresses));
          } catch { finish([]); }
        });
        phase('dns-fallback-resolved', { candidate_count: candidates.length });
      }
      if (destroyed || settled) return true;
      if (remaining() <= 0) { fail('ETIMEDOUT'); return true; }
      if (candidateIndex >= candidates.length) return false;
      const selected = candidates[candidateIndex++];
      phase('dns-fallback-attempt', { attempt: candidateIndex, candidate_count: candidates.length });
      launch(selected);
      return true;
    }
    function launch(connectAddress) {
      if (destroyed || settled) return;
      if (request.requestTimeout > 0 && remaining() <= 0) { fail('ETIMEDOUT'); return; }
      const state = { requestSent: false, ssl: null };
      const submitted = { ...source, timeout_seconds: Math.max(0.001, remaining() / 1000) };
      if (connectAddress !== undefined) submitted.connect_address = connectAddress;
      const worker = spawn(python, ['-B', helper], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'], env: process.env });
      child = worker;
      phase('helper-spawned');
      const output = [];
      let bytes = 0;
      child.stdout.on('data', value => {
        bytes += value.length;
        if (bytes > MAX_ENVELOPE) { fail('MEMORY_LIMIT'); return; }
        output.push(value);
      });
      let stderrBuffer = '';
      if (typeof child.stderr.on === 'function') child.stderr.on('data', value => {
        stderrBuffer += value.toString('utf8');
        if (stderrBuffer.length > 4096) { stderrBuffer = ''; return; }
        let end;
        while ((end = stderrBuffer.indexOf('\n')) >= 0) {
          const line = stderrBuffer.slice(0, end); stderrBuffer = stderrBuffer.slice(end + 1);
          try {
            const record = JSON.parse(line);
            if (record.kind === 'quark-https-phase') {
              if (record.phase === 'request-sent') state.requestSent = true;
              if (record.phase === 'transport-error') state.ssl = record;
              phase(record.phase, record);
            }
          } catch {}
        }
      });
      else child.stderr.resume();
      child.on('error', () => fail('BRIDGE_START_FAILED'));
      child.stdin.on('error', () => fail('BRIDGE_PIPE_FAILED'));
      child.on('close', async code => {
        phase('helper-exit', { status: code });
        if (destroyed || settled) return;
        let result;
        try { result = JSON.parse(Buffer.concat(output).toString('utf8')); }
        catch { fail('INVALID_BRIDGE_RESPONSE'); return; }
        if (code || result.transport_error) {
          const category = typeof result.transport_error === 'string' && /^[A-Z0-9_]{1,64}$/.test(result.transport_error) ? result.transport_error : 'BRIDGE_EXITED';
          if (await fallback(category, state)) return;
          fail(category); return;
        }
        const data = Buffer.from(result.body_base64 || '', 'base64');
        if (data.length > MAX_BYTES) { fail('MEMORY_LIMIT'); return; }
        settled = true; clear();
        if (connectAddress !== undefined) phase('dns-fallback-complete', { attempt: candidateIndex, status: result.status, bytes: data.length });
        phase('sdk-response', { status: result.status, bytes: data.length });
        const response = Readable.from(data);
        response.statusCode = result.status;
        response.statusMessage = result.status_message || '';
        response.headers = {};
        for (const [key, value] of result.headers || []) {
          const name = key.toLowerCase();
          if (name === 'set-cookie') response.headers[name] = [].concat(response.headers[name] || [], value);
          else if (response.headers[name] === undefined) response.headers[name] = value;
          else response.headers[name] += (name === 'cookie' ? '; ' : ', ') + value;
        }
        if (typeof callback === 'function') callback(response);
        request.emit('response', response);
        response.once('end', () => request.emit('close'));
      });
      child.stdin.end(JSON.stringify(submitted), () => phase('stdin-finished'));
    }
    if (request.requestTimeout > 0) timer = setTimeout(() => {
      if (!destroyed && !settled) { phase('timeout'); request.emit('timeout'); fail('ETIMEDOUT'); }
    }, request.requestTimeout);
    launch();
    const completed = typeof chunk === 'function' ? chunk : typeof encoding === 'function' ? encoding : done;
    if (typeof completed === 'function') setImmediate(completed);
    setImmediate(() => request.emit('finish'));
    return request;
  };
  return request;
}

function installMemoryHttpsBridge(options = {}) {
  if ((options.mode ?? process.env.SANMOU_QUARK_MEMORY_HTTPS) !== 'urllib') return false;
  const python = options.python || process.env.SANMOU_QUARK_PYTHON;
  const helper = options.helper || path.join(__dirname, 'quark_memory_https.py');
  if (!python || !path.isAbsolute(python) || !fs.existsSync(python) || !fs.existsSync(helper)) {
    throw new Error('The optional RAM HTTPS bridge requires existing absolute Python and helper paths.');
  }
  const original = https.request;
  https.request = function (...args) {
    const spec = specification(args);
    if (!spec) return Reflect.apply(original, this, args);
    const callback = typeof args[args.length - 1] === 'function' ? args[args.length - 1] : undefined;
    return bridgeRequest(spec, callback, python, helper, options.resolverFactory);
  };
  return true;
}

module.exports = { installMemoryHttpsBridge, specification, isGlobalIPv4 };
