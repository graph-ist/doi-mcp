---
name: argument-map
description: Build or update a thesis note, the hub that records who supports a position, who objects and with what, each with a page, across the reading notes and the sources in Zotero. Use when the user asks who argues for or against a thesis, wants to map a debate, or asks to update the argument map of a topic.
---

# Argument map

Servers: Obsidian MCP (prompt `map_argument`; `query_notes`, `search_notes`, `backlinks`,
`similar_notes`, `read_note`, `write_note`, `patch_frontmatter`, `append_note`,
`refresh_notes_snapshot`), Zotero MCP (`get_fulltext`, `check_claims`, `get_annotations`),
DOI MCP (`search_everything`, `map_intents`, `search_literature`, `citation_graph`).

The Obsidian prompt `map_argument` holds the note's form (`kind: thesis`; Position,
Supporters, Objections, Replies, My Observations, Open Questions, To Verify). This skill is
the method around it.

## Rules that hold everywhere

- A thesis note cites notes and sources, with a page, one line per entry, in the form
  `[[Note]] (p. N): one sentence`. No entry without a page or a section reference.
- The vault is the evidence: a supporter or objector is listed only from a reading note or
  a passage read with `get_fulltext`; never from memory of the literature.
- The user's position goes to My Observations, in the user's words; the model's reading of a
  source goes to the source's note (reading-note skill), not to the thesis note.
- Distinctions first: when a term in the thesis admits several readings, Position says which
  one the note takes, and entries that argue about a different reading say so.

## Steps

1. **Locate.** `query_notes(field="kind", value="thesis")` and `search_notes` for an existing
   note; `read_note` it. If there is none, the prompt `map_argument` creates it.
2. **Collect.** `search_everything(thesis words)` over notes, PDF pages and highlights:
   every hit is a candidate entry with its note or item and page. `backlinks` and
   `similar_notes` on the concept and person notes the thesis names. `read_note` each
   candidate note and take only what Thesis, Argument, My Observations or Claude's Additions
   say.
3. **Check.** For an entry whose note gives no page, `get_fulltext(key, pages=…)` on the item
   in its reference, or `check_claims` for the batch; what is not found goes to To Verify.
4. **From the graph.** If a literature map covers the topic, `map_intents(map)` on its seeds:
   edges flagged `contrasting` (a citing work whose citation sentences carry contrast or
   critique) are candidates for Objections, and `influential` ones for Supporters; each is
   read in the text (`get_fulltext`) before it becomes an entry, because the flag is a
   wording heuristic. `search_everything(…, semantic=true)` finds the passages across
   languages once the corpus is embedded.
5. **Widen** only on request: `search_literature` or `citation_graph` for positions the vault
   does not hold yet; those become reading candidates (literature-map skill), not entries.
6. **Write.** Show the note; `write_note` or `patch_frontmatter` / `append_note` to the right
   section; `refresh_notes_snapshot`.

## When to stop and ask

- The thesis is ambiguous between readings and the vault holds notes on both: ask which the
  note is about, or propose two notes.
- A source seems to both support and object (different parts of the work): list it in both
  sections with the pages, and say so.
