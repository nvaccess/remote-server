from typing import Any, Final
from twisted.trial import unittest
from twisted.internet.testing import StringTransport
from twisted.internet.task import Clock
from twisted.internet import reactor
from twisted.internet.protocol import Protocol, connectionDone
from unittest import mock
from server import RemoteServerFactory, Channel, User, Handler, ServerState, GENERATED_KEY_EXPIRATION_TIME 
import json

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


class TestGenerateKey(unittest.TestCase):
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
		import random
		self.clock = Clock()
		reactor.callLater = self.clock.callLater
		self.state = ServerState()
		factory = RemoteServerFactory(self.state)
		factory.protocol = Handler
		self.protocol = factory.buildProtocol(('127.0.01', 0))
		self.transport = StringTransport()
		self.protocol.makeConnection(self.transport)
		self.protocol.dataReceived(b'{"type": "protocol_version", "version": 2}\n')
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
		import random
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": self.EXPECTED_KEYS[0]})
		# Increment the time so the server doesn't think we're spamming it.
		mock_time.return_value += 10
		# And force the PRNG back to a prior state to guarantee a collision.
		random.seed(self.RANDOM_SEED)
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": self.EXPECTED_KEYS[1]})


class TestP2P(unittest.TestCase):
	def setUp(self) -> None:
		self.ipLSB = 1
		self.state = ServerState()
		self.factory = RemoteServerFactory(self.state)
		self.factory.protocol = Handler
		User.user_id = 0
	
	def _addClient(self) -> tuple[Protocol, StringTransport]:
		protocol = self.factory.buildProtocol((f'127.0.0.{self.ipLSB}', 0))
		self.ipLSB += 1
		transport = StringTransport()
		protocol.makeConnection(transport)
		protocol.dataReceived(b'{"type": "protocol_version", "version": 2}\n')
		return protocol, transport
	
	def _send(self, protocol: Protocol, payload: dict[str, Any]) -> None:
		protocol.dataReceived(json.dumps(payload).encode() + b'\n')
	
	def _receive(self, transport: StringTransport, payload: dict[str, Any]) -> None:
		self.assertEqual(json.loads(transport.value().decode()), payload)
		transport.clear()

	def test_lifecycle(self):
		# Our channel should not exist yet
		self.assertNotIn('channel1', self.state.channels)
		# Create 2 clients
		p1, t1 = self._addClient()
		p2, t2 = self._addClient()
		# Client 1 join channel 1 as leader
		self._send(p1, {'type': 'join', 'channel': 'channel1', 'connection_type': 'master'})
		# The channel should have been created
		self.assertIn('channel1', self.state.channels)
		self._receive(t1, {'type': 'channel_joined', 'channel': 'channel1', 'origin': 1, 'clients': []})
		# Client 2 join channel 1 as follower
		self._send(p2, {'type': 'join', 'channel': 'channel1', 'connection_type': 'slave'})
		self._receive(t2, {'type': 'channel_joined', 'channel': 'channel1', 'origin': 2, 'clients': [{'id': 1, 'connection_type': 'master'}]})
		self._receive(t1, {'type':'client_joined', 'client': {'id': 2, 'connection_type': 'slave'}})
		# Client 1 leave channel 1
		p1.connectionLost(connectionDone)
		self._receive(t2, {'type':'client_left', 'client': {'id': 1, 'connection_type': 'master'}})
		# client 2 leave channel 1
		p2.connectionLost(connectionDone)
		# The channel should have been deleted
		self.assertNotIn('channel1', self.state.channels)


