from twisted.trial import unittest
from twisted.internet.testing import StringTransport
from twisted.internet.task import Clock
from twisted.internet import reactor
from unittest import mock
from server import RemoteServerFactory, Channel, User, Handler, ServerState, GENERATED_KEY_EXPIRATION_TIME 
import json

class TestUser(unittest.TestCase):
	def setUp(self) -> None:
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
	def setUp(self) -> None:
		self.clock = Clock()
		reactor.callLater = self.clock.callLater
		self.state = ServerState()
		factory = RemoteServerFactory(self.state)
		factory.protocol = Handler
		self.protocol = factory.buildProtocol(('127.0.01', 0))
		self.transport = StringTransport()
		self.protocol.makeConnection(self.transport)
	
	def _test(self, serverReceived: bytes, clientReceived: bytes) -> None:
		self.protocol.dataReceived(b'{"type": "protocol_version", "version": 2}\n')
		self.protocol.dataReceived(json.dumps(serverReceived).encode() + b'\n')
		self.assertEqual(json.loads(self.transport.value().decode()), clientReceived)
		self.protocol.dataReceived(json.dumps(None).encode())

	def test_generateKey(self):
		import random
		random.seed(0)
		key = "6604876"
		self._test({"type": "generate_key"}, {"type": "generate_key", "key": key})
		self.assertIn(key, self.state.generated_keys)
		self.clock.advance(GENERATED_KEY_EXPIRATION_TIME)
		self.assertNotIn(key, self.state.generated_keys)
