"""Private domain (Life-OS): calendar and mail, read and summarised by local models only.

Everything here is PRIVATE. No cloud route exists (jarvis_core.router.policy fails closed for
this domain); egress is CalDAV and IMAP only, through egress-private. Only metadata leaves
the domain: audit entries hold hashes and counts, telemetry holds states and durations.
"""
