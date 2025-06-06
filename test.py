import json
import random
from typing import Any, Final, NamedTuple
from unittest import mock

from twisted.internet import reactor
from twisted.internet.protocol import Protocol, connectionDone
from twisted.internet.task import Clock
from twisted.internet.testing import StringTransport
from twisted.trial import unittest

from server import GENERATED_KEY_EXPIRATION_TIME, Channel, Handler, RemoteServerFactory, ServerState, User


class Client(NamedTuple):
	"""Structure representing a client connection to the server."""

	protocol: Protocol
	"""Serverside protocol. Write to this to represent the client sending to the server."""

	transport: StringTransport
	"""Connection transport. Read from this to represent the client receiving a response from the server."""


def mockUser(id: int) -> mock.MagicMock:
	return mock.MagicMock(
		spec=User,
		user_id=id,
		protocol = MockHandler(),
	)

def MockHandler(protocol_version: int=2) -> mock.MagicMock:
	return mock.MagicMock(
		spec=Handler,
		protocol_version=protocol_version
	)

class TestUser(unittest.TestCase):
	def setUp(self) -> None:
		User.user_id = 0
	
	def tearDown(self) -> None:
		User.user_id = 0
		
	def test_consecutiveUserCreation(self):
		"""Test that creating several users sequentially creates them with sequential user IDs."""
		users = (User(mock.Mock(Handler)) for _ in range(10))
		self.assertSequenceEqual(list(map(lambda user: user.user_id, users)), range(1, 11))


class TestServerState(unittest.TestCase):
	def setUp(self) -> None:
		self.serverState = ServerState()

	def _addChannels(self) -> list[Channel]:
		channels = [
			Channel(key, self.serverState)
			for key in 'abcd'
		]
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
		"""Check that passing an existant key returns the associated channelcreates a new channel."""
		exttantChannels = self._addChannels()
		oldChannels = self.serverState.channels.copy()
		self.assertIn('c', self.serverState.channels)
		expectedChannel = exttantChannels[2]
		foundChannel = self.serverState.find_or_create_channel('c')
		self.assertIs(expectedChannel, foundChannel)
		self.assertEqual(oldChannels, self.serverState.channels)

class BaseServerTestCase(unittest.TestCase):
	"""Base for test cases covering the server.

	Handles instantiation and cleanup of connections and global objects.
	"""

	def setUp(self) -> None:
		# Ensure we're starting from a common baseline
		self._oldUserId = User.user_id
		User.user_id = 0
		self.state = ServerState()
		self.factory = RemoteServerFactory(self.state)
		self.factory.protocol = Handler
	
	def tearDown(self) -> None:
		# Put things back how they were wen we found them
		User.user_id = self._oldUserId
	
	def _connectClient(self) -> Client:
		"""Create and initialize a new connection."""
		protocol = self.factory.buildProtocol(('127.0.0.1', 0))
		transport = StringTransport()
		protocol.makeConnection(transport)
		protocol.dataReceived(b'{"type": "protocol_version", "version": 2}\n')
		return Client(protocol=protocol, transport=transport)

	def _disconnectClient(self, client: Client) -> None:
		"""Disconnect an existing client."""
		client.protocol.connectionLost(connectionDone)


class TestGenerateKey(BaseServerTestCase):
	RANDOM_SEED: Final[int] = 0
	EXPECTED_KEYS: Final[tuple[str, str, str]] = ('6604876', '4759382', '4219489')
	"""
	First 3 keys generated with current key generation method and random seed of 0.

	>>> import random, string
	>>> random.seed(0)
	>>> tuple("".join(random.choice(string.digits) for _ in range(7)) for _ in range(3))
	('6604876', '4759382', '4219489')
	"""

	def setUp(self) -> None:
		super().setUp()
		self.clock = Clock()
		reactor.callLater = self.clock.callLater
		self.protocol, self.transport = self._connectClient()
		random.seed(self.RANDOM_SEED)
	
	def _test(self, serverReceived: bytes, clientReceived: bytes) -> None:
		self.protocol.dataReceived(json.dumps(serverReceived).encode() + b'\n')
		self.assertEqual(json.loads(self.transport.value().decode()), clientReceived)
		self.transport.clear()

	def test_generateKey(self):
		"""Test that requesting the server to generate a key returns the expected result, and temporarily persists the key to avoid collisions."""
		key = self.EXPECTED_KEYS[0]
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": key})
		self.assertIn(key, self.state.generated_keys, "Key was not persisted where expected.")
		self.clock.advance(GENERATED_KEY_EXPIRATION_TIME)
		self.assertNotIn(key, self.state.generated_keys, "Key was not removed after expiration.")
	
	@mock.patch('time.time', return_value=12345)
	def test_repeated_generateKey_ok(self, mock_time: mock.MagicMock):
		"""Test that multiple requests to generate a key result in different keys."""
		for key in self.EXPECTED_KEYS:
			self._test({"type": "generate_key"}, {"type": "generate_key", "key": key})
			# Increment the time so the server knows we're not spamming it
			mock_time.return_value += 10
	
	@mock.patch('time.time', return_value=12345)
	def test_repeated_generateKey_spam(self, mock_time: mock.MagicMock):
		"""Test that multiple requests to generate a key in quick succession result in an error."""
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": self.EXPECTED_KEYS[0]})
		mock_time.return_value += 0.5
		self._test({"type": "generate_key"}, {"type": "error", "message": "too many keys"})

	@mock.patch('time.time', return_value=12345)
	def test_generateKey_collision(self, mock_time: mock.MagicMock):
		"""Test that key requests don't result in the same key."""
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": self.EXPECTED_KEYS[0]})
		# Increment the time so the server doesn't think we're spamming it.
		mock_time.return_value += 10
		# And force the PRNG back to a prior state to guarantee a collision.
		random.seed(self.RANDOM_SEED)
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": self.EXPECTED_KEYS[1]})


class TestP2P(BaseServerTestCase):
	def _send(self, client: Client, payload: dict[str, Any]) -> None:
		client.protocol.dataReceived(json.dumps(payload).encode() + b'\n')
	
	def _receive(self, client: Client) -> dict[str, Any]:
		received = json.loads(client.transport.value().decode())
		client.transport.clear()
		return received

	def test_lifecycle(self):
		"""Test channel lifecycle, from initial connection to final disconnection."""
		# Our channel should not exist yet
		self.assertNotIn('channel1', self.state.channels)
		# Create 2 clients
		c1 = self._connectClient()
		c2 = self._connectClient()
		# Client 1 join channel 1 as leader
		self._send(c1, {'type': 'join', 'channel': 'channel1', 'connection_type': 'master'})
		# The channel should have been created
		self.assertIn('channel1', self.state.channels)
		self.assertEqual(self._receive(c1), {'type': 'channel_joined', 'channel': 'channel1', 'origin': 1, 'clients': []})
		# Client 2 join channel 1 as follower
		self._send(c2, {'type': 'join', 'channel': 'channel1', 'connection_type': 'slave'})
		self.assertEqual(self._receive(c2), {'type': 'channel_joined', 'channel': 'channel1', 'origin': 2, 'clients': [{'id': 1, 'connection_type': 'master'}]})
		self.assertEqual(self._receive(c1), {'type':'client_joined', 'client': {'id': 2, 'connection_type': 'slave'}})
		# Client 1 leave channel 1
		self._disconnectClient(c1)
		self.assertEqual(self._receive(c2), {'type':'client_left', 'client': {'id': 1, 'connection_type': 'master'}})
		# client 2 leave channel 1
		self._disconnectClient(c2)
		# The channel should have been deleted
		self.assertNotIn('channel1', self.state.channels)
	
	def _makeMessage(self, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
		outgoing = dict(type="message", message=f"This is client {index+1} speaking.")
		incoming = outgoing | dict(origin=index+1)
		return outgoing, incoming
	
	def test_broadcast(self):
		NUM_CLIENTS = 4
		clients = [self._connectClient() for _ in range(NUM_CLIENTS)]
		for index, client in enumerate(clients):
			self._send(client, {'type': 'join', 'channel': 'channel1', 'connection_type': 'slave' if index % 2 == 0 else 'master'})
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


class TestChannel(unittest.TestCase):
	def setUp(self) -> None:
		self.state = ServerState()
		self.channel = Channel("channel", self.state)
		self.state.channels["channel"] = self.channel

	def test_addClient(self):
		oldUsers = [mockUser(id=id) for id in range(3)]
		self.channel.clients.update({user.user_id: user for user in oldUsers})
		newUser = mockUser(id=4)
		self.channel.add_client(newUser)
		self.assertEqual(newUser, self.channel.clients[4])
		newUser.send.assert_called_once()
		self.assertEqual(newUser.send.call_args.kwargs['type'], 'channel_joined')
		for oldUser in oldUsers:
			oldUser.send.assert_called_once()
			self.assertEqual(oldUser.send.call_args.kwargs['type'], 'client_joined')
	
	def test_removeConnection(self):
		allUsers = [mockUser(id=id) for id in range(4)]
		leavingUser = allUsers[1]
		leftUsers = [user for user in allUsers if user is not leavingUser]
		self.channel.clients.update({user.user_id: user for user in allUsers})
		self.channel.remove_connection(leavingUser)
		self.assertNotIn(leavingUser.user_id, self.channel.clients)
		self.assertNotIn(leavingUser, self.channel.clients.values())
		for leftUser in leftUsers:
			leftUser.send.assert_called_once()
			self.assertEqual(leftUser.send.call_args.kwargs['type'], 'client_left')
		
	def test_cleanup(self):
		user = mockUser(id=1)
		self.channel.add_client(user)
		self.channel.remove_connection(user)
		self.assertNotIn("channel", self.state.channels)
	
	def test_sendToClients_all(self):
		users = [mockUser(id) for id in range(4)]
		self.channel.clients.update({user.user_id: user for user in users})
		self.channel.send_to_clients({"this": "is a message"}, origin=99)
		for user in users:
			user.send.assert_called_once_with(this="is a message", origin=99)

	def test_sendToClients_except(self):
		users = [mockUser(id) for id in range(4)]
		self.channel.clients.update({user.user_id: user for user in users})
		self.channel.send_to_clients({"this": "is a message"}, origin=99, exclude=users[2])
		for user in users:
			if user is users[2]:
				user.send.assert_not_called()
			else:
				user.send.assert_called_once_with(this="is a message", origin=99)
	
	def test_ping(self):
		users = [mockUser(id) for id in range(4)]
		self.channel.clients.update({user.user_id: user for user in users})
		self.channel.ping_clients()
		for user in users:
			user.send.assert_called_once_with(type='ping', origin=None)
