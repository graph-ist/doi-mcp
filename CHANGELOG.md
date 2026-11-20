# Changelog

Renames and merges of tools break compatibility with clients: after the update the client must re-read the tool list (in Claude web, disable and re-enable the connector).

## Unreleased

Corpus and search
- `search_everything` searches one passage per library item from `library.json` (title, creators, year, abstract, tags; scope `items`), so items whose PDF was never indexed are found too. Items are tracked one by one by a fingerprint of their text: a snapshot rewritten after a Zotero write re-embeds only the changed items. Needs the Zotero MCP's snapshot with `abstract` (contract fixture updated); older snapshots work, without abstracts.
- `mode` parameter: `lexical` (default), `semantic` (same as `semantic=true`, kept), `hybrid` (lexical and semantic rankings fused by reciprocal rank; `match` per result; falls back to lexical with a note when the model is missing). The answer carries `mode`.
- `rerank=true`: the top 30 candidates reordered by a local multilingual cross-encoder within 8 seconds (`rerank` in the answer); `paper-doi-semantic download-reranker`, `SEMANTIC_RERANK_MODEL_DIR`; `paper-doi-semantic status` reports the reranker. Without the model files the results come back unreordered with the reason.

Fixed
- The DNB (`verify_book`, `search_literature` with `database="dnb"`) is left alone for six hours after an HTTP 403 or 429: an address that asks too often is blocked, and insisting prolongs it. The answer says `paused for N more minutes after HTTP 429`. The pause is kept in memory and ends with a restart.

## 0.9.0

Incompatible for server installs
- The modules now live in the `paper_doi_mcp` package; the commands are `paper-doi-mcp`, `paper-doi-weekly`, `paper-doi-semantic` and `paper-doi-philarchive` (or `python -m paper_doi_mcp`). A server that ran `python server.py` or `weekly.py` from a copy of the files installs the package (README, *Remote use*) and points its units at the commands; `deploy/doi-weekly.service` is updated. `check_creds.py` moved to `deploy/`.
- The maps page link is no longer built in: set `MAPS_PAGE_URL` to keep giving it to Claude. `map_list` returns it as `page`.

What's new
- Published on PyPI as `paper-doi-mcp`, MIT licence; `uvx paper-doi-mcp` runs it without a clone.
- Without `STATE_DIRECTORY` and without `/var/lib/doi-mcp`, the state goes to `~/.local/state/paper-doi-mcp` (or `$XDG_STATE_HOME`), so a desktop install works without root.

Maps and jobs
- Background jobs: `map_create`, `map_add_seeds`, `map_expand`, `map_intents`, `map_add_patents` and `map_refresh` accept `background` and, beyond 100 seconds, answer `pending` with a `job_id`; new tool `map_job_status` (status, progress, result). `jobs` table in `maps.sqlite` (migration at first start; `running` jobs become `interrupted` at restart); one job per map at a time (`busy`).
- `map_create` answers `partial` if no seed can be added and carries the errors in `warnings`. `map_refresh` does not advance the check date if a seed errors or a page is full (`truncated`, `watch_not_advanced`); with `no_seeds` it also has `coverage_gaps` and `new`.
- SQLite: WAL, 15-second wait on locks, migrations once per process, connection closed at the end of the request; a locked database gives `status: "busy"`. `map_get`, `map_suggest`, `map_export`, `map_prune` and the merging of duplicates run in threads; the merge reads inside the transaction.
- `map_get`: for each work `cited_in_map`, `cited_in_map_nonseed`, `cited_by_seed_citers`; `max_pairs` parameter and `cocitation_truncated` / `coupling_truncated` flags.
- `map_intents`: `works_skipped` and `truncated`, saving every 5 works. Intents are deleted with `map_remove`, `map_prune`, `map_delete`.
- `map_expand`: `works_in_map` counts all merged records. OpenCitations: three requests at a time, retries on 429s, seeds without a count in `source_errors`. With `OPENALEX_API_KEY` the minimum interval towards OpenAlex drops to 0.2 seconds.
- `map_add_patents`: `status: "error"` if no patent succeeds, one retry on EPO's 403. `map_suggest` accepts `limit=None`.
- `MCP_ALLOW_QUERY_TOKEN=true`: the token is also accepted as `?token=` in the URL, for the Claude web connector form, which has only a name and a URL. Off by default.
- `map_get`: optional parameters `compact` (removes from the works `notes`, `verified`, `zotero_tags`, `found_by`, `note`, `added`, `refs_source`) and `fields` (keeps only the listed fields plus `id`, takes precedence over `compact`; unknown names are ignored and reported in `warnings`). Without the parameters the response does not change.
- Merging of duplicates: the `edge_intents` of the deleted records pass to the kept id (the `citing->cited` key is rewritten; if the new key exists the record with more contexts stays, `influential` and `contrasting` in OR; edges that would become self-citations are removed). Idempotent, in the same transaction.
- In-memory cache (8 entries per process) of the view of `map_get` and `map_suggest`, keyed on `updated`, count and last rowid of nodes and intents and a write counter (`maps.WRITES`) incremented at every commit: `updated` has a one-second resolution, by itself it would not be enough. Connections that write only the `jobs` table do not increment it.
- Request limiter: priority per host. Requests from long jobs (contextvar `_priority`, set by `_run_job`) go after those of quick tools queued on the same host; same minimum interval and same concurrency, no interruption of requests already started.
- *Literature maps* page: *Add patents* button (`map_add_patents`, 1 to 20 numbers, also as seeds).
- *Literature maps* page: the year filter applies as you type (from four digits), reads an inverted range and, with subfield and off-topic, also filters the suggestions list.
- *Literature maps* page: the year filter also applies to seeds (before they always stayed visible); works with 0 citations are no longer cut off by the chart's edge.
- *Literature maps* page: in the network the labels keep their size when zooming, as in the timeline.

## 0.8.2

- *Literature maps* page republished: patents (hexagons, dotted edges) and books (triangles), `verified` chip and Zotero matching criterion, OpenCitations count and `coverage_gaps` after the refresh, buttons and markings for citation intents (`map_intents`, Debate item); `map_watch` removed from the manifest.

## 0.8.1

- `concept-card` skill (concept cards with typed relations, Obsidian MCP 0.5.0); Obsidian contract fixtures updated with `relations`.

## 0.8.0

- OpenCitations Index as a third count in the `coverage_check` of `citation_graph` (`opencitations_cited_by`, `opencitations_references` and verdicts) and in `map_refresh`, which saves `cited_by_oc` for each seed and reports in `coverage_gaps` the seeds that OpenCitations counts as much higher than OpenAlex; the weekly report lists them. `OPENCITATIONS_TOKEN` optional.
- `coverage_report` also returns `projects`, `anzsrc`, `years`, `notes_total`, `items_total`.
- *Knowledge views* page (`pages/knowledge-views.html`): the state of the three archives, coverage by project, fields and years, corpus search, search log, weekly report, maps.

## 0.7.0

- `search_everything(semantic=true)`: search by meaning with local multilingual embeddings (multilingual-e5-small in ONNX, `semantic.py`, `semantic` extra); vectors in `corpus.sqlite`, incremental indexing at every call, in the weekly job and with `semantic.py embed`; `semantic_unavailable` without the model.
- `map_intents`: intents, influence and citation sentences from Semantic Scholar on a map's edges, with the heuristic `contrasting` flag; `map_get` returns `edge_intents`.
- `GET /status` reports the availability of the semantic layer; the weekly report the state of the indexing.

## 0.6.1

- Search log: every `search_literature` and `map_create` appends a line to `STATE_DIRECTORY/searches.jsonl`; `search_log(query)` says whether the same or a similar search has already been done, when and with how many results; the weekly report lists the week's searches.
- Weekly job: ntfy notification with the summary when the report is ready (`NTFY_URL`, `NTFY_TOKEN`).
- Patents: the first author's surname in literature citations is also read in the form "FIRSTNAME SURNAME:" and with particles ("VAN DER LINDEN J").

## 0.6.0

Incompatible with 0.5.x
- HTTP transport: the token is accepted only in the `Authorization: Bearer` header; `?token=` in the URL is rejected.

What's new
- `search_everything`: an FTS5 index (`corpus.py`, `corpus.sqlite` in `STATE_DIRECTORY`) over Obsidian notes by section, PDF pages and annotations, from the files that the other two servers write in `KNOWLEDGE_DIR`; it updates itself by mtime.
- `check_draft`: Pandoc `[@citekey]` citations of a manuscript resolved against the library (Better BibTeX citekey, item key, unique surname+year); reports unresolved or ambiguous keys, unverified items, literal quotations without a page, retractions on request; returns the keys for the bibliography.
- `coverage_report`: items of a project without a note, PDF or verification; notes without an item or with a vanished item.
- `weekly_report` and `weekly.py` with `deploy/doi-weekly.{service,timer}`: new citing works per map, retractions in the library, coverage by project, PhilArchive harvest, backup with rotation; Markdown report in `KNOWLEDGE_DIR/reports` for the vault's Inbox folder.
- `philarchive` among the databases of `search_literature`: a local copy harvested via OAI-PMH (`philarchive.py`), incremental.
- `GET /status` on loopback without credentials.
- `tests/test_contract_flows.py`: the flows between servers on the contract files of the other two MCPs (`tests/fixtures/contract`).
- `draft` and `argument-map` skills; the others updated for `search_everything`, `coverage_report` and the weekly job.

## 0.5.0

- Bridge with Zotero: besides DOI and key, a work is matched by patent number (`P:`) and by title, first author's surname and year with the rules of `matching.py`; `library_match` says which criterion decided. Nodes carry `verified` (the `Verified:` line of the Zotero item) and `map_export` shows it.
- `map_add_patents` (only with EPO credentials): patents as `P:<number>` nodes with the cited literature that has a printed DOI; `map_get` returns `patent_edges` and `kind` (`work`, `book`, `patent`); patents do not enter co-citation, coupling or the merging of duplicates.
- The maps page was not updated: the new fields are not shown yet.

## 0.4.1

- `matching.py`: the bibliographic comparison (title normalisation, similarity with subtitle variants, surnames, year, errata and replies, parsing of bibliography entries) leaves `server.py` for a standard-library-only module, included in the wheel. No change of behaviour; the Zotero MCP uses an identical copy, so `create_item_from_doi` and `extract_references` apply the same rules as `find_doi` and `verify_doi`.
- `parse_ref` (bibliographies from PDFs): recognises APA entries with several initials before the year ("Schön, D. A. (1983). Title") and places of publication with accented letters; before, the title came out as "Schön, D. A. (1983)". The reference cache keys change for those entries, which are searched again once.
- Snapshots: `knowledge.py` reads `schema_version` from `library.json` and `notes.json`, rejects a version greater than the supported one (1) and reports it in `map_get` (`snapshots.problems`).
- `patent_get`: each literature citation carries `category` (the examiner's X, Y, A) in addition to `phase`.
- `skills/`: four Claude skills for the flows between the three MCPs (`add-to-library`, `reading-note`, `literature-map`, `prior-art`).

## 0.4.0

Tools removed or merged:
- `openalex_autocomplete` → `openalex_entity(entity, autocomplete=...)`.
- `semantic_scholar_recommendations` → `semantic_scholar` with `action` `recommend_single` / `recommend_multi`.
- The `dataset_*` actions of `semantic_scholar` → `semantic_scholar_datasets` (`releases`, `release`, `files`, `diffs`).
- `openalex_query` only executes; `translate`, `validate`, `context`, `properties`, `spec` → `openalex_query_inspect`.
- `map_watch` → `map_refresh(map, new_citing=True)`.
- `map_check_retractions` → `check_retractions(map=...)`.

What's new:
- `patent_search` and `patent_get` (EPO Open Patent Services), registered only with `EPO_OPS_KEY` and `EPO_OPS_SECRET`. The literature cited by a patent carries `doi` only if printed in the citation; a Crossref `doi_candidate` follows the rules of `find_doi` and must be confirmed with `verify_doi`.
- The gateway layer (`openalex_*`, `semantic_scholar*`, `google_books`, `orcid`) is registered only with `ENABLE_GATEWAY=true`.
- `check_citations`: cross-check between in-text citations and the APA bibliography.
- `dnb` (Deutsche Nationalbibliothek, SRU) among the databases of `search_literature`: monographs and translations in German, without abstracts. PhilArchive exposes only OAI-PMH, which does not allow searching.
- Offline reference set for `find_doi` (`tests/fixtures/find_doi_cases.json`) with precision and recall measured in the tests.
- Every tool answers with a dictionary with `status`; invalid arguments give `invalid_input` with the parameter and the allowed values.
- The link to the maps page is in the server instructions; `states`, `modes`, `scope` are enumerations; `per_page` 25 and `limit` 20 by default on the pass-throughs.
- Fixes: `find_doi` with `is_book=True` returns `edition_mismatch` for any different year, as documented (before, only for a one-year difference); the function for boolean query expansion was shadowed by a homonym in the maps, so Europe PMC, DOAJ, HAL and Zenodo failed at runtime.
- The wheel includes `maps.py` and `knowledge.py` (before they were missing and the installed package was broken).
- No reference to the deployment machine in code, descriptions and page; `KNOWLEDGE_DIR` and `STATE_DIRECTORY` documented.

## 0.3.0

State before this changelog.
