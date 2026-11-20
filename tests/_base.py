"""测试共用夹具。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import unittest  # noqa: E402

from changeover.app import Application  # noqa: E402

DATA = ROOT / "data"


class AppCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Application(DATA)
        cls.pack = cls.app.pack
        cls.facts = cls.app.facts
