"""Authenticated loopback adapter for Mobile; the connection controller owns publication.

No public bind/start command. Each upstream TCP connection is checked before
credentials or user data are sent. All native grants remain inside this process.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import re
import secrets
import sys
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web, WSMsgType
from yarl import URL

from access_security import KEY_HEADER, valid_key
from native_control import ControlError
from mobile_rpc import MobileRpc, RpcRefusal

MAX_BODY = 32 * 1024 * 1024
MAX_WS_MESSAGE = 8 * 1024 * 1024
MAX_REQUESTS = 32
MAX_SOCKETS = 8
MONITOR_INTERVAL = 2.0
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=5, sock_read=30)
log = logging.getLogger('hermes_mobile_access.bridge')

# The core Mobile chat paths present in v2.9.6. Dashboard, raw credentials,
# plugin installation and gateway lifecycle routes are not published here.
READ_PATHS = frozenset(('/api/health', '/api/status', '/api/profiles', '/api/profiles/active',
    '/api/model/options', '/api/model/info', '/api/sessions', '/api/sessions/search',
    '/api/sessions/stats', '/api/sessions/empty/count', '/api/files', '/api/files/read',
    '/api/files/download', '/api/files/stream'))
MEDIA_PATHS = frozenset(('/api/files/download', '/api/files/stream'))
WRITE_PATHS = frozenset(('/api/files/upload', '/api/files/upload-stream',
                         '/api/audio/transcribe', '/api/audio/speak'))
# Mobile deliberately preserves '/' in server-issued session IDs. These are
# read-only session paths; dot-segments are separately rejected before dispatch.
SESSION_PATH = re.compile(r'/api/sessions/[A-Za-z0-9_.:-]{1,128}(?:/[A-Za-z0-9_.:-]{1,128}){0,15}\Z')


def route_allowed(method, path):
    return ((method in ('GET', 'HEAD') and (path in READ_PATHS or SESSION_PATH.fullmatch(path)))
            or (method == 'POST' and path in WRITE_PATHS))


def json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def request_profile(query, body=None):
    values = query.getall('profile', [])
    if len(values) > 1:
        raise ControlError('ambiguous_profile')
    if isinstance(body, dict) and 'profile' in body:
        if values and values[0] != body['profile']:
            raise ControlError('ambiguous_profile')
        values = [body['profile']]
    if values and (not isinstance(values[0], str) or not values[0]):
        raise ControlError('invalid_profile')
    return values[0] if values else None


class _ServerLog(logging.LoggerAdapter):
    def log(self, level, msg, *args, **kwargs):
        # aiohttp parser errors may contain a raw request line/credential.
        error = sys.exc_info()[0]
        self.logger.log(level, 'http_protocol_event type=%s', error.__name__ if error else 'none')


class DownstreamDisconnected(Exception):
    """A departing phone must not revoke another accepted connection."""


async def reject_redirect(session, context, params):
    # ws_connect has no allow_redirects argument in the pinned aiohttp release.
    # This public trace hook runs before any redirected request is transmitted.
    params.response.close()
    raise ControlError('upstream_redirect_refused')


class OwnedConnector(aiohttp.TCPConnector):
    """aiohttp 3.14.5 seam: inspect the TCP tuple before ClientRequest.send."""
    def __init__(self, backend):
        super().__init__(force_close=True, limit=MAX_REQUESTS, use_dns_cache=False)
        self.backend = backend

    async def _create_connection(self, req, traces, timeout):
        proto = await super()._create_connection(req, traces, timeout)
        try:
            transport = proto.transport
            if transport is None:
                raise ControlError('upstream_transport_unavailable')
            await asyncio.wait_for(self.backend.verify_socket(transport.get_extra_info('sockname'),
                                                             transport.get_extra_info('peername')), 5)
            return proto
        except BaseException:
            proto.close()
            raise


class AccessBridge:
    def __init__(self, server_url, key, backend, binding_current, *, gated=False):
        parsed = urlsplit(server_url)
        if (parsed.scheme != 'https' or parsed.path != '/' or parsed.query or parsed.fragment
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not parsed.hostname or not re.fullmatch(r'[a-z0-9.-]+', parsed.hostname)):
            raise ControlError('invalid_public_url')
        if not valid_key(key):
            raise ControlError('invalid_access_key')
        self.host, self.key = parsed.hostname, key
        self.backend, self.binding_current = backend, binding_current
        self.marker = secrets.token_urlsafe(32)
        self.reason = None
        self.gated = gated
        self.requests, self.sockets, self.upstream_sockets = set(), set(), set()
        self.ws_admitted = 0
        self._revoke_task = None
        self.session = self.runner = self.monitor_task = None
        self.port = None

    def __repr__(self):
        return '<AccessBridge credentials=redacted>'

    async def start(self):
        if self.runner is not None or self.reason:
            raise ControlError('bridge_already_started_or_revoked')
        await self._check()
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(reject_redirect)
        self.session = aiohttp.ClientSession(connector=OwnedConnector(self.backend), trust_env=False,
            timeout=REQUEST_TIMEOUT, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
            skip_auto_headers={'Accept-Encoding'}, trace_configs=[trace])
        # Pinned aiohttp 3.14.5 retries disconnected GETs by default, which would
        # replay a consumed native ticket after an ambiguous upstream failure.
        self.session._retry_connection = False
        app = web.Application(client_max_size=MAX_BODY, handler_args={'logger': _ServerLog(log, {})})
        app.router.add_route('*', '/{path:.*}', self.handle)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=2, logger=_ServerLog(log, {}))
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, '127.0.0.1', 0)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            self.monitor_task = asyncio.create_task(self._monitor())
            log.info('bridge_started port=%s publication=none', self.port)
            return self.port
        except BaseException:
            await self.close()
            raise

    async def _check(self):
        if self.reason:
            raise ControlError('bridge_revoked')
        if not await asyncio.wait_for(asyncio.to_thread(self.binding_current), 5):
            raise ControlError('configuration_changed')
        await asyncio.wait_for(self.backend.check(), 6)

    async def _monitor(self):
        try:
            while True:
                await asyncio.sleep(MONITOR_INTERVAL)
                await self._check()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            await self.revoke('binding_unavailable')
            log.warning('bridge_invalidated type=%s', type(exc).__name__)

    async def revoke(self, reason='revoked'):
        self.reason = self.reason or reason
        if self._revoke_task is None:
            self._revoke_task = asyncio.create_task(self._revoke(asyncio.current_task()))
        # A caller cancellation must not interrupt revocation halfway through.
        await asyncio.shield(self._revoke_task)

    async def _revoke(self, caller):
        # Disable admission before closing either side. Every accepted HTTP
        # stream and WS belongs to this bridge instance, not another service.
        if self.session is not None:
            await self.session.close()
        await asyncio.gather(*(ws.close(code=1008, message=b'access revoked')
                               for ws in list(self.sockets | self.upstream_sockets)),
                             return_exceptions=True)
        others = [task for task in self.requests if task is not caller]
        for task in others:
            task.cancel()
        # Do not await request tasks here: another request may be awaiting this
        # same revocation. AppRunner cleanup joins remaining handlers.

    async def close(self):
        if self.monitor_task and self.monitor_task is not asyncio.current_task():
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        await self.revoke('stopped')
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        self.key, self.marker = '', ''
        log.info('bridge_stopped')

    @staticmethod
    def error(status, reason):
        return web.json_response({'error': reason}, status=status, headers={'Cache-Control': 'no-store'})

    async def handle(self, request):
        # These checks precede reading bodies, native control calls or traffic
        # to the Gateway. No forwarded header participates in authentication.
        if request.headers.getall('Host', []) not in ([self.host], [self.host + ':443']) or 'Origin' in request.headers:
            return self.error(403, 'host_or_origin_rejected')
        keys = request.headers.getall(KEY_HEADER, [])
        if self.reason or len(keys) != 1 or not valid_key(keys[0]) or not secrets.compare_digest(keys[0], self.key):
            return self.error(401, 'access_denied')
        raw_path = request.raw_path.split('?', 1)[0]
        if not raw_path.startswith('/') or re.search(r'%2[fFeE]|%5[cC]|\\|//', raw_path) or any(p in ('.', '..') for p in request.path.split('/')):
            return self.error(400, 'invalid_path')
        if len(self.requests) >= MAX_REQUESTS:
            return self.error(503, 'busy')
        task = asyncio.current_task()
        self.requests.add(task)
        try:
            await self._check()
            if request.path == '/' and request.method == 'GET' and not request.query:
                return web.Response(text=f'<script>window.__HERMES_SESSION_TOKEN__ = "{self.marker}";</script>',
                    content_type='text/html', headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
            if self.gated:
                return self.error(503, 'publication_pending')
            if request.path == '/api/ws' and request.method == 'GET':
                return await self._websocket(request)
            if not route_allowed(request.method, request.path):
                return self.error(403, 'route_not_published')
            return await self._http(request)
        except DownstreamDisconnected:
            # Cancel this handler so aiohttp closes its transport instead of
            # trying to append an error document to an already started stream.
            raise asyncio.CancelledError() from None
        except web.HTTPException as exc:
            return self.error(exc.status, 'request_rejected')
        except ControlError as exc:
            if str(exc) in ('ambiguous_profile', 'invalid_profile', 'profile_not_served'):
                return self.error(422, str(exc))
            log.warning('bridge_binding_failed reason=%s', str(exc))
            await self.revoke('binding_unavailable')
            return self.error(503, 'binding_unavailable')
        except (ValueError, UnicodeError, RecursionError):
            return self.error(400, 'invalid_payload')
        except Exception as exc:
            # A ticket exchange/request is never replayed automatically.
            log.warning('bridge_request_failed type=%s', type(exc).__name__)
            await self.revoke('upstream_unavailable')
            return self.error(503, 'upstream_unavailable')
        finally:
            self.requests.discard(task)

    async def _http(self, request):
        if 'ticket' in request.query:
            return self.error(400, 'unexpected_credential')
        if 'token' in request.query and (request.path not in MEDIA_PATHS
                or request.query.getall('token', []) != [self.marker]):
            return self.error(400, 'unexpected_credential')
        if request.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            return self.error(415, 'encoded_request_not_supported')
        try:
            async with asyncio.timeout(30):
                body = await request.read()
        except (aiohttp.ClientPayloadError, ConnectionResetError):
            raise DownstreamDisconnected() from None
        except TimeoutError:
            return self.error(408, 'request_body_timeout')
        document = None
        if body and request.content_type == 'application/json':
            document = json_object(body)
        profile = request_profile(request.query, document)
        self.backend.profile_home(profile)
        ticket = await self.backend.mint(profile, 'native-http')
        # The target is fixed. Fragments, absolute URLs, cookies, native
        # tickets and client forwarding/authorization headers cannot redirect it.
        headers = {'X-Hermes-Gateway-Ticket': ticket, 'Accept-Encoding': 'identity'}
        for name in ('Content-Type', 'Accept', 'Range', 'If-Range', 'If-None-Match', 'If-Modified-Since'):
            if name in request.headers:
                headers[name] = request.headers[name]
        target = request.rel_url
        if 'token' in request.query:
            target = target.with_query([(key, value) for key, value in request.query.items() if key != 'token'])
        url = URL(self.backend.origin + str(target), encoded=True)
        async with self.session.request(request.method, url, headers=headers, data=body or None,
                                        allow_redirects=False) as upstream:
            if 300 <= upstream.status < 400:
                return self.error(502, 'upstream_redirect_refused')
            if upstream.status == 401:
                # An upstream rejection is distinct from a caller's valid
                # access key; revoke rather than returning a misleading login.
                await self.revoke('native_authorization_rejected')
                return self.error(503, 'native_authorization_rejected')
            response_headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
            # A normal file policy refusal (403) remains a route result; it is
            # not evidence that every granted connection lost authentication.
            for name in ('Content-Type', 'Content-Length', 'Content-Encoding', 'Content-Disposition', 'Content-Range', 'Accept-Ranges', 'ETag', 'Last-Modified', 'Retry-After'):
                if name in upstream.headers:
                    response_headers[name] = upstream.headers[name]
            response = web.StreamResponse(status=upstream.status, headers=response_headers)
            try:
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    if self.reason:
                        raise ControlError('bridge_revoked')
                    await response.write(chunk)
                await response.write_eof()
            except ConnectionResetError:
                raise DownstreamDisconnected() from None
            except Exception as exc:
                # Once 200/206 has started, a second JSON/HTTP error response
                # would corrupt the same stream. Revoke and close it instead.
                log.warning('upstream_stream_failed type=%s', type(exc).__name__)
                await self.revoke('upstream_stream_failed')
                raise DownstreamDisconnected() from None
            return response

    async def _websocket(self, request):
        if (request.query.getall('token', []) != [self.marker] or set(request.query) != {'token'}
                or request.headers.getall('Sec-WebSocket-Protocol', [])):
            return self.error(401, 'invalid_session_marker')
        if self.ws_admitted >= MAX_SOCKETS:
            return self.error(503, 'busy')
        self.ws_admitted += 1
        try:
            return await self._open_websocket(request)
        finally:
            self.ws_admitted -= 1

    async def _open_websocket(self, request):
        ticket = await self.backend.mint(None, 'interactive')
        async with self.session.ws_connect(self.backend.origin.replace('http:', 'ws:', 1) + '/api/ws',
                protocols=['hermes-gateway-v1', 'hermes-gateway-ticket.' + ticket],
                max_msg_size=MAX_WS_MESSAGE, compress=0,
                timeout=aiohttp.ClientWSTimeout(ws_receive=90, ws_close=1)) as upstream:
            if upstream.protocol != 'hermes-gateway-v1':
                raise ControlError('native_ws_protocol_mismatch')
            downstream = web.WebSocketResponse(max_msg_size=MAX_WS_MESSAGE, compress=False,
                                                timeout=1, heartbeat=30)
            try:
                await downstream.prepare(request)
            except ConnectionResetError:
                raise DownstreamDisconnected() from None
            self.sockets.add(downstream)
            self.upstream_sockets.add(upstream)
            rpc = MobileRpc(preferred_profile=self.backend.preferred)

            async def upload():
                async for message in downstream:
                    if message.type != WSMsgType.TEXT:
                        break
                    frame = json_object(message.data)
                    if not isinstance(frame, dict):
                        await downstream.close(code=1008)
                        break
                    params = frame.get('params', {})
                    if isinstance(params, dict) and 'profile' in params:
                        self.backend.profile_home(params['profile'])
                    # A phone is never the local Desktop capability surface.
                    if frame.get('method') == 'client.capabilities' and isinstance(params, dict) and params.get('desktop_local'):
                        await downstream.close(code=1008)
                        break
                    try:
                        translated = rpc.upload(frame)
                    except RpcRefusal as exc:
                        await downstream.send_json(exc.frame)
                        continue
                    routed_params = translated.get('params')
                    if isinstance(routed_params, dict) and 'profile' in routed_params:
                        self.backend.profile_home(routed_params['profile'])
                    await upstream.send_str(message.data if translated is frame else json.dumps(translated))

            async def download():
                async for message in upstream:
                    if message.type != WSMsgType.TEXT:
                        break
                    frame = json_object(message.data)
                    translated = rpc.download(frame)
                    await downstream.send_str(message.data if translated is frame else json.dumps(translated))

            tasks = {asyncio.create_task(upload()), asyncio.create_task(download())}
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                if any(isinstance(result, Exception) for result in results):
                    log.warning('websocket_stream_ended reason=protocol_or_transport')
                await upstream.close()
                await downstream.close()
                self.sockets.discard(downstream)
                self.upstream_sockets.discard(upstream)
            return downstream
