# paper-doi-mcp

Server MCP per verificare citazioni accademiche e cercare letteratura. Risolve e controlla DOI, verifica edizioni di libri, cerca su più database aperti e costruisce grafi di citazione da visualizzare. Il principio guida è la precisione: quando una corrispondenza non è sicura, lo strumento lo dice invece di proporre un DOI plausibile.

## Strumenti

| Tool | Cosa fa | Fonti |
|---|---|---|
| `find_doi` | Trova il DOI di un articolo o libro a partire da titolo, primo autore e anno. Restituisce `match` solo se titolo, autore e anno concordano; altrimenti `no_confident_match`. Per i libri (`is_book=true`) l'anno deve coincidere con l'edizione citata. | Crossref, Semantic Scholar, OpenAlex, Open Library |
| `verify_doi` | Controlla che un DOI esista e appartenga al lavoro atteso (titolo, autore, anno, volume, numero, pagine). Segnala ritrattazioni, correzioni ed expressions of concern. | Crossref (con dati Retraction Watch), doi.org |
| `verify_book` | Verifica un'edizione (anno, editore, ISBN) confrontando tre cataloghi. `verified` se almeno due concordano. | Open Library, Crossref, Deutsche Nationalbibliothek |
| `format_apa` | Restituisce il riferimento APA di un DOI tramite content negotiation. `unsupported` se l'agenzia di registrazione non lo offre. | doi.org |
| `search_literature` | Ricerca con sintassi booleana su uno o più database, con abstract e keyword, deduplicazione e log riproducibile delle query. | OpenAlex, Europe PMC, Semantic Scholar, HAL, DOAJ, arXiv, OSF Preprints, Zenodo |
| `get_metadata` | Abstract, keyword, topic, citazioni e accesso aperto per un DOI o un identificativo arXiv. | OpenAlex, Semantic Scholar, arXiv |
| `citation_graph` | Grafo di citazione attorno a uno o più lavori: riferimenti, lavori citanti più citati, archi di citazione e coppie co-citate. Pensato per visualizzazioni D3 (timeline anno/citazioni, rete di co-citazione). | OpenAlex |

## Come recupera e confronta i dati

**Richieste.** Tutte le chiamate passano da un unico client HTTP condiviso. Un trasporto dedicato limita le richieste contemporanee per host (5 di default, 1 per Semantic Scholar e arXiv) e impone un intervallo minimo dove le API lo chiedono (1 s per Semantic Scholar, 3 s per arXiv). Sui 429 il server ritenta fino a quattro volte, con attese di al massimo 10 s. Le fonti vengono interrogate in parallelo: se una fallisce, l'errore finisce in `source_errors` e le altre proseguono. I record Crossref restano in cache per un'ora.

**Confronto dei titoli.** I titoli vengono normalizzati (accenti rimossi, script non latini conservati, HTML e prefissi come "RETRACTED:" eliminati) e confrontati con `SequenceMatcher`, considerando anche le varianti con e senza sottotitolo. La soglia è 0.88.

**Regole di accettazione in `find_doi`.**
- Un autore che non corrisponde, o un anno distante più di uno, esclude il candidato.
- Titoli sotto le quattro parole richiedono la conferma dell'autore.
- Errata, avvisi di ritrattazione, peer review e repliche vengono scartati (`excluded_derivatives`).
- Le fonti secondarie ricavano i DOI in buona parte da Crossref: la presenza di un DOI in più indici non è una conferma indipendente.

**Grafo di citazione.** `citation_graph` parte dai lavori seed, scarica i loro riferimenti e i lavori citanti più citati, ordina i candidati per numero di seed collegati e poi per citazioni, e tiene i primi `max_nodes`. Le co-citazioni sono calcolate solo sui lavori presenti nel grafo, quindi sono un limite inferiore.

## Limiti noti

- La filosofia è coperta male da tutte le fonti; PhilPapers non ha un'API accessibile.
- OpenAlex ha liste di riferimenti incomplete per molti lavori umanistici e datati.
- In `search_literature` i filtri `doc_types`, `open_access_only` ed `exclude_retracted` valgono solo per OpenAlex; gli altri database li riportano in `ignored_filters`. La deduplicazione vale per la singola pagina.
- Senza chiave API Semantic Scholar risponde spesso 429.
- Le edizioni ("2nd ed.") e il numero di pagine dei libri non sono verificabili con queste fonti.

## Installazione

Richiede Python 3.10+ e [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest -q tests
```

### Uso locale (stdio), ad esempio in VS Code

```json
{
  "servers": {
    "paper-doi": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/percorso/a/doi-mcp", "run", "server.py"],
      "env": { "CONTACT_EMAIL": "tu@example.org" }
    }
  }
}
```

### Uso remoto (HTTP), ad esempio per Claude web

Con `MCP_TRANSPORT=http` il server espone Streamable HTTP su `127.0.0.1` e richiede un token, passato come header `Authorization: Bearer` oppure come parametro `?token=` (Claude web non permette header personalizzati). Va pubblicato dietro un proxy HTTPS, ad esempio Tailscale Funnel o Cloudflare Tunnel. Il log degli accessi è disattivato perché il token viaggia nell'URL.

## Variabili d'ambiente

| Variabile | Uso |
|---|---|
| `CONTACT_EMAIL` | Email per il polite pool di Crossref e OpenAlex (consigliata) |
| `SEMANTIC_SCHOLAR_API_KEY` | Riduce i 429 di Semantic Scholar |
| `MCP_TRANSPORT` | `stdio` (default) o `http` |
| `PORT` | Porta HTTP, default 3002 |
| `MCP_AUTH_TOKEN` | Obbligatorio in modalità HTTP, almeno 32 caratteri (`openssl rand -hex 32`) |
| `MCP_ALLOWED_HOSTS` | Host pubblici accettati dietro il proxy, separati da virgola |
