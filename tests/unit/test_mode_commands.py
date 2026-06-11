import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from notifications.telegram_commands.mode_commands import handle


class TestModeCommands(unittest.TestCase):
    def test_maxpositions_show_current(self):
        with patch("notifications.telegram_commands.mode_commands.send_telegram_message") as mock_send, \
             patch("notifications.telegram_commands.mode_commands.get_config", return_value={"max_open_positions": 5}), \
             patch("notifications.telegram_commands.mode_commands.count_open_positions", return_value=3):
            self.assertTrue(handle("/maxpositions"))
            msg = mock_send.call_args[0][0]
            self.assertIn("5", msg)
            self.assertIn("3", msg)
            self.assertIn("/maxpositions ANZAHL", msg)

    def test_maxpositions_set_valid(self):
        with patch("notifications.telegram_commands.mode_commands.send_telegram_message") as mock_send, \
             patch("notifications.telegram_commands.mode_commands._save_mode_updates", return_value=True) as mock_save, \
             patch("notifications.telegram_commands.mode_commands.reload_config"), \
             patch("notifications.telegram_commands.mode_commands.count_open_positions", return_value=2):
            self.assertTrue(handle("/maxpositions 10"))
            mock_save.assert_called_once_with({"max_open_positions": 10})
            self.assertIn("10", mock_send.call_args[0][0])

    def test_maxpositions_rejects_invalid(self):
        with patch("notifications.telegram_commands.mode_commands.send_telegram_message") as mock_send:
            self.assertTrue(handle("/maxpositions 0"))
            self.assertIn("/maxpositions", mock_send.call_args[0][0])


if __name__ == "__main__":
    unittest.main()