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
from app.email_utils import send_credentials_email  # Asegurate de que esté en app/email_utils.py
from pgvector.psycopg2 import register_vector  # Asegurate de tener instalado pgvector
import bcrypt
import urllib.parse  # Para decodificar URLs

load_dotenv()

# Configuración de Google Cloud Storage
service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
storage_client = storage.Client.from_service_account_info(service_account_info)
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")

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
            port=5432,  # Fijo en 5432
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

def sanitize_filename(filename: str) -> str:
    """Reemplaza espacios por guiones bajos y elimina caracteres problemáticos.
    Nota: Esta función está pensada para sanitizar nombres de archivo, no rutas completas.
    """
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "", filename)
    return filename

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
        
        # Convertir el email a minúsculas para mantener la consistencia
        user_email = user_email.lower()
        print(f"✅ Registro encontrado para {user_email} con CV URL: {cv_url}")

        # Decodificar la URL para convertir %20 a espacios
        decoded_url = urllib.parse.unquote(cv_url)
        print(f"🔎 URL decodificada: {decoded_url}")

        # Extraer el path del objeto (la parte luego de la URL base)
        old_path_full = decoded_url.replace(f"https://storage.googleapis.com/{BUCKET_NAME}/", "")
        # Dividir en carpeta y nombre del archivo para sanitizar solo el nombre
        parts = old_path_full.split("/", 1)
        if len(parts) == 2:
            folder, filename = parts
            filename = sanitize_filename(filename)
            old_path = f"{folder}/{filename}"
        else:
            old_path = sanitize_filename(old_path_full)
        print(f"🔎 Path del archivo obtenido: {old_path}")

        # Generar el nuevo path reemplazando la carpeta
        new_path = old_path.replace("pending_cv_uploads", "employee-documents")
        print(f"🔎 Nuevo path: {new_path}")

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

        # Extraer datos: teléfono y el nombre usando OpenAI
        phone_number = extract_phone(text_content)
        print(f"✅ Teléfono extraído: {phone_number}")
        name_from_cv = extract_name(text_content)
        if not name_from_cv:
            print("⚠️ OpenAI no encontró el nombre en el CV, usando parte del email como referencia.")
            name_from_cv = user_email.split("@")[0]
        print(f"✅ Nombre extraído con OpenAI: {name_from_cv}")

        # Generar descripción automática con OpenAI(revisar)
        description_prompt = [
            {"role": "system", "content": "Eres un experto en recursos humanos."},
            {"role": "user", "content": f"Genera una descripción profesional para el siguiente CV:\n\n{text_content[:2000]}"}
        ]

        description_response = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=description_prompt,
            max_tokens=300,
            temperature=0.7
        )

        description = description_response.choices[0].message.content.strip()

        # Si la descripción parece cortada, pedimos a OpenAI que continúe
        if len(description) >= 280:
            follow_up_prompt = [
                {"role": "system", "content": "Eres un experto en recursos humanos."},
                {"role": "user", "content": "Continúa la descripción anterior con más detalles."}
            ]
            follow_up_response = client.chat.completions.create(
                model="gpt-4-turbo",
                messages=follow_up_prompt,
                max_tokens=200
            )
            description += " " + follow_up_response.choices[0].message.content.strip()

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

        # Generar embedding de la descripción
        embedding_response_desc = client.embeddings.create(
            model="text-embedding-ada-002",
            input=description
        )
        embedding_desc = embedding_response_desc.data[0].embedding
        print("✅ Embedding de la descripción generado exitosamente")

        # Generar contraseña segura y hashearla
        plain_password, hashed_password = generate_secure_password()
        print("✅ Contraseña segura generada y hasheada")

        # Insertar o actualizar el usuario en la tabla "User"
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
