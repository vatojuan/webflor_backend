from fastapi import APIRouter, HTTPException, Depends
from google.cloud import storage
from openai import OpenAI
from supabase import create_client
import json 
import os
import psycopg2
from dotenv import load_dotenv

# Cargar variables de entorno
load_dotenv()

# Configurar Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Configurar OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Configurar Google Cloud Storage
#storage_client = storage.Client()
#BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
# Configuración de Google Cloud Storage
service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
storage_client = storage.Client.from_service_account_info(service_account_info)
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")


# Configurar router
router = APIRouter(
    prefix="/files",
    tags=["files"]
)

# Función para obtener embeddings
def get_embedding(text):
    response = client.embeddings.create(
        model="text-embedding-ada-002",
        input=text
    )
    return response["data"][0]["embedding"]

# Conectar a la base de datos
def get_db_connection():
    return psycopg2.connect(
        user=os.getenv("USER"),
        password=os.getenv("PASSWORD"),
        host=os.getenv("HOST"),
        port=os.getenv("PORT"),
        dbname=os.getenv("DBNAME")
    )

# Función para descargar y leer el contenido del archivo desde Google Storage
def read_file_from_gcs(file_url):
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(file_url)
    content = blob.download_as_text()
    return content

# Endpoint para procesar archivos y guardar embeddings
@router.post("/process")
async def process_file(file_url: str, user_id: str):
    try:
        # Leer el archivo desde Google Storage
        text_content = read_file_from_gcs(file_url)

        # Obtener embedding del contenido
        embedding = get_embedding(text_content)

        # Guardar en Supabase
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO file_embeddings (user_id, file_url, file_name, embedding) VALUES (%s, %s, %s, %s)",
            (user_id, file_url, file_url.split("/")[-1], embedding)
        )
        conn.commit()
        cur.close()
        conn.close()

        return {"message": "Embedding guardado con éxito!", "file_url": file_url}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error procesando archivo: {str(e)}")
