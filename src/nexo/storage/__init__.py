"""Object-storage backends for user uploads.

Currently Volcengine TOS (`tos.py`). Importing this package should stay
cheap — clients are built lazily on first use so a missing/misconfigured
store only errors when something actually tries to persist.
"""
