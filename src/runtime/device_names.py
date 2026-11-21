"""Resolve xdev device addresses and their separate, explicitly configured SSH targets."""

import os
import re
import socket
import unicodedata

REPORT_CARRIERS = ("relay", "sqs", "github")
MAIL_ROUTES = ("direct", "ssh", *REPORT_CARRIERS)


def github_mailbox_config(config):
    """Read one mail profile; older releases only recognize the retired github_mail name."""
    if "github_mailbox" in config and "github_mail" in config:
        raise ValueError("Keep one GitHub mailbox configuration; remove the retired github_mail section")
    key = "github_mailbox" if "github_mailbox" in config else "github_mail"
    if key not in config:
        return None
    mailbox = config[key]
    if not isinstance(mailbox, dict) or not isinstance(mailbox.get("peers"), dict):
        raise ValueError("The GitHub mailbox must define its paired devices")
    if mailbox.get("delivery_scope", "mailbox") != "mailbox":
        raise ValueError("GitHub mail supports only delivery_scope 'mailbox'")
    return mailbox


class UnknownDeviceError(ValueError):
    """The selector is outside this installation's registered addresses."""


def _key(name):
    return unicodedata.normalize("NFC", name).casefold()


def valid_name(value):
    """Logical names are portable mail/NATS tokens. 'local' is a selector only."""
    return (isinstance(value, str) and 1 <= len(value) <= 63 and value.casefold() != "local"
            and value[0].isalnum() and value[-1].isalnum()
            and all(c.isalnum() or c in "-_" for c in value))


def valid_ssh(value):
    """An SSH host or Host alias is one argument, never an option or user@host expression."""
    return (isinstance(value, str) and len(value) <= 253
            and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", value) is not None
            and ".." not in value and value.casefold() != "local")


def machine_name():
    name = os.environ.get("COMPUTERNAME") or socket.gethostname()
    if not valid_name(name):
        raise ValueError("The Windows computer name cannot be used as an xdev device name")
    return name


def _name(value):
    if not valid_name(value):
        raise ValueError(f"Invalid xdev device name: {value!r}")
    return value


def _aliases(record):
    names = record.get("aliases", [])
    if not isinstance(names, list):
        raise ValueError("Device aliases must be a list of names")
    return [_name(name) for name in names]


class DeviceDirectory:
    """One validated snapshot, preserving configured spelling in results and mail."""

    def __init__(self, config):
        if not isinstance(config, dict):
            raise ValueError("Expected a device configuration object")
        self.legacy_clients = config.get("legacy_clients", False)
        if type(self.legacy_clients) is not bool:
            raise ValueError("legacy_clients must be true or false")
        legacy = (not self.legacy_clients and config.get("device") in ("desktop", "laptop")
                  and not {"machine_name", "aliases", "peers"}.intersection(config))
        node = config.get("primary_name") if self.legacy_clients else config.get("device")
        if self.legacy_clients and (not valid_name(node) or config.get("device") not in ("desktop", "laptop")):
            raise ValueError("Legacy clients require a primary_name and a historical laptop or desktop device")
        if node is None or node == "local":
            node = machine_name()
        self.node = _name(node)
        self._selectors = {}
        self._routes = {self.node: None}
        self._rpc_profiles = {}
        self._mail_transports = {}
        self._mail_routes = {}
        self._names = {self.node: []}
        self._identity = {}
        self._mail = {}
        self._selectors["local"] = self.node
        self._bind(self.node, self.node)
        if self.legacy_clients:
            self._bind(config["device"], self.node)
        self.machine_name = _name(config["machine_name"]) if "machine_name" in config else machine_name()
        self._bind(self.machine_name, self.node)
        for alias in _aliases(config):
            self._bind(alias, self.node)
        self._set_identity(self.node, config)
        peers = config.get("peers", {})
        if legacy:
            # These two role addresses were permanently paired in the original
            # schema. New or unconfigured installations never infer other peers.
            other = "desktop" if self.node == "laptop" else "laptop"
            peers = {other: {"ssh": other}}
        if not isinstance(peers, dict):
            raise ValueError("Device peers must be an object keyed by device name")
        for peer, settings in peers.items():
            _name(peer)
            if not isinstance(settings, dict):
                raise ValueError(f"Invalid peer settings for {peer}")
            if _key(peer) in self._selectors:
                raise ValueError(f"Device name is already registered: {peer}")
            mail_transport = settings.get("mail_transport", "auto")
            if mail_transport not in ("auto", *MAIL_ROUTES):
                raise ValueError(f"Unsupported mail transport for {peer}")
            self._mail_transports[peer] = mail_transport
            routes = settings.get("mail_routes")
            if "mail_routes" in settings:
                if (not isinstance(routes, list) or not routes or
                        any(route not in MAIL_ROUTES for route in routes)
                        or len(set(routes)) != len(routes)):
                    raise ValueError(f"Specify an ordered list of distinct mail routes for {peer}")
                self._mail_routes[peer] = tuple(routes)
            ssh_optional = (mail_transport in REPORT_CARRIERS or
                            (routes and not {"direct", "ssh"}.intersection(routes) and mail_transport == "auto"))
            ssh = settings.get("ssh", None if ssh_optional else peer)
            if not (ssh is None and ssh_optional) and not valid_ssh(ssh):
                raise ValueError(f"Configure an ASCII SSH host or Host alias for {peer}")
            self._routes[peer] = ssh
            profile = settings.get("rpc", "windows")
            if profile not in ("windows", "linux-mailbox"):
                raise ValueError(f"Unsupported SSH RPC profile for {peer}")
            self._rpc_profiles[peer] = profile
            self._names[peer] = []
            for alias in (peer, *_aliases(settings)):
                self._bind(alias, peer)
            if ssh:
                self._bind(ssh, peer)
            self._set_identity(peer, settings)
        if self.legacy_clients:
            local_role = config["device"]
            opposite = "laptop" if local_role == "desktop" else "desktop"
            peer = self._selectors.get(opposite)
            if (peer is None or peer == self.node or self.identity_key(self.node) != local_role
                    or self.identity_key(peer) != opposite):
                raise ValueError("Legacy clients require the original laptop and desktop identity keys on separate devices")
            self._mail = {self.node: local_role, peer: opposite}

    def _bind(self, alias, canonical):
        key = _key(alias)
        bound = self._selectors.setdefault(key, canonical)
        if bound != canonical:
            raise ValueError(f"Device address belongs to multiple devices: {alias}")
        names = self._names[canonical]
        if valid_name(alias) and all(_key(name) != key for name in names):
            names.append(alias)

    def _set_identity(self, canonical, record):
        names = {_key(name) for name in self._names[canonical]}
        if "identity_key" in record:
            key = record["identity_key"]
            if not isinstance(key, str) or _key(key) not in names:
                raise ValueError(f"identity_key must belong to {canonical} as a registered name or alias")
            key = _key(key)
        else:
            # Pre-versioned inventories could carry a role-based author/group key.
            previous = names & {"laptop", "desktop"}
            if len(previous) > 1:
                raise ValueError(f"Set an explicit identity_key for {canonical} to disambiguate both old roles")
            key = next(iter(previous), _key(canonical))
        self._identity[canonical] = key

    def resolve(self, selector):
        if isinstance(selector, str):
            canonical = self._selectors.get(_key(selector))
            if canonical is not None:
                return canonical
        raise UnknownDeviceError(f"Unknown device address: {selector!r}. Use local, this device, or a configured peer.")

    def ssh_target(self, selector):
        peer = self.resolve(selector)
        target = self._routes[peer]
        if target is None and peer != self.node:
            raise ValueError(f"No SSH route configured for {peer}")
        return target

    def mail_transport(self, selector):
        return self._mail_transports.get(self.resolve(selector), "auto")

    def mail_routes(self, selector):
        return self._mail_routes.get(self.resolve(selector))

    def rpc_profile(self, selector):
        return self._rpc_profiles.get(self.resolve(selector))

    def names_for(self, selector):
        """Persistable addresses that denote this device, excluding the local shortcut."""
        return tuple(self._names[self.resolve(selector)])

    def identity_key(self, selector):
        return self._identity[self.resolve(selector)]

    def mail_device(self, selector):
        """Choose the address this reader generation can store, filter and acknowledge."""
        canonical = self.resolve(selector)
        return self._mail.get(canonical, canonical)

    def describe(self):
        return {"device": self.node, "machine_name": self.machine_name, "identity_key": self.identity_key(self.node),
                "legacy_clients": self.legacy_clients,
                "aliases": [name for name in self._names[self.node] if name != self.node],
                "peers": [name for name in self._routes if name != self.node]}

    def check_machine(self):
        actual = machine_name()
        if _key(self.machine_name) != _key(actual):
            raise ValueError(f"Configuration is bound to {self.machine_name!r}, this computer is {actual!r}")
        return self

    def same(self, left, right):
        """Compare stored addresses without granting routing authority to an unknown name."""
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        a, b = _key(left), _key(right)
        if a in self._selectors and b in self._selectors:
            return self._selectors[a] == self._selectors[b]
        return valid_name(left) and valid_name(right) and a == b
