"""pytest configuration: add src/ to the import path so tests can
`from tweeting.scheduler import ...` the same way main.py does."""

import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
