# Skills: the workflows across the three MCP servers

The DOI MCP, the Zotero MCP and the Obsidian MCP each expose tools; none of them knows the
whole cycle of a piece of research (find, verify, acquire, read, write, link back). That
sequence, and what each step writes where, lives here as Claude skills: one `SKILL.md` per
entry point, loaded by Claude when the task matches its description.

| Skill | Entry point | Servers |
|---|---|---|
| `add-to-library` | a work to add to Zotero, from a title, a DOI, a map or a reference list | DOI, Zotero |
| `reading-note` | a work read, to turn into an Obsidian note | Zotero, Obsidian, DOI |
| `literature-map` | a topic or a collection to map, expand and triage | DOI, Zotero, Obsidian |
| `prior-art` | a technique or a paper, to see its patents and the literature they cite | DOI, Zotero |
| `draft` | a manuscript with `[@citekey]` citations, to check, verify and give a bibliography | DOI, Zotero, Obsidian |
| `argument-map` | a thesis, to record who supports it and who objects, with pages | Obsidian, Zotero, DOI |

Reference checking (`find_doi`, `verify_doi`, `verify_book`, `format_apa`, `check_citations`)
stays a prompt of the DOI MCP (*Verifica riferimenti*), because it involves one server only.

## Install

Claude Code: copy each folder into `~/.claude/skills/` (personal) or `.claude/skills/` of a
project. Claude web and desktop: zip each folder and upload it under Settings → Capabilities →
Skills. The MCP connectors must be enabled in the same conversation; a skill names tools, it
does not provide them.

## Rules that every skill repeats

These are the invariants of the system; the skills restate them so that each one works alone.

- A DOI is attached only after `find_doi` returns `match` and `verify_doi` returns
  `registered_match`; never a DOI from memory. `doi_candidate` fields (patents, extracted
  references) are candidates, not DOIs.
- Zotero is the source of tags (`sub/` ANZSRC codes, `cited/*`, `core/seminal`); a note copies
  them, never the other way round.
- Whether a work is *in the library* is decided by Zotero (`find_dois`); whether it is *read* is
  decided by the map state (`map_mark`) and by the existence of its note. The reading state never
  goes into the note.
- The maps read the two snapshots and nothing else. Both servers refresh their snapshot by
  themselves a few seconds after a write; call `refresh_library_snapshot` or
  `refresh_notes_snapshot` only when a map is read in the same breath as the write.
- A bibliographic check leaves a trace: `create_item_from_doi` with the expected fields writes
  a `Verified:` line on the item, `set_verification` records the outcome of `verify_doi` or
  `verify_book` on an existing item, and the maps show it as `verified`.
- Show a note or a batch of changes before writing it; one confirmation per batch, not per item.
- `search_everything` searches the user's own corpus (notes, PDF pages, highlights), lexically,
  in the language of the query; it never replaces reading the passage it points to.
- The weekly job (`weekly_report`, the Monday timer) brings new citing works, retractions and
  coverage gaps to the vault inbox; `coverage_report` gives the same for one project on demand.
