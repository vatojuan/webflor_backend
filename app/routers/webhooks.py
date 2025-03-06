# app/routers/webhooks.py
from fastapi import APIRouter, HTTPException, BackgroundTasks
from supabase import create_client
import psycopg2
import os
import uuid
import json
from dotenv import load_dotenv
from openai import OpenAI
from google.cloud import storage
import io
from PyPDF2 import PdfReader
from pgvector.psycopg2 import register_vector  # Asegúrate de tener instalado pgvector

load_dotenv()

# Configurar Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise Exception("SUPABASE_URL o SUPABASE_KEY no están configurados")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Configurar OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Configurar Google Cloud Storage
#storage_client = storage.Client()
#BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
#if not BUCKET_NAME:
#    raise Exception("GOOGLE_STORAGE_BUCKET no está definido")
# Configuración de Google Cloud Storage
service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
storage_client = storage.Client.from_service_account_info(service_account_info)
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")

router = APIRouter(
    prefix="/webhooks",
    tags=["webhooks"]
)

# Función para obtener embeddings (usando notación de punto)
def get_embedding(text):
    response = client.embeddings.create(
        model="text-embedding-ada-002",
        input=text
    )
    return response.data[0].embedding

# Función para extraer texto de un PDF desde GCS
def read_pdf_from_gcs(file_url):
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(file_url)
    file_bytes = blob.download_as_bytes()  # Descarga como bytes
    reader = PdfReader(io.BytesIO(file_bytes))
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text

# Función para obtener la conexión a la base de datos y registrar pgvector
def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER", "postgres.apnfioxjddccokgkljvd"),
            password=os.getenv("PASSWORD", "Pachamama190"),
            host=os.getenv("HOST", "aws-0-sa-east-1.pooler.supabase.com"),
            port=5432,  # Fijo en 5432
            sslmode="require"
        )
        register_vector(conn)
        return conn
    except Exception as e:
        raise Exception(f"Error en la conexión a la base de datos: {e}")

# Tarea en background para procesar el archivo
def process_file_task(payload: dict):
    try:
        user_id_raw = payload.get("user_id")
        # Si viene como lista, extraemos el primer elemento
        if isinstance(user_id_raw, list):
            user_id_raw = user_id_raw[0]
        try:
            # Intentamos convertir a UUID
            user_id = uuid.UUID(user_id_raw)
        except Exception as e:
            try:
                # Si falla, intentamos convertir a entero
                user_id = int(user_id_raw)
            except Exception as e2:
                raise Exception(f"El user_id '{user_id_raw}' no es un UUID válido ni un entero: {e2}")

        file_url = payload["file_url"]

        print(f"📥 Procesando archivo en background: {file_url} para usuario: {user_id}")

        # Leer el archivo (suponemos que es PDF)
        text_content = read_pdf_from_gcs(file_url)
        if not text_content:
            raise Exception("No se pudo extraer texto del archivo")
        
        # Obtener embedding del contenido
        embedding = get_embedding(text_content)

        # Insertar el embedding en la base de datos (omitiendo la columna 'id' que se genera automáticamente)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO file_embeddings 
                (user_id, file_name, content, embedding, created_at)
            VALUES 
                (%s, %s, %s, %s, NOW())
            """,
            (user_id, file_url.split("/")[-1], text_content, embedding)
        )
        conn.commit()
        cur.close()
        conn.close()

        print(f"✅ Embedding guardado con éxito para archivo: {file_url}")
    except Exception as e:
        print(f"❌ Error en procesamiento de archivo en background: {e}")

# Endpoint webhook para notificar subida de archivo
@router.post("/file_uploaded")
async def file_uploaded(payload: dict, background_tasks: BackgroundTasks):
    try:
        background_tasks.add_task(process_file_task, payload)
        return {"message": "Webhook recibido. El procesamiento se realizará en background."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en el webhook: {str(e)}")

# Endpoint webhook para notificar eliminación de archivo
@router.post("/file_deleted")
async def file_deleted(payload: dict):
    try:
        file_url = payload["file_url"]

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM file_embeddings WHERE file_name = %s", (file_url.split("/")[-1],))
        conn.commit()
        cur.close()
        conn.close()

        return {"message": "Embedding eliminado con éxito!", "file_url": file_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error eliminando embedding: {str(e)}")
