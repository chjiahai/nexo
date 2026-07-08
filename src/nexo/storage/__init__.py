"""Object-storage backends for user uploads.

Currently Huawei Cloud OBS (`obs.py`). Importing this package should stay
cheap — clients are built lazily on first use so a missing/misconfigured
store only errors when something actually tries to persist.
"""
