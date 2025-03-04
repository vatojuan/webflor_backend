# app/routers/file_processing.py
from fastapi import APIRouter, File, UploadFile, HTTPException, Depends
from sentence_transformers import SentenceTransformer
import PyPDF2
import io
import psycopg2
from dotenv import load_dotenv
import os
from datetime import datetime

load_dotenv()

router = APIRouter(
    prefix="/files",
    tags=["files"],
)

# Cargar el modelo de embeddings (ajusta el nombre del modelo si es necesario)
model = SentenceTransformer('all-MiniLM-L6-v2')  # Modelo de 384 dimensiones

# Función para extraer texto de un PDF
def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error extrayendo el texto: {e}")

# Función para obtener la conexión a la base de datos (usando el pooler de Supabase)
def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER", "postgres.apnfioxjddccokgkljvd"),
            password=os.getenv("PASSWORD", "Pachamama190"),
            host=os.getenv("HOST", "aws-0-sa-east-1.pooler.supabase.com"),
            port=os.getenv("PORT", "5432"),
            sslmode="require"
        )
        return conn
    except Exception as e:
        raise Exception(f"Error en la conexión a la base de datos: {e}")

@router.post("/upload")
async def upload_file(user_id: int, file: UploadFile = File(...)):
    # Verifica que el archivo sea PDF (puedes ampliar a otros tipos)
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF")
    
    # Lee el archivo
    file_bytes = await file.read()
    
    # Extrae el texto del PDF
    text = extract_text_from_pdf(file_bytes)
    if not text:
        raise HTTPException(status_code=400, detail="No se pudo extraer texto del archivo")
    
    # Genera el embedding a partir del texto
    embedding = model.encode(text).tolist()  # Convertir a lista para almacenarla (puedes guardar como JSON)
    
    # Almacenar en la base de datos (ejemplo usando una tabla "embeddings")
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        insert_query = """
            INSERT INTO embeddings (user_id, file_name, embedding, created_at)
            VALUES (%s, %s, %s, %s)
            RETURNING id;
        """
        cur.execute(insert_query, (user_id, file.filename, embedding, datetime.utcnow()))
        inserted_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error almacenando el embedding: {e}")
    
    return {"message": "Archivo procesado y embedding almacenado", "embedding_id": inserted_id}
