---
name: draft
description: Check and finish a manuscript written with Pandoc citekeys against the Zotero library, the verified references and the full texts, and produce its bibliography. Use when the user writes or revises a paper, chapter, essay or thesis section, asks to check citations in a draft, or wants the reference list of a text.
---

# Draft

Servers: DOI MCP (`check_draft`, `search_everything`, `verify_doi`, `verify_book`,
`check_retractions`, `check_citations`), Zotero MCP (`check_claims`, `get_fulltext`,
`set_verification`, `export_items`, `format_citations`), Obsidian MCP (`read_note`,
`write_note`, `append_note`) when the draft lives in the vault (`Drafts/`, outside the schema).

## Rules that hold everywhere

- A citation in the draft is `[@citekey, p. N]`: Better BibTeX keys as pinned in Zotero
  (`Citation Key:` in Extra), or the Zotero item key. A key the library does not know is an
  error in the draft, never a reason to invent an item.
- A quotation carries a page. A claim attributed to a source is checked on its text
  (`get_fulltext` by pages, or `check_claims` for many at once), not on memory.
- A reference enters the bibliography only from an item with `Verified: … match`; the
  unverified ones go through `verify_doi` / `verify_book` and `set_verification` first.
- The bibliography is exported from Zotero (`export_items`, CSL JSON or BibTeX), never typed.

## Steps

1. **Read.** `read_note` on the draft (or take the text from the chat). `check_draft(text,
   retractions=True)`: unresolved and ambiguous keys, unverified items, quotations without a
   page, retracted DOIs, and `bibliography_keys`.
2. **Repair keys.** For each unresolved key ask which work is meant; `search_items` in Zotero
   to find it, or the add-to-library skill to add it; for an ambiguous surname+year key use the
   pinned citekey or the item key in the draft.
3. **Verify.** For each unverified item: `verify_doi` (or `verify_book`) with the expected
   title, author and year; on `registered_match` record it with `set_verification(key,
   "match", "verify_doi registered_match")`; on a mismatch tell the user and leave the item
   unverified.
4. **Claims.** For the sentences that attribute a position or quote a passage: `check_claims`
   (one call for the batch) or `get_fulltext(key, pages=…)`; `search_everything` to find where
   the user read something when the citation is missing. Report each claim as supported with
   page, not found, or contradicted; never rewrite the user's argument. For a source in
   another language than the claim, `search_everything(…, semantic=true, zotero_key=key)`.
5. **Bibliography.** `export_items(keys=bibliography_keys, format="csljson")` (or BibTeX)
   for Pandoc; `format_citations(style="apa")` when the user wants the list in prose.
6. **Report** in a table: key, item, verified, claims checked, what remains. Run `check_draft`
   again at the end: zero unresolved, zero unverified is the condition for sending the text.

## When to stop and ask

- A quotation whose text does not appear in the PDF (wrong edition, wrong page, or wrong
  attribution): show the nearest passage and ask.
- A retracted source: the text must say it is retracted, or drop it; the user decides.
