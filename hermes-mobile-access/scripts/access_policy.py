"""Connection-local HTTP/RPC publication policy, independent of Hermes tool authority.

Missing settings mean enabled. Only declared booleans are accepted; no truthy
strings, cross-source merging, arbitrary upstreams, or credential configuration.
"""
from __future__ import annotations

import copy
import math
import re

GROUPS = (
    ('sessions', '聊天、会话与历史', '聊天、历史、会话整理和子 Agent。'),
    ('cron', '定时任务', '列表、详情、蓝图，以及任务创建、修改、运行和删除。'),
    ('profiles', 'Profile 管理', '查看、创建和修改 Profile；关闭可能影响手机初始化。'),
    ('models', '模型与提供方', '模型选择、提供方、订阅及本地模型管理。'),
    ('skills_plugins', 'Skill 与插件', '查看、安装、启停和更新，以及插件页面。'),
    ('tools_mcp', '工具与 MCP', '工具集、MCP 服务、连接测试及工具审批交互。'),
    ('files_audio', '文件与语音', '文件、附件、图片和语音接口。'),
    ('messaging', '消息平台与 Webhook', '消息平台、配对、连接器、群组和 Webhook。'),
    ('memory', '记忆与学习', '记忆、学习图谱和 Curator 管理。'),
    ('configuration', '配置与凭证', '配置、环境变量、凭证池及秘密输入交互。'),
    ('maintenance', '状态、日志与维护', '统计、日志、进程、网关、更新、备份及维护。'),
    ('other', '其他接口与通用命令', '未归类接口，以及 Shell、CLI、斜杠命令等通用执行入口。'),
)
GROUP_IDS = frozenset(row[0] for row in GROUPS)
HTTP_ROOTS = {
    'sessions': ('sessions',), 'cron': ('cron',), 'profiles': ('profiles',),
    'models': ('model', 'models', 'providers', 'local-models', 'billing', 'subscription'),
    'skills_plugins': ('skills', 'plugins', 'dashboard'), 'tools_mcp': ('tools', 'mcp'),
    'files_audio': ('files', 'audio', 'images'),
    'messaging': ('messaging', 'pairing', 'webhooks', 'connectors', 'groups'),
    'memory': ('memory', 'learning', 'curator'), 'configuration': ('config', 'env', 'credentials'),
    'maintenance': ('status', 'health', 'system', 'analytics', 'logs', 'portal', 'gateway', 'hermes', 'ops', 'actions'),
}
RPC_ROOTS = {
    'sessions': ('session', 'sessions', 'prompt', 'subagent', 'agents', 'delegation', 'spawn_tree',
                 'project', 'projects', 'clarify', 'message', 'reasoning', 'thinking', 'btw', 'background',
                 'agent', 'review', 'todo', 'reaction', 'error', 'notice', 'handoff', 'a2a', 'worker'),
    'cron': ('cron',), 'profiles': ('profile', 'profiles'),
    'models': ('model', 'models', 'provider', 'providers', 'llm', 'moa', 'billing', 'subscription', 'free_tier', 'verification'),
    'skills_plugins': ('skill', 'skills', 'plugin', 'plugins', 'kanban'),
    'tools_mcp': ('tool', 'tools', 'toolsets', 'mcp', 'approval'),
    'files_audio': ('file', 'files', 'image', 'images', 'audio', 'voice', 'pdf', 'clipboard', 'paste', 'input', 'wake'),
    'messaging': ('messaging', 'pairing', 'platforms', 'webhook', 'webhooks', 'connection', 'connectors', 'groups', 'bot_relay'),
    'memory': ('memory', 'learning', 'curator'),
    'configuration': ('config', 'env', 'credential', 'credentials', 'secret', 'vault', 'onboarding', 'setup', 'setup_beat'),
    'maintenance': ('gateway', 'runtime', 'system', 'machine', 'process', 'diagnostics', 'insights', 'usage', 'logs', 'ops', 'actions', 'rollback', 'shared_metrics'),
}
LIVENESS = frozenset(('ping', 'gateway.ping', 'client.capabilities'))
HTTP_METHODS = frozenset(('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'))
MAX_PENDING = 256


def normalize_access(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {'http', 'websocket', 'websocket_enabled'}:
        raise ValueError('invalid_access_policy')
    result = {'websocket_enabled': value.get('websocket_enabled', True)}
    if type(result['websocket_enabled']) is not bool:
        raise ValueError('invalid_access_policy')
    for protocol in ('http', 'websocket'):
        values = value.get(protocol, {})
        if not isinstance(values, dict) or set(values) - GROUP_IDS or any(type(v) is not bool for v in values.values()):
            raise ValueError('invalid_access_policy')
        result[protocol] = {key: values.get(key, True) for key, *_ in GROUPS}
    return result


def _group(root, mapping):
    return next((group for group, roots in mapping.items() if root in roots), 'other')


def http_group(path):
    if not path.startswith('/api/'):
        return None
    root = path[5:].split('/', 1)[0]
    # Native identity/bootstrap endpoints are not Mobile business APIs. The
    # bridge owns their tickets; the WebSocket route has its own admission gate.
    if not root or root in ('auth', 'ws', 'native'):
        return None
    return _group(root, HTTP_ROOTS)


def rpc_groups(method, params=None):
    if method in LIVENESS:
        return ()
    group = _group(method.split('.', 1)[0], RPC_ROOTS)
    groups = {group}
    params = params if isinstance(params, dict) else {}
    # Methods whose namespace is not their actual operation.
    if method == 'reload.env':
        groups = {'configuration'}
    elif method == 'reload.mcp':
        groups = {'tools_mcp'}
    elif method == 'complete.path':
        groups = {'files_audio'}
    elif method == 'session.mutate':
        if params.get('operation') in ('model', 'reasoning'):
            groups.add('models')
        elif params.get('operation') == 'workspace':
            groups.add('files_audio')
        elif params.get('operation') == 'edit_policy':
            groups.add('tools_mcp')
    elif method.startswith('groups.attachment.'):
        groups.add('files_audio')
    elif method in ('session.workspace.move', 'session.cwd.set', 'session.export'):
        groups.add('files_audio')
    elif method == 'model.save_key':
        groups.add('configuration')
    elif method == 'session.automation':
        groups.add('cron')
    # Session-scoped frames must not leak after disabling chat, even when the
    # event itself belongs to another topic (e.g. tool output or a secret prompt).
    if params.get('session_id'):
        groups.add('sessions')
    return tuple(sorted(groups))


class AccessPolicy:
    def __init__(self, value=None):
        self.values = normalize_access(value)

    @property
    def websocket_enabled(self):
        return self.values['websocket_enabled']

    def http_allowed(self, method, path):
        group = http_group(path)
        return method in HTTP_METHODS and group is not None and self.values['http'][group]

    def denied_rpc_group(self, method, params=None):
        return next((g for g in rpc_groups(method, params) if not self.values['websocket'][g]), None)

    def event_allowed(self, params):
        if not isinstance(params, dict) or not isinstance(params.get('type'), str):
            return False
        event = params['type']
        if event == 'gateway.ready':
            return True
        if event == 'request.cancel':
            payload = params.get('payload', {})
            return isinstance(payload, dict) and isinstance(payload.get('method'), str) and not self.denied_rpc_group(payload['method'], params)
        return self.denied_rpc_group(event, params) is None


def rpc_error(frame, group):
    return {'jsonrpc': '2.0', 'id': frame.get('id'), 'error': {
        'code': 4001, 'message': 'Disabled by this connection access policy',
        'data': {'reason': 'access_policy_denied', 'protocol': 'websocket', 'group': group}}}


def _id(frame):
    value = frame.get('id')
    if type(value) not in (str, int, float) or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError('invalid_rpc_id')
    return (str if isinstance(value, str) else float, value)


class WebSocketPolicy:
    """Bounded, per-socket gates for calls, replies, live events and replay.

This is protocol publication, not a sandbox for an Agent's tools or shell.
Unrelated business fields are never recursively interpreted as protocol frames.
"""
    def __init__(self, policy):
        self.policy = policy
        self.calls = {}
        self.server_requests = {}

    def upload(self, frame):
        """Return (forward, local_error); a refused notification gets no reply."""
        method = frame.get('method')
        if method is None:
            if ('result' in frame) == ('error' in frame):
                raise ValueError('invalid_rpc_reply')
            key = _id(frame)
            if self.server_requests.pop(key, None) is None:
                raise ValueError('unsolicited_rpc_reply')
            return frame, None
        if not isinstance(method, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', method):
            raise ValueError('invalid_rpc_method')
        group = self.policy.denied_rpc_group(method, frame.get('params'))
        if method == 'request.answer':
            params = frame.get('params')
            answer_id = params.get('id') if isinstance(params, dict) else None
            answered = self.server_requests.get((str, answer_id)) if isinstance(answer_id, str) else None
            if answered:
                group = self.policy.denied_rpc_group(answered)
                if group is None:
                    self.server_requests.pop((str, answer_id))
            elif not all(self.policy.values['websocket'].values()):
                # This RPC can answer a request the client never received. With
                # restrictions we cannot establish its topic, so do not forward.
                group = 'other'
        if group:
            return None, rpc_error(frame, group) if 'id' in frame else None
        if 'id' in frame:
            key = _id(frame)
            if key in self.calls or len(self.calls) >= MAX_PENDING:
                raise ValueError('rpc_request_correlation_invalid')
            self.calls[key] = method
        return frame, None

    def _server_request(self, frame, refused):
        method = frame.get('method')
        if not isinstance(method, str):
            return False
        group = self.policy.denied_rpc_group(method, frame.get('params'))
        if group:
            refused.append(rpc_error(frame, group))
            return False
        key = _id(frame)
        if key not in self.server_requests and len(self.server_requests) >= MAX_PENDING:
            raise ValueError('rpc_server_request_limit')
        self.server_requests[key] = method
        return True

    def download(self, frame):
        """Return (mobile_frame, denied_server_replies)."""
        refused = []
        if not isinstance(frame, dict):
            raise ValueError('invalid_rpc_frame')
        method = frame.get('method')
        if method in ('event', 'gateway.event'):
            params = frame.get('params')
            if isinstance(params, dict) and params.get('type') == 'request.cancel':
                payload = params.get('payload', {})
                if isinstance(payload, dict) and 'id' in payload:
                    self.server_requests.pop(_id(payload), None)
            return (frame if self.policy.event_allowed(params) else None), refused
        if method is not None:
            if 'id' in frame:
                return (frame if self._server_request(frame, refused) else None), refused
            return (frame if isinstance(method, str) and self.policy.denied_rpc_group(method, frame.get('params')) is None else None), refused
        key = _id(frame)
        original = self.calls.pop(key, None)
        if original is None:
            return None, refused
        result = frame.get('result')
        if isinstance(result, dict):
            result = copy.copy(result)
            # Both canonical and Mobile replay carry event params, not frames.
            if original == 'session.events.since' and isinstance(result.get('events'), list):
                result['events'] = [event for event in result['events'] if self.policy.event_allowed(event)]
                if 'count' in result:
                    result['count'] = len(result['events'])
            if isinstance(result.get('open_requests'), list):
                result['open_requests'] = [request for request in result['open_requests']
                    if isinstance(request, dict) and self._server_request(request, refused)]
            frame = {**frame, 'result': result}
        return frame, refused
