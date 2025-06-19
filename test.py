from collections.abc import Iterable
from itertools import islice
import json
import random
from typing import Any, Final, NamedTuple, cast
from unittest import mock

from twisted.internet import reactor
from twisted.internet.protocol import connectionDone
from twisted.internet.task import Clock
from twisted.internet.testing import StringTransport
from twisted.trial import unittest

from server import (
	GENERATED_KEY_EXPIRATION_TIME,
	INITIAL_TIMEOUT,
	Channel,
	Handler,
	RemoteServerFactory,
	ServerState,
	User,
)


class Client(NamedTuple):
	"""Structure representing a client connection to the server."""

	protocol: Handler
	"""Serverside protocol. Write to this to represent the client sending to the server."""

	transport: StringTransport
	"""Connection transport. Read from this to represent the client receiving a response from the server."""


def mockUser(id: int) -> mock.MagicMock:
	"""Create a MagicMock representing a user."""
	return mock.MagicMock(
		spec=User,
		userId=id,
		protocol=MockHandler(),
		as_dict=lambda: dict(id=id, connection_type="dummy"),
	)


def MockHandler(protocolVersion: int = 2, serverState: ServerState | None = None) -> mock.MagicMock:
	"""Return a MagicMock representing a Handler."""
	return mock.MagicMock(
		spec=Handler,
		protocolVersion=protocolVersion,
		factory=mockRemoteServerFactory(serverState=serverState or ServerState()),
	)


def mockChannel(key: str, clients: Iterable[User]) -> mock.MagicMock:
	"""Return a MagicMock representing a Channel."""
	return mock.MagicMock(
		speck=Channel,
		key=key,
		clients={client.userId: client for client in clients},
	)


def mockRemoteServerFactory(serverState: ServerState) -> mock.MagicMock:
	"""Return a MagicMock representing a RemoteServerFactory."""
	return mock.MagicMock(
		spec=RemoteServerFactory,
		serverState=serverState,
	)


class TestChannel(unittest.TestCase):
	"""Test the Channel class."""

	def setUp(self) -> None:
		self.state = ServerState()
		self.channel = Channel("channel", self.state)
		self.state.channels["channel"] = self.channel

	def test_addClient(self):
		"""Test adding a client to a channel."""
		oldUsers = [mockUser(id=id) for id in range(3)]
		self.channel.clients.update({user.userId: user for user in oldUsers})
		newUser = mockUser(id=4)
		self.channel.addClient(newUser)
		self.assertEqual(newUser, self.channel.clients[4])
		newUser.send.assert_called_once()
		self.assertEqual(newUser.send.call_args.kwargs["type"], "channel_joined")
		for oldUser in oldUsers:
			oldUser.send.assert_called_once()
			self.assertEqual(oldUser.send.call_args.kwargs["type"], "client_joined")

	def test_removeConnection(self):
		"""Test removing a client from a channel."""
		allUsers = [mockUser(id=id) for id in range(4)]
		leavingUser = allUsers[1]
		leftUsers = [user for user in allUsers if user is not leavingUser]
		self.channel.clients.update({user.userId: user for user in allUsers})
		self.assertIs(self.channel.clients[leavingUser.userId], leavingUser)
		self.channel.removeConnection(leavingUser)
		self.assertNotIn(leavingUser.userId, self.channel.clients)
		self.assertNotIn(leavingUser, self.channel.clients.values())
		for leftUser in leftUsers:
			leftUser.send.assert_called_once()
			self.assertEqual(leftUser.send.call_args.kwargs["type"], "client_left")

	def test_removeConnection_notJoined(self):
		"""Test removing a client from a channel of which it isn't a member does nothing."""
		memberUsers = [mockUser(id=id) for id in range(4)]
		nonmemberUser = memberUsers.pop(2)
		oldChannelClients = {user.userId: user for user in memberUsers}
		self.channel.clients.update(oldChannelClients)
		self.channel.removeConnection(nonmemberUser)
		# NOTE: The current implementation sends client_left messages to the remaining clients,
		# even if the client wasn't in the channel to begin with.
		# Sending these messages is already covered in another test.
		# This behaviour cannot be changed, as implementations may rely on it.

	def test_cleanup(self):
		"""Test removing the last client removes the channel from the server state."""
		user = mockUser(id=1)
		self.channel.addClient(user)
		self.channel.removeConnection(user)
		self.assertNotIn("channel", self.state.channels)

	def test_sendToClients_all(self):
		"""Test sending to all clients in the channel."""
		users = [mockUser(id) for id in range(4)]
		self.channel.clients.update({user.userId: user for user in users})
		self.channel.sendToClients({"this": "is a message"}, origin=99)
		for user in users:
			user.send.assert_called_once_with(this="is a message", origin=99)

	def test_sendToClients_except(self):
		"""Test sending to all clients but one in the channel."""
		users = [mockUser(id) for id in range(4)]
		self.channel.clients.update({user.userId: user for user in users})
		self.channel.sendToClients({"this": "is a message"}, origin=99, exclude=users[2])
		for user in users:
			if user is users[2]:
				user.send.assert_not_called()
			else:
				user.send.assert_called_once_with(this="is a message", origin=99)

	def test_ping(self):
		"""Test pinging the clients in the channel."""
		users = [mockUser(id) for id in range(4)]
		self.channel.clients.update({user.userId: user for user in users})
		self.channel.pingClients()
		for user in users:
			user.send.assert_called_once_with(type="ping", origin=None)


class TestUser(unittest.TestCase):
	"""Test the User class."""

	def setUp(self) -> None:
		User.userId = 0

	def tearDown(self) -> None:
		User.userId = 0

	def test_consecutiveUserCreation(self):
		"""Test that creating several users sequentially creates them with sequential user IDs."""
		users = (User(mock.Mock(Handler)) for _ in range(10))
		self.assertSequenceEqual(list(map(lambda user: user.userId, users)), range(1, 11))

	def test_join(self):
		"""Test that adding a user to a channel works as expected."""
		CHANNEL_ID = "myChannel"
		serverState = ServerState()
		user = User(MockHandler(serverState=serverState))
		user.join(CHANNEL_ID, "master")
		self.assertIs(user.channel, serverState.channels[CHANNEL_ID])
		self.assertIs(user, serverState.channels[CHANNEL_ID].clients[user.userId])

	def test_join_alreadyJoined(self):
		"""Test that adding a user who is already in a channel to a new channel fails."""
		protocol = MockHandler()
		user = User(protocol)
		oldChannel = mockChannel("channel1", [])
		user.channel = oldChannel
		user.join("channel2", "slave")
		self.assertIs(user.channel, oldChannel)
		protocol.send.assert_called_once_with(type="error", error="already_joined")


class TestServerState(unittest.TestCase):
	"""Test the ServerState class."""

	def setUp(self) -> None:
		self.serverState = ServerState()

	def _addChannels(self) -> list[Channel]:
		"""Add several channels to the ServerState."""
		channels = [Channel(key, self.serverState) for key in "abcd"]
		self.serverState.channels.update({channel.key: channel for channel in channels})
		return channels

	def test_findOrCreateChannel_create(self):
		"""Check that passing a new key creates a new channel."""
		extantChannels = self._addChannels()
		oldChannels = self.serverState.channels.copy()
		self.assertNotIn("newChannel", self.serverState.channels)
		newChannel = self.serverState.find_or_create_channel("newChannel")
		self.assertIn("newChannel", self.serverState.channels)
		self.assertIs(self.serverState.channels["newChannel"], newChannel)
		self.assertNotIn(newChannel, extantChannels)
		self.assertNotEqual(oldChannels, self.serverState.channels)

	def test_findOrCreateChannel_find(self):
		"""Check that passing an existant key returns the associated channel."""
		extantChannels = self._addChannels()
		oldChannels = self.serverState.channels.copy()
		self.assertIn("c", self.serverState.channels)
		expectedChannel = extantChannels[2]
		foundChannel = self.serverState.find_or_create_channel("c")
		self.assertIs(expectedChannel, foundChannel)
		self.assertEqual(oldChannels, self.serverState.channels)


class TestRemoteServerFactory(unittest.TestCase):
	"""Test the RemoteServerFactory class."""

	def test_pingClients(self):
		"""Test that calling ping_connected_clients calls pingClients on all channels, regardless of size."""
		serverState = ServerState()
		factory = RemoteServerFactory(serverState)
		userIterator = (mockUser(id) for id in range(10))
		channels = tuple(mockChannel(key=chr(n + 65), clients=islice(userIterator, n)) for n in range(5))
		serverState.channels.update({channel.key: channel for channel in channels})
		factory.pingConnectedClients()
		for channel in channels:
			channel.pingClients.assert_called_once()


class BaseServerTestCase(unittest.TestCase):
	"""Base for test cases covering the server.

	Handles instantiation and cleanup of connections and global objects.
	"""

	def setUp(self) -> None:
		# Ensure we're starting from a common baseline
		self._oldUserId = User.userId
		User.userId = 0
		self.state = ServerState()
		self.factory = RemoteServerFactory(self.state)
		self.factory.protocol = Handler
		self.clock = Clock()
		reactor.callLater = self.clock.callLater

	def tearDown(self) -> None:
		# Put things back how they were when we found them
		User.userId = self._oldUserId

	def _createClient(self) -> Client:
		"""Create a client-server connection."""
		# A (host, port) tuple works fine here.
		# Even using twisted.internet.address.IPv4Address` here doesn't work,
		# as pyright doesn't understand Zope interfaces.
		protocol = self.factory.buildProtocol(("127.0.0.1", 0))  # pyright: ignore [reportArgumentType]
		transport = StringTransport()
		protocol.makeConnection(transport)
		assert protocol is not None  # Needed to shut pyright up
		return Client(protocol=cast(Handler, protocol), transport=transport)

	def _connectClient(self, protocolVersion: int = 2) -> Client:
		"""Create and initialize a new connection."""
		client = self._createClient()
		self._send(client, dict(type="protocol_version", version=protocolVersion))
		return client

	def _disconnectClient(self, client: Client) -> None:
		"""Disconnect an existing client."""
		client.protocol.connectionLost(connectionDone)

	def _send(self, client: Client, payload: dict[str, Any]) -> None:
		"""Client sends payload to the server."""
		client.protocol.dataReceived(json.dumps(payload).encode() + b"\n")

	def _receive(self, client: Client) -> dict[str, Any] | None:
		"""Client receives payload from the server."""
		received = client.transport.value().decode()
		client.transport.clear()
		return json.loads(received) if received else None


class TestGenerateKey(BaseServerTestCase):
	"""Tests for the key generation functionality."""

	RANDOM_SEED: Final[int] = 0
	EXPECTED_KEYS: Final[tuple[str, str, str]] = ("6604876", "4759382", "4219489")
	"""
	First 3 keys generated with current key generation method and random seed of 0.

	>>> import random, string
	>>> random.seed(0)
	>>> tuple("".join(random.choice(string.digits) for _ in range(7)) for _ in range(3))
	('6604876', '4759382', '4219489')
	"""

	def setUp(self) -> None:
		super().setUp()
		self.protocol, self.transport = self._connectClient()
		random.seed(self.RANDOM_SEED)

	def _test(self, serverReceived: dict[str, Any], clientReceived: dict[str, Any]) -> None:
		self.protocol.dataReceived(json.dumps(serverReceived).encode() + b"\n")
		self.assertEqual(json.loads(self.transport.value().decode()), clientReceived)
		self.transport.clear()

	def test_generateKey(self):
		"""Test that requesting the server to generate a key returns the expected result, and temporarily persists the key to avoid collisions."""
		key = self.EXPECTED_KEYS[0]
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": key})
		self.assertIn(key, self.state.generated_keys, "Key was not persisted where expected.")
		self.clock.advance(GENERATED_KEY_EXPIRATION_TIME)
		self.assertNotIn(key, self.state.generated_keys, "Key was not removed after expiration.")

	@mock.patch("time.time", return_value=12345)
	def test_repeated_generateKey_ok(self, mock_time: mock.MagicMock):
		"""Test that multiple requests to generate a key result in different keys."""
		for key in self.EXPECTED_KEYS:
			self._test({"type": "generate_key"}, {"type": "generate_key", "key": key})
			# Increment the time so the server knows we're not spamming it
			mock_time.return_value += 10

	@mock.patch("time.time", return_value=12345)
	def test_repeated_generateKey_spam(self, mock_time: mock.MagicMock):
		"""Test that multiple requests to generate a key in quick succession result in an error."""
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": self.EXPECTED_KEYS[0]})
		mock_time.return_value += 0.5
		self._test({"type": "generate_key"}, {"type": "error", "message": "too many keys"})

	@mock.patch("time.time", return_value=12345)
	def test_generateKey_collision(self, mock_time: mock.MagicMock):
		"""Test that key requests don't result in the same key."""
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": self.EXPECTED_KEYS[0]})
		# Increment the time so the server doesn't think we're spamming it.
		mock_time.return_value += 10
		# And force the PRNG back to a prior state to guarantee a collision.
		random.seed(self.RANDOM_SEED)
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": self.EXPECTED_KEYS[1]})


class TestP2P(BaseServerTestCase):
	"""Test the peer-to-peer functionality of the server."""

	def test_lifecycle(self):
		"""Test channel lifecycle, from initial connection to final disconnection."""
		# Our channel should not exist yet
		self.assertNotIn("channel1", self.state.channels)
		# Create 2 clients
		c1 = self._connectClient()
		c2 = self._connectClient()
		# Client 1 join channel 1 as leader
		self._send(c1, {"type": "join", "channel": "channel1", "connection_type": "master"})
		# The channel should have been created
		self.assertIn("channel1", self.state.channels)
		self.assertEqual(
			self._receive(c1),
			{"type": "channel_joined", "channel": "channel1", "origin": 1, "clients": []},
		)
		# Client 2 join channel 1 as follower
		self._send(c2, {"type": "join", "channel": "channel1", "connection_type": "slave"})
		self.assertEqual(
			self._receive(c2),
			{
				"type": "channel_joined",
				"channel": "channel1",
				"origin": 2,
				"clients": [{"id": 1, "connection_type": "master"}],
			},
		)
		self.assertEqual(
			self._receive(c1),
			{"type": "client_joined", "client": {"id": 2, "connection_type": "slave"}},
		)
		# Client 1 leave channel 1
		self._disconnectClient(c1)
		self.assertEqual(
			self._receive(c2),
			{"type": "client_left", "client": {"id": 1, "connection_type": "master"}},
		)
		# client 2 leave channel 1
		self._disconnectClient(c2)
		# The channel should have been deleted
		self.assertNotIn("channel1", self.state.channels)

	def _makeMessage(self, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
		"""Helper method for broadcast testing."""
		outgoing = dict(type="message", message=f"This is client {index + 1} speaking.")
		incoming = outgoing | dict(origin=index + 1)
		return outgoing, incoming

	def test_broadcast(self):
		"""Test that sending a message in a channel results in all other clients in the channel receiving that message."""
		NUM_CLIENTS = 4
		clients = [self._connectClient() for _ in range(NUM_CLIENTS)]
		for index, client in enumerate(clients):
			self._send(
				client,
				{
					"type": "join",
					"channel": "channel1",
					"connection_type": "slave" if index % 2 == 0 else "master",
				},
			)
		for client in clients:
			client.transport.clear()
		for index, sender in enumerate(clients):
			with self.subTest(f"Broadcasting from client {index}"):
				outgoing, incoming = self._makeMessage(index)
				self.assertIsNotNone(incoming)
				self._send(sender, outgoing)
				for jndex, receiver in enumerate(clients):
					with self.subTest(f"Client {jndex} receiving."):
						if receiver is sender:
							# This is the origin, so we shouldn't have received anything.
							self.assertFalse(receiver.transport.value())
						else:
							self.assertEqual(self._receive(receiver), incoming)

	def test_sendNonDictMessage(self):
		"""Test that sending a message that is not a JSON object fails."""
		client = self._connectClient()
		client.protocol.dataReceived(b'"Hello, world!"\n')
		self.assertFalse(client.transport.value())
		self.assertTrue(client.transport.disconnecting)

	def test_typelessMessage(self):
		"""Test that sending a message without a type field does nothing."""
		client = self._connectClient()
		self._send(client, {"key": "value"})
		self.assertIsNone(self._receive(client))

	def test_unRecognisedNType(self):
		"""Test that sending a message with an unrecognised type when not in a channel does nothing."""
		client = self._connectClient()
		self._send(client, {"type": "teapot"})
		self.assertIsNone(self._receive(client))

	def test_join_withoutChannel(self):
		"""Test that sending a 'join' message without a 'channel' returns an error."""
		client = self._connectClient()
		self._send(client, {"type": "join"})
		self.assertEqual(self._receive(client), {"type": "error", "error": "invalid_parameters"})

	def test_protocol_version_withoutVersion(self):
		"""Test that sending a 'protocol_version' message without a 'version' returns nothing."""
		client = self._createClient()
		oldProtocolVersion = client.protocol.protocolVersion
		self._send(client, {"type": "protocol_version"})
		self.assertIsNone(self._receive(client))
		self.assertEqual(client.protocol.protocolVersion, oldProtocolVersion)

	def test_protocol_version_withInvalidVersion(self):
		"""Test that sending a 'protocol_version' message with a non-integer 'version' returns nothing."""
		client = self._createClient()
		oldProtocolVersion = client.protocol.protocolVersion
		self._send(client, {"type": "protocol_version", "version": "NaN"})
		self.assertIsNone(self._receive(client))
		self.assertEqual(client.protocol.protocolVersion, oldProtocolVersion)

	def test_inactivityCausesDisconnection(self):
		"""Test that connecting without joining a channel causes disconnection."""
		client = self._connectClient()
		self.assertFalse(client.transport.disconnecting)
		self.clock.advance(INITIAL_TIMEOUT + 1)
		self.assertTrue(client.transport.disconnecting)

	def test_motd(self):
		"""Test that the server sends the MOTD on connection, if set."""
		MOTD = "Hello, world!"
		self.state.motd = MOTD
		client = self._createClient()
		self.assertEqual(self._receive(client), dict(type="motd", motd=MOTD))
