# Demo API

A stand-in service used by this repository's self-test. The claims below match `demo/server.py`.

## Tools

- `search_notes` — Search stored notes and return the matches, newest first.
  - `query` (string, required): The text to search for.
  - `limit` (number, optional): Maximum number of matches to return (default: 10).
  - `include_archived` (boolean, optional): Also search archived notes (default: false).
- `export_notes` — Export a whole notebook.
  - `notebook` (string, required): Name of the notebook to export.
  - `fmt` (string, optional): Output format, `markdown` or `html` (default: `markdown`).
- `delete_note` — Delete one note. This cannot be undone.
  - `note_id` (string, required): Identifier of the note to delete.
