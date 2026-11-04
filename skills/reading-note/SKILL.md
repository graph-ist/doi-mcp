---
name: reading-note
description: Turn a work read (and the user's highlights in Zotero, and the discussion in chat) into an Obsidian reading note that follows the vault template, verified on the PDF. Use when the user asks for a note, a summary to save in Obsidian, or to update the note of a book or paper.
---

# Reading note

Servers: Zotero MCP (`search_items`, `get_item`, `format_citations`, `get_outline`,
`get_fulltext`, `get_annotations`, `export_annotations`, `annotate_pdf`, `ocr_pdf`), Obsidian MCP
(`search_notes`, `query_notes`, `read_note`, `write_note`, `patch_frontmatter`, `append_note`,
`validate_note`, `suggest_links`, `similar_notes`, `refresh_notes_snapshot`), DOI MCP
(`map_mark`, and `find_doi` / `verify_doi` only for works not yet in Zotero).

The Obsidian MCP's prompt `save_to_obsidian` holds the template (frontmatter fields, headings,
lineage diagram, alias rules). This skill is the order of operations around it.

## Rules that hold everywhere

- Zotero holds sources and the user's highlights; every note lives in Obsidian; English.
- Zotero is the source of `cssclasses` (the item's `sub/` tags without prefix) and `tags`
  (`cited/*`, `core/seminal`): copy, never edit them from the note side.
- *My Observations* is the user's reading: chat plus Zotero annotations, in the user's words.
  *Claude's Additions* only with a page read through `get_fulltext`. Everything else goes to
  *To Verify*. Never present a quotation you have not read in the text.
- The reading state does not go into the note: it is `map_mark` and the note's existence.

## Steps

1. **Locate.** `search_items` / `get_item` for the key. `search_notes` and `query_notes` for an
   existing note: if one exists, the result is an update (`patch_frontmatter`, `append_note` to a
   heading), not a second note.
2. **Read.** `get_outline(key)` for the sections; `get_annotations(key)` for the highlights
   (page order; `export_annotations` when there are many); `get_fulltext(key, pages="a-b")` for
   each passage a claim needs. A scan without text: `ocr_pdf`, then wait for `ocr_status`.
3. **Reference.** `format_citations(keys=[key], style="apa")` → `reference` as
   `[APA](zotero://select/library/items/KEY)`. For a work not in Zotero, stop and offer the
   add-to-library skill first; plain APA text only if the user declines.
4. **Draft** the note with the template; `suggest_links` / `similar_notes` for *Connections*,
   each with one sentence of justification. Show the note.
5. **Write.** `write_note` in `Zettelkasten/Reading Notes/<title without subtitle>.md`; report the
   validator's warnings. If the user confirms items of *To Verify* later, move them to *Claude's
   Additions* with the page and, on request, `annotate_pdf` the passage.
6. **Link back.** `refresh_notes_snapshot`; if the work is in a literature map, `map_mark(map,
   [work], "read")`.

## When to stop and ask

- No PDF or text available: say which sections cannot be verified rather than writing them.
- More than three `sub/` codes on the item, or a `subclasses` value not in the vocabulary.
- An existing note whose structure predates the template (`Zettelkasten/Storia della
  filosofia/`): append, do not restructure, unless asked.
