"""Storage backends for user uploads.

Currently the local VFS backend (`vfs.py`): uploads are written directly to the
nexo-vfs distributed filesystem mounted at `NEXO_VFS_DIR`, flat under
`<org_id>/<user_id>/`. Importing this package should stay cheap — config is
read lazily so a missing/misconfigured store only errors when something
actually tries to persist.
"""
