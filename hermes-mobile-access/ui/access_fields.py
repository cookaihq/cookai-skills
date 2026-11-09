"""Shared field declarations for the real configuration page and synthetic preview."""
from access_policy import GROUPS, HTTP_ROOTS, RPC_ROOTS, normalize_access


def access_fields(record, cid, target, *, readonly=False):
    explicit = record.get('access', {})
    values = normalize_access(explicit)
    base = 'HMA_CONNECTIONS_JSON.connections.' + cid + '.access'
    fields = [dict(id='access_websocket_enabled', label='允许 WebSocket 连接',
        type='boolean', value=values['websocket_enabled'], default=True, readonly=readonly,
        explicit='websocket_enabled' in explicit, group='访问范围', scope='当前连接', path=target,
        help='总开关；关闭后拒绝 WebSocket 握手。各功能勾选会保留，重新开启后继续生效。',
        effect='保存后应用到当前连接；权限变化会关闭旧连接并重新验证。')]
    paths = {'access_websocket_enabled': base + '.websocket_enabled'}
    groups = []
    for key, label, detail in GROUPS:
        groups.append(dict(id=key, label=label, help=detail,
            http=' · '.join('/api/' + root for root in HTTP_ROOTS.get(key, ())) or '其他业务 /api/ 路径；不开放原生身份及认证接口。',
            websocket=' · '.join(root + '.*' for root in RPC_ROOTS.get(key, ())) or '未归类 RPC / 事件；含 shell.exec、cli.exec、command.dispatch、slash.exec。'))
        for protocol, title in (('http', 'HTTP'), ('websocket', 'WebSocket')):
            fid = protocol + '_' + key
            paths[fid] = base + '.' + protocol + '.' + key
            fields.append(dict(id=fid, label=label + ' · ' + title, type='boolean',
                value=values[protocol][key], default=True, readonly=readonly, explicit=key in explicit.get(protocol, {}),
                group='访问范围', scope='当前连接', path=target, help=detail,
                effect='保存后应用到当前连接；权限变化会关闭旧连接并重新验证。'))
    return fields, dict(access_groups=groups, field_paths=paths, access_target=target)
