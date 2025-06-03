import io
import re
import os
import json
import uuid
import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Form
from google.cloud import storage
from PyPDF2 import PdfReader
from openai import OpenAI
from app.email_utils import send_confirmation_email
from app.routers.match import run_matching_for_user  # Importar la función de recálculo de matchings

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

def extract_text_from_pdf(pdf_bytes):
    """Extrae el texto completo de un PDF en formato bytes sin necesidad de guardarlo en disco."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = " ".join([page.extract_text() or "" for page in reader.pages])
        return text.strip()
    except Exception as e:
        raise Exception(f"Error extrayendo texto del PDF: {e}")

COMMON_TLDS = {"com", "org", "net", "edu", "gov", "io", "co", "us", "ar", "comar"}

def extract_email(text):
    """
    Extrae el primer email del texto y recorta cualquier texto extra pegado al TLD,
    usando una lista de TLDs comunes para determinar dónde cortar.
    """
    cleaned_text = re.sub(r'[\r\n\t]+', ' ', text)
    cleaned_text = re.sub(r'\s{2,}', ' ', cleaned_text)
    pattern = r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}[A-Za-z]*'
    match = re.search(pattern, cleaned_text)
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

    max_length = min(9, len(tld_contig)+1)
    valid_tld = None
    for i in range(max_length-1, 1, -1):
        possible_tld = tld_contig[:i].lower()
        if possible_tld in COMMON_TLDS:
            valid_tld = possible_tld
            break

    if valid_tld:
        final_email = candidate[:last_dot+1+len(valid_tld)]
        return final_email
    else:
        return candidate

def sanitize_filename(filename: str) -> str:
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "", filename)
    return filename

@router.post("/upload")
async def upload_cv(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    email: str = Form(None),
):
    try:
        file_bytes = await file.read()
        safe_filename = sanitize_filename(file.filename)

        # Subir a GCS en carpeta de pendings
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(f"pending_cv_uploads/{safe_filename}")
        blob.upload_from_string(file_bytes, content_type=file.content_type)

        # Extraer texto y email
        text_content = extract_text_from_pdf(file_bytes)
        if not text_content:
            raise HTTPException(status_code=400, detail="No se pudo extraer texto del CV")

        extracted_email = extract_email(text_content)
        user_email = (email or extracted_email or "").lower()
        if not user_email:
            raise HTTPException(status_code=400, detail="No se encontró un email válido en el CV")

        # Generar código de confirmación y almacenar en pending_users
        confirmation_code = str(uuid.uuid4())
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pending_users (id, email, confirmation_code, cv_url)
            VALUES (gen_random_uuid(), %s, %s, %s)
            ON CONFLICT (email)
            DO UPDATE SET confirmation_code = EXCLUDED.confirmation_code, cv_url = EXCLUDED.cv_url
            RETURNING id;
            """,
            (user_email, confirmation_code, blob.public_url),
        )
        result = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        # Enviar correo de confirmación
        background_tasks.add_task(send_confirmation_email, user_email, confirmation_code)

        # Si el usuario ya existe en "User", recalcular matchings inmediatamente
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT id FROM "User" WHERE email = %s;', (user_email,))
        existing = cur.fetchone()
        cur.close()
        conn.close()
        if existing:
            user_id = existing[0]
            background_tasks.add_task(run_matching_for_user, user_id)

        return {
            "message": f"Se ha enviado un email de confirmación a {user_email}. "
                       "Te recomendamos revisar tu bandeja de correo no deseado o spam.",
            "email": user_email,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando el CV: {e}")
