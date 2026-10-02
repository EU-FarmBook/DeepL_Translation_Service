# DeepL Translation Service

A small FastAPI microservice that translates EU-FarmBook knowledge objects (KOs) with the
[DeepL API](https://developers.deepl.com/docs). It translates two things:

- **Documents**: the file attached to a KO (PDF, DOCX, PPTX, …). The translated file is returned
  as-is, with its layout preserved by DeepL.
- **JSON**: KO metadata (titles, summaries, keywords, …). Every string value is translated and the
  structure is kept.

The service is stateless. It does not store anything; the caller saves the result.

## Where it sits

The browser never calls this service directly. **api-core** is its only client, and it finds the
service through the `EUF_TRANSLATIONS_SERVICE` environment variable.

```
frontend ──► api-core /translations/translate_document ──► POST /translate-document ──► DeepL
                 │                                                    │
                 │   ◄──────────── translated file ───────────────────┘
                 ▼
             api-database  (stores the translated file in S3 + MongoDB)

frontend ──► api-core /translations/translate_json ──► POST /translate-json ──► DeepL
```

For documents, api-core looks up the KO file's metadata in api-database and sends it here. This
service downloads the file from its `@id` URL, has DeepL translate it, and streams the result
back. api-core then saves it through api-database as a new "available translation" of the KO.

## API

Interactive docs are served at `/docs`; the OpenAPI schema is at `/openapi.json`.

### `POST /translate-document`

Request body:

```json
{
  "object_metadata": {
    "@id": "https://<object-storage-host>/<bucket>/<object-key>",
    "object_name": "soil-health-factsheet.pdf",
    "object_hash": "5d41402abc4b2a76b9719d911017c592",
    "object_extension": ".pdf",
    "object_size": 3612345
  },
  "target_lang": "DE",
  "source_lang": "EN"
}
```

| Field | Notes |
|---|---|
| `object_metadata.@id` | URL the service downloads the file from with a plain `GET` (redirects are followed, 60 s timeout). It must be directly reachable from the service, with no extra auth headers. |
| `object_metadata.object_extension` | A dot plus 1–10 letters or digits (`.pdf`, `.docx`). DeepL detects the document type from it; anything else is rejected with `422` before DeepL is called. |
| `object_metadata.object_name` | Used only to build the response filename (`translated_<object_name>`). Any characters are fine. |
| `object_hash`, `object_size` | Required by the schema but not used. |
| `target_lang` | Required. See [Language codes](#language-codes). |
| `source_lang` | Optional. Leave it out to let DeepL detect the language. |

Response: `200` with the translated file as `application/octet-stream` and a
`Content-Disposition` header carrying the name `translated_<object_name>` twice:

```
attachment; filename*=UTF-8''translated_%CE%9F%CE%B4%CE%B7%CE%B3%CF%8C%CF%82.pdf; filename="translated_______.pdf"
```

- `filename*` (RFC 5987) holds the exact name, percent-encoded as UTF-8.
- `filename`, which comes **last** on purpose, holds a Latin-1-safe copy. Latin-1 characters are
  kept as they are; others become their unaccented letter (`č` → `c`), a plain `-` or `'` for
  typographic dashes and quotes, or `_`. api-core reads the name by splitting the header on
  `filename=` and saves the translated file under it, so keep this order.

The call is **synchronous**: the request stays open until DeepL has finished. A few-MB PDF
typically takes 20–30 seconds. Translations run in a thread pool, so several can be in progress
at once. File types and size limits are DeepL's; see
[DeepL's document translation docs](https://developers.deepl.com/docs/api-reference/document).

### `POST /translate-json?target_lang=DE&source_lang=EN`

The body is any JSON object. Every non-blank string, at any depth, is translated. Keys, numbers,
booleans, `null`, empty strings and the overall structure are returned unchanged.

```bash
curl -X POST 'http://localhost:8008/translate-json?target_lang=DE' \
  -H 'Content-Type: application/json' \
  -d '{"title": "Soil health", "keywords": ["cover crops", "compost"], "pages": 12}'
```

```json
{"title": "Bodengesundheit", "keywords": ["Zwischenfrüchte", "Kompost"], "pages": 12}
```

Identical strings are translated once, and the strings are sent to DeepL in batches (at most 50
texts and ~100 KB per request), so a typical KO's metadata takes a single DeepL request. An empty
`source_lang` is treated as "auto-detect".

### `GET /deepl-usage`

Returns the DeepL account's usage, i.e. characters used and the character limit for the
current billing period.

## Language codes

Codes are passed to DeepL unchanged (case-insensitive). The service does no validation of its
own, so DeepL's rules apply:

- **`target_lang`**: bare `EN` and `PT` are rejected. Use `EN-GB` / `EN-US` and `PT-PT` / `PT-BR`.
  api-core already maps `en → en-gb` and `pt → pt-pt` before calling this service.
- **`source_lang`**: bare codes only (`EN`, not `EN-GB`). Leave it out for auto-detection.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `DEEPL_API_KEY` | yes | DeepL API key. Keys ending in `:fx` (free plan) are routed to DeepL's free endpoint automatically. |

The key is read from the environment, or from a `.env` file in the working directory
(`.env` is git-ignored).

> **Careful:** every call (including local testing) uses real DeepL characters from whichever
> key you configure. The dev and prd deployments share one DeepL account.

## Running locally

Python 3.12.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo 'DEEPL_API_KEY=your-key-here' > .env
uvicorn app.main:app --reload --port 8008
```

Then open <http://localhost:8008/docs>, or check the key works with:

```bash
curl http://localhost:8008/deepl-usage
```

### With Docker

```bash
docker compose up --build
```

This builds the image, mounts the project directory into the container (so `.env` is picked up
from it) and serves on port 8008 with auto-reload. The image itself runs without `--reload`, and
`.dockerignore` keeps `.env`, `.git` and local tooling out of it.

## Errors

Errors are JSON: `{"detail": "<message>"}`. DeepL's own message is kept, prefixed with
`DeepL API error:`.

| Status | When |
|---|---|
| `400` | `@id` is empty. |
| `422` | Body or query parameters don't match the schema (FastAPI validation); `object_extension` isn't a plain `.ext`; or DeepL rejected the request or the document (unsupported language code, unsupported/oversized/unreadable file, …). Retrying won't help. |
| `429` | DeepL is rate-limiting us, even after the client library's own retries. Retry later. |
| `502` | The file couldn't be downloaded (connection error or a non-200 answer from storage), the DeepL key was refused, DeepL couldn't be reached, or DeepL had a server error. |
| `503` | The DeepL account's character quota for this billing period is used up. |
| `504` | Downloading the file from storage timed out. |
| `500` | Anything unexpected, i.e. a bug in this service; also when `DEEPL_API_KEY` isn't set. |

api-core passes `4xx` responses through to its caller unchanged and turns `5xx` responses into its
own `502`, with this service's status and body under `detail.upstream_status` / `upstream_body`.

## Deployment

- The image is built from the `Dockerfile` (`python:3.12.8`, uvicorn on port **8008**). The image
  build and push are not defined in this repository.
- It runs on UGent's Nomad cluster as job `deepl-translations-service` (defined in the
  `nomad-jobs-ns-farmbook` repository) using image `farmbook/deepl-translations-service:latest`:
  one instance, 500 MHz CPU, 512 MB memory.
- `DEEPL_API_KEY` is injected from Vault at deploy time.
- It registers in Consul as `farmbook-deepl-translations-service`. No health check is configured
  yet.

## Known limitations

- **Synchronous document translation.** The caller's request stays open for the whole DeepL run,
  and nothing is stored here, so a request cut off by a timeout further up the chain has to be
  repeated (and DeepL charges again).
- **Approximate names for some scripts.** Greek, Cyrillic and similar names survive only in
  `filename*`. api-core currently reads the plain `filename`, so for those files it saves a name
  made of `_` (the extension is kept).
- **No authentication in the app.** Access control is left to the deployment, so the service
  should only be reachable by api-core.
