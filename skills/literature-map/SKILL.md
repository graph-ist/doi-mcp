---
name: literature-map
description: Build, grow and triage a persistent literature map around seed works, a Zotero collection or a tag, and export it to Obsidian. Use when the user wants to see what a set of papers cites and is cited by, find what is missing from the library, or keep a reading list for a project.
---

# Literature map

Servers: DOI MCP (`map_create`, `map_add_seeds`, `map_expand`, `map_suggest`, `map_mark`,
`map_get`, `map_refresh`, `map_prune`, `map_export`, `paper_table`, `check_retractions`),
Zotero MCP (`refresh_library_snapshot`, `save_references`, `find_dois`), Obsidian MCP
(`write_note`, `refresh_notes_snapshot`). The user views maps on the *Literature maps* page
linked in the DOI MCP's instructions; give that link whenever a map changes.

## Rules that hold everywhere

- A map node is a candidate until the user decides: `map_mark` with `to_read`, `read` or
  `dismissed` is the only reading state; it never goes into notes or Zotero.
- `in_library`, `zotero_key`, `library_match` (doi, key, patent or title), `verified` and
  `notes` on a node come from the snapshots, which the other servers refresh by themselves
  after their writes: refresh them by hand
  (`refresh_library_snapshot`, `refresh_notes_snapshot`) before judging what is missing, and
  read `snapshots` in `map_get` for their age or a `problems` entry.
- Citation metrics favour older works and OpenAlex lacks references for most books and
  humanities: say so when ranking.

## Steps

1. **Seeds.** `map_create(name, description)`; then `map_add_seeds` with DOIs, or
   `zotero_collection` / `zotero_tag` (e.g. `cited/<project>`). Items without DOI enter as
   `Z:` nodes; if the result warns that their bibliography is not extracted, run the Zotero
   MCP's `save_references(collection=…)` (books and humanities) and add the seeds again.
2. **Expand.** `map_expand(map, modes=["references", "citing"])` first; `similar` and
   `authors` only on request, since they widen the topic. Watch `max_new` and the 2000-work cap.
3. **Triage.** `map_suggest` for the candidates most linked to the seeds (`seeds_linked`,
   `seed_cocited`, `seed_coupling`); `paper_table` on the top ones for abstracts; judge
   relevance from abstracts, never from titles alone, and `map_mark` in batches:
   `to_read` / `dismissed` with a short `note` saying why.
4. **Acquire.** `coverage_report(project)` says which cited works still lack a note, a PDF or
   a verification. For `to_read` works not `in_library`, hand over to the add-to-library skill
   (`find_dois` first); after it, `refresh_library_snapshot` and `map_refresh`.
5. **Maintain.** `map_refresh(map, new_citing=True)` for new citing works since the last
   check; `check_retractions(map=…)` before citing from the map; `map_prune` when the map
   grows past what the page can show.
6. **Export.** `map_export(map)` → `write_note` in `Maps/<map name>.md` (outside the schema
   scope), then `refresh_notes_snapshot`. The export is a reading list with DOI, Zotero and note
   links; it is regenerated, not edited by hand.

## When to stop and ask

- Before `map_delete` or `map_remove` of seeds.
- When `coverage_check` shows OpenAlex and Semantic Scholar disagree strongly for a seed: the
  gap may be coverage, not absence.
- When a candidate's relevance cannot be judged from the abstract: list it as undecided.
