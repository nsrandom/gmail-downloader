"""Shared building blocks for the pipelines runner.

`attachments_downloader.py` still carries its own copy of the auth, state, and
logging code. That is deliberate -- see docs/pipelines_design.md -- and it
migrates onto these modules in a separate change once they have run against
real mail for a while.
"""
