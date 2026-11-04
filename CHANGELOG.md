# Changelog

Le rinomine e le fusioni di strumenti rompono la compatibilità con i client: dopo l'aggiornamento il client deve rileggere l'elenco degli strumenti (in Claude web, disattivare e riattivare il connettore).

## 0.4.0

Strumenti rimossi o fusi:
- `openalex_autocomplete` → `openalex_entity(entity, autocomplete=...)`.
- `semantic_scholar_recommendations` → `semantic_scholar` con `action` `recommend_single` / `recommend_multi`.
- Le azioni `dataset_*` di `semantic_scholar` → `semantic_scholar_datasets` (`releases`, `release`, `files`, `diffs`).
- `openalex_query` esegue soltanto; `translate`, `validate`, `context`, `properties`, `spec` → `openalex_query_inspect`.
- `map_watch` → `map_refresh(map, new_citing=True)`.
- `map_check_retractions` → `check_retractions(map=...)`.

Novità:
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
