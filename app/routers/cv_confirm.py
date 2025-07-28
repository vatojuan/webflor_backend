# app/routers/cv_confirm.py
"""
Módulo para la confirmación de registro de usuarios a través de su CV.

Este endpoint se activa cuando un usuario hace clic en el enlace de confirmación
enviado a su email. Procesa el CV, extrae datos, crea el perfil de usuario,
REGISTRA EL DOCUMENTO, y envía las credenciales de acceso.
"""
import io
import random
import string
import re
import os
import json
import logging
import psycopg2
import bcrypt
import urllib.parse

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from dotenv import load_dotenv
from google.cloud import storage
from PyPDF2 import PdfReader
from openai import OpenAI
from pgvector.psycopg2 import register_vector

# Importaciones centralizadas de email_utils
from app.email_utils import send_credentials_email, send_admin_alert

# --- Configuración Inicial ---
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Clientes de Servicios Externos ---
try:
    service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "{}"))
    storage_client = storage.Client.from_service_account_info(service_account_info)
    BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
except (json.JSONDecodeError, TypeError) as e:
    logger.error(f"Error al cargar credenciales de Google Cloud: {e}")
    storage_client = None
    BUCKET_NAME = None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

router = APIRouter(prefix="/cv", tags=["cv"])

# --- Funciones de Utilidad ---

def get_db_connection():
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        register_vector(conn)
        return conn
    except Exception as e:
        logger.error(f"Error en la conexión a la base de datos: {e}")
        raise

def generate_secure_password(length=12):
    plain_password = "".join(random.choice(string.ascii_letters + string.digits + "!@#$%^&*()") for _ in range(length))
    hashed = bcrypt.hashpw(plain_password.encode('utf-8'), bcrypt.gensalt())
    return plain_password, hashed.decode('utf-8')

def extract_text_from_pdf(pdf_bytes):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return " ".join([page.extract_text() or "" for page in reader.pages]).strip()
    except Exception as e:
        logger.error(f"Error extrayendo texto del PDF: {e}")
        return ""

def extract_phone(text):
    phones = re.findall(r"\+?\d[\d\s\-]{8,}", text)
    return phones[0] if phones else None

def extract_name(text):
    name_prompt = [
        {"role": "system", "content": "Eres un experto en análisis de currículums."},
        {"role": "user", "content": f"A partir del siguiente CV, extrae solo el nombre completo del candidato sin incluir títulos o cargos. Si no encuentras un nombre, responde 'No encontrado'.\n\nCV:\n{text[:1000]}"}
    ]
    try:
        name_response = client.chat.completions.create(model="gpt-4-turbo", messages=name_prompt, max_tokens=15)
        name_from_cv = name_response.choices[0].message.content.strip()
        return None if "no encontrado" in name_from_cv.lower() or not name_from_cv else name_from_cv
    except Exception as e:
        logger.error(f"Error llamando a OpenAI para extraer nombre: {e}")
        return None

def sanitize_filename(filename: str) -> str:
    filename = filename.replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_.-]", "", filename)

# --- Endpoint Principal ---

@router.get("/confirm")
async def confirm_email(code: str = Query(...), bg: BackgroundTasks = BackgroundTasks()):
    conn = None
    cur = None
    user_data = None
    try:
        logger.info(f"🔎 Iniciando confirmación de cuenta con código: {code[:8]}...")
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT email, cv_url FROM pending_users WHERE confirmation_code = %s", (code,))
        user_data = cur.fetchone()
        if not user_data:
            raise HTTPException(status_code=404, detail="Código de confirmación inválido o ya utilizado.")
        
        user_email, cv_url = user_data[0].lower(), user_data[1]
        logger.info(f"✅ Registro pendiente encontrado para {user_email}.")

        # Mover archivo en GCS
        decoded_url = urllib.parse.unquote(cv_url)
        old_path = decoded_url.replace(f"https://storage.googleapis.com/{BUCKET_NAME}/", "")
        new_path = old_path.replace("pending_cv_uploads", "employee-documents", 1)
        
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(old_path)
        if not blob.exists():
                raise HTTPException(status_code=404, detail="El archivo del CV no fue encontrado en el bucket de pendientes.")
        new_blob = bucket.rename_blob(blob, new_path)
        new_cv_url = new_blob.public_url
        logger.info(f"✅ CV movido a: {new_path}")

        # Procesar contenido del CV
        text_content = extract_text_from_pdf(new_blob.download_as_bytes())
        if not text_content:
            raise HTTPException(status_code=400, detail="El CV está vacío o no se pudo leer su contenido.")

        # Extraer datos y generar embeddings
        name_from_cv = extract_name(text_content) or user_email.split("@")[0]
        phone_number = extract_phone(text_content)
        
        description_prompt = [
             {"role": "system", "content": "Eres un analista de RR.HH. experto y tu objetivo es crear un resumen profesional y atractivo para un perfil de candidato. Tu respuesta debe ser un párrafo único y coherente. Sigue estas reglas estrictamente:\n1. Basa tu análisis exclusivamente en el texto del CV proporcionado.\n2. Identifica y destaca la experiencia laboral más relevante, las habilidades clave y los logros más notables.\n3. La longitud de tu resumen debe ser proporcional a la cantidad de información útil en el CV. Si el CV es breve o poco detallado, genera un resumen breve y conciso. NO inventes ni añadas información de relleno para alargarlo.\n4. El resumen final NO debe superar los 950 caracteres. Usa este límite para ser conciso, no para rellenar espacio.\n5. Redacta en un tono profesional y directo. No incluyas frases como 'El candidato parece...' o 'El CV sugiere...'."},
             {"role": "user", "content": f"Analiza y resume el siguiente CV:\n\n---\n{text_content[:4000]}\n---"}
        ]
        description_response = client.chat.completions.create(model="gpt-4-turbo", messages=description_prompt, max_tokens=700)
        description = description_response.choices[0].message.content.strip()
        
        embedding_desc_response = client.embeddings.create(model="text-embedding-ada-002", input=description)
        embedding_desc = embedding_desc_response.data[0].embedding
        logger.info(f"✅ Datos extraídos y embeddings generados para {user_email}.")

        # Crear usuario y credenciales
        plain_password, hashed_password = generate_secure_password()
        
        cur.execute(
            'INSERT INTO "User" (email, name, role, description, phone, password, confirmed, "cvUrl", embedding) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s) '
            'ON CONFLICT (email) DO UPDATE SET name=EXCLUDED.name, description=EXCLUDED.description, phone=EXCLUDED.phone, password=EXCLUDED.password, confirmed=TRUE, "cvUrl"=EXCLUDED."cvUrl", embedding=EXCLUDED.embedding RETURNING id',
            (user_email, name_from_cv, "empleado", description, phone_number, hashed_password, new_cv_url, embedding_desc)
        )
        user_id = cur.fetchone()[0]

        # --- ¡ESTA ES LA CORRECCIÓN FINAL! ---
        # Ahora que tenemos el user_id, creamos el registro del documento incluyendo la URL.
        original_filename = os.path.basename(new_path)
        file_key = new_path

        logger.info(f"📝 Registrando documento en EmployeeDocument para el usuario {user_id}...")
        cur.execute(
            """
            INSERT INTO "EmployeeDocument" ("originalName", "fileKey", "userId", "url")
            VALUES (%s, %s, %s, %s)
            """,
            (original_filename, file_key, user_id, new_cv_url) # Se añade new_cv_url aquí
        )
        logger.info(f"✅ Documento '{original_filename}' registrado exitosamente.")
        # --- FIN DE LA CORRECCIÓN ---

        # Limpiar registro pendiente
        cur.execute("DELETE FROM pending_users WHERE email = %s", (user_email,))
        conn.commit()
        logger.info(f"✅ Usuario {user_id} ({user_email}) creado/actualizado y registro pendiente eliminado.")

        # Enviar email de credenciales en segundo plano
        bg.add_task(send_credentials_email, user_email, name_from_cv, plain_password)
        logger.info(f"📨 Email de credenciales para {user_email} encolado.")

        return {"message": "¡Cuenta confirmada exitosamente! Recibirás un correo con tus credenciales en breve."}

    except HTTPException:
        raise
    except Exception as e:
        if conn: conn.rollback()
        logger.exception(f"❌ Error crítico confirmando cuenta para el código {code[:8]}: {e}")
        send_admin_alert(
            subject="Fallo Crítico en Confirmación de Cuenta de Usuario",
            details=f"El proceso de confirmación para el código que empieza con '{code[:8]}' falló.\nEmail potencial: {user_data[0] if user_data else 'No disponible'}\nError: {e}"
        )
        raise HTTPException(status_code=500, detail="Ha ocurrido un error inesperado al confirmar tu cuenta. Nuestro equipo ha sido notificado.")
    finally:
        if cur: cur.close()
        if conn: conn.close()
