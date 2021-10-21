# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:
#
# Copyright 2020, Battelle Memorial Institute.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This material was prepared as an account of work sponsored by an agency of
# the United States Government. Neither the United States Government nor the
# United States Department of Energy, nor Battelle, nor any of their
# employees, nor any jurisdiction or organization that has cooperated in the
# development of these materials, makes any warranty, express or
# implied, or assumes any legal liability or responsibility for the accuracy,
# completeness, or usefulness or any information, apparatus, product,
# software, or process disclosed, or represents that its use would not infringe
# privately owned rights. Reference herein to any specific commercial product,
# process, or service by trade name, trademark, manufacturer, or otherwise
# does not necessarily constitute or imply its endorsement, recommendation, or
# favoring by the United States Government or any agency thereof, or
# Battelle Memorial Institute. The views and opinions of authors expressed
# herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY operated by
# BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830
# }}}


import bisect
import logging
import os
import random
import re
import shutil
from typing import Optional
import copy
import uuid
from collections import defaultdict

import gevent
import gevent.core
from gevent.fileobject import FileObject
from zmq import green as zmq

from volttron.platform import jsonapi, get_home
from volttron.platform.agent.known_identities import (
    VOLTTRON_CENTRAL_PLATFORM,
    CONTROL,
    CONTROL_CONNECTION,
    PROCESS_IDENTITIES,
)

from volttron.platform.auth.auth_utils import dump_user, load_user
from volttron.platform.auth.auth_entry import AuthEntry
from volttron.platform.auth.auth_file import AuthFile
from volttron.platform.auth.certs import Certs
from volttron.platform.auth.auth_exception import AuthException
from volttron.platform.jsonrpc import RemoteError
from volttron.platform.vip.agent.errors import VIPError, Unreachable
from volttron.platform.vip.pubsubservice import ProtectedPubSubTopics
from volttron.platform.agent.utils import (
    create_file_if_missing,
    watch_file,
    get_messagebus,
)
from volttron.platform.vip.agent import Agent, Core, RPC


_log = logging.getLogger(__name__)

class AuthService(Agent):
    def __init__(
            self,
            auth_file,
            protected_topics_file,
            setup_mode,
            aip,
            *args,
            **kwargs
    ):
        """Initializes AuthService, and prepares AuthFile."""
        self.allow_any = kwargs.pop("allow_any", False)
        self.is_zap_required = kwargs.pop('zap_required', True)
        self.auth_protocol = kwargs.pop('auth_protocol', None)
        super(AuthService, self).__init__(*args, **kwargs)

        # This agent is started before the router so we need
        # to keep it from blocking.
        self.core.delay_running_event_set = False
        self._certs = None
        if get_messagebus() == "rmq":
            self._certs = Certs()
        self.auth_file_path = os.path.abspath(auth_file)
        self.auth_file = AuthFile(self.auth_file_path)
        self.export_auth_file()
        self.can_update = False
        self.needs_rpc_update = False
        self.aip = aip
        self.zap_socket = None
        self._zap_greenlet = None
        self.auth_entries = []
        self._is_connected = False
        self._protected_topics_file = protected_topics_file
        self._protected_topics_file_path = os.path.abspath(
            protected_topics_file
        )
        self._protected_topics_for_rmq = ProtectedPubSubTopics()
        self._setup_mode = setup_mode
        self._auth_pending = []
        self._auth_denied = []
        self._auth_approved = []

        def topics():
            return defaultdict(set)

        self._user_to_permissions = topics()

    def export_auth_file(self):
        """
        Export all relevant AuthFile methods to external agents
        through AuthService
        :params: None
        :return: None
        """

        def auth_file_read():
            """
            Returns AuthFile data object
            :params: None
            :return: auth_data
            """
            return self.auth_file.auth_data

        def auth_file_add(entry):
            """
            Wrapper function to add entry to AuthFile
            :params: entry
            :return: None
            """
            self.auth_file.add(AuthEntry(**entry))

        def auth_file_update_by_index(auth_entry, index, is_allow=True):
            """
            Wrapper function to update entry in AuthFile
            :params: auth_entry, index, is_allow
            :return: None
            """
            self.auth_file.update_by_index(
                AuthEntry(**auth_entry), index, is_allow
            )

        self.vip.rpc.export(auth_file_read, "auth_file.read")
        self.vip.rpc.export(
            self.auth_file.find_by_credentials, "auth_file.find_by_credentials"
        )
        self.vip.rpc.export(auth_file_add, "auth_file.add")
        self.vip.rpc.export(
            auth_file_update_by_index, "auth_file.update_by_index"
        )
        self.vip.rpc.export(
            self.auth_file.remove_by_credentials,
            "auth_file.remove_by_credentials",
        )
        self.vip.rpc.export(
            self.auth_file.remove_by_index, "auth_file.remove_by_index"
        )
        self.vip.rpc.export(
            self.auth_file.remove_by_indices, "auth_file.remove_by_indices"
        )
        self.vip.rpc.export(self.auth_file.set_groups, "auth_file.set_groups")
        self.vip.rpc.export(self.auth_file.set_roles, "auth_file.set_roles")

    @Core.receiver("onsetup")
    def setup_auth_protocol():
        pass

    @RPC.export
    def update_id_rpc_authorizations(self, identity, rpc_methods):
        """
        Update RPC methods for an auth entry. This is called by the subsystem
        on agent start-up to ensure that the agent's current rpc allowances are
        recorded with it's auth entry.
        :param identity: The agent's identity in the auth entry
        :param rpc_methods: The rpc methods to update in the format
            {rpc_method_name: [allowed_rpc_capability_1, ...]}
        :return: updated_rpc_methods or None
        """
        entries = self.auth_file.read_allow_entries()
        for entry in entries:
            if entry.identity == identity:
                updated_rpc_methods = {}
                # Only update auth_file if changed
                is_updated = False
                for method in rpc_methods:
                    updated_rpc_methods[method] = rpc_methods[method]
                    # Check if the rpc method exists in the auth file entry
                    if method not in entry.rpc_method_authorizations:
                        # Create it and set it to have the provided
                        # rpc capabilities
                        entry.rpc_method_authorizations[method] = rpc_methods[
                            method
                        ]
                        is_updated = True
                    # Check if the rpc method does not have any
                    # rpc capabilities
                    if not entry.rpc_method_authorizations[method]:
                        # Set it to have the provided rpc capabilities
                        entry.rpc_method_authorizations[method] = rpc_methods[
                            method
                        ]
                        is_updated = True
                    # Check if the rpc method's capabilities match
                    # what have been provided
                    if (
                            entry.rpc_method_authorizations[method]
                            != rpc_methods[method]
                    ):
                        # Update rpc_methods based on auth entries
                        updated_rpc_methods[
                            method
                        ] = entry.rpc_method_authorizations[method]
                # Update auth file if changed and return rpc_methods
                if is_updated:
                    self.auth_file.update_by_index(entry, entries.index(entry))
                return updated_rpc_methods
        return None

    def get_entry_authorizations(self, identity):
        """
        Gets all rpc_method_authorizations for an agent using RPC.
        :param identity: Agent identity in the auth file
        :return: rpc_method_authorizations
        """
        rpc_method_authorizations = {}
        try:
            rpc_method_authorizations = self.vip.rpc.call(
                identity, "auth.get_all_rpc_authorizations"
            ).get()
            _log.debug(f"RPC Methods are: {rpc_method_authorizations}")
        except Unreachable:
            _log.warning(
                f"{identity} "
                f"is unreachable while attempting to get rpc methods"
            )

        return rpc_method_authorizations

    def update_rpc_authorizations(self, entries):
        """
        Update allowed capabilities for an rpc method if it
        doesn't match what is in the auth file.
        :param entries: Entries read in from the auth file
        :return: None
        """
        for entry in entries:
            # Skip if core agent
            if (
                    entry.identity is not None
                    and entry.identity not in PROCESS_IDENTITIES
                    and entry.identity != CONTROL_CONNECTION
            ):
                # Collect all modified methods
                modified_methods = {}
                for method in entry.rpc_method_authorizations:
                    # Check if the rpc method does not have
                    # any rpc capabilities
                    if not entry.rpc_method_authorizations[method]:
                        # Do not need to update agent capabilities
                        # if no capabilities in auth file
                        continue
                    modified_methods[method] = entry.rpc_method_authorizations[
                        method
                    ]
                if modified_methods:
                    method_error = True
                    try:
                        self.vip.rpc.call(
                            entry.identity,
                            "auth.set_multiple_rpc_authorizations",
                            rpc_authorizations=modified_methods,
                        ).wait(timeout=4)
                        method_error = False
                    except gevent.Timeout:
                        _log.error(
                            f"{entry.identity} "
                            f"has timed out while attempting "
                            f"to update rpc_method_authorizations"
                        )
                        method_error = False
                    except RemoteError:
                        method_error = True

                    # One or more methods are invalid, need to iterate
                    if method_error:
                        for method in modified_methods:
                            try:
                                self.vip.rpc.call(
                                    entry.identity,
                                    "auth.set_rpc_authorizations",
                                    method_str=method,
                                    capabilities=
                                    entry.rpc_method_authorizations[
                                        method
                                    ],
                                )
                            except gevent.Timeout:
                                _log.error(
                                    f"{entry.identity} "
                                    f"has timed out while attempting "
                                    f"to update "
                                    f"rpc_method_authorizations"
                                )
                            except RemoteError:
                                _log.error(f"Method {method} does not exist.")

    @RPC.export
    def add_rpc_authorizations(self, identity, method, authorizations):
        """
        Adds authorizations to method in auth entry in auth file.

        :param identity: Agent identity in the auth file
        :param method: RPC exported method in the auth entry
        :param authorizations: Allowed capabilities to access the RPC exported
        method
        :return: None
        """
        if identity in PROCESS_IDENTITIES or identity == CONTROL_CONNECTION:
            _log.error(f"{identity} cannot be modified using this command!")
            return
        entries = copy.deepcopy(self.auth_file.read_allow_entries())
        for entry in entries:
            if entry.identity == identity:
                if method not in entry.rpc_method_authorizations:
                    entry.rpc_method_authorizations[method] = authorizations
                elif not entry.rpc_method_authorizations[method]:
                    entry.rpc_method_authorizations[method] = authorizations
                else:
                    entry.rpc_method_authorizations[method].extend(
                        [
                            rpc_auth
                            for rpc_auth in authorizations
                            if rpc_auth in authorizations
                            and rpc_auth
                            not in entry.rpc_method_authorizations[method]
                        ]
                    )
                self.auth_file.update_by_index(entry, entries.index(entry))
                return
        _log.error("Agent identity not found in auth file!")
        return

    @RPC.export
    def delete_rpc_authorizations(
            self,
            identity,
            method,
            denied_authorizations
    ):
        """
        Removes authorizations to method in auth entry in auth file.

        :param identity: Agent identity in the auth file
        :param method: RPC exported method in the auth entry
        :param denied_authorizations: Capabilities that can no longer access
        the RPC exported method
        :return: None
        """
        if identity in PROCESS_IDENTITIES or identity == CONTROL_CONNECTION:
            _log.error(f"{identity} cannot be modified using this command!")
            return
        entries = copy.deepcopy(self.auth_file.read_allow_entries())
        for entry in entries:
            if entry.identity == identity:
                if method not in entry.rpc_method_authorizations:
                    _log.error(
                        f"{entry.identity} does not have a method called "
                        f"{method}"
                    )
                elif not entry.rpc_method_authorizations[method]:
                    _log.error(
                        f"{entry.identity}.{method} does not have any "
                        f"authorized capabilities."
                    )
                else:
                    any_match = False
                    for rpc_auth in denied_authorizations:
                        if (
                                rpc_auth
                                not in entry.rpc_method_authorizations[method]
                        ):
                            _log.error(
                                f"{rpc_auth} is not an authorized capability "
                                f"for {method}"
                            )
                        else:
                            any_match = True
                    if any_match:
                        entry.rpc_method_authorizations[method] = [
                            rpc_auth
                            for rpc_auth in entry.rpc_method_authorizations[
                                method
                            ]
                            if rpc_auth not in denied_authorizations
                        ]
                        if not entry.rpc_method_authorizations[method]:
                            entry.rpc_method_authorizations[method] = [""]
                        self.auth_file.update_by_index(
                            entry, entries.index(entry)
                        )
                    else:
                        _log.error(
                            f"No matching authorized capabilities provided "
                            f"for {method}"
                        )
                return
        _log.error("Agent identity not found in auth file!")
        return

    def _update_auth_lists(self, entries, is_allow=True):
        auth_list = []
        for entry in entries:
            auth_list.append(
                {
                    "domain": entry.domain,
                    "address": entry.address,
                    "mechanism": entry.mechanism,
                    "credentials": entry.credentials,
                    "user_id": entry.user_id,
                    "retries": 0,
                }
            )
        if is_allow:
            self._auth_approved = [
                entry for entry in auth_list if entry["address"] is not None
            ]
        else:
            self._auth_denied = [
                entry for entry in auth_list if entry["address"] is not None
            ]

    def _get_updated_entries(self, old_entries, new_entries):
        """
        Compare old and new entries rpc_method_authorization data. Return
        which entries have been changed.
        :param old_entries: Old entries currently stored in memory
        :type old_entries: list
        :param new_entries: New entries read in from auth_file.json
        :type new_entries: list
        :return: modified_entries
        """
        modified_entries = []
        for entry in new_entries:
            if (
                    entry.identity is not None
                    and entry.identity not in PROCESS_IDENTITIES
                    and entry.identity != CONTROL_CONNECTION
            ):

                for old_entry in old_entries:
                    if entry.identity == old_entry.identity:
                        if (
                                entry.rpc_method_authorizations
                                != old_entry.rpc_method_authorizations
                        ):
                            modified_entries.append(entry)
                        else:
                            pass
                    else:
                        pass
                if entry.identity not in [
                        old_entry.identity for old_entry in old_entries
                ]:
                    modified_entries.append(entry)
            else:
                pass
        return modified_entries

    def read_auth_file(self):
        _log.info("loading auth file %s", self.auth_file_path)
        # Update from auth file into memory
        if self.auth_file.auth_data:
            old_entries = self.auth_file.read_allow_entries().copy()
            self.auth_file.load()
            entries = self.auth_file.read_allow_entries()
            count = 0
            # Allow for multiple tries to ensure auth file is read
            while not entries and count < 3:
                self.auth_file.load()
                entries = self.auth_file.read_allow_entries()
                count += 1
            modified_entries = self._get_updated_entries(old_entries, entries)
            denied_entries = self.auth_file.read_deny_entries()
        else:
            self.auth_file.load()
            entries = self.auth_file.read_allow_entries()
            denied_entries = self.auth_file.read_deny_entries()
        # Populate auth lists with current entries
        self._update_auth_lists(entries)
        self._update_auth_lists(denied_entries, is_allow=False)
        entries = [entry for entry in entries if entry.enabled]
        # sort the entries so the regex credentails follow the concrete creds
        entries.sort()
        self.auth_entries = entries
        if self._is_connected:
            try:
                _log.debug("Sending auth updates to peers")
                # Give it few seconds for platform to startup or for the
                # router to detect agent install/remove action
                gevent.sleep(2)
                self._send_update(modified_entries)
            except BaseException as err:
                _log.error(
                    "Exception sending auth updates to peer. %r",
                    err
                )
                raise err
        _log.info("auth file %s loaded", self.auth_file_path)

    def get_protected_topics(self):
        protected = self._protected_topics
        return protected

    def _read_protected_topics_file(self):
        # Read protected topics file and send to router
        try:
            create_file_if_missing(self._protected_topics_file)
            with open(self._protected_topics_file) as fil:
                # Use gevent FileObject to avoid blocking the thread
                data = FileObject(fil, close=False).read()
                self._protected_topics = jsonapi.loads(data) if data else {}
                if self.core.messagebus == "rmq":
                    self._load_protected_topics_for_rmq()
                    # Deferring the RMQ topic permissions to after "onstart"
                    # event
                else:
                    self._send_protected_update_to_pubsub(
                        self._protected_topics
                    )
        except Exception:
            _log.exception("error loading %s", self._protected_topics_file)

    def _send_update(self, modified_entries=None):
        """
        Compare old and new entries rpc_method_authorization data. Return
        which entries have been changed.

        :param modified_entries: Entries that have been modified when compared
        to the auth file.
        :type modified_entries: list
        """
        user_to_caps = self.get_user_to_capabilities()
        i = 0
        peers = None
        # peerlist times out lots of times when running test suite. This
        # happens even with higher timeout in get()
        # but if we retry peerlist succeeds by second attempt most of the
        # time!!!
        while not peers and i < 3:
            try:
                i = i + 1
                peers = self.vip.peerlist().get(timeout=0.5)
            except BaseException as err:
                _log.warning(
                    "Attempt %i to get peerlist failed with " "exception %s",
                    i,
                    err,
                )
                peers = list(self.vip.peerlist.peers_list)
                _log.warning("Get list of peers from subsystem directly")

        if not peers:
            raise BaseException("No peers connected to the platform")

        _log.debug("after getting peerlist to send auth updates")

        for peer in peers:
            if peer not in [self.core.identity, CONTROL_CONNECTION]:
                _log.debug(f"Sending auth update to peers {peer}")
                self.vip.rpc.call(peer, "auth.update", user_to_caps)

        # Update RPC method authorizations on agents
        if modified_entries:
            try:
                gevent.spawn(
                    self.update_rpc_authorizations, modified_entries
                ).join(timeout=15)
            except gevent.Timeout:
                _log.error("Timed out updating methods from auth file!")
        if self.core.messagebus == "rmq":
            self._check_rmq_topic_permissions()
        else:
            self._send_auth_update_to_pubsub()

    def _send_auth_update_to_pubsub(self):
        user_to_caps = self.get_user_to_capabilities()
        # Send auth update message to router
        json_msg = jsonapi.dumpb(dict(capabilities=user_to_caps))
        frames = [zmq.Frame(b"auth_update"), zmq.Frame(json_msg)]
        # <recipient, subsystem, args, msg_id, flags>
        self.core.socket.send_vip(b"", b"pubsub", frames, copy=False)

    def _send_protected_update_to_pubsub(self, contents):
        protected_topics_msg = jsonapi.dumpb(contents)

        frames = [
            zmq.Frame(b"protected_update"),
            zmq.Frame(protected_topics_msg),
        ]
        if self._is_connected:
            try:
                # <recipient, subsystem, args, msg_id, flags>
                self.core.socket.send_vip(b"", b"pubsub", frames, copy=False)
            except VIPError as ex:
                _log.error(
                    "Error in sending protected topics update to clear "
                    "PubSub: %s",
                    ex,
                )

    @Core.receiver("onstop")
    def stop_auth_protocol(self, sender, **kwargs):
        self.auth_protocol.stop()

    @Core.receiver("onfinish")
    def unbind_auth_protocol(self, sender, **kwargs):
        self.auth_protocol.unbind()

    @Core.receiver('onstart')
    def start_auth_protocol(self, sender, **kwargs):
        self.auth_protocol.start()

    def authenticate(self, domain, address, mechanism, credentials):
        for entry in self.auth_entries:
            if entry.match(domain, address, mechanism, credentials):
                return entry.user_id or dump_user(
                    domain, address, mechanism, *credentials[:1]
                )
        if mechanism == "NULL" and address.startswith("localhost:"):
            parts = address.split(":")[1:]
            if len(parts) > 2:
                pid = int(parts[2])
                agent_uuid = self.aip.agent_uuid_from_pid(pid)
                if agent_uuid:
                    return dump_user(domain, address, "AGENT", agent_uuid)
            uid = int(parts[0])
            if uid == os.getuid():
                return dump_user(domain, address, mechanism, *credentials[:1])
        if self.allow_any:
            return dump_user(domain, address, mechanism, *credentials[:1])

    @RPC.export
    def get_user_to_capabilities(self):
        """RPC method

        Gets a mapping of all users to their capabiliites.

        :returns: mapping of users to capabilities
        :rtype: dict
        """
        user_to_caps = {}
        for entry in self.auth_entries:
            user_to_caps[entry.user_id] = entry.capabilities
        return user_to_caps

    @RPC.export
    def get_authorizations(self, user_id):
        """RPC method

        Gets capabilities, groups, and roles for a given user.

        :param user_id: user id field from VOLTTRON Interconnect Protocol
        :type user_id: str
        :returns: tuple of capabiliy-list, group-list, role-list
        :rtype: tuple
        """
        use_parts = True
        try:
            domain, address, mechanism, credentials = load_user(user_id)
        except ValueError:
            use_parts = False
        for entry in self.auth_entries:
            if entry.user_id == user_id:
                return [entry.capabilities, entry.groups, entry.roles]
            elif use_parts:
                if entry.match(domain, address, mechanism, [credentials]):
                    return entry.capabilities, entry.groups, entry.roles

    @RPC.export
    @RPC.allow(capabilities="allow_auth_modifications")
    def approve_authorization_failure(self, user_id):
        """RPC method

        Approves a pending CSR or credential, based on provided identity.
        The approved CSR or credential can be deleted or denied later.
        An approved credential is stored in the allow list in auth.json.

        :param user_id: user id field from VOLTTRON Interconnect Protocol or
        common name for CSR
        :type user_id: str
        """

        val_err = None
        if self._certs:
            # Will fail with ValueError when a zmq credential user_id is
            # passed.
            try:
                self._certs.approve_csr(user_id)
                permissions = self.core.rmq_mgmt.get_default_permissions(
                    user_id
                )

                if "federation" in user_id:
                    # federation needs more than
                    # the current default permissions
                    # TODO: Fix authorization in rabbitmq
                    permissions = dict(configure=".*", read=".*", write=".*")
                self.core.rmq_mgmt.create_user_with_permissions(
                    user_id, permissions, True
                )
                _log.debug("Created cert and permissions for user: %r", user_id)
            # Stores error message in case it is caused by an unexpected
            # failure
            except ValueError as err:
                val_err = err
        index = 0
        matched_index = -1
        for pending in self._auth_pending:
            if user_id == pending["user_id"]:
                self._update_auth_entry(
                    pending["domain"],
                    pending["address"],
                    pending["mechanism"],
                    pending["credentials"],
                    pending["user_id"],
                )
                matched_index = index
                val_err = None
                break
            index = index + 1
        if matched_index >= 0:
            del self._auth_pending[matched_index]

        for pending in self._auth_denied:
            if user_id == pending["user_id"]:
                self.auth_file.approve_deny_credential(
                    user_id, is_approved=True
                )
                val_err = None
        # If the user_id supplied was not for a ZMQ credential, and the
        # pending_csr check failed,
        # output the ValueError message to the error log.
        if val_err:
            _log.error(f"{val_err}")

    @RPC.export
    @RPC.allow(capabilities="allow_auth_modifications")
    def deny_authorization_failure(self, user_id):
        """RPC method

        Denies a pending CSR or credential, based on provided identity.
        The denied CSR or credential can be deleted or accepted later.
        A denied credential is stored in the deny list in auth.json.

        :param user_id: user id field from VOLTTRON Interconnect Protocol or
        common name for CSR
        :type user_id: str
        """

        val_err = None
        if self._certs:
            # Will fail with ValueError when a zmq credential user_id is
            # passed.
            try:
                self._certs.deny_csr(user_id)
                _log.debug("Denied cert for user: {}".format(user_id))
            # Stores error message in case it is caused by an unexpected
            # failure
            except ValueError as err:
                val_err = err

        index = 0
        matched_index = -1
        for pending in self._auth_pending:
            if user_id == pending["user_id"]:
                self._update_auth_entry(
                    pending["domain"],
                    pending["address"],
                    pending["mechanism"],
                    pending["credentials"],
                    pending["user_id"],
                    is_allow=False,
                )
                matched_index = index
                val_err = None
                break
            index = index + 1
        if matched_index >= 0:
            del self._auth_pending[matched_index]

        for pending in self._auth_approved:
            if user_id == pending["user_id"]:
                self.auth_file.approve_deny_credential(
                    user_id, is_approved=False
                )
                val_err = None
        # If the user_id supplied was not for a ZMQ credential, and the
        # pending_csr check failed,
        # output the ValueError message to the error log.
        if val_err:
            _log.error(f"{val_err}")

    @RPC.export
    @RPC.allow(capabilities="allow_auth_modifications")
    def delete_authorization_failure(self, user_id):
        """RPC method

        Deletes a pending CSR or credential, based on provided identity.
        To approve or deny a deleted pending CSR or credential,
        the request must be resent by the remote platform or agent.

        :param user_id: user id field from VOLTTRON Interconnect Protocol or
        common name for CSR
        :type user_id: str
        """

        val_err = None
        if self._certs:
            # Will fail with ValueError when a zmq credential user_id is
            # passed.
            try:
                self._certs.delete_csr(user_id)
                _log.debug("Denied cert for user: {}".format(user_id))
            # Stores error message in case it is caused by an unexpected
            # failure
            except ValueError as err:
                val_err = err

        index = 0
        matched_index = -1
        for pending in self._auth_pending:
            if user_id == pending["user_id"]:
                self._update_auth_entry(
                    pending["domain"],
                    pending["address"],
                    pending["mechanism"],
                    pending["credentials"],
                    pending["user_id"],
                )
                matched_index = index
                val_err = None
                break
            index = index + 1
        if matched_index >= 0:
            del self._auth_pending[matched_index]

        index = 0
        matched_index = -1
        for pending in self._auth_pending:
            if user_id == pending["user_id"]:
                matched_index = index
                val_err = None
                break
            index = index + 1
        if matched_index >= 0:
            del self._auth_pending[matched_index]

        for pending in self._auth_approved:
            if user_id == pending["user_id"]:
                self._remove_auth_entry(pending["credentials"])
                val_err = None

        for pending in self._auth_denied:
            if user_id == pending["user_id"]:
                self._remove_auth_entry(pending["credentials"], is_allow=False)
                val_err = None

        # If the user_id supplied was not for a ZMQ credential, and the
        # pending_csr check failed,
        # output the ValueError message to the error log.
        if val_err:
            _log.error(f"{val_err}")

    @RPC.export
    def get_authorization_pending(self):
        """RPC method

        Returns a list of failed (pending) ZMQ credentials.

        :rtype: list
        """
        return list(self._auth_pending)

    @RPC.export
    def get_authorization_approved(self):
        """RPC method

        Returns a list of approved ZMQ credentials.
        This list is updated whenever the auth file is read.
        It includes all allow entries from the auth file that contain a
        populated address field.

        :rtype: list
        """
        return list(self._auth_approved)

    @RPC.export
    def get_authorization_denied(self):
        """RPC method

        Returns a list of denied ZMQ credentials.
        This list is updated whenever the auth file is read.
        It includes all deny entries from the auth file that contain a
        populated address field.

        :rtype: list
        """
        return list(self._auth_denied)


    def _get_authorizations(self, user_id, index):
        """Convenience method for getting authorization component by index"""
        auths = self.get_authorizations(user_id)
        if auths:
            return auths[index]
        return []

    @RPC.export
    def get_capabilities(self, user_id):
        """RPC method

        Gets capabilities for a given user.

        :param user_id: user id field from VOLTTRON Interconnect Protocol
        :type user_id: str
        :returns: list of capabilities
        :rtype: list
        """
        return self._get_authorizations(user_id, 0)

    @RPC.export
    def get_groups(self, user_id):
        """RPC method

        Gets groups for a given user.

        :param user_id: user id field from VOLTTRON Interconnect Protocol
        :type user_id: str
        :returns: list of groups
        :rtype: list
        """
        return self._get_authorizations(user_id, 1)

    @RPC.export
    def get_roles(self, user_id):
        """RPC method

        Gets roles for a given user.

        :param user_id: user id field from VOLTTRON Interconnect Protocol
        :type user_id: str
        :returns: list of roles
        :rtype: list
        """
        return self._get_authorizations(user_id, 2)

    def _update_auth_entry(
            self,
            domain,
            address,
            mechanism,
            credential,
            user_id,
            is_allow=True
    ):
        """Adds a pending auth entry to AuthFile."""
        # Make a new entry
        fields = {
            "domain": domain,
            "address": address,
            "mechanism": mechanism,
            "credentials": credential,
            "user_id": user_id,
            "groups": "",
            "roles": "",
            "capabilities": "",
            "rpc_method_authorizations": {},
            "comments": "Auth entry added in setup mode",
        }
        new_entry = AuthEntry(**fields)

        try:
            self.auth_file.add(new_entry, overwrite=False, is_allow=is_allow)
        except AuthException as err:
            _log.error("ERROR: %s\n", str(err))

    def _remove_auth_entry(self, credential, is_allow=True):
        try:
            self.auth_file.remove_by_credentials(credential, is_allow=is_allow)
        except AuthException as err:
            _log.error("ERROR: %s\n", str(err))

    def _update_auth_pending(
            self,
            domain,
            address,
            mechanism,
            credential,
            user_id
    ):
        """Handles incoming pending auth entries."""
        for entry in self._auth_denied:
            # Check if failure entry has been denied. If so, increment the
            # failure's denied count
            if (
                    (entry["domain"] == domain)
                    and (entry["address"] == address)
                    and (entry["mechanism"] == mechanism)
                    and (entry["credentials"] == credential)
            ):
                entry["retries"] += 1
                return

        for entry in self._auth_pending:
            # Check if failure entry exists. If so, increment the failure count
            if (
                    (entry["domain"] == domain)
                    and (entry["address"] == address)
                    and (entry["mechanism"] == mechanism)
                    and (entry["credentials"] == credential)
            ):
                entry["retries"] += 1
                return
        # Add a new failure entry
        fields = {
            "domain": domain,
            "address": address,
            "mechanism": mechanism,
            "credentials": credential,
            "user_id": user_id,
            "retries": 1,
        }
        self._auth_pending.append(dict(fields))
        return


    def _update_topic_permission_tokens(self, identity, not_allowed):
        """
        Make rules for read and write permission on topic (routing key)
        for an agent based on protected topics setting.

        :param identity: identity of the agent
        :return:
        """
        read_tokens = [
            "{instance}.{identity}".format(
                instance=self.core.instance_name, identity=identity
            ),
            "__pubsub__.*",
        ]
        write_tokens = ["{instance}.*".format(instance=self.core.instance_name)]

        if not not_allowed:
            write_tokens.append(
                "__pubsub__.{instance}.*".format(
                    instance=self.core.instance_name
                )
            )
        else:
            not_allowed_string = "|".join(not_allowed)
            write_tokens.append(
                "__pubsub__.{instance}.".format(
                    instance=self.core.instance_name
                )
                + "^(!({not_allow})).*$".format(not_allow=not_allowed_string)
            )
        current = self.core.rmq_mgmt.get_topic_permissions_for_user(identity)
        # _log.debug("CURRENT for identity: {0}, {1}".format(identity,
        # current))
        if current and isinstance(current, list):
            current = current[0]
            dift = False
            read_allowed_str = "|".join(read_tokens)
            write_allowed_str = "|".join(write_tokens)
            if re.search(current["read"], read_allowed_str):
                dift = True
                current["read"] = read_allowed_str
            if re.search(current["write"], write_allowed_str):
                dift = True
                current["write"] = write_allowed_str
                # _log.debug("NEW {0}, DIFF: {1} ".format(current, dift))
                # if dift:
                #     set_topic_permissions_for_user(current, identity)
        else:
            current = dict()
            current["exchange"] = "volttron"
            current["read"] = "|".join(read_tokens)
            current["write"] = "|".join(write_tokens)
            # _log.debug("NEW {0}, New string ".format(current))
            # set_topic_permissions_for_user(current, identity)

    def _check_token(self, actual, allowed):
        pending = actual[:]
        for tk in actual:
            if tk in allowed:
                pending.remove(tk)
        return pending