"""A tiny stand-in service, used by this repository's self-test.

demo/API.md documents it. The self-test pull request changes this file and checks that the
action notices the documentation going stale.
"""
from dataclasses import dataclass


@dataclass
class SearchResult:
    title: str
    url: str


def search_notes(query: str, limit: int = 25, include_trashed: bool = False) -> list[SearchResult]:
    """Search stored notes and return the matches, newest first."""
    raise NotImplementedError


def export_notes(notebook: str, fmt: str) -> bytes:
    """Export a whole notebook. Supported formats: markdown, html, pdf. There is no default."""
    raise NotImplementedError


def delete_note(note_id: str) -> None:
    """Delete one note. This cannot be undone."""
    raise NotImplementedError
