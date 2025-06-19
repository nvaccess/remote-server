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
	"""Typed dictionary representing a user.

	Keys in this dictionary cannot be renamed, as clients rely on them.
	"""

	id: int
	connection_type: str | None


class Message(TypedDict):
	"""Type hints for protocol messages.

	Keys in this dictionary cannot be renamed, as clients rely on them.
	"""

	type: str


class Channel:
	"""Collection of connected users in the one "session"."""

	def __init__(self, key: str, serverState: "ServerState | None" = None) -> None:
		"""Constructor

		:param key: Unique identifier of this channel.
		:param serverState: Server state, defaults to None
		"""
		self.clients: OrderedDict[int, User] = OrderedDict()
		self.key = key
		self.serverState = serverState

	def addClient(self, client: "User") -> None:
		"""Joined when a new user wants to join the channel.

		:param client: The new channel member.
		"""
		if client.protocol.protocolVersion == 1:  # pragma: no cover - protocol v1 is not tested
			ids = [c.userId for c in self.clients.values()]
			msg = dict(type="channel_joined", channel=self.key, user_ids=ids, origin=client.userId)
		else:
			clients = [i.asDict() for i in self.clients.values()]
			msg = dict(type="channel_joined", channel=self.key, origin=client.userId, clients=clients)
		client.send(**msg)
		for existingClient in self.clients.values():
			if existingClient.protocol.protocolVersion == 1:  # pragma: no cover - protocol v1 is not tested
				existingClient.send(type="client_joined", user_id=client.userId)
			else:
				existingClient.send(type="client_joined", client=client.asDict())
		self.clients[client.userId] = client

	def removeConnection(self, con: "User") -> None:
		"""Called when a user leaves the channel.

		:param con: The leaving channel member.
		"""
		if con.userId in self.clients:
			del self.clients[con.userId]
		for client in self.clients.values():
			if client.protocol.protocolVersion == 1:  # pragma: no cover - protocol v1 is not tested
				client.send(type="client_left", user_id=con.userId)
			else:
				client.send(type="client_left", client=con.asDict())
		if not self.clients:
			self.serverState.removeChannel(self.key)

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
	connectionId = 0
	MAX_LENGTH = 20 * 1048576

	def __init__(self) -> None:
		self.connectionId = Handler.connectionId + 1
		Handler.connectionId += 1
		self.protocolVersion = 1

	def connectionMade(self) -> None:
		"""Called when a user first connects."""
		logger.info("Connection %d from %s", self.connectionId, self.transport.getPeer())
		# We use a non-tcp transport for unit testing,
		# which doesn't support setTcpNoDelay.
		if isinstance(self.transport, ITCPTransport):  # pragma: no cover
			# Methods of Zope interfaces don't take self, so pyright thinks this call has too many arguments
			self.transport.setTcpNoDelay(True)  # pyright: ignore [reportCallIssue]
		self.bytesSent = 0
		self.bytesReceived = 0
		self.user = User(protocol=self)
		self.cleanupTimer = reactor.callLater(INITIAL_TIMEOUT, self.cleanup)
		self.user.sendMotd()

	def connectionLost(self, reason: Failure = connectionDone) -> None:
		"""Called when the connection is dropped."""
		logger.info(
			"Connection %d lost, bytes sent: %d received: %d",
			self.connectionId,
			self.bytesSent,
			self.bytesReceived,
		)
		self.user.connectionLost()
		if (
			self.cleanupTimer is not None and not self.cleanupTimer.cancelled
		):  # pragma: no cover - not sure how to trigger this
			self.cleanupTimer.cancel()

	def lineReceived(self, line: bytes) -> None:
		"""Called when a new line (a command) has been received.

		:param line: The incoming line.
		"""
		self.bytesReceived += len(line)
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
			self.user.channel.sendToClients(parsed, exclude=self.user, origin=self.user.userId)
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
		self.user.join(obj["channel"], connectionType=obj["connection_type"])
		self.cleanupTimer.cancel()

	def do_protocol_version(self, obj: dict[str, int | str]) -> None:
		"""Called when a "protocol_version" message is received."""
		# TODO: Why don't we send an error message back?
		if "version" not in obj:
			return
		try:
			self.protocolVersion = int(obj["version"])
		except ValueError:
			return

	def do_generate_key(self, obj: dict[str, str]) -> None:
		"""Called when a "generate_key" message is received."""
		self.user.generateKey()

	def send(self, origin: int | None = None, **msg: Any) -> None:
		"""Send a message.

		:param origin: Originating user of the message, defaults to None
		"""
		if self.protocolVersion > 1 and origin:
			msg["origin"] = origin
		obj = json.dumps(msg).encode("ascii")
		self.bytesSent += len(obj)
		self.sendLine(obj)

	def cleanup(self) -> None:
		"""Clean up this connection."""
		logger.info("Connection %d timed out", self.connectionId)
		self.transport.abortConnection()
		self.cleanupTimer = None


class User:
	"""A single connected user."""

	userId = 0

	def __init__(self, protocol: Handler) -> None:
		"""Initializer.

		:param protocol: The Handler through which this user connected.
		"""
		self.protocol = protocol
		self.channel: Channel | None = None
		self.serverState: ServerState = self.protocol.factory.serverState
		self.connectionType = None
		self.userId = User.userId + 1
		User.userId += 1

	def asDict(self) -> UserDict:
		"""Get a representation of this user suitable for sending over the wire."""
		return UserDict(id=self.userId, connection_type=self.connectionType)

	def generateKey(self) -> str | None:
		"""Generate a key for the user.

		:return: A channel key, or None if too many keys have been requested.

		:postcondition: The key will be temporarily persisted so that future key generation requests don't result in duplicate keys.
		"""
		ip: str = self.protocol.transport.getPeer().host  # type: ignore
		if ip in self.serverState.generatedIps and time.time() - self.serverState.generatedIps[ip] < 1:
			self.send(type="error", message="too many keys")
			self.protocol.transport.loseConnection()
			return
		key = "".join([random.choice(string.digits) for _ in range(7)])
		while key in self.serverState.generatedKeys or key in self.serverState.channels.keys():
			key = "".join([random.choice(string.digits) for _ in range(7)])
		self.serverState.generatedKeys.add(key)
		self.serverState.generatedIps[ip] = time.time()
		reactor.callLater(GENERATED_KEY_EXPIRATION_TIME, lambda: self.serverState.generatedKeys.remove(key))
		if key:  # pragma: no cover - I can't work out why this branch is here. When would this be False?
			self.send(type="generate_key", key=key)
		return key

	def connectionLost(self) -> None:
		"""Remove this user when they disconnect."""
		if (
			self.channel is not None
		):  # pragma: no branch - we don't care about the alternative, as it's a no-op
			self.channel.removeConnection(self)

	def join(self, channel: str, connectionType: str) -> None:
		"""Add this user to a channel.

		:param channel: Key of the channel to join. If no channel with this key exists, a new channel will be created.
		:param connectionType: Leader ("master") or follower ("slave").
		"""
		if self.channel:
			self.send(type="error", error="already_joined")
			return
		self.connectionType = connectionType
		self.channel = self.serverState.findOrCreateChannel(channel)
		self.channel.addClient(self)

	# TODO: Work out if this is ever called.
	def do_generate_key(self) -> None:  # pragma: no cover
		"""Not sure what calls this?"""
		key = self.generateKey()
		if key:
			self.send(type="generate_key", key=key)

	def send(self, **obj: Any) -> None:
		"""Send a message to this user."""
		self.protocol.send(**obj)

	def sendMotd(self) -> None:
		"""Send the message of the day to this user."""
		if self.serverState.motd is not None:
			self.send(type="motd", motd=self.serverState.motd)


class RemoteServerFactory(Factory):
	"""Factory to add common functionality to connections."""

	def __init__(self, serverState: "ServerState") -> None:
		"""Initializer.

		:param serverState: Status tracking object.
		"""
		self.serverState = serverState

	def pingConnectedClients(self) -> None:
		"""Ping all users in all channels to determine if they're still connected."""
		for channel in self.serverState.channels.values():
			channel.pingClients()


class ServerState:
	"""Object that tracks the status of the server."""

	def __init__(self) -> None:
		self.channels: dict[str, Channel] = {}
		# Set of already generated keys
		self.generatedKeys: set[str] = set()
		# Mapping of IPs to generated time for people who have generated keys.
		self.generatedIps: dict[str, float] = {}
		self.motd: str | None = None

	def removeChannel(self, channel: str) -> None:
		"""Close a channel.

		:param channel: Key of the channel to remove.
		"""
		del self.channels[channel]

	def findOrCreateChannel(self, name: str) -> Channel:
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
	contextFactory = ssl.CertificateOptions(
		privateKey=privkey,
		certificate=certificate,
		extraCertChain=[chain],
	)
	# Initialise the server state machine
	state = ServerState()
	if os.path.isfile(config["motd"]):
		with open(config["motd"], "r", encoding="utf-8") as fp:
			state.motd = fp.read().strip()
	else:
		state.motd = None
	# Set up the machinery of the server.
	factory = RemoteServerFactory(state)
	looper = LoopingCall(factory.pingConnectedClients)
	looper.start(PING_INTERVAL)
	factory.protocol = Handler
	# Start running the server.
	reactor.listenSSL(int(config["port"]), factory, contextFactory, interface=config["network-interface"])
	reactor.run()
	return defer.Deferred()


if __name__ == "__main__":
	res = main()
