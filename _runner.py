import os, sys, importlib
ROOT = r"Y:\Programs\Codex to Unsloth Offloader"
sys.path.insert(0, ROOT)
import tests.test_offload as mod
importlib.reload(mod)
import unittest
suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
print("tests found:", suite.countTestCases())
runner = unittest.TextTestRunner(verbosity=2)
res = runner.run(suite)
sys.exit(0 if res.wasSuccessful() else 1)
