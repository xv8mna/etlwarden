"""A tiny job to try etlwarden with: it prints a few lines and finishes."""

import platform
import sys
from datetime import datetime

print(f"Hello from etlwarden at {datetime.now():%H:%M:%S}")
print(f"Python {platform.python_version()} at {sys.executable}")
print("Everything a job prints ends up in its log.")
