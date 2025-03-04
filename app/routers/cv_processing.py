from fastapi import APIRouter, HTTPException, UploadFile, File
import io
from PyPDF2 import PdfReader
import docx

router = APIRouter(
    prefix="/cv",
    tags=["cv"]
)

def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            extracted = page.extract_text()
            text += extracted if extracted else ""
        return text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error extrayendo texto del PDF: {e}")

def extract_text_from_docx(file_bytes: bytes) -> str:
    try:
        document = docx.Document(io.BytesIO(file_bytes))
        text = "\n".join([para.text for para in document.paragraphs])
        return text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error extrayendo texto del DOCX: {e}")

@router.post("/process")
async def process_cv(file: UploadFile = File(...)):
    # Lee el contenido del archivo
    file_bytes = await file.read()
    
    # Dependiendo del tipo de archivo, extraemos el texto
    if file.content_type == "application/pdf":
        text = extract_text_from_pdf(file_bytes)
    elif file.content_type in [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword"
    ]:
        text = extract_text_from_docx(file_bytes)
    else:
        raise HTTPException(status_code=400, detail="Tipo de archivo no soportado. Se aceptan PDF o DOCX.")
    
    if not text:
        raise HTTPException(status_code=400, detail="No se pudo extraer texto del archivo.")
    
    # Para depuración, logueamos el texto extraído (o al menos los primeros caracteres)
    print("Texto extraído (primeros 200 caracteres):", text[:200])
    
    return {"message": "Texto extraído correctamente", "text": text}
