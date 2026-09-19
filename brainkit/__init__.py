"""brainkit — parse, validate and reindex the markdown brain (PLATFORM.md §5, §9, §10 P1).

``brain/`` markdown files are the source of truth. This package turns them into the
derived index (``provenance``'s SQLAlchemy models) and enforces, at parse/validate
time and again at reindex time, the provenance contract in PLATFORM.md §4.
"""
