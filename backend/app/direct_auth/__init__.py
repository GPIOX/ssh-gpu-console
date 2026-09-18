"""Server→server direct-transfer authorization (Phase 4.2C).

`metadata.py` — persistence of configured dedicated-key pairs.
`service.py`  — check / setup-key / revoke transaction + zero-SSH listing.

Security model: LOCAL auth (console→server) is fully separate from DIRECT
auth (source→target). A password is NEVER used for server-to-server rsync.
Priority: existing native BatchMode SSH > SGC dedicated key > unavailable.
"""
