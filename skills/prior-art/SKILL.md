---
name: prior-art
description: Find patents around an interaction technique or a paper, read what they cite from the scientific literature and bring the verified works into Zotero or a literature map. Use when the user asks about patents, prior art, industrial uptake of a paper, or which patents cite a technique.
---

# Prior art

Servers: DOI MCP (`patent_search`, `patent_get`, `find_doi`, `verify_doi`, `paper_table`,
`map_add_seeds`), Zotero MCP (`find_dois`, `create_item_from_doi`, `create_item`). The patent
tools exist only when the server has EPO OPS credentials; if they are missing from the tool
list, say so and stop.

## Rules that hold everywhere

- A patent citation is made by an examiner to establish prior art, not by an author to
  acknowledge a debt. Do not read it as a scholarly citation, and do not count it with the
  co-citation measures of a map; report it as a separate relation with its `category`
  (X = alone prejudices novelty or inventive step, Y = together with another document,
  A = background).
- `doi` on a cited literature entry is printed in the search report; `doi_candidate` is a
  Crossref match under `find_doi`'s rules and needs `verify_doi` before any use.
- The unit is the family: two publications with the same `family_id` are one invention.

## Steps

1. **Search.** `patent_search(query)` with free words (title and abstract) or native CQL
   (`ta="foldable display" and pd>=2015`). Group the results by `family_id`; keep one
   publication per family, preferring the granted one (kind `B`).
2. **Read.** `patent_get(number, resolve_literature=20, citing=True)` for the families that
   matter: applicants, priority date, IPC classes, cited patents, cited literature, citing
   patents. The Espacenet `url` is for the user.
3. **Literature.** For each cited literature entry: with `doi`, `verify_doi` against the title
   and author in the citation text; with `doi_candidate`, `verify_doi` on the candidate; with
   neither, `find_doi` from the parsed `title`, `first_author`, `year`. Keep three lists:
   verified, candidate rejected, unresolved (with the raw text).
4. **Use.** `paper_table` on the verified DOIs for abstracts; `find_dois` to see which are
   already in Zotero; add the rest through the add-to-library skill. A patent itself goes into
   Zotero as `create_item(item_type="patent")` with `patentNumber`, `assignee`, `issueDate`,
   `priorityNumbers`, `url` from `patent_get`; it has no DOI.
5. **Map.** Verified literature can seed or extend a literature map (`map_add_seeds`). The
   patents themselves do not enter the map: list them in the map's description or in the
   Obsidian export as a separate section.

## Report

For each family: number, title, applicant, priority year, category of the key citations, and
the literature it rests on, verified or not. State explicitly that OPS covers published
applications and grants, not pending unpublished ones, and that NPL citations are typed by
examiners and often incomplete.
