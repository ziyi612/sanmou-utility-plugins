'use strict';

// Compatibility preload for one official Quark authorization endpoint.
// Default: preserve Node HTTPS and frame the existing JSON bytes with an exact
// Content-Length. Optional SANMOU_QUARK_HTTP_TRANSPORT=urllib uses the adjacent
// Python bridge; neither mode changes the URL, signatures, nonce or JSON body.
const https = require('node:https');
const fs = require('node:fs');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { Readable } = require('node:stream');
const { spawn } = require('node:child_process');
const originalRequest = https.request;
const endpoint = '/agent/v1/get_authorize_page_url';

function record(event) {
  const output = process.env.SANMOU_QUARK_HTTP_DIAGNOSTICS;
  if (!output) return;
  const target = path.resolve(output);
  if (!path.isAbsolute(output) || !/^f:/i.test(target)) {
    throw new Error('Quark transport diagnostics must use an absolute F-drive path.');
  }
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
  fs.appendFileSync(target, JSON.stringify({ at: new Date().toISOString(), ...event }) + '\n', { mode: 0o600 });
}

function eligible(options) {
  return options && typeof options === 'object' && !(options instanceof URL) &&
    String(options.method || 'GET').toUpperCase() === 'POST' &&
    options.hostname === 'open-api-drive.quark.cn' &&
    (!options.port || Number(options.port) === 443) &&
    String(options.path || '').split('?')[0] === endpoint;
}

function frameNativeRequest(options, callback) {
  const request = originalRequest.call(https, options, callback);
  const originalEnd = request.end;
  const chunks = [];
  const writeCallbacks = [];
  let ended = false;
  request.write = function (chunk, encoding, done) {
    if (ended) throw new Error('Quark authorization JSON write after end.');
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk,
      typeof encoding === 'string' ? encoding : undefined));
    if (typeof encoding === 'function') writeCallbacks.push(encoding);
    if (typeof done === 'function') writeCallbacks.push(done);
    return true;
  };
  request.end = function (chunk, encoding, done) {
    if (ended) return request;
    let callbackEnd = typeof chunk === 'function' ? chunk :
      typeof encoding === 'function' ? encoding : done;
    if (chunk != null && typeof chunk !== 'function') request.write(chunk, encoding);
    ended = true;
    const body = Buffer.concat(chunks);
    if (request.headersSent) throw new Error('Quark authorization headers were sent before JSON framing.');
    request.removeHeader('Transfer-Encoding');
    request.setHeader('Content-Length', String(body.length));
    record({ phase: 'request', transport: 'node_content_length', host: options.hostname,
      path: endpoint, content_length: body.length, header_names: Object.keys(options.headers || {}) });
    request.once('finish', () => { for (const callbackWrite of writeCallbacks) callbackWrite(); });
    return originalEnd.call(request, body, callbackEnd);
  };
  request.once('response', response => record({ phase: 'response', transport: 'node_content_length',
    path: endpoint, http_status: response.statusCode }));
  request.once('error', error => record({ phase: 'error', transport: 'node_content_length',
    path: endpoint, category: error.code || error.name }));
  return request;
}

function bridgeRequest(options, callback) {
  const request = new EventEmitter();
  const chunks = [];
  let worker;
  let ended = false;
  let destroyed = false;
  request.setTimeout = function (milliseconds) { request.requestTimeout = milliseconds; return request; };
  request.write = function (chunk, encoding, done) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk,
      typeof encoding === 'string' ? encoding : undefined));
    if (typeof encoding === 'function') setImmediate(encoding);
    if (typeof done === 'function') setImmediate(done);
    return true;
  };
  request.destroy = function (error) {
    if (destroyed) return request;
    destroyed = true;
    if (worker && !worker.killed) worker.kill();
    if (error) setImmediate(() => request.emit('error', error));
    setImmediate(() => request.emit('close'));
    return request;
  };
  request.end = function (chunk) {
    if (ended) return request;
    ended = true;
    if (chunk) request.write(chunk);
    const body = Buffer.concat(chunks);
    const python = process.env.SANMOU_QUARK_PYTHON;
    if (!python || !path.isAbsolute(python) || !fs.existsSync(python)) {
      throw new Error('SANMOU_QUARK_PYTHON must name an existing Python executable by absolute path.');
    }
    record({ phase: 'request', transport: 'urllib', host: options.hostname, path: endpoint,
      content_length: body.length, header_names: Object.keys(options.headers || {}) });
    worker = spawn(python, ['-B', path.join(__dirname, 'quark_http_transport.py')],
      { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, env: process.env });
    const output = [];
    worker.stdout.on('data', chunk => output.push(chunk));
    worker.stderr.resume();
    worker.on('error', error => request.emit('error', error));
    worker.on('close', code => {
      if (destroyed) return;
      let result;
      try { result = JSON.parse(Buffer.concat(output).toString('utf8')); }
      catch { result = { transport_error: 'InvalidBridgeResponse' }; }
      if (code || result.transport_error) {
        const error = new Error('Quark authorization HTTP bridge failed: ' + (result.transport_error || 'BridgeExited'));
        error.code = result.transport_error || 'BRIDGE_EXITED';
        record({ phase: 'error', transport: 'urllib', path: endpoint, category: error.code });
        request.emit('error', error);
        request.emit('close');
        return;
      }
      record({ phase: 'response', transport: 'urllib', path: endpoint, http_status: result.status });
      const response = Readable.from(Buffer.from(result.body_base64 || '', 'base64'));
      response.statusCode = result.status;
      response.headers = {};
      for (const [key, value] of result.headers || []) {
        const name = key.toLowerCase();
        if (response.headers[name] === undefined) response.headers[name] = value;
        else response.headers[name] = [].concat(response.headers[name], value);
      }
      callback(response);
      response.on('end', () => request.emit('close'));
    });
    worker.stdin.end(JSON.stringify({ url: 'https://' + options.hostname + options.path,
      method: 'POST', headers: options.headers || {}, body_base64: body.toString('base64'),
      timeout_seconds: Math.min((request.requestTimeout || 60000) / 1000, 45) }));
    return request;
  };
  return request;
}

https.request = function (options, callback) {
  if (!eligible(options)) return Reflect.apply(originalRequest, this, arguments);
  const mode = process.env.SANMOU_QUARK_HTTP_TRANSPORT || 'content-length';
  if (mode === 'content-length') return frameNativeRequest(options, callback);
  if (mode === 'urllib') return bridgeRequest(options, callback);
  throw new Error('Unknown SANMOU_QUARK_HTTP_TRANSPORT mode.');
};
