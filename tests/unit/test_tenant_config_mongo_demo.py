import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from data_manager import _should_use_mongo_for_tenant_config


class TestTenantConfigMongoDemo(unittest.TestCase):
    def test_demo_mode_uses_mongo_when_demo_backend_mongo(self):
        cfg = {"demo": {"backend": "mongo"}, "paper": {"backend": "local"}}
        with patch("data_manager.is_demo_mode", return_value=True):
            self.assertTrue(_should_use_mongo_for_tenant_config(cfg))

    def test_non_demo_uses_paper_backend(self):
        cfg = {"demo": {"backend": "mongo"}, "paper": {"backend": "local"}}
        with patch("data_manager.is_demo_mode", return_value=False):
            self.assertFalse(_should_use_mongo_for_tenant_config(cfg))


if __name__ == "__main__":
    unittest.main()