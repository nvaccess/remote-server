from twisted.trial import unittest
from unittest import mock
from server import RemoteServerFactory, Channel, User, Handler, ServerState

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

