"""Mobile v2.9.6 request adaptation to Hermes' native session contract.

Only request/reply pairs are adapted. session.usage push events retain their
original payload. This does not retry calls or emulate unavailable statistics.
"""
import hashlib
import json
import logging
import math
import secrets

MAX_PENDING_RPC = 256
MAX_SESSION_BINDINGS = 512
log = logging.getLogger('hermes_mobile_access.rpc')
DIAGNOSTIC_METHODS = frozenset(('session.usage', 'session.stats', 'session.context_breakdown',
    'session.resume', 'session.create', 'session.info', 'session.events.since',
    'session.detach', 'subagent.list', 'gateway.ping', 'client.capabilities',
    'session.title', 'session.history', 'commands.catalog', 'prompt.submit',
    'process.list', 'process.kill', 'subagent.tail', 'subagent.roster',
    'approval.pending', 'mcp.servers.status', 'session.control.read'))
DIAGNOSTIC_REASONS = frozenset(('unknown_method', 'not_found', 'invalid_params',
    'permission_denied', 'profile_mismatch', 'session_busy', 'revision_conflict',
    'stale_generation', 'usage_stats_invalid'))
COUNTERS = {
    'input': 'input_tokens', 'output': 'output_tokens',
    'reasoning': 'reasoning_tokens', 'prompt': 'prompt_tokens',
    'completion': 'completion_tokens', 'total': 'total_tokens',
    'calls': 'api_calls', 'cache_read': 'cache_read_tokens',
    'cache_write': 'cache_write_tokens', 'compressions': 'compression_count',
    'active_subagents': 'active_subagents',
}


def usage_snapshot(stats):
    """Project real counters to Mobile's UsageSnapshotResponse field names.

    Context, cache hit and rolling rate semantics follow Hermes 0.22.5's
    gateway/session_usage.py::tui_usage. Missing optional figures stay absent.
    """
    if not isinstance(stats, dict):
        raise ValueError('usage_stats_invalid')
    usage = {target: stats[source] for target, source in COUNTERS.items()
             if type(stats.get(source)) is int and stats[source] >= 0}
    if not any(name in usage for name in ('input', 'output', 'prompt', 'completion', 'total', 'calls')):
        raise ValueError('usage_stats_invalid')
    for target, fallback in (('input', 'prompt'), ('output', 'completion')):
        if not usage.get(target) and fallback in usage:
            usage[target] = usage[fallback]
    if isinstance(stats.get('model'), str):
        usage['model'] = stats['model']
    used, maximum = stats.get('last_prompt_tokens'), stats.get('context_length')
    if type(used) is int and used > 0 and type(maximum) is int and maximum > 0:
        used = min(used, maximum)
        source = stats.get('context_source') or 'provider_usage'
        usage.update(context_used=used, context_max=maximum,
                     context_percent=max(0, min(100, round(used / maximum * 100))),
                     context_source=source, context_estimated=source != 'provider_usage')
    prompt, cached = usage.get('prompt', 0), usage.get('cache_read', 0)
    if prompt > 0 and cached > 0:
        usage['cache_hit_pct'] = max(0, min(100, round(cached / prompt * 100)))
    for target, source in (('avg_latency_s', 'avg_latency_s'), ('avg_tps', 'tokens_per_s')):
        value = stats.get(source)
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            usage[target] = round(value, 1)
    return usage


def _id_key(value):
    if isinstance(value, str) or value is None:
        return type(value), value
    if type(value) in (int, float) and math.isfinite(value):
        return int, value
    return None


class RpcRefusal(Exception):
    def __init__(self, rid):
        self.frame = {'jsonrpc': '2.0', 'id': rid, 'error': {
            'code': 4001, 'message': 'An explicit profile is required for this session',
            'data': {'reason': 'ambiguous_profile'}}}


class MobileRpc:
    """One WS's bounded request correlation; no state crosses connections."""
    def __init__(self, preferred_profile=None):
        self.pending = {}
        self.preferred_profile = preferred_profile
        self.session_profiles = {}
        self.socket_tag = secrets.token_hex(4)
        self._salt = secrets.token_bytes(16)
        self._submission_namespace = secrets.token_bytes(32)

    def _tag(self, value):
        # Per-socket, salted correlation only. Never log request IDs, profile
        # names, session IDs, params, backend messages or user content.
        if not isinstance(value, str):
            return 'none'
        return hashlib.sha256(self._salt + value.encode()).hexdigest()[:12]

    def _submission_id(self, key):
        # A socket's repeated RPC ID keeps one native admission identity, even
        # after a reply. Different sockets have independent ID counters. This
        # is not cross-reconnect deduplication; this bridge never replays sends.
        kind, value = key
        if type(value) is float and value.is_integer():
            value = int(value)
        identity = json.dumps([kind.__name__, value], ensure_ascii=True).encode()
        return 'hma-' + hashlib.sha256(self._submission_namespace + identity).hexdigest()

    def upload(self, frame):
        # Kotlin's JsonRpcRequest.jsonrpc defaults to "2.0" and the shared
        # serializer has encodeDefaults=false: actual Mobile frames omit it.
        if (frame.get('jsonrpc', '2.0') != '2.0' or not isinstance(frame.get('method'), str)
                or 'id' not in frame or 'result' in frame or 'error' in frame):
            return frame
        key = _id_key(frame['id'])
        if key is None or key in self.pending or len(self.pending) >= MAX_PENDING_RPC:
            raise ValueError('rpc_request_correlation_invalid')
        params = frame.get('params')
        params = params if isinstance(params, dict) else {}
        session_id = params.get('session_id')
        if ('profile' not in params and isinstance(session_id, str)
                and session_id in self.session_profiles):
            profile = self.session_profiles[session_id]
            if profile is None:
                raise RpcRefusal(frame['id'])
            params = {**params, 'profile': profile}
            frame = {**frame, 'params': params}
        if (frame['method'] == 'prompt.submit' and frame['id'] is not None
                and isinstance(frame.get('params'), dict)
                and not {'submission_id', 'input_id'} & params.keys()):
            # Mobile's PromptSubmitParams has neither native identity field.
            # Do not replace explicit (even invalid) client values, alter
            # approval/queue flags, or derive identity from private chat text.
            params = {**params, 'submission_id': self._submission_id(key)}
            frame = {**frame, 'params': params}
        adapted = frame['method'] == 'session.usage'
        self.pending[key] = {'adapted': adapted,
            'method': frame['method'] if frame['method'] in DIAGNOSTIC_METHODS else 'other',
            'session': self._tag(params.get('session_id')), 'profile': self._tag(params.get('profile')),
            'session_id': session_id, 'profile_value': params.get('profile', self.preferred_profile),
            'version': 'explicit' if 'jsonrpc' in frame else 'omitted'}
        return {**frame, 'method': 'session.stats'} if adapted else frame

    def _remember_session(self, call, result):
        profile = call['profile_value']
        if not isinstance(profile, str) or not profile:
            return
        # Only a successful native create/resume proves ownership. Never use
        # a mutable "last active profile", a failed call or a guessed lookup.
        for sid in (result.get('session_id'), result.get('stored_session_id'), call['session_id']):
            if not isinstance(sid, str) or not sid:
                continue
            if sid not in self.session_profiles:
                if len(self.session_profiles) >= MAX_SESSION_BINDINGS:
                    raise ValueError('rpc_session_binding_limit')
                self.session_profiles[sid] = profile
            elif self.session_profiles[sid] != profile:
                self.session_profiles[sid] = None

    def download(self, frame):
        if (not isinstance(frame, dict) or frame.get('jsonrpc') != '2.0'
                or 'method' in frame or 'id' not in frame
                or not ('result' in frame or 'error' in frame)):
            return frame
        call = self.pending.pop(_id_key(frame['id']), None)
        if call and 'error' in frame:
            error = frame['error'] if isinstance(frame['error'], dict) else {}
            data = error.get('data') if isinstance(error.get('data'), dict) else {}
            reason = data.get('reason')
            reason = reason if isinstance(reason, str) and reason in DIAGNOSTIC_REASONS else 'other'
            code = error.get('code') if type(error.get('code')) is int else 0
            log.warning('rpc_refused socket=%s method=%s adapted=%s version=%s code=%s reason=%s session=%s profile=%s',
                self.socket_tag, call['method'], call['adapted'], call['version'], code, reason,
                call['session'], call['profile'])
        elif call and call['method'] in ('session.resume', 'session.create'):
            result = frame.get('result')
            if isinstance(result, dict) and isinstance(result.get('session_id'), str) and result['session_id']:
                self._remember_session(call, result)
                log.info('rpc_session_ready socket=%s method=%s requested_session=%s runtime_session=%s profile=%s',
                    self.socket_tag, call['method'], call['session'], self._tag(result.get('session_id')), call['profile'])
        elif call and call['method'] == 'prompt.submit':
            result = frame.get('result')
            status = result.get('status') if isinstance(result, dict) else None
            status = status if isinstance(status, str) and status in (
                'queued', 'started', 'terminal', 'cancelled', 'unknown', 'steered') else 'other'
            log.info('rpc_prompt_receipt socket=%s status=%s session=%s profile=%s',
                     self.socket_tag, status, call['session'], call['profile'])
        if not call or not call['adapted'] or 'error' in frame:
            return frame
        try:
            result = usage_snapshot(frame['result'])
        except ValueError:
            return {'jsonrpc': '2.0', 'id': frame['id'], 'error': {
                'code': -32603, 'message': 'Invalid session.stats response',
                'data': {'reason': 'usage_stats_invalid'}}}
        return {**frame, 'result': result}
