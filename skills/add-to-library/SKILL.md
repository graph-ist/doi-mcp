---
name: add-to-library
description: Add a work to the Zotero library with a verified DOI or edition, its PDF and the right tags. Use when the user wants to save, add or import a paper or book into Zotero, from a title, a DOI, a reference list, a search result, a literature map or a patent's cited literature.
---

# Add to library

Servers: DOI MCP (`find_doi`, `verify_doi`, `verify_book`, `check_retractions`), Zotero MCP
(`find_dois`, `create_item_from_doi`, `create_item`, `create_item_from_patent`,
`set_verification`, `attach_pdf`, `update_item_links`, `list_collections`), optionally
`map_mark` of the DOI MCP.

## Rules that hold everywhere

- Never a DOI from memory. A DOI enters Zotero only after `find_doi` → `match` and
  `verify_doi` → `registered_match` (books: `find_doi(is_book=true)` with the year of the
  edition cited, then `verify_book`; a missing book DOI is normal).
- `doi_candidate` (from `patent_get` or `extract_references`) is a candidate: run `verify_doi`
  on it with the expected title, author and year before using it.
- Zotero decides what is already in the library: `find_dois` before any creation.
- Tags come from the user's vocabulary in Zotero: `sub/<ANZSRC code>-<slug>` (at most 3),
  `cited/<project>`, `core/seminal`. Ask for them once per batch; never invent a code.

## Steps

1. **Resolve.** For each work: if it has a DOI, `verify_doi` with `expected_title`,
   `expected_first_author`, `expected_year` (and volume, issue, pages when known). If it has
   none, `find_doi` with title, first author surname and year; on `no_confident_match` show the
   candidates and stop for that work. Books: `is_book=true`, then `verify_book`.
2. **Deduplicate.** `find_dois` with every DOI of the batch. Works already in the library are
   reported with their key and skipped; the user may still want them tagged or moved
   (`update_item_links`). If step 1 verified an item that was already there, record it:
   `set_verification(key, "match", "verify_doi registered_match")` (or `"mismatch"`,
   `"retracted"` with what was found).
3. **Create.** `create_item_from_doi(doi, expected_title=…, expected_first_author=…,
   expected_year=…, collection_keys=[…], tags=[…])`. A status `not_created` means the DOI's
   record does not match the work or the work is retracted: report it, do not pass `force`
   unless the user says so. Works without DOI (books, grey literature): `create_item` with the
   fields verified in step 1.
4. **PDF.** `attach_pdf(key)` looks for an open access copy; a refusal from a publisher is
   expected and not an error. For a scan the OCR worker runs by itself.
5. **Maps.** If the works came from a map, `map_mark(map, works, "to_read")` so the map shows
   them as acquired. The library snapshot refreshes by itself a few seconds after the writes;
   `refresh_library_snapshot` only if the map is read right away.
6. **Report** a table: work, outcome (`added` / `already in library` / `not created: reason` /
   `no DOI found`), key, PDF yes/no. Only corrections and failures need words.

## When to stop and ask

- More than one plausible candidate for a title.
- A `title mismatch` or `author mismatch` from `create_item_from_doi`.
- A retracted work: say so, and add it only if the user confirms (it may be needed as an object
  of study, tagged accordingly).
