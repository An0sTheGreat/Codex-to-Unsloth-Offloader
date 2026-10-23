"""Unit tests for the Unsloth coding-handoff offloader.

Run from the project root (so the ``bridge`` package is importable):

    python -m unittest discover -s tests -v

All tests use the in-memory MockTransport and a temp config file; none require a
live Unsloth instance (Phase 1 acceptance criteria)."""

# Ensure the project root is on sys.path even when tests are run in isolation.
import os
import sys

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

