import logging
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Optional, Union
from urllib.parse import quote

import deepl
import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# The DeepL client logs every status check at INFO, and httpx logs every request with its full
# URL, which may be a pre-signed storage URL. Keep both to warnings.
logging.getLogger("deepl").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("deepl_translation_service")

app = FastAPI(title="EU Farmbook Translation Service",
              description="Standalone FastAPI microservice for the translation of documents and JSON files using DeepL API.")

# The endpoints below are plain `def`, not `async def`, on purpose. The DeepL client is
# synchronous, and inside an `async def` it froze the whole event loop: every other request on
# the instance queued behind one translation. FastAPI runs `def` endpoints in a thread pool
# instead, so translations run side by side.

# httpx's default is 5 s for every phase, which a slow object-storage response could exceed.
DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Chunk size for streaming files to and from disk. The DeepL client's own default for fetching a
# translated document is 1 byte, which took ~10 s for a 3.6 MB file and ~80 s for 30 MB.
CHUNK_BYTES = 64 * 1024

# Total time a document translation may take, from request to translated file. The frontend
# gives up after 300 s, so DeepL's result would only be thrown away after that.
DOCUMENT_TIMEOUT_S = float(os.getenv("DOCUMENT_TIMEOUT_S", "280"))

# How often to ask DeepL whether a document is done. The client library's own loop waits a fixed
# 5 s between checks, which added 2.5 s to the average document.
POLL_INTERVAL_S = 1.0

# Uploading is the one step that holds a document in memory: the DeepL client builds the whole
# multipart body, about twice the file size at its peak (~64 MB for a 30 MB file). Everything else
# streams through disk. Capping concurrent uploads keeps a burst of large documents inside the
# container's memory limit; the other stages, including DeepL's processing, are not capped.
UPLOAD_SLOTS = threading.BoundedSemaphore(int(os.getenv("MAX_CONCURRENT_UPLOADS", "3")))

# object_extension becomes part of a temp-file path and is how DeepL detects the document
# type, so accept only a plain ".ext".
EXTENSION_PATTERN = re.compile(r"\.[A-Za-z0-9]{1,10}")

# DeepL accepts at most 50 texts per request, and a request body of at most 128 KiB.
MAX_TEXTS_PER_REQUEST = 50
MAX_BATCH_BYTES = 100_000

# Typographic punctuation that has no Latin-1 form and no Unicode decomposition.
FALLBACK_PUNCTUATION = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": "'", "”": "'"}

# Reused across requests rather than created per request, which cost a new TLS handshake each
# time. httpx.Client is thread-safe; the DeepL client wraps a requests.Session, which is not
# guaranteed to be, so each worker thread gets its own.
http_client = httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
thread_local = threading.local()


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

    translator = getattr(thread_local, "translator", None)
    if translator is None:
        translator = thread_local.translator = deepl.Translator(api_key)

    return translator


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


def to_http_exception(e: Exception, what: str) -> HTTPException:
    """Turn any exception raised while handling a request into the HTTPException to answer with."""
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, deepl.DeepLException):
        return deepl_http_exception(e)

    logger.exception("%s", what)
    return HTTPException(status_code=500, detail=f"{what}: {str(e)}")


@contextmanager
def timed(timings: Dict[str, float], stage: str) -> Iterator[None]:
    """Record in `timings` how long a stage took, including when it fails."""
    start = time.monotonic()
    try:
        yield
    finally:
        timings[stage] = time.monotonic() - start


def format_timings(timings: Dict[str, float]) -> str:
    return " ".join(f"{stage}={seconds:.1f}s" for stage, seconds in timings.items())


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


def download_file(url: str, path: str) -> None:
    """Stream the file at `url` to `path`, so it is never held in memory whole."""
    try:
        with http_client.stream("GET", url) as response:
            if response.status_code != 200:
                raise HTTPException(status_code=502,
                                    detail=f"Failed to download file: HTTP {response.status_code}")
            with open(path, "wb") as file:
                for chunk in response.iter_bytes(CHUNK_BYTES):
                    file.write(chunk)
    except httpx.TimeoutException as e:
        raise HTTPException(status_code=504, detail=f"Timed out downloading file: {type(e).__name__}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Failed to download file: {type(e).__name__}: {e}")


def wait_until_translated(translator: deepl.Translator, handle: deepl.DocumentHandle,
                          deadline: float) -> deepl.DocumentStatus:
    """Poll DeepL until the document is done, has failed, or the deadline has passed."""
    status = translator.translate_document_get_status(handle)
    while status.ok and not status.done:
        if time.monotonic() + POLL_INTERVAL_S > deadline:
            raise HTTPException(status_code=504,
                                detail=f"Timed out after {DOCUMENT_TIMEOUT_S:.0f} s waiting for DeepL "
                                       f"to translate the document")
        time.sleep(POLL_INTERVAL_S)
        status = translator.translate_document_get_status(handle)

    if not status.ok:
        # The same exception and message the client library raises, so it maps to 422 as before.
        raise deepl.DocumentTranslationException(
            f"Error occurred while translating document: {status.error_message or 'unknown error'}", handle)

    return status


def read_and_close(file: BinaryIO) -> Iterator[bytes]:
    with file:
        while chunk := file.read(CHUNK_BYTES):
            yield chunk


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

    started = time.monotonic()
    deadline = started + DOCUMENT_TIMEOUT_S
    timings: Dict[str, float] = {}
    context = (f"file={file_name!r} hash={request.object_metadata.object_hash} "
               f"target={request.target_lang} source={request.source_lang or 'auto'}")
    temp_dir = tempfile.mkdtemp()

    try:
        deepl_client = get_translator()
        input_path = os.path.join(temp_dir, f"input{file_ext}")
        output_path = os.path.join(temp_dir, f"output{file_ext}")

        with timed(timings, "download"):
            download_file(file_url, input_path)

        with timed(timings, "queued"):
            if not UPLOAD_SLOTS.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise HTTPException(status_code=504, detail="Timed out waiting for a free DeepL upload slot")
        try:
            with timed(timings, "upload"), open(input_path, "rb") as input_file:
                handle = deepl_client.translate_document_upload(
                    input_file,
                    target_lang=request.target_lang,
                    source_lang=request.source_lang or None,
                    filename=f"input{file_ext}"
                )
        finally:
            UPLOAD_SLOTS.release()

        with timed(timings, "deepl"):
            status = wait_until_translated(deepl_client, handle, deadline)

        with timed(timings, "fetch"), open(output_path, "wb") as output_file:
            deepl_client.translate_document_download(handle, output_file, chunk_size=CHUNK_BYTES)

        headers = {
            "Content-Disposition": content_disposition(f"translated_{file_name}"),
            "Content-Length": str(os.path.getsize(output_path)),
        }
        logger.info("document translated: %s in_bytes=%d out_bytes=%s billed_characters=%s %s total=%.1fs",
                    context, os.path.getsize(input_path), headers["Content-Length"], status.billed_characters,
                    format_timings(timings), time.monotonic() - started)

        # The open handle keeps the translated file readable while it is streamed back, even
        # though the directory is removed below (POSIX unlink semantics).
        translated_file = open(output_path, "rb")

    except Exception as e:
        error = to_http_exception(e, "Error translating document")
        logger.warning("document translation failed: %s status=%d %s total=%.1fs detail=%s",
                       context, error.status_code, format_timings(timings), time.monotonic() - started, error.detail)
        raise error

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return StreamingResponse(read_and_close(translated_file), media_type="application/octet-stream", headers=headers)


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

    started = time.monotonic()
    try:
        deepl_client = get_translator()

        # api-core sends source_lang="" when it has none; that means "auto-detect".
        return translate_json_values(data, deepl_client, target_lang, source_lang or None)

    except Exception as e:
        error = to_http_exception(e, "Error translating JSON")
        logger.warning("json translation failed: target=%s source=%s status=%d total=%.1fs detail=%s",
                       target_lang, source_lang or "auto", error.status_code, time.monotonic() - started, error.detail)
        raise error


@app.get("/deepl-usage")
def get_deepl_usage():
    try:
        deepl_client = get_translator()
        return deepl_client.get_usage()

    except Exception as e:
        raise to_http_exception(e, "Error checking DeepL usage")


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
    started = time.monotonic()
    texts: List[str] = []
    map_json_strings(data, texts.append)
    unique_texts = list(dict.fromkeys(texts))

    translations: Dict[str, str] = {}
    batch: List[str] = []
    batch_bytes = 0
    requests_made = 0

    def flush():
        nonlocal requests_made
        results = translator.translate_text(batch, target_lang=target_lang, source_lang=source_lang)
        translations.update(zip(batch, (result.text for result in results)))
        requests_made += 1

    for text in unique_texts:
        size = len(text.encode("utf-8"))
        if batch and (len(batch) == MAX_TEXTS_PER_REQUEST or batch_bytes + size > MAX_BATCH_BYTES):
            flush()
            batch, batch_bytes = [], 0
        batch.append(text)
        batch_bytes += size

    if batch:
        flush()

    logger.info("json translated: target=%s source=%s strings=%d unique=%d characters=%d deepl_requests=%d total=%.1fs",
                target_lang, source_lang or "auto", len(texts), len(unique_texts),
                sum(len(text) for text in unique_texts), requests_made, time.monotonic() - started)

    return map_json_strings(data, translations.__getitem__)
