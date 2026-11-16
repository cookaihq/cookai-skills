// Test-only transport for the installed public CLI. Never imported by Video.
import { appendFileSync } from 'node:fs';

globalThis.fetch = async (input, options = {}) => {
  const url = new URL(input instanceof Request ? input.url : input);
  const method = options.method ?? 'GET';
  const body = typeof options.body === 'string' ? JSON.parse(options.body) : {};
  appendFileSync(process.env.VTM_FIXTURE_LOG, JSON.stringify({ method, path: url.pathname, model: body.model }) + '\n');
  if (url.origin !== 'https://api.aihubmax.com') throw new Error('Offline fixture denied unexpected origin');
  let result;
  let status = 200;
  if (url.pathname === '/v1/models') result = { data: [{ id: 'paraformer-v2' }] };
  else if (url.pathname === '/e/v1/files/upload/base64') {
    if (body.auto_cleanup !== false) throw new Error('Fixture expected retained upload');
    if (process.env.VTM_FIXTURE_SCENARIO === 'upload-unknown') {
      result = { error: { code: 'uncertain' } }; status = 503;
    } else result = { id: 'synthetic-file', size: Buffer.from(body.file_data, 'base64').length,
      filename: body.file_name, url: 'https://api.aihubmax.com/fixture-audio', created: 0 };
  } else if (method === 'POST' && url.pathname.endsWith('/generations')) {
    result = { id: 'synthetic-task', model: body.model, status: 'pending' };
  } else if (url.pathname === '/e/v1/tasks/synthetic-task') {
    if (process.env.VTM_FIXTURE_QUERY_FAILED === '1') {
      result = { error: { code: 'denied' } }; status = 403;
    } else result = { id: 'synthetic-task', model: 'paraformer-v2', status: 'completed', results: [{ transcripts: [{
      text: 'First sentence.Second sentence.', channel_id: 0, sentences: [
        { text: 'First sentence.', begin_time: 200, end_time: 500, speaker_id: 0 },
        { text: 'Second sentence.', begin_time: 600, end_time: 1000, speaker_id: 0 },
      ],
    }] }] };
  } else throw new Error('Offline fixture denied unhandled request ' + method + ' ' + url.pathname);
  const text = JSON.stringify(result);
  return new Response(text, { status, headers: { 'content-type': 'application/json', 'content-length': String(Buffer.byteLength(text)) } });
};
