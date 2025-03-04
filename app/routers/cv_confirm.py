import io
import random
import string
import re
import os
import json
import uuid
import psycopg2
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, UploadFile, File, Form
from dotenv import load_dotenv
from google.cloud import storage
from PyPDF2 import PdfReader
from openai import OpenAI
from app.email_utils import send_credentials_email  # Asegúrate de que esté en app/email_utils.py
from pgvector.psycopg2 import register_vector  # Asegúrate de tener instalado pgvector
import bcrypt

load_dotenv()

# Configuración de Google Cloud Storage
storage_client = storage.Client()
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
if not BUCKET_NAME:
    raise Exception("GOOGLE_STORAGE_BUCKET no está definido")

# Configuración de OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Función para obtener conexión a la base de datos y registrar pgvector
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
        register_vector(conn)
        return conn
    except Exception as e:
        raise Exception(f"Error en la conexión a la base de datos: {e}")

def generate_secure_password(length=12):
    """Genera una contraseña segura aleatoria y la hashea con bcrypt."""
    plain_password = "".join(random.choice(string.ascii_letters + string.digits + "!@#$%^&*()") for _ in range(length))
    hashed = bcrypt.hashpw(plain_password.encode('utf-8'), bcrypt.gensalt())
    return plain_password, hashed.decode('utf-8')

router = APIRouter(prefix="/cv", tags=["cv"])

def extract_text_from_pdf(pdf_bytes):
    """Extrae el texto completo de un PDF dado en bytes."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = " ".join([page.extract_text() or "" for page in reader.pages])
        return text.strip()
    except Exception as e:
        raise Exception(f"Error extrayendo texto del PDF: {e}")

def extract_email(text):
    """Extrae el primer email encontrado en el texto."""
    emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    return emails[0] if emails else None

def extract_phone(text):
    """Extrae un número de teléfono si se encuentra."""
    phones = re.findall(r"\+?\d[\d\s\-]{8,}", text)
    return phones[0] if phones else None

def extract_name(text):
    """
    Usa OpenAI para extraer el nombre completo del candidato a partir del CV.
    Si no se encuentra, retorna None.
    """
    name_prompt = [
        {"role": "system", "content": "Eres un experto en análisis de currículums."},
        {"role": "user", "content": f"A partir del siguiente CV, extrae solo el nombre completo del candidato sin incluir títulos o cargos. Si no encuentras un nombre, responde 'No encontrado'.\n\nCV:\n{text[:1000]}"}
    ]
    name_response = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=name_prompt,
        max_tokens=10
    )
    name_from_cv = name_response.choices[0].message.content.strip()
    if name_from_cv.lower() == "no encontrado" or not name_from_cv:
        return None
    return name_from_cv

@router.get("/confirm")
async def confirm_email(code: str = Query(...)):
    conn = None
    cur = None
    try:
        print(f"🔎 Buscando código de confirmación: {code}")
        conn = get_db_connection()
        cur = conn.cursor()

        # Buscar el registro pendiente en pending_users
        cur.execute("SELECT email, cv_url FROM pending_users WHERE confirmation_code = %s", (code,))
        user_data = cur.fetchone()
        if not user_data:
            raise HTTPException(status_code=400, detail="Código de confirmación inválido")
        user_email, cv_url = user_data
        print(f"✅ Registro encontrado para {user_email} con CV URL: {cv_url}")

        # Mover el archivo de "pending_cv_uploads" a "employee-documents"
        old_path = cv_url.replace(f"https://storage.googleapis.com/{BUCKET_NAME}/", "")
        new_path = old_path.replace("pending_cv_uploads", "employee-documents")
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(old_path)
        new_blob = bucket.rename_blob(blob, new_path)
        new_cv_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{new_path}"
        print(f"✅ CV movido a {new_cv_url}")

        # Descargar el CV desde GCS como bytes
        file_bytes = new_blob.download_as_bytes()

        # Extraer el texto completo del CV
        text_content = extract_text_from_pdf(file_bytes)
        if not text_content:
            raise HTTPException(status_code=400, detail="No se pudo extraer texto del CV")
        print(f"✅ Texto del CV obtenido (total de {len(text_content)} caracteres)")
        print(f"🔎 Fragmento inicial del texto:\n{text_content[:500]}")

        # Extraer datos: teléfono y extraer el nombre usando OpenAI
        phone_number = extract_phone(text_content)
        print(f"✅ Teléfono extraído: {phone_number}")
        name_from_cv = extract_name(text_content)
        if not name_from_cv:
            print("⚠️ OpenAI no encontró el nombre en el CV, usando parte del email como referencia.")
            name_from_cv = user_email.split("@")[0]
        print(f"✅ Nombre extraído con OpenAI: {name_from_cv}")

        # Generar descripción automática con OpenAI
        description_prompt = [
            {"role": "system", "content": "Eres un experto en recursos humanos."},
            {"role": "user", "content": f"Genera una breve descripción profesional para el siguiente CV:\n\n{text_content[:500]}"}
        ]
        description_response = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=description_prompt,
            max_tokens=50
        )
        description = description_response.choices[0].message.content.strip()
        print(f"✅ Descripción generada: {description}")

        # Generar embedding del CV completo
        embedding_response = client.embeddings.create(
            model="text-embedding-ada-002",
            input=text_content
        )
        embedding_cv = embedding_response.data[0].embedding
        print("✅ Embedding del CV generado exitosamente")

        # Guardar el embedding del CV en la tabla FileEmbedding
        cur.execute(
            'INSERT INTO "FileEmbedding" ("fileKey", embedding, "createdAt") VALUES (%s, %s::vector, NOW()) '
            'ON CONFLICT ("fileKey") DO UPDATE SET embedding = EXCLUDED.embedding, "createdAt" = NOW()',
            (new_path, embedding_cv)
        )
        conn.commit()
        print("✅ Embedding del CV almacenado en FileEmbedding")

        # Generar embedding de la descripción (sin casteo, pues la columna en "User" es double precision[])
        embedding_response_desc = client.embeddings.create(
            model="text-embedding-ada-002",
            input=description
        )
        embedding_desc = embedding_response_desc.data[0].embedding
        print("✅ Embedding de la descripción generado exitosamente")

        # Generar contraseña segura y hashearla
        plain_password, hashed_password = generate_secure_password()
        print("✅ Contraseña segura generada y hasheada")

        # Insertar o actualizar el usuario en la tabla "User" y obtener su id,
        # almacenando el embedding de la descripción en la columna embedding
        cur.execute(
            'INSERT INTO "User" (email, name, role, description, phone, password, confirmed, "cvUrl", embedding) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s) '
            'ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description, phone = EXCLUDED.phone, '
            'password = EXCLUDED.password, confirmed = TRUE, "cvUrl" = EXCLUDED."cvUrl", embedding = EXCLUDED.embedding RETURNING id',
            (user_email, name_from_cv, "empleado", description, phone_number, hashed_password, new_cv_url, embedding_desc)
        )
        user_id = cur.fetchone()[0]
        conn.commit()
        print("✅ Usuario insertado/actualizado en la base de datos con id:", user_id)

        # Insertar el registro en EmployeeDocument para que el CV figure en el perfil del usuario
        cur.execute(
            'INSERT INTO "EmployeeDocument" ("userId", url, "fileKey", "originalName", "createdAt") VALUES (%s, %s, %s, %s, NOW())',
            (user_id, new_cv_url, new_path, new_path.split("/")[-1])
        )
        conn.commit()
        print("✅ Registro en EmployeeDocument insertado")

        # Eliminar el registro en pending_users
        cur.execute("DELETE FROM pending_users WHERE email = %s", (user_email,))
        conn.commit()
        print("✅ Registro en pending_users eliminado")

        # Enviar email de credenciales al usuario
        send_credentials_email(user_email, user_email, plain_password)
        print(f"✅ Credenciales enviadas a {user_email}")

        return {"message": "Cuenta confirmada exitosamente."}

    except Exception as e:
        print(f"❌ Error confirmando cuenta: {e}")
        raise HTTPException(status_code=500, detail=f"Error confirmando cuenta: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
