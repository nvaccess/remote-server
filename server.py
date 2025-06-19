import json
import os
import random
import string
import sys
import time
from collections import OrderedDict
from logging import getLogger

from OpenSSL import crypto
from twisted.internet import reactor, ssl
from twisted.internet.interfaces import ITCPTransport
from twisted.internet.protocol import Factory, defer, connectionDone
from twisted.internet.task import LoopingCall
from twisted.protocols.basic import LineReceiver
from twisted.python import log, usage
from twisted.internet.defer import Deferred
from twisted.python.failure import Failure
from typing import Any, TypedDict, cast

logger = getLogger("remote-server")

PING_INTERVAL: int = 300
INITIAL_TIMEOUT: int = 30
# Expiration time for generated keys, in seconds
GENERATED_KEY_EXPIRATION_TIME: int = 60 * 60 * 24  # One day


class UserDict(TypedDict):
	"""Typed dictionary representing a user."""

	id: int
	connection_type: str | None


class Message(TypedDict):
	"""Type hints for protocol messages."""

	type: str


class Channel:
	"""Collection of connected users in the one "session"."""

	def __init__(self, key: str, serverState: "ServerState | None" = None) -> None:
		"""Constructor

		:param key: Unique identifier of this channel.
		:param server_state: Server state, defaults to None
		"""
		self.clients: OrderedDict[int, User] = OrderedDict()
		self.key = key
		self.serverState = serverState

	def addClient(self, client: "User") -> None:
		"""Joined when a new user wants to join the channel.

		:param client: The new channel member.
		"""
		if client.protocol.protocol_version == 1:  # pragma: no cover - protocol v1 is not tested
			ids = [c.user_id for c in self.clients.values()]
			msg = dict(type="channel_joined", channel=self.key, user_ids=ids, origin=client.user_id)
		else:
			clients = [i.as_dict() for i in self.clients.values()]
			msg = dict(type="channel_joined", channel=self.key, origin=client.user_id, clients=clients)
		client.send(**msg)
		for existingClient in self.clients.values():
			if existingClient.protocol.protocol_version == 1:  # pragma: no cover - protocol v1 is not tested
				existingClient.send(type="client_joined", user_id=client.user_id)
			else:
				existingClient.send(type="client_joined", client=client.as_dict())
		self.clients[client.user_id] = client

	def removeConnection(self, con: "User") -> None:
		"""Called when a user leaves the channel.

		:param con: The leaving channel member.
		"""
		if con.user_id in self.clients:
			del self.clients[con.user_id]
		for client in self.clients.values():
			if client.protocol.protocol_version == 1:  # pragma: no cover - protocol v1 is not tested
				client.send(type="client_left", user_id=con.user_id)
			else:
				client.send(type="client_left", client=con.as_dict())
		if not self.clients:
			self.serverState.remove_channel(self.key)

	def pingClients(self) -> None:
		"""Ping clients to ensure they're still connected."""
		self.sendToClients({"type": "ping"})

	def sendToClients(
		self,
		obj: dict[str, Any],
		exclude: "User | None" = None,
		origin: int | None = None,
	) -> None:
		"""Broadcast a message to all users in this channel.

		:param obj: Message to send.
		:param exclude: User to exclude from the broadcast, defaults to None
		:param origin: Originating user, defaults to None
		"""
		for client in self.clients.values():
			if client is exclude:
				continue
			client.send(origin=origin, **obj)


class Handler(LineReceiver):
	"""Handle sending and receiving messages."""

	delimiter = b"\n"
	connection_id = 0
	MAX_LENGTH = 20 * 1048576

	def __init__(self) -> None:
		self.connection_id = Handler.connection_id + 1
		Handler.connection_id += 1
		self.protocol_version = 1

	def connectionMade(self) -> None:
		"""Called when a user first connects."""
		logger.info("Connection %d from %s", self.connection_id, self.transport.getPeer())
		# We use a non-tcp transport for unit testing,
		# which doesn't support setTcpNoDelay.
		if isinstance(self.transport, ITCPTransport):  # pragma: no cover
			# Methods of Zope interfaces don't take self, so pyright thinks this call has too many arguments
			self.transport.setTcpNoDelay(True)  # pyright: ignore [reportCallIssue]
		self.bytes_sent = 0
		self.bytes_received = 0
		self.user = User(protocol=self)
		self.cleanup_timer = reactor.callLater(INITIAL_TIMEOUT, self.cleanup)
		self.user.send_motd()

	def connectionLost(self, reason: Failure = connectionDone) -> None:
		"""Called when the connection is dropped."""
		logger.info(
			"Connection %d lost, bytes sent: %d received: %d",
			self.connection_id,
			self.bytes_sent,
			self.bytes_received,
		)
		self.user.connection_lost()
		if (
			self.cleanup_timer is not None and not self.cleanup_timer.cancelled
		):  # pragma: no cover - not sure how to trigger this
			self.cleanup_timer.cancel()

	def lineReceived(self, line: bytes) -> None:
		"""Called when a new line (a command) has been received.

		:param line: The incoming line.
		"""
		self.bytes_received += len(line)
		try:
			parsed = json.loads(line)
			if not isinstance(parsed, dict):
				raise ValueError
		except ValueError:
			logger.warning("Unable to parse %r", line)
			self.transport.loseConnection()
			return
		cast(dict[str, Any], parsed)
		if "type" not in parsed:
			logger.warning("Invalid object received: %r", parsed)
			return
		parsed.pop("origin", None)  # Remove an existing origin, we know where the message comes from.
		if self.user.channel is not None:
			self.user.channel.sendToClients(parsed, exclude=self.user, origin=self.user.user_id)
			return
		elif not hasattr(self, "do_" + parsed["type"]):
			logger.warning("No function for type %s", parsed["type"])
			return
		getattr(self, "do_" + parsed["type"])(parsed)

	def do_join(self, obj: dict[str, str]) -> None:
		"""Called when receiving a "join" message."""
		if (
			"channel" not in obj
			or not obj["channel"]
			or "connection_type" not in obj
			or not obj["connection_type"]
		):
			self.send(type="error", error="invalid_parameters")
			return
		self.user.join(obj["channel"], connection_type=obj["connection_type"])
		self.cleanup_timer.cancel()

	def do_protocol_version(self, obj: dict[str, int | str]) -> None:
		"""Called when a "protocol_version" message is received."""
		# TODO: Why don't we send an error message back?
		if "version" not in obj:
			return
		try:
			self.protocol_version = int(obj["version"])
		except ValueError:
			return

	def do_generate_key(self, obj: dict[str, str]) -> None:
		"""Called when a "generate_key" message is received."""
		self.user.generate_key()

	def send(self, origin: int | None = None, **msg: Any) -> None:
		"""Send a message.

		:param origin: Originating user of the message, defaults to None
		"""
		if self.protocol_version > 1 and origin:
			msg["origin"] = origin
		obj = json.dumps(msg).encode("ascii")
		self.bytes_sent += len(obj)
		self.sendLine(obj)

	def cleanup(self) -> None:
		"""Clean up this connection."""
		logger.info("Connection %d timed out", self.connection_id)
		self.transport.abortConnection()
		self.cleanup_timer = None


class User:
	"""A single connected user."""

	user_id = 0

	def __init__(self, protocol: Handler) -> None:
		"""Initializer.

		:param protocol: The Handler through which this user connected.
		"""
		self.protocol = protocol
		self.channel: Channel | None = None
		self.server_state: ServerState = self.protocol.factory.server_state
		self.connection_type = None
		self.user_id = User.user_id + 1
		User.user_id += 1

	def as_dict(self) -> UserDict:
		"""Get a representation of this user suitable for sending over the wire."""
		return UserDict(id=self.user_id, connection_type=self.connection_type)

	def generate_key(self) -> str | None:
		"""Generate a key for the user.

		:return: A channel key, or None if too many keys have been requested.

		:postcondition: The key will be temporarily persisted so that future key generation requests don't result in duplicate keys.
		"""
		ip: str = self.protocol.transport.getPeer().host  # type: ignore
		if ip in self.server_state.generated_ips and time.time() - self.server_state.generated_ips[ip] < 1:
			self.send(type="error", message="too many keys")
			self.protocol.transport.loseConnection()
			return
		key = "".join([random.choice(string.digits) for _ in range(7)])
		while key in self.server_state.generated_keys or key in self.server_state.channels.keys():
			key = "".join([random.choice(string.digits) for _ in range(7)])
		self.server_state.generated_keys.add(key)
		self.server_state.generated_ips[ip] = time.time()
		reactor.callLater(GENERATED_KEY_EXPIRATION_TIME, lambda: self.server_state.generated_keys.remove(key))
		if key:  # pragma: no cover - I can't work out why this branch is here. When would this be False?
			self.send(type="generate_key", key=key)
		return key

	def connection_lost(self) -> None:
		"""Remove this user when they disconnect."""
		if (
			self.channel is not None
		):  # pragma: no branch - we don't care about the alternative, as it's a no-op
			self.channel.removeConnection(self)

	def join(self, channel: str, connection_type: str) -> None:
		"""Add this user to a channel.

		:param channel: Key of the channel to join. If no channel with this key exists, a new channel will be created.
		:param connection_type: Leader ("master") or follower ("slave").
		"""
		if self.channel:
			self.send(type="error", error="already_joined")
			return
		self.connection_type = connection_type
		self.channel = self.server_state.find_or_create_channel(channel)
		self.channel.addClient(self)

	# TODO: Work out if this is ever called.
	def do_generate_key(self) -> None:  # pragma: no cover
		"""Not sure what calls this?"""
		key = self.generate_key()
		if key:
			self.send(type="generate_key", key=key)

	def send(self, **obj: Any) -> None:
		"""Send a message to this user."""
		self.protocol.send(**obj)

	def send_motd(self) -> None:
		"""Send the message of the day to this user."""
		if self.server_state.motd is not None:
			self.send(type="motd", motd=self.server_state.motd)


class RemoteServerFactory(Factory):
	"""Factory to add common functionality to connections."""

	def __init__(self, server_state: "ServerState") -> None:
		"""Initializer.

		:param server_state: Status tracking object.
		"""
		self.server_state = server_state

	def ping_connected_clients(self) -> None:
		"""Ping all users in all channels to determine if they're still connected."""
		for channel in self.server_state.channels.values():
			channel.pingClients()


class ServerState:
	"""Object that tracks the status of the server."""

	def __init__(self) -> None:
		self.channels: dict[str, Channel] = {}
		# Set of already generated keys
		self.generated_keys: set[str] = set()
		# Mapping of IPs to generated time for people who have generated keys.
		self.generated_ips: dict[str, float] = {}
		self.motd: str | None = None

	def remove_channel(self, channel: str) -> None:
		"""Close a channel.

		:param channel: Key of the channel to remove.
		"""
		del self.channels[channel]

	def find_or_create_channel(self, name: str) -> Channel:
		"""Find an existing channel, or create one if one doesn't already exist.

		:param name: Key of the channel to find/create.
		:return: The found or created channel.
		"""
		if name in self.channels:
			channel = self.channels[name]
		else:
			channel = Channel(name, self)
			self.channels[name] = channel
		return channel


class Options(usage.Options):
	optParameters = [
		["certificate", "c", "cert", "SSL certificate"],
		["privkey", "k", "privkey", "SSL private key"],
		["chain", "C", "chain", "SSL chain"],
		["motd", "m", "motd", "MOTD"],
		["network-interface", "i", "::", "Interface to listen on"],
		["port", "p", "6837", "Server port"],
	]


# Exclude from coverage as it's hard to unit test.
def main() -> Deferred[None]:  # pragma: no cover
	# Read options from CLI.
	config = Options()
	config.parseOptions()
	# Open SSL keys.
	privkey = open(config["privkey"]).read()
	certData = open(config["certificate"], "rb").read()
	chain = open(config["chain"], "rb").read()
	log.startLogging(sys.stdout)
	# Initialise encryption
	privkey = crypto.load_privatekey(crypto.FILETYPE_PEM, privkey)
	certificate = crypto.load_certificate(crypto.FILETYPE_PEM, certData)
	chain = crypto.load_certificate(crypto.FILETYPE_PEM, chain)
	context_factory = ssl.CertificateOptions(
		privateKey=privkey,
		certificate=certificate,
		extraCertChain=[chain],
	)
	# Initialise the server state machine
	state = ServerState()
	if os.path.exists(config["motd"]):
		with open(config["motd"], encoding="utf-8") as fp:
			state.motd = fp.read().strip()
	else:
		state.motd = None
	# Set up the machinery of the server.
	factory = RemoteServerFactory(state)
	looper = LoopingCall(factory.ping_connected_clients)
	looper.start(PING_INTERVAL)
	factory.protocol = Handler
	# Start running the server.
	reactor.listenSSL(int(config["port"]), factory, context_factory, interface=config["network-interface"])
	reactor.run()
	return defer.Deferred()


if __name__ == "__main__":
	res = main()
