// @ts-check

import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { join } from 'node:path';

/** @typedef {import('./miniapp-api.js').MiniAppContext} MiniAppContext */
/** @typedef {import('./miniapp-api.js').MiniAppLifecycle} MiniAppLifecycle */

const PYTHON = process.platform === 'win32' ? 'python' : 'python3';
const QUERY_TIMEOUT_MS = 30000;
const CACHE_TTL_MS = 2000;
const MAX_OUTPUT_BYTES = 20 * 1024 * 1024;
const RANGE_KEYS = ['1h', '24h', '7d', '30d', 'all'];

/**
 * mcode-usage-monitor Node 入口:
 *  - 在 Host 分配的端口上服务 /dashboard(客户端页面)与 /echarts.min.js
 *  - /api/data 通过一次性 spawn python api.py 取数(stdout JSON),
 *    不建立 Node->本机端口的 TCP 连接(该路径在 Mini App 运行时内不可用)
 *  - 查询串行化 + 2s 内存缓存, 避免并发快照读放大
 *  - dispose() 关闭 HTTP 监听并结束在途子进程
 * @param {MiniAppContext} context
 * @returns {Promise<MiniAppLifecycle>}
 */
export async function start(context) {
  const clientRoot = join(context.pluginRoot, 'miniapp/client');
  const apiPyPath = join(context.pluginRoot, 'miniapp/node/api.py');

  const indexHtml = await readFile(join(clientRoot, 'index.html'));
  const echartsJs = await readFile(join(clientRoot, 'echarts.min.js'));

  // ---- Python 查询子进程 ----
  const activeChildren = new Set();
  let disposed = false;
  let queue = Promise.resolve(); // 串行执行, 避免 sqlite 快照并发
  const cache = new Map(); // key -> { at, promise }

  function runPython(args) {
    return new Promise((resolve, reject) => {
      let proc;
      try {
        proc = spawn(PYTHON, [apiPyPath, ...args], {
          cwd: context.pluginRoot,
          windowsHide: true,
          stdio: ['ignore', 'pipe', 'pipe'],
        });
      } catch (err) {
        reject(err);
        return;
      }
      activeChildren.add(proc);
      let out = '';
      let errText = '';
      let settled = false;
      const finish = (err, payload) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        activeChildren.delete(proc);
        if (err) reject(err);
        else resolve(payload);
      };
      const timer = setTimeout(() => {
        try { proc.kill(); } catch { /* 已退出 */ }
        finish(new Error('query timeout'));
      }, QUERY_TIMEOUT_MS);
      proc.stdout.setEncoding('utf-8');
      proc.stdout.on('data', (chunk) => {
        if (out.length < MAX_OUTPUT_BYTES) out += chunk;
      });
      proc.stderr.setEncoding('utf-8');
      proc.stderr.on('data', (chunk) => { errText += chunk; });
      proc.on('error', (err) => finish(err));
      proc.on('close', (code) => {
        if (code === 0) {
          try {
            finish(null, JSON.parse(out));
          } catch (e) {
            finish(new Error(`bad payload: ${e.message}`));
          }
        } else {
          const hint = (out || errText || '').trim().slice(0, 300);
          finish(new Error(`backend exit ${code}: ${hint}`));
        }
      });
    });
  }

  function queryBackend(rangeKey, models, sessions) {
    const key = `${rangeKey}|${models ? models.join(',') : ''}|${sessions ? sessions.join(',') : ''}`;
    const hit = cache.get(key);
    if (hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.promise;
    const promise = queue.then(() =>
      runPython(['--range', rangeKey,
                 '--models', models ? models.join(',') : '',
                 '--sessions', sessions ? sessions.join(',') : '']));
    cache.set(key, { at: Date.now(), promise });
    promise.catch(() => cache.delete(key)); // 失败不缓存
    queue = promise.catch(() => {}); // 链条继续
    return promise;
  }

  function sendJson(res, code, obj) {
    const body = JSON.stringify(obj);
    res.writeHead(code, {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
    });
    res.end(body);
  }

  // ---- HTTP 服务 ----
  const server = createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://miniapp.local');
    if (req.method === 'GET' && url.pathname === '/dashboard') {
      res.writeHead(200, {
        'content-type': 'text/html; charset=utf-8',
        'cache-control': 'no-store',
      });
      res.end(indexHtml);
      return;
    }
    if (req.method === 'GET' && url.pathname === '/echarts.min.js') {
      res.writeHead(200, {
        'content-type': 'application/javascript; charset=utf-8',
        'cache-control': 'no-store',
      });
      res.end(echartsJs);
      return;
    }
    if (req.method === 'GET' && url.pathname === '/api/data') {
      const rawRange = url.searchParams.get('range') ?? 'all';
      const rangeKey = RANGE_KEYS.includes(rawRange) ? rawRange : 'all';
      const rawModels = url.searchParams.get('models');
      const models = rawModels
        ? rawModels.split(',').map((s) => s.trim()).filter(Boolean)
        : null;
      const rawSessions = url.searchParams.get('sessions');
      const sessions = rawSessions
        ? rawSessions.split(',').map((s) => s.trim()).filter(Boolean)
        : null;
      queryBackend(rangeKey, models && models.length ? models : null,
                   sessions && sessions.length ? sessions : null)
        .then((payload) => sendJson(res, 200, payload))
        .catch((err) => sendJson(res, 502, { error: `backend unavailable: ${err.message}` }));
      return;
    }
    sendJson(res, 404, { error: 'not_found' });
  });

  await listen(server, context.listen.host, context.listen.port);
  context.logger.info('miniapp.runtime.listening');

  // 预热首次查询(不阻塞就绪)
  queryBackend('all', null).catch((err) => {
    context.logger.warn('miniapp.backend.warmup_failed', { message: err.message });
  });

  const dispose = async () => {
    if (disposed) return;
    disposed = true;
    context.signal.removeEventListener('abort', onAbort);
    for (const proc of activeChildren) {
      try { proc.kill(); } catch { /* 已退出 */ }
    }
    activeChildren.clear();
    await close(server);
  };
  const onAbort = () => {
    void dispose();
  };
  context.signal.addEventListener('abort', onAbort, { once: true });
  if (context.signal.aborted) await dispose();

  return { dispose };
}

function listen(server, host, port) {
  return new Promise((resolve, reject) => {
    const onError = (error) => reject(error);
    server.once('error', onError);
    server.listen(port, host, () => {
      server.off('error', onError);
      resolve();
    });
  });
}

function close(server) {
  return new Promise((resolve) => {
    server.close(() => resolve());
  });
}
