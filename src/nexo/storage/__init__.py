"""Storage backends for user uploads.

Currently the remote-folder backend (`remote.py`): uploads are scp'd to a
specified folder on a remote machine via `scripts/ship_media.sh`. Importing
this package should stay cheap — config is read lazily so a
missing/misconfigured store only errors when something actually tries to
persist.
"""
