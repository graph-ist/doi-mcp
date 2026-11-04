---
name: concept-card
description: Build or update a concept note, the card of a contested term (affordance, embodiment, agency) with the readings by author and the typed relations between them (adopts, revises, rejects, applies, extends, contrasts), each with a page or marked as interpretation. Use when the user asks what a term means for different authors, how readings of a concept relate, or to map a concept.
---

# Concept card

Servers: Obsidian MCP (prompt `concept_card`; `concept_relations`, `query_notes`, `search_notes`,
`backlinks`, `read_note`, `write_note`, `patch_frontmatter`, `append_note`,
`refresh_notes_snapshot`), Zotero MCP (`get_fulltext`, `check_claims`), DOI MCP
(`search_everything`, `search_literature`).

The Obsidian prompt `concept_card` holds the note's form (`kind: concept`; the `relations`
field; Definition, Readings, Relations, My Observations, Open Questions, To Verify, Cited by).
This skill is the method around it.

## Rules that hold everywhere

- A relation has a type from the schema and an evidence: the page where the relation is stated,
  or the word `interpretation` when it is a reading (the user's or the model's) that no passage
  states. A relation without evidence is not written.
- `from` and `to` are notes that exist (a person note or a reading note); a relation to a work
  with no note first gets its note (reading-note skill) or is kept in To Verify.
- The vault is the evidence: a reading is recorded from a reading note or from a passage read
  with `get_fulltext`, never from memory of the literature.
- Distinctions first: Definition names what separates the readings before Readings lists
  them, so that `rejects` and `revises` have a stated object.

## Steps

1. **Locate.** `query_notes(field="kind", value="concept")`, `search_notes` for the term; if
   the note exists, `read_note` it and `concept_relations(concept=…)` for what is recorded.
2. **Collect.** `search_notes` and `backlinks` for the reading notes that define or use the
   term; `search_everything(term)` (and `semantic=true` once the corpus is embedded) for
   passages in PDFs and highlights; `read_note` each candidate and take its definition with
   the page. One ##### subsection per author in Readings.
3. **Relate.** For each pair of readings that the texts connect: the type, and the page where
   the later author names the earlier one (`get_fulltext`); otherwise `interpretation`. Write
   the frontmatter entry and the line in Relations together.
4. **Write.** Show the note; `write_note` or `patch_frontmatter` / `append_note`; report the
   validator's warnings (a missing note named in a relation, a type outside the list, an
   evidence that is not a page); `refresh_notes_snapshot`.
5. **Use.** `concept_relations(note=…)` answers who rejects or revises whose reading across
   concepts; a thesis note (argument-map skill) cites the concept note for the distinction it
   depends on.

## Pilot

Three concepts, chosen among the terms that recur in the user's reading notes with more than
one author each (`query_notes` on `subclasses`, `search_notes` on the candidates). Write the
three cards, then read `concept_relations()` as a whole: if the types or the evidence rule do
not fit what the texts actually do, change the schema before writing more cards.

## When to stop and ask

- A term whose readings split along a distinction the user has not named: propose the
  distinction in Definition and ask.
- Two authors who use the same word for different concepts: two notes, not one.
