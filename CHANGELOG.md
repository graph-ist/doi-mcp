# Changelog

Le rinomine e le fusioni di strumenti rompono la compatibilità con i client: dopo l'aggiornamento il client deve rileggere l'elenco degli strumenti (in Claude web, disattivare e riattivare il connettore).

## Non pubblicata

- `MCP_ALLOW_QUERY_TOKEN=true`: il token è accettato anche come `?token=` nell'URL, per il modulo dei connettori di Claude web che ha solo nome e URL. Spento per default.
- Pagina *Literature maps*: pulsante *Add patents* (`map_add_patents`, da 1 a 20 numeri, anche come seed).
- Pagina *Literature maps*: il filtro degli anni si applica mentre si scrive (da quattro cifre), legge un intervallo invertito e, con sottocampo e off-topic, filtra anche la lista dei suggerimenti.

## 0.8.2

- Pagina *Literature maps* ripubblicata: brevetti (esagoni, archi punteggiati) e libri (triangoli), chip `verified` e criterio di abbinamento con Zotero, conteggio OpenCitations e `coverage_gaps` dopo il refresh, pulsanti e marcature degli intenti di citazione (`map_intents`, voce Debate); `map_watch` tolto dal manifest.

## 0.8.1

- Skill `concept-card` (schede di concetto con relazioni tipizzate, MCP Obsidian 0.5.0); fixture di contratto di Obsidian aggiornate con `relations`.

## 0.8.0

- OpenCitations Index come terzo conteggio nel `coverage_check` di `citation_graph` (`opencitations_cited_by`, `opencitations_references` e verdetti) e in `map_refresh`, che salva `cited_by_oc` per ogni seed e segnala in `coverage_gaps` i seed che OpenCitations conta molto più di OpenAlex; il report settimanale li elenca. `OPENCITATIONS_TOKEN` facoltativo.
- `coverage_report` restituisce anche `projects`, `anzsrc`, `years`, `notes_total`, `items_total`.
- Pagina *Knowledge views* (`pages/knowledge-views.html`): lo stato dei tre archivi, copertura per progetto, campi e anni, ricerca nel corpus, registro delle ricerche, report settimanale, mappe.

## 0.7.0

- `search_everything(semantic=true)`: ricerca per significato con embedding locali multilingue (multilingual-e5-small in ONNX, `semantic.py`, extra `semantic`); vettori in `corpus.sqlite`, indicizzazione incrementale a ogni chiamata, nel lavoro settimanale e con `semantic.py embed`; `semantic_unavailable` senza modello.
- `map_intents`: intenti, influenza e frasi delle citazioni da Semantic Scholar sugli archi di una mappa, con il flag euristico `contrasting`; `map_get` restituisce `edge_intents`.
- `GET /status` riporta la disponibilità dello strato semantico; il report settimanale lo stato dell'indicizzazione.

## 0.6.1

- Registro delle ricerche: ogni `search_literature` e `map_create` aggiunge una riga a `STATE_DIRECTORY/searches.jsonl`; `search_log(query)` dice se la stessa ricerca o una simile è già stata fatta, quando e con quanti risultati; il report settimanale elenca le ricerche della settimana.
- Lavoro settimanale: notifica ntfy con il riassunto quando il report è pronto (`NTFY_URL`, `NTFY_TOKEN`).
- Brevetti: il cognome del primo autore nelle citazioni di letteratura è letto anche nella forma «NOME COGNOME:» e con le particelle («VAN DER LINDEN J»).

## 0.6.0

Incompatibile con 0.5.x
- Trasporto HTTP: il token è accettato solo nell'header `Authorization: Bearer`; `?token=` nell'URL viene rifiutato.

Novità
- `search_everything`: un indice FTS5 (`corpus.py`, `corpus.sqlite` in `STATE_DIRECTORY`) su note Obsidian per sezione, pagine dei PDF e annotazioni, dai file che gli altri due server scrivono in `KNOWLEDGE_DIR`; si aggiorna da solo per mtime.
- `check_draft`: citazioni Pandoc `[@citekey]` di un manoscritto risolte sulla libreria (citekey Better BibTeX, chiave item, cognome+anno univoco); segnala chiavi irrisolte o ambigue, item non verificati, citazioni letterali senza pagina, ritrattazioni a richiesta; restituisce le chiavi per la bibliografia.
- `coverage_report`: item di un progetto senza nota, PDF o verifica; note senza item o con item sparito.
- `weekly_report` e `weekly.py` con `deploy/doi-weekly.{service,timer}`: nuovi lavori citanti per mappa, ritrattazioni in libreria, copertura per progetto, raccolta PhilArchive, backup con rotazione; report Markdown in `KNOWLEDGE_DIR/reports` per la cartella Inbox del vault.
- `philarchive` tra i database di `search_literature`: copia locale raccolta via OAI-PMH (`philarchive.py`), incrementale.
- `GET /status` su loopback senza credenziali.
- `tests/test_contract_flows.py`: i flussi tra server sui file di contratto degli altri due MCP (`tests/fixtures/contract`).
- Skill `draft` e `argument-map`; le altre aggiornate per `search_everything`, `coverage_report` e il lavoro settimanale.

## 0.5.0

- Ponte con Zotero: oltre a DOI e chiave, un lavoro viene abbinato per numero di brevetto (`P:`) e per titolo, cognome del primo autore e anno con le regole di `matching.py`; `library_match` dice quale criterio ha deciso. I nodi portano `verified` (la riga `Verified:` dell'item Zotero) e `map_export` lo mostra.
- `map_add_patents` (solo con credenziali EPO): brevetti come nodi `P:<numero>` con la letteratura citata a DOI stampato; `map_get` restituisce `patent_edges` e `kind` (`work`, `book`, `patent`); i brevetti non entrano in co-citazione, coupling né nella fusione dei doppioni.
- La pagina delle mappe non è stata aggiornata: i campi nuovi non sono ancora mostrati.

## 0.4.1

- `matching.py`: il confronto bibliografico (normalizzazione dei titoli, similarità con varianti di sottotitolo, cognomi, anno, errata e repliche, analisi delle voci di bibliografia) esce da `server.py` in un modulo di sola libreria standard, incluso nel wheel. Nessun cambiamento di comportamento; l'MCP Zotero ne usa una copia identica, così `create_item_from_doi` ed `extract_references` applicano le stesse regole di `find_doi` e `verify_doi`.
- `parse_ref` (bibliografie dai PDF): riconosce le voci APA con più iniziali prima dell'anno («Schön, D. A. (1983). Titolo») e i luoghi di edizione con lettere accentate; prima il titolo risultava «Schön, D. A. (1983)». Le chiavi della cache dei riferimenti cambiano per quelle voci, che vengono ricercate di nuovo una volta.
- Istantanee: `knowledge.py` legge `schema_version` da `library.json` e `notes.json`, rifiuta una versione maggiore di quella supportata (1) e lo riporta in `map_get` (`snapshots.problems`).
- `patent_get`: ogni citazione di letteratura porta `category` (X, Y, A dell'esaminatore) oltre a `phase`.
- `skills/`: quattro skill di Claude per i flussi tra i tre MCP (`add-to-library`, `reading-note`, `literature-map`, `prior-art`).

## 0.4.0

Strumenti rimossi o fusi:
- `openalex_autocomplete` → `openalex_entity(entity, autocomplete=...)`.
- `semantic_scholar_recommendations` → `semantic_scholar` con `action` `recommend_single` / `recommend_multi`.
- Le azioni `dataset_*` di `semantic_scholar` → `semantic_scholar_datasets` (`releases`, `release`, `files`, `diffs`).
- `openalex_query` esegue soltanto; `translate`, `validate`, `context`, `properties`, `spec` → `openalex_query_inspect`.
- `map_watch` → `map_refresh(map, new_citing=True)`.
- `map_check_retractions` → `check_retractions(map=...)`.

Novità:
- `patent_search` e `patent_get` (EPO Open Patent Services), registrati solo con `EPO_OPS_KEY` e `EPO_OPS_SECRET`. La letteratura citata da un brevetto porta `doi` solo se stampato nella citazione; un `doi_candidate` Crossref segue le regole di `find_doi` e va confermato con `verify_doi`.
- Lo strato gateway (`openalex_*`, `semantic_scholar*`, `google_books`, `orcid`) si registra solo con `ENABLE_GATEWAY=true`.
- `check_citations`: controllo incrociato tra citazioni nel testo e bibliografia APA.
- `dnb` (Deutsche Nationalbibliothek, SRU) tra i database di `search_literature`: monografie e traduzioni in tedesco, senza abstract. PhilArchive espone solo OAI-PMH, che non permette la ricerca.
- Insieme di riferimento offline per `find_doi` (`tests/fixtures/find_doi_cases.json`) con precisione e richiamo misurati nei test.
- Ogni strumento risponde con un dizionario con `status`; gli argomenti non validi danno `invalid_input` con il parametro e i valori ammessi.
- Il link alla pagina delle mappe è nelle istruzioni del server; `states`, `modes`, `scope` sono enumerazioni; `per_page` 25 e `limit` 20 di default sui passanti.
- Correzioni: `find_doi` con `is_book=True` restituisce `edition_mismatch` per qualsiasi anno diverso, come documentato (prima solo per una differenza di un anno); la funzione di espansione booleana delle query era oscurata da un'omonima delle mappe, per cui Europe PMC, DOAJ, HAL e Zenodo fallivano a runtime.
- Il wheel include `maps.py` e `knowledge.py` (prima mancavano e il pacchetto installato era rotto).
- Nessun riferimento alla macchina di deployment in codice, descrizioni e pagina; `KNOWLEDGE_DIR` e `STATE_DIRECTORY` documentate.

## 0.3.0

Stato precedente a questo changelog.
