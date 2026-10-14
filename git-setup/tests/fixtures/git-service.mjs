import { mkdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import https from 'node:https';
import http from 'node:http';
import net from 'node:net';

const exec = promisify(execFile);
export async function gitService(t, fixture, targets) {
  const root = join(fixture.root, 'git-service'); await mkdir(root);
  const repositories = {};
  for (const name of Object.keys(targets)) {
    const directory = join(root, `${name}.git`); await mkdir(directory, { recursive: true });
    await fixture.git(directory, 'init', '--bare', '-q', '-b', 'main');
    await fixture.git(directory, 'config', 'http.receivepack', 'true');
    repositories[name] = directory;
  }
  await exec('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', join(root, 'key.pem'), '-out', join(root, 'cert.pem'), '-subj', '/CN=fixture', '-days', '1'], { timeout: 30000 });
  const requests = [], sockets = new Set(), droppedResponses = new Set();
  const server = https.createServer({ key: await readFile(join(root, 'key.pem')), cert: await readFile(join(root, 'cert.pem')) }, (req, res) => {
    const endpoint = new URL(req.url, 'https://fixture');
    const name = endpoint.pathname.match(/^\/(.+)\.git\//)?.[1];
    const expected = targets[name] && `Basic ${Buffer.from(`cnb:${targets[name]}`).toString('base64')}`;
    if (!expected || req.headers.authorization !== expected) { res.writeHead(401, { 'www-authenticate': 'Basic realm="fixture"' }); res.end(); return; }
    requests.push({ repository: name, path: endpoint.pathname, method: req.method });
    const drop = req.method === 'POST' && endpoint.pathname.endsWith('/git-receive-pack') && droppedResponses.delete(name);
    const child = spawn('git', ['http-backend'], { env: { ...fixture.env, GIT_PROJECT_ROOT: root, GIT_HTTP_EXPORT_ALL: '1', PATH_INFO: endpoint.pathname, QUERY_STRING: endpoint.search.slice(1), REQUEST_METHOD: req.method, CONTENT_TYPE: req.headers['content-type'] || '', CONTENT_LENGTH: req.headers['content-length'] || '', REMOTE_USER: 'cnb', HTTP_GIT_PROTOCOL: req.headers['git-protocol'] || '' }, stdio: ['pipe', 'pipe', 'pipe'] });
    const timer = setTimeout(() => child.kill('SIGKILL'), 30000);
    let buffer = Buffer.alloc(0), sent = false;
    req.pipe(child.stdin); child.stdin.on('error', () => {}); child.stderr.resume();
    child.stdout.on('data', chunk => {
      if (drop) {
        // Start the response before truncating it: an entirely empty response on
        // a reused connection makes libcurl replay POST inside a single Git push.
        if (!sent) { res.writeHead(200, { 'content-type': 'application/x-git-receive-pack-result', 'content-length': '4096' }); res.flushHeaders(); sent = true; }
        return;
      }
      if (sent) { res.write(chunk); return; }
      buffer = Buffer.concat([buffer, chunk]); const split = buffer.indexOf('\r\n\r\n');
      if (split < 0) return;
      let status = 200; const headers = {};
      for (const line of buffer.subarray(0, split).toString().split('\r\n')) {
        const index = line.indexOf(':'), key = line.slice(0, index), value = line.slice(index + 1).trim();
        if (key.toLowerCase() === 'status') status = Number.parseInt(value, 10); else headers[key] = value;
      }
      res.writeHead(status, headers); res.write(buffer.subarray(split + 4)); sent = true;
    });
    child.on('error', () => { clearTimeout(timer); res.destroy(); });
    child.on('close', () => { clearTimeout(timer); if (drop) { res.destroy(); return; } if (!sent) res.writeHead(500); res.end(); });
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const proxy = http.createServer();
  proxy.on('connect', (req, socket, head) => {
    if (req.url !== 'cnb.cool:443') { socket.destroy(); return; }
    const upstream = net.connect(server.address().port, '127.0.0.1', () => { socket.write('HTTP/1.1 200 Connection Established\r\n\r\n'); if (head.length) upstream.write(head); socket.pipe(upstream); upstream.pipe(socket); });
    for (const stream of [socket, upstream]) { sockets.add(stream); stream.on('error', () => stream.destroy()); stream.on('close', () => sockets.delete(stream)); }
  });
  await new Promise(done => proxy.listen(0, '127.0.0.1', done));
  t.after(async () => { for (const socket of sockets) socket.destroy(); await Promise.all([new Promise(done => proxy.close(done)), new Promise(done => server.close(done))]); });
  const proxyUrl = `http://127.0.0.1:${proxy.address().port}`;
  Object.assign(fixture.env, { https_proxy: proxyUrl, HTTPS_PROXY: proxyUrl, ALL_PROXY: '', NO_PROXY: '', no_proxy: '', GIT_SSL_NO_VERIFY: '1' });
  return { repositories, requests, loseNextReceive: name => droppedResponses.add(name) };
}
