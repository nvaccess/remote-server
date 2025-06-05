from twisted.trial import unittest
from unittest import mock
from server import RemoteServerFactory, Channel, User, Handler, ServerState

class TestUser(unittest.TestCase):
	def test_consecutiveUserCreation(self):
		"""Test that creating several users sequentially creates them with sequential user IDs."""
		users = (User(mock.Mock(Handler)) for _ in range(10))
		self.assertSequenceEqual(list(map(lambda user: user.user_id, users)), range(1, 11))