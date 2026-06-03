from __future__ import annotations

from pathlib import Path
import sys
import unittest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class BackendCurrentFlowTests(unittest.TestCase):
    def test_legacy_planner_module_is_removed(self) -> None:
        self.assertFalse((BACKEND_DIR / "app" / "planner.py").exists())


if __name__ == "__main__":
    unittest.main()
