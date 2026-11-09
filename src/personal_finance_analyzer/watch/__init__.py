"""Folder imports: PDFs put into the documents folder by hand are imported automatically.

The documents folder keeps its layout, and that layout says whose file it is and what it is::

    <documents>/<Person>/Statements/<any subfolders>/<any name>.pdf   -> a bank / card statement
    <documents>/<Person>/Tax/<year>/<any name>.pdf                    -> a W-2 / 1099 / 1040
    <documents>/Shared/...                                            -> no person: needs one

- `classify`: pure path -> (person, type, year) rules, and the person-folder matching.
- `scanner`: one scan (list the folder, wait for files to settle, import them through the same
  pipelines as an upload, record what happened in `watch_files`).
- `service`: the background thread `pfa serve` runs it on, and its status.

Files stay where the user put them: their location is recorded as ``stored_path``; nothing is
renamed or moved (`pfa documents reorganize` still does that on request).
"""
