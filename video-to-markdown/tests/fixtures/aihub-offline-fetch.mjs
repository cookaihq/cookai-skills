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
  else if (url.pathname === '/e/v1/configs/llm_generations_models') {
    result = { data: [{ id: 'gemini-4-hydrogen', capabilities: ['audio'] }] };
  }
  else if (url.pathname === '/e/v1/files/upload/base64') {
    if (!process.env.VTM_FIXTURE_REVIEW_MODE && body.auto_cleanup !== false) throw new Error('Fixture expected retained ASR upload');
    if (process.env.VTM_FIXTURE_SCENARIO === 'upload-unknown') {
      result = { error: { code: 'uncertain' } }; status = 503;
    } else result = { id: 'synthetic-file', size: Buffer.from(body.file_data, 'base64').length,
      filename: body.file_name, url: 'https://api.aihubmax.com/fixture-audio', created: 0 };
  } else if (method === 'POST' && url.pathname === '/e/v1/llm/generations') {
    if (process.env.VTM_FIXTURE_REVIEW_MODE === 'rejected') {
      result = { error: { code: 'insufficient_user_quota' } }; status = 403;
    } else {
      if (!body.messages?.some(message => message.role === 'system')) throw new Error('Review requires system instructions');
      const input = JSON.parse(body.messages.find(message => message.role === 'user').content[0].text);
      const assets = [...input.artifacts.map(asset => asset.id), ...input.inputs_in_content_order.map(asset => asset.id)];
      const report = {
        findings: input.requirements.map(requirement => ({ requirement_id: requirement.id, verdict: 'mismatched',
          evidence: assets.map(asset_id => ({ asset_id, observation: 'Synthetic speech differs from the sine-tone source.', location: '0–2 seconds' })) })),
        coverage: assets.map(asset_id => ({ asset_id, complete: true, note: 'Synthetic full-coverage fixture',
          ...(asset_id.startsWith('source') ? { time_ranges: [[0, 2]], audio_checked: true } : {}) })),
      };
      result = { id: 'synthetic-review', model: body.model,
        status: process.env.VTM_FIXTURE_REVIEW_MODE === 'pending' ? 'pending' : 'completed', results: [{ text: JSON.stringify(report) }] };
    }
  } else if (method === 'POST' && url.pathname.endsWith('/generations')) {
    result = { id: 'synthetic-task', model: body.model, status: 'pending' };
  } else if (url.pathname === '/e/v1/tasks/synthetic-task') {
    if (process.env.VTM_FIXTURE_INTERRUPT_QUERY === '1') process.exit(86);
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
