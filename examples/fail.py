"""A job that always fails, to try the failure notification with."""

import sys

print("This job failed on purpose.")
sys.exit(1)
