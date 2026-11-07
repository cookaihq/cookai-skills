"""Pinned, same-user Gateway binding for the Mobile access adapter."""
from __future__ import annotations

import asyncio
import re

from desktop_profiles import read_profiles, checked_identity, path_key
from discovery import discover_live
from native_control import ControlError, check_process, query, verify_connected_peer


class NativeBackend:
    def __init__(self, candidate, identity, profiles, binding):
        self.candidate, self.identity = candidate, identity
        self.home = profiles['home']
        self.origin, self.served = checked_identity(identity, candidate, self.home)
        self.homes = {row['name']: row['path'] for row in profiles['profiles'] if row['served']}
        self.preferred = binding['preferred_profile']
        if (path_key(binding['home']) != path_key(self.home)
                or self.preferred not in self.homes
                or path_key(binding['profile_home']) != path_key(self.homes[self.preferred])
                or profiles['instance_id'] != identity['instance_id']):
            raise ControlError('configured_profile_binding_changed')

    @classmethod
    def discover(cls, binding, selected=None):
        profiles = read_profiles(selected=selected)
        if profiles.get('result') != 'profiles_verified':
            raise ControlError(profiles.get('reason', 'profiles_unavailable'))
        report = discover_live(selected=profiles['candidate'])
        if report.get('selected') != profiles['candidate']:
            raise ControlError('backend_identity_changed')
        candidate = next(row for row in report['candidates'] if row['id'] == report['selected'])
        identity = query(profiles['home'], candidate)
        return cls(candidate, identity, profiles, binding)

    def profile_home(self, name):
        # An omitted selector uses the saved preference. Explicit values are
        # never replaced with the Desktop's mutable active/default selector.
        name = self.preferred if name is None else name
        if not isinstance(name, str) or name not in self.homes:
            raise ControlError('profile_not_served')
        return self.homes[name]

    async def check(self):
        def inspect():
            check_process(self.candidate)
            latest = query(self.home, self.candidate)
            origin, served = checked_identity(latest, self.candidate, self.home)
            if (latest['instance_id'] != self.identity['instance_id']
                    or served != self.served or origin != self.origin):
                raise ControlError('backend_identity_changed')
        await asyncio.wait_for(asyncio.to_thread(inspect), 5)

    async def verify_socket(self, local, peer):
        # A short allowance for the accepted socket to appear in the OS table;
        # nothing, including the native ticket, has been sent on it yet.
        for attempt in range(3):
            try:
                await asyncio.to_thread(verify_connected_peer, self.candidate, local, peer)
                return
            except ControlError as exc:
                if str(exc) != 'upstream_peer_owner_unverified' or attempt == 2:
                    raise
                await asyncio.sleep(0.025)

    async def mint(self, name, purpose):
        if purpose not in ('native-http', 'interactive'):
            raise ControlError('unsupported_ticket_purpose')
        home = self.profile_home(name)
        await self.check()
        params = {'profile_id': home, 'instance_id': self.identity['instance_id'], 'purpose': purpose}
        if purpose == 'interactive':
            # Mobile carries its selected profile in each RPC. The saved
            # preference is not a single-profile authorization sandbox.
            params['scope'] = 'host'
        grant = await asyncio.wait_for(asyncio.to_thread(query, self.home, self.candidate,
                                                        'session-ticket', params), 5)
        ticket = grant.get('ticket')
        if (grant.get('instance_id') != self.identity['instance_id'] or grant.get('profile_id') != home
                or grant.get('runtime_protocol') != 1 or not isinstance(ticket, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', ticket)):
            raise ControlError('native_ticket_mismatch')
        return ticket
