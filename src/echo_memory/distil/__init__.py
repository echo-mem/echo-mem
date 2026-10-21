"""Turning what the harness already recorded into facts.

The capture gap, approached from the side that needs no new instrumentation:
the transcript is already on disk. See transcripts.py for why, and for what
this takes from Beacon and what it deliberately does differently.
"""

from echo_memory.distil.transcripts import (
    Session,
    human_turns,
    project_slug,
    sessions_for,
)

__all__ = ["Session", "human_turns", "project_slug", "sessions_for"]
