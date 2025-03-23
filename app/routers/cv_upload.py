import io
import random
import string
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

def extract_email(text):
    """
    Limpia el texto y busca un email en dos pasadas:
    1) Regex formal (con lookahead).
    2) Si no hay match, forzamos un espacio tras el TLD pegado a la siguiente palabra.
    """
    # Limpieza inicial
    cleaned_text = re.sub(r'[\n\r\t]+', ' ', text)
    cleaned_text = re.sub(r'\s{2,}', ' ', cleaned_text)

    # Regex con lookahead
    pattern = r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,8}(?=\b|[^a-zA-Z])'

    # PASADA 1
    emails = re.findall(pattern, cleaned_text)
    if emails:
        return emails[0]

    # PASADA 2: Forzamos espacio si el TLD se junta con una letra
    forced_text = re.sub(r'(\.[a-zA-Z]{2,8})([A-Za-z])', r'\1 \2', cleaned_text)
    emails2 = re.findall(pattern, forced_text)
    if emails2:
        return emails2[0]

    # Nada encontrado
    return None

def sanitize_filename(filename: str) -> str:
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "", filename)
    return filename

@router.post("/upload")
async def upload_cv(background_tasks: BackgroundTasks, file: UploadFile = File(...), email: str = Form(None)):
    try:
        file_bytes = await file.read()
        print(f"✅ Archivo recibido: {file.filename}, tamaño: {len(file_bytes)} bytes")

        safe_filename = sanitize_filename(file.filename)
        print(f"✅ Nombre del archivo normalizado: {safe_filename}")

        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(f"pending_cv_uploads/{safe_filename}")
        blob.upload_from_string(file_bytes, content_type=file.content_type)
        print(f"✅ Archivo subido a GCS: {blob.public_url}")

        text_content = extract_text_from_pdf(file_bytes)
        if not text_content:
            raise HTTPException(status_code=400, detail="No se pudo extraer texto del CV")
        print(f"✅ Texto extraído correctamente. Total de caracteres: {len(text_content)}")
        print(f"🔎 Fragmento inicial del texto:\n{text_content[:500]}")

        extracted_email = extract_email(text_content)
        user_email = email or extracted_email
        if not user_email:
            raise HTTPException(status_code=400, detail="No se encontró un email válido en el CV")
        print(f"✅ Email extraído: {user_email}")

        user_email = user_email.lower()
        print(f"✅ Email convertido a minúsculas: {user_email}")

        confirmation_code = str(uuid.uuid4())
        print(f"✅ Código de confirmación generado: {confirmation_code}")

        # Insertar en pending_users
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO pending_users (id, email, confirmation_code, cv_url)
                VALUES (gen_random_uuid(), %s, %s, %s)
                ON CONFLICT (email)
                DO UPDATE SET confirmation_code = EXCLUDED.confirmation_code, cv_url = EXCLUDED.cv_url
                """,
                (user_email, confirmation_code, blob.public_url)
            )
            conn.commit()
            cur.close()
            conn.close()
            print("✅ Registro pendiente insertado/actualizado en la base de datos")
        except Exception as db_err:
            print(f"❌ Error insertando en la base de datos: {db_err}")
            raise HTTPException(status_code=500, detail=f"Error insertando en la base de datos: {db_err}")

        # Enviar email de confirmación
        background_tasks.add_task(send_confirmation_email, user_email, confirmation_code)

        return {
            "message": f"Se ha enviado un email de confirmación a {user_email}. "
                       f"Te recomendamos revisar tu bandeja de correo no deseado o spam.",
            "email": user_email
        }
    except Exception as e:
        print(f"❌ Error procesando el CV: {e}")
        raise HTTPException(status_code=500, detail=f"Error procesando el CV: {e}")
