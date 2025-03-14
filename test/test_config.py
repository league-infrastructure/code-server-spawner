import unittest
from cspawn.util.config import get_config
from pathlib import Path


class TestConfig(unittest.TestCase):
    def test_config_basic(self):
        import cspawn

        root = Path(cspawn.__file__).parent.parent
        self.assertTrue(root.exists())
        print(root)
        c = get_config(root)
        print(c["__CONFIG_PATH"])
