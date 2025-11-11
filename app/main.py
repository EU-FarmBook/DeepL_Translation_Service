import os
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body
from typing import Any, Dict, List, Union, Optional
from fastapi.params import Query
from fastapi.responses import StreamingResponse,JSONResponse
import httpx
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from pypdf import PdfReader

# Load environment variables from .env file
load_dotenv()

app = FastAPI(title="EU Farmbook Translation Service",
              description="Standalone FastAPI microservice for the translation of documents and JSON files using DeepL API.")

file_type = ['.ppt','.pptx','.doc','.docx','.pdf']

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


@app.post("/translate-document")
async def translate_document(request: TranslateDocumentRequest):
    import tempfile
    import os
    import deepl

    try:
        file_url = request.object_metadata.id
        if not file_url:
            raise HTTPException(status_code=400, detail="Missing file URL in object metadata")

        file_name = request.object_metadata.object_name
        file_ext = request.object_metadata.object_extension
        file_size = request.object_metadata.object_size
        async with httpx.AsyncClient() as client:
            response = await client.get(file_url)
            if response.status_code != 200:
                raise HTTPException(status_code=500,
                                    detail=f"Failed to download file: HTTP {response.status_code}")
            contents = response.content

        api_key = os.getenv("DEEPL_API_KEY")
        if not api_key:
            raise HTTPException(status_code=500, detail="DeepL API key not configured")

        deepl_client = deepl.Translator(api_key)

        with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as temp_input:
            temp_input.write(contents)
            input_path = temp_input.name
            pdf = PdfReader(input_path)
            number_of_pages = len(pdf.pages)

        output_path = tempfile.NamedTemporaryFile(suffix=file_ext, delete=False).name

        if file_ext not in file_type:
            return JSONResponse(content = {"message":f"The translation service does not support {file_ext} files"},status_code=400)

        if file_size > int(2E+8):
            return JSONResponse(content={"message":"File size over 25 MB"},status_code=400)

        if number_of_pages > 25:
            return JSONResponse(content={f"message":f"Translation service for files with {number_of_pages} pages is not supported"},status_code=400)

        deepl_client.translate_document_from_filepath(
                input_path,
                output_path,
                target_lang=request.target_lang,
                source_lang=request.source_lang
            )



        with open(output_path, "rb") as output_file:
            translated_content = output_file.read()

        output_filename = f"translated_{file_name}"

        os.unlink(input_path)
        os.unlink(output_path)

        return StreamingResponse(
            content=iter([translated_content]),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={output_filename}"}
        )

    except deepl.DeepLException as e:
        raise HTTPException(status_code=500, detail=f"DeepL API error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error translating document: {str(e)}")


@app.post("/translate-json")
async def translate_json(
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

    import deepl
    import os

    try:
        api_key = os.getenv("DEEPL_API_KEY")
        if not api_key:
            raise HTTPException(status_code=500, detail="DeepL API key not configured")

        deepl_client = deepl.Translator(api_key)
        translated_data = await translate_json_values(
            data,
            deepl_client,
            target_lang,
            source_lang
        )

        return translated_data

    except deepl.DeepLException as e:
        raise HTTPException(status_code=500, detail=f"DeepL API error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error translating JSON: {str(e)}")


@app.get("/deepl-usage")
async def get_deepl_usage():
    import deepl
    try:
        api_key = os.getenv("DEEPL_API_KEY")
        if not api_key:
            raise HTTPException(status_code=500, detail="DeepL API key not configured")

        deepl_client = deepl.Translator(api_key)
        return deepl_client.get_usage()

    except deepl.DeepLException as e:
        raise HTTPException(status_code=500, detail=f"DeepL API error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking DeepL usage: {str(e)}")


async def translate_json_values(
        data: Union[Dict, List, str, int, float, bool, None],
        translator: Any,
        target_lang: str,
        source_lang: str = None
) -> Union[Dict, List, str, int, float, bool, None]:
    """Recursively translate values in JSON while preserving keys and structure"""

    # Handle dictionary
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            result[key] = await translate_json_values(value, translator, target_lang, source_lang)
        return result

    # Handle list
    elif isinstance(data, list):
        return [await translate_json_values(item, translator, target_lang, source_lang) for item in data]

    # Translate string values
    elif isinstance(data, str) and data.strip():
        translation = translator.translate_text(
            data,
            target_lang=target_lang,
            source_lang=source_lang,
        )
        return str(translation)

    # Return non-translatable values as is
    else:
        return data