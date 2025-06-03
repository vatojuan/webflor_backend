import io
import re
import os
import json
import uuid
import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Depends
from google.cloud import storage
from PyPDF2 import PdfReader
from openai import OpenAI
from app.email_utils import send_confirmation_email
from app.routers.match import run_matching_for_user  # Para recalcular matchings
from app.routers.auth import get_current_user          # Dependencia que devuelve el usuario logueado

load_dotenv()

# Configuración de Google Cloud Storage
service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
storage_client = storage.Client.from_service_account_info(service_account_info)
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")

# Configuración de OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv("DBNAME", "postgres"),
        user=os.getenv("USER", "postgres.apnfioxjddccokgkljvd"),
        password=os.getenv("PASSWORD", "Pachamama190"),
        host=os.getenv("HOST", "aws-0-sa-east-1.pooler.supabase.com"),
        port=5432,
        sslmode="require"
    )

router = APIRouter(prefix="/cv", tags=["cv"])

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extrae el texto completo de un PDF sin guardarlo en disco."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "".join([page.extract_text() or "" for page in reader.pages])
        return text.strip()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error extrayendo texto del PDF: {e}")

COMMON_TLDS = {"com", "org", "net", "edu", "gov", "io", "co", "us", "ar", "comar"}

def extract_email(text: str) -> str | None:
    """
    Extrae el primer email del texto y recorta cualquier texto extra pegado al TLD,
    apoyándose en la lista COMMON_TLDS.
    """
    cleaned = re.sub(r'[\r\n\t]+', ' ', text)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned)
    pattern = r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}[A-Za-z]*'
    match = re.search(pattern, cleaned)
    if not match:
        return None
    candidate = match.group(0)
    last_dot = candidate.rfind('.')
    if last_dot == -1:
        return candidate
    tld_contig = ""
    for ch in candidate[last_dot+1:]:
        if ch.isalpha():
            tld_contig += ch
        else:
            break
    max_len = min(9, len(tld_contig) + 1)
    valid_tld = None
    for i in range(max_len - 1, 1, -1):
        possible = tld_contig[:i].lower()
        if possible in COMMON_TLDS:
            valid_tld = possible
            break
    if valid_tld:
        return candidate[: last_dot + 1 + len(valid_tld)]
    return candidate

def sanitize_filename(filename: str) -> str:
    fname = filename.replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_.-]", "", fname)

@router.post(
    "/process",
    summary="Procesar CV (PDF/DOCX) y recalcular matchings si el usuario existe",
)
async def process_cv(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """
    1) Extrae texto de PDF o DOCX.
    2) Obtiene el email del CV o del usuario autenticado si no está en el archivo.
    3) Almacena el archivo en GCS bajo 'user_cvs/{user_id}/{uuid}_{nombre}'.
    4) Actualiza el registro del usuario con la URL del CV.
    5) Recalcula matchings llamando a run_matching_for_user(user_id).
    """
    try:
        # 1) Leer bytes y determinar tipo
        file_bytes = await file.read()
        content_type = file.content_type
        if content_type == "application/pdf":
            text = extract_text_from_pdf(file_bytes)
        elif content_type in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ):
            # mismo proceso, usando PdfReader al pasar a texto
            from docx import Document
            document = Document(io.BytesIO(file_bytes))
            text = "\n".join([para.text for para in document.paragraphs])
        else:
            raise HTTPException(
                status_code=400,
                detail="Tipo de archivo no soportado. Solo PDF o DOCX.",
            )
        if not text:
            raise HTTPException(status_code=400, detail="No se extrajo texto del CV.")

        # 2) Determinar email: preferimos el del usuario autenticado
        user_email = current_user.get("email")
        if not user_email:
            # Si por algún motivo no viene en token (raro), buscamos en texto
            extracted = extract_email(text)
            if not extracted:
                raise HTTPException(
                    status_code=400, detail="No se encontró email en el CV ni en el token."
                )
            user_email = extracted.lower()

        user_id = current_user.get("id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Usuario no autenticado correctamente.")

        # 3) Subir el archivo a GCS en carpeta del usuario
        safe_name = sanitize_filename(file.filename)
        unique_prefix = str(uuid.uuid4())
        blob_path = f"user_cvs/{user_id}/{unique_prefix}_{safe_name}"
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)
        blob.upload_from_string(file_bytes, content_type=content_type)
        cv_url = blob.public_url

        # 4) Actualizar la columna cvUrl del usuario en BD
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'UPDATE "User" SET "cvUrl" = %s WHERE id = %s RETURNING id;',
            (cv_url, user_id),
        )
        updated = cur.fetchone()
        if not updated:
            raise HTTPException(status_code=404, detail="Usuario no encontrado en BD.")
        conn.commit()
        cur.close()
        conn.close()

        # 5) Recalcular matchings para este usuario
        background_tasks.add_task(run_matching_for_user, user_id)

        return {"message": "CV procesado y matchings en cola de recálculo.", "cvUrl": cv_url}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando CV: {e}")
