import http from 'node:http';
import { writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

/** The public CLI uses its real HTTP client; only the external API is a fixture. */
export async function issueAPI(t, f, targets) {
  const state = { requests: [], issues: [], comments: [], drop: null, beforeResponse: null, hiddenIssues: false };
  const issue = (repository, body, number) => ({ ...body, number, state: body.state || 'open', repository_url: `https://api.github.com/repos/${repository}`, html_url: `https://github.com/${repository}/issues/${number}` });
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://fixture');
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = raw ? JSON.parse(raw) : null;
    const token = req.headers.authorization?.replace(/^Bearer /, '');
    const repository = /^\/repos\/([^/]+\/[^/]+)/.exec(url.pathname)?.[1].toLowerCase();
    const allowed = repository ? targets[repository] === token : Object.values(targets).includes(token);
    state.requests.push({ method: req.method, path: url.pathname, repository, authorized: allowed, body });
    const send = (status, value) => { res.writeHead(status, { 'content-type': 'application/json' }); res.end(JSON.stringify(value)); };
    if (!allowed) return send(401, { message: 'Unauthorized fixture identity' });
    let result;
    if (url.pathname === '/user') result = { login: 'issue-fixture', id: 421 };
    else if (/^\/repos\/[^/]+\/[^/]+$/.test(url.pathname)) result = { full_name: repository, has_issues: true, archived: false };
    else if (req.method === 'POST' && url.pathname.endsWith('/issues')) {
      result = issue(repository, body, state.issues.length + 1); state.issues.push(result);
    } else if (req.method === 'GET' && url.pathname.endsWith('/issues')) {
      result = state.hiddenIssues ? [] : state.issues.filter(item => item.repository_url.endsWith('/' + repository));
      if (state.fullPages && !state.hiddenIssues) result = [...result, ...Array.from({ length: 100 - result.length }, (_, i) => issue(repository, { title: 'Unrelated', body: 'Unrelated item' }, i + 100))];
    }
    else if (/\/issues\/comments\/[1-9][0-9]*$/.test(url.pathname)) result = state.comments.find(item => item.id === Number(url.pathname.split('/').at(-1)));
    else if (/\/issues\/[1-9][0-9]*\/comments$/.test(url.pathname)) {
      const number = Number(url.pathname.split('/').at(-2));
      const parent = `https://api.github.com/repos/${repository}/issues/${number}`;
      if (req.method === 'POST') {
        const id = state.comments.length + 10;
        result = { id, body: body.body, issue_url: parent, html_url: `https://github.com/${repository}/issues/${number}#issuecomment-${id}` }; state.comments.push(result);
      } else result = state.comments.filter(item => item.issue_url === parent);
    } else if (/\/issues\/[1-9][0-9]*$/.test(url.pathname)) {
      const number = Number(url.pathname.split('/').at(-1));
      result = state.issues.find(item => item.number === number && item.repository_url.endsWith('/' + repository));
      if (result && req.method === 'PATCH') Object.assign(result, body);
    }
    if (state.beforeResponse) await state.beforeResponse({ method: req.method, path: url.pathname });
    if (state.drop && state.drop.method === req.method && state.drop.path === url.pathname) { state.drop = null; res.destroy(); return; }
    return result === undefined ? send(404, { message: 'Not found' }) : send(200, result);
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  t.after(() => new Promise(done => server.close(done)));
  const preload = join(f.root, 'issue-api-preload.mjs');
  await writeFile(preload, `const send = globalThis.fetch;\nglobalThis.fetch = (url, options) => {\n const target = new URL(url);\n if (target.origin !== 'https://api.github.com') throw new Error('Unexpected network target');\n return send(new URL(target.pathname + target.search, ${JSON.stringify(`http://127.0.0.1:${server.address().port}`)}), options);\n};\n`);
  f.env.NODE_OPTIONS = `--import=${pathToFileURL(preload).href}`;
  return state;
}
