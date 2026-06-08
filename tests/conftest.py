"""
Pytest config + shared fixtures for deep-research-project tests.

Portable: derives sys.path from this file's location, not from any
hardcoded /opt, /root, /home, or absolute Windows path.

Tests can be run from any of:
  - the repo root:  python3 -m pytest
  - a clean copy:   cp -a . /tmp/foo && cd /tmp/foo && PYTHONPATH=src python3 -m pytest
  - an extracted archive:  tar -xzf project-v0.8.2.tar.gz && cd project && PYTHONPATH=src python3 -m pytest
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Force-disable LLM-conditional in tests (don't hit OpenRouter from CI)
LLM_DISABLED = True
