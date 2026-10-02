import os
import re
import tempfile
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Union
from urllib.parse import quote

import deepl
import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

# Load environment variables from .env file
load_dotenv()

app = FastAPI(title="EU Farmbook Translation Service",
              description="Standalone FastAPI microservice for the translation of documents and JSON files using DeepL API.")

# The endpoints below are plain `def`, not `async def`, on purpose. The DeepL client is
# synchronous (a document translation blocks in time.sleep() while DeepL works, often for
# 20-30 s), and inside an `async def` that froze the whole event loop: every other request
# on the instance queued behind one translation. FastAPI runs `def` endpoints in a thread
# pool instead, so translations run side by side.

# httpx's default is 5 s for every phase, which a slow object-storage response could exceed.
DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# object_extension becomes part of a temp-file path and is how DeepL detects the document
# type, so accept only a plain ".ext".
EXTENSION_PATTERN = re.compile(r"\.[A-Za-z0-9]{1,10}")

# DeepL accepts at most 50 texts per request, and a request body of at most 128 KiB.
MAX_TEXTS_PER_REQUEST = 50
MAX_BATCH_BYTES = 100_000

# Typographic punctuation that has no Latin-1 form and no Unicode decomposition.
FALLBACK_PUNCTUATION = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": "'", "”": "'"}


class DocumentMetadata(BaseModel):
    id: str = Field(..., alias="@id")
    object_name: str
    object_hash: str
    object_extension: str
    object_size: int

    class Config:
        populate_by_name = True


class TranslateDocumentRequest(BaseModel):
    object_metadata: DocumentMetadata
    target_lang: str
    source_lang: Optional[str] = None


def get_translator() -> deepl.Translator:
    api_key = os.getenv("DEEPL_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="DeepL API key not configured")

    return deepl.Translator(api_key)


def deepl_http_exception(e: deepl.DeepLException) -> HTTPException:
    """Map a DeepL failure onto a status that tells the caller what kind of failure it was.

    Everything used to be a 500, so api-core (which turns any 5xx into its own 502) could not
    tell "DeepL cannot translate this document" apart from "the service is broken".
    """
    # translate_document() wraps errors raised while it waits for DeepL; classify the cause.
    cause = e.__cause__ if isinstance(e, deepl.DocumentTranslationException) else None
    error = cause if isinstance(cause, deepl.DeepLException) else e
    detail = f"DeepL API error: {str(e)}"

    if isinstance(error, deepl.TooManyRequestsException):
        # The DeepL client has already retried with backoff before giving up.
        return HTTPException(status_code=429, detail=detail)
    if isinstance(error, deepl.QuotaExceededException):
        return HTTPException(status_code=503, detail=detail)
    if isinstance(error, (deepl.AuthorizationException, deepl.ConnectionException)):
        return HTTPException(status_code=502, detail=detail)
    if isinstance(error, deepl.DocumentTranslationException):
        # DeepL accepted the document but could not translate it (e.g. no extractable text).
        return HTTPException(status_code=422, detail=detail)
    if error.http_status_code is None or 400 <= error.http_status_code < 500:
        # The request itself was rejected: by DeepL (bad language code, unsupported or oversized
        # file, ...) or, with no status, by the client library before sending (target_lang "EN").
        return HTTPException(status_code=422, detail=detail)

    return HTTPException(status_code=502, detail=detail)


def content_disposition(filename: str) -> str:
    """Build an attachment header that works for any filename.

    Starlette encodes header values as Latin-1, so writing the raw name in the header raised
    for Greek or Polish letters, an en dash or a curly apostrophe. That happened after DeepL
    had already translated (and billed) the document.

    The exact name goes in RFC 5987's `filename*`. The plain `filename` comes last and keeps
    every Latin-1 character as-is, because api-core reads the name by splitting the header on
    "filename=" and saves the translated file under whatever follows.
    """
    fallback = []
    for char in filename:
        char = FALLBACK_PUNCTUATION.get(char, char)
        if ord(char) > 0xFF:
            # Drop accents where the base letter exists (ą -> a, č -> c); otherwise "_".
            folded = "".join(c for c in unicodedata.normalize("NFKD", char)
                             if ord(c) <= 0xFF and not unicodedata.combining(c))
            char = folded or "_"
        if char == '"':
            char = "'"
        elif char == "\\" or ord(char) < 0x20 or 0x7F <= ord(char) < 0xA0:
            char = "_"
        fallback.append(char)

    return f"attachment; filename*=UTF-8''{quote(filename, safe='')}; filename=\"{''.join(fallback)}\""


@app.post("/translate-document")
def translate_document(request: TranslateDocumentRequest):
    file_url = request.object_metadata.id
    if not file_url:
        raise HTTPException(status_code=400, detail="Missing file URL in object metadata")

    file_name = request.object_metadata.object_name
    file_ext = request.object_metadata.object_extension
    if not EXTENSION_PATTERN.fullmatch(file_ext):
        raise HTTPException(status_code=422,
                            detail=f"Unsupported file extension {file_ext!r}: expected a form like '.pdf', "
                                   f"which DeepL uses to detect the document type")

    try:
        deepl_client = get_translator()

        try:
            with httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                response = client.get(file_url)
        except httpx.TimeoutException as e:
            raise HTTPException(status_code=504, detail=f"Timed out downloading file: {type(e).__name__}")
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Failed to download file: {type(e).__name__}: {e}")

        if response.status_code != 200:
            raise HTTPException(status_code=502,
                                detail=f"Failed to download file: HTTP {response.status_code}")

        # The directory, and both files in it, are removed however the translation ends.
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = os.path.join(temp_dir, f"input{file_ext}")
            output_path = os.path.join(temp_dir, f"output{file_ext}")

            with open(input_path, "wb") as input_file:
                input_file.write(response.content)

            deepl_client.translate_document_from_filepath(
                input_path,
                output_path,
                target_lang=request.target_lang,
                source_lang=request.source_lang or None
            )

            with open(output_path, "rb") as output_file:
                translated_content = output_file.read()

        return Response(
            content=translated_content,
            media_type="application/octet-stream",
            headers={"Content-Disposition": content_disposition(f"translated_{file_name}")}
        )

    except HTTPException:
        raise
    except deepl.DeepLException as e:
        raise deepl_http_exception(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error translating document: {str(e)}")


@app.post("/translate-json")
def translate_json(
    data: dict = Body(..., description="JSON content to translate"),
    target_lang: str = Query(..., description="Target language code"),
    source_lang: str = Query(None, description="Source language code (optional)")
):
    """Translate JSON content using DeepL API.
    Example input format:
    {
        "key1": "Hello",
        "key2": {
            "key3": "World"
        },
        "key4": [
            "This is a test",
            "Another test"
        ]
    }

   Query parameters (in the URL):
    ```
      /translate-json?target_lang=DE&source_lang=EN
    ```
    """

    try:
        deepl_client = get_translator()

        # api-core sends source_lang="" when it has none; that means "auto-detect".
        return translate_json_values(data, deepl_client, target_lang, source_lang or None)

    except HTTPException:
        raise
    except deepl.DeepLException as e:
        raise deepl_http_exception(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error translating JSON: {str(e)}")


@app.get("/deepl-usage")
def get_deepl_usage():
    try:
        deepl_client = get_translator()
        return deepl_client.get_usage()

    except HTTPException:
        raise
    except deepl.DeepLException as e:
        raise deepl_http_exception(e)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking DeepL usage: {str(e)}")


JsonValue = Union[Dict, List, str, int, float, bool, None]


def map_json_strings(data: JsonValue, fn: Callable[[str], Any]) -> JsonValue:
    """Rebuild `data` with every non-blank string passed through `fn`; keys and structure are kept."""
    if isinstance(data, dict):
        return {key: map_json_strings(value, fn) for key, value in data.items()}
    if isinstance(data, list):
        return [map_json_strings(item, fn) for item in data]
    if isinstance(data, str) and data.strip():
        return fn(data)
    return data


def translate_json_values(
        data: JsonValue,
        translator: deepl.Translator,
        target_lang: str,
        source_lang: Optional[str] = None
) -> JsonValue:
    """Translate every string value in `data`, preserving keys and structure.

    Strings are de-duplicated and sent to DeepL in batches rather than one request each:
    a KO's metadata easily holds dozens of strings, and one round-trip per string was slow
    and ran into DeepL's rate limit.
    """
    texts: List[str] = []
    map_json_strings(data, texts.append)
    unique_texts = list(dict.fromkeys(texts))

    translations: Dict[str, str] = {}
    batch: List[str] = []
    batch_bytes = 0

    def flush():
        results = translator.translate_text(batch, target_lang=target_lang, source_lang=source_lang)
        translations.update(zip(batch, (result.text for result in results)))

    for text in unique_texts:
        size = len(text.encode("utf-8"))
        if batch and (len(batch) == MAX_TEXTS_PER_REQUEST or batch_bytes + size > MAX_BATCH_BYTES):
            flush()
            batch, batch_bytes = [], 0
        batch.append(text)
        batch_bytes += size

    if batch:
        flush()

    return map_json_strings(data, translations.__getitem__)
