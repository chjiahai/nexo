"""Storage backends for user uploads.

The OBS backend (`obs.py`): media uploaded by `nexo drain` to Huawei Cloud OBS,
under deterministic keys `<org>/<user>/<msg_id>-<name>`. Importing this package
should stay cheap — config is read lazily so a missing/misconfigured store only
errors when something actually tries to persist.
"""
