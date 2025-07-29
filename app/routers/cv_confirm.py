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
            port=5432,
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

router = APIRouter(tags=["cv"])

def extract_text_from_pdf(pdf_bytes):
    """Extrae el texto completo de un PDF dado en bytes."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = " ".join([page.extract_text() or "" for page in reader.pages])
        return text.strip()
    except Exception as e:
        raise Exception(f"Error extrayendo texto del PDF: {e}")

# --- FUNCIÓN MEJORADA PARA EXTRAER TELÉFONO ---
def extract_phone(text):
    """
    Extrae un número de teléfono de forma más inteligente, evitando confundirlo con fechas.
    Busca secuencias de números y verifica que tengan una cantidad mínima de dígitos.
    """
    # Expresión regular para encontrar posibles candidatos a números de teléfono.
    # Busca secuencias de 9 a 20 caracteres que incluyan dígitos, espacios, guiones, paréntesis y el signo +.
    potential_phones = re.findall(r'[\d\s\-\(\)\+]{9,20}', text)
    
    for candidate in potential_phones:
        # Elimina todo lo que no sea un dígito para contarlos.
        digits_only = re.sub(r'\D', '', candidate)
        
        # Un número de teléfono real debería tener más de 8 dígitos.
        # Esto ayuda a descartar fechas como 'dd-mm-yyyy' (8 dígitos) o 'dd/mm/yy' (6 dígitos).
        if len(digits_only) > 8:
            # Si cumple la condición, devolvemos el candidato con su formato original.
            return candidate.strip()
            
    # Si no se encuentra ningún candidato adecuado, devuelve None.
    return None

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
    """Reemplaza espacios por guiones bajos y elimina caracteres problemáticos."""
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "", filename)
    return filename

def run_regeneration_for_all_users():
    """
    Tarea en segundo plano para regenerar los perfiles de todos los usuarios
    a partir de sus CVs existentes.
    """
    print("🚀 INICIANDO TAREA DE REGENERACIÓN DE PERFILES PARA TODOS LOS USUARIOS 🚀")
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute('SELECT id, email, "cvUrl" FROM "User" WHERE "cvUrl" IS NOT NULL')
        users = cur.fetchall()
        print(f"👥 Se encontraron {len(users)} usuarios para procesar.")

        bucket = storage_client.bucket(BUCKET_NAME)

        for user_id, user_email, cv_url in users:
            try:
                print(f"\n--- 🔄 Procesando usuario ID: {user_id}, Email: {user_email} ---")
                
                if not cv_url or not cv_url.startswith(f"https://storage.googleapis.com/{BUCKET_NAME}/"):
                    print(f"⚠️ URL de CV inválida o ausente para el usuario {user_id}. Saltando.")
                    continue
                
                file_path = cv_url.replace(f"https://storage.googleapis.com/{BUCKET_NAME}/", "")
                blob = bucket.blob(file_path)
                
                if not blob.exists():
                    print(f"⚠️ El archivo del CV no se encontró en GCS en la ruta: {file_path}. Saltando.")
                    continue

                file_bytes = blob.download_as_bytes()
                print(f"✅ CV descargado desde: {cv_url}")

                text_content = extract_text_from_pdf(file_bytes)
                if not text_content:
                    print(f"⚠️ No se pudo extraer texto del CV para el usuario {user_id}. Saltando.")
                    continue
                
                # Se utiliza la nueva función mejorada
                phone_number = extract_phone(text_content)
                print(f"✅ Nuevo teléfono extraído: {phone_number}")

                description_prompt = [
                    {
                        "role": "system",
                        "content": (
                            "Eres un analista de RR.HH. experto y tu objetivo es crear un resumen profesional y atractivo para un perfil de candidato. "
                            "Tu respuesta debe ser un párrafo único y coherente."
                            "Sigue estas reglas estrictamente:\n"
                            "1. Basa tu análisis exclusivamente en el texto del CV proporcionado.\n"
                            "2. Identifica y destaca la experiencia laboral más relevante, las habilidades clave y los logros más notables.\n"
                            "3. La longitud de tu resumen debe ser proporcional a la cantidad de información útil en el CV. Si el CV es breve o poco detallado, genera un resumen breve y conciso. NO inventes ni añadas información de relleno para alargarlo.\n"
                            "4. El resumen final NO debe superar los 950 caracteres. Usa este límite para ser conciso, no para rellenar espacio.\n"
                            "5. Redacta en un tono profesional y directo. No incluyas frases como 'El candidato parece...' o 'El CV sugiere...'."
                        )
                    },
                    {"role": "user", "content": f"Analiza y resume el siguiente CV:\n\n---\n{text_content[:4000]}\n---"}
                ]
                description_response = client.chat.completions.create(
                    model="gpt-4-turbo", messages=description_prompt, max_tokens=700, temperature=0.6, top_p=1,
                    frequency_penalty=0.1, presence_penalty=0.1
                )
                description = description_response.choices[0].message.content.strip()
                print(f"✅ Nueva descripción generada ({len(description)} caracteres).")

                embedding_response_desc = client.embeddings.create(model="text-embedding-ada-002", input=description)
                embedding_desc = embedding_response_desc.data[0].embedding
                print("✅ Nuevo embedding de descripción generado.")

                cur.execute(
                    'UPDATE "User" SET description = %s, phone = %s, embedding = %s WHERE id = %s',
                    (description, phone_number, embedding_desc, user_id)
                )
                conn.commit()
                print(f"✅ Perfil del usuario {user_id} actualizado en la base de datos.")

            except Exception as e:
                print(f"❌ ERROR procesando al usuario {user_id} ({user_email}): {e}")
                conn.rollback() 

    except Exception as e:
        print(f"❌❌ ERROR CRÍTICO durante la tarea de regeneración: {e}")
    finally:
        if cur: cur.close()
        if conn: conn.close()
        print("\n🏁 TAREA DE REGENERACIÓN DE PERFILES FINALIZADA 🏁")

@router.post("/cv/regenerate-all-profiles/")
async def regenerate_all_profiles(background_tasks: BackgroundTasks):
    """
    Endpoint para administradores. Inicia una tarea en segundo plano para
    actualizar todos los perfiles de usuario con la última lógica de IA.
    """
    print("⚡️ Solicitud recibida para regenerar todos los perfiles. Añadiendo a tareas en segundo plano. ⚡️")
    background_tasks.add_task(run_regeneration_for_all_users)
    return {"message": "El proceso de regeneración de perfiles ha comenzado en segundo plano. Revisa los logs del servidor para ver el progreso."}


@router.get("/cv/confirm/")
async def confirm_email(code: str = Query(...)):
    conn = None
    cur = None
    try:
        print(f"🔎 Buscando código de confirmación: {code}")
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("SELECT email, cv_url FROM pending_users WHERE confirmation_code = %s", (code,))
        user_data = cur.fetchone()
        if not user_data:
            raise HTTPException(status_code=400, detail="Código de confirmación inválido")
        user_email, cv_url = user_data
        
        user_email = user_email.lower()
        print(f"✅ Registro encontrado para {user_email} con CV URL: {cv_url}")

        decoded_url = urllib.parse.unquote(cv_url)
        print(f"🔎 URL decodificada: {decoded_url}")

        old_path_full = decoded_url.replace(f"https://storage.googleapis.com/{BUCKET_NAME}/", "")
        parts = old_path_full.split("/", 1)
        if len(parts) == 2:
            folder, filename = parts
            filename = sanitize_filename(filename)
            old_path = f"{folder}/{filename}"
        else:
            old_path = sanitize_filename(old_path_full)
        print(f"🔎 Path del archivo obtenido: {old_path}")

        new_path = old_path.replace("pending_cv_uploads", "employee-documents")
        print(f"🔎 Nuevo path: {new_path}")

        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(old_path)
        new_blob = bucket.rename_blob(blob, new_path)
        new_cv_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{new_path}"
        print(f"✅ CV movido a {new_cv_url}")

        file_bytes = new_blob.download_as_bytes()

        text_content = extract_text_from_pdf(file_bytes)
        if not text_content:
            raise HTTPException(status_code=400, detail="No se pudo extraer texto del CV")
        print(f"✅ Texto del CV obtenido (total de {len(text_content)} caracteres)")
        print(f"🔎 Fragmento inicial del texto:\n{text_content[:500]}")

        # Se utiliza la nueva función mejorada
        phone_number = extract_phone(text_content)
        print(f"✅ Teléfono extraído: {phone_number}")
        name_from_cv = extract_name(text_content)
        if not name_from_cv:
            print("⚠️ OpenAI no encontró el nombre en el CV, usando parte del email como referencia.")
            name_from_cv = user_email.split("@")[0]
        print(f"✅ Nombre extraído con OpenAI: {name_from_cv}")

        print("🧠 Iniciando generación de descripción profesional y adaptativa...")
        description_prompt = [
            {
                "role": "system",
                "content": (
                    "Eres un analista de RR.HH. experto y tu objetivo es crear un resumen profesional y atractivo para un perfil de candidato. "
                    "Tu respuesta debe ser un párrafo único y coherente."
                    "Sigue estas reglas estrictamente:\n"
                    "1. Basa tu análisis exclusivamente en el texto del CV proporcionado.\n"
                    "2. Identifica y destaca la experiencia laboral más relevante, las habilidades clave y los logros más notables.\n"
                    "3. La longitud de tu resumen debe ser proporcional a la cantidad de información útil en el CV. Si el CV es breve o poco detallado, genera un resumen breve y conciso. NO inventes ni añadas información de relleno para alargarlo.\n"
                    "4. El resumen final NO debe superar los 950 caracteres. Usa este límite para ser conciso, no para rellenar espacio.\n"
                    "5. Redacta en un tono profesional y directo. No incluyas frases como 'El candidato parece...' o 'El CV sugiere...'."
                )
            },
            {"role": "user", "content": f"Analiza y resume el siguiente CV:\n\n---\n{text_content[:4000]}\n---"}
        ]
        description_response = client.chat.completions.create(
            model="gpt-4-turbo", messages=description_prompt, max_tokens=700, temperature=0.6, top_p=1,
            frequency_penalty=0.1, presence_penalty=0.1
        )
        description = description_response.choices[0].message.content.strip()
        if len(description) > 1000:
            print(f"⚠️ Advertencia: La descripción generada superó los 1000 caracteres ({len(description)}). Se truncará.")
            last_period_index = description[:1000].rfind('.')
            if last_period_index != -1:
                description = description[:last_period_index + 1]
            else:
                description = description[:997] + "..."
        print(f"✅ Descripción generada ({len(description)} caracteres): {description}")

        embedding_response = client.embeddings.create(model="text-embedding-ada-002", input=text_content)
        embedding_cv = embedding_response.data[0].embedding
        print("✅ Embedding del CV generado exitosamente")

        cur.execute(
            'INSERT INTO "FileEmbedding" ("fileKey", embedding, "createdAt") VALUES (%s, %s::vector, NOW()) '
            'ON CONFLICT ("fileKey") DO UPDATE SET embedding = EXCLUDED.embedding, "createdAt" = NOW()',
            (new_path, embedding_cv)
        )
        conn.commit()
        print("✅ Embedding del CV almacenado en FileEmbedding")

        embedding_response_desc = client.embeddings.create(model="text-embedding-ada-002", input=description)
        embedding_desc = embedding_response_desc.data[0].embedding
        print("✅ Embedding de la descripción generado exitosamente")

        plain_password, hashed_password = generate_secure_password()
        print("✅ Contraseña segura generada y hasheada")

        cur.execute(
            'INSERT INTO "User" (email, name, role, description, phone, password, confirmed, "cvUrl", embedding) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s) '
            'ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description, phone = EXCLUDED.phone, '
            'password = EXCLUDED.password, confirmed = TRUE, "cvUrl" = EXCLUDED."cvUrl", embedding = EXCLUDED.embedding RETURNING id',
            (user_email, name_from_cv, "empleado", description, phone_number, hashed_password, new_cv_url, embedding_desc)
        )
        user_id = cur.fetchone()[0]
        conn.commit()
        print("✅ Usuario insertado/actualizado en la base de datos con id:", user_id)

        cur.execute(
            'INSERT INTO "EmployeeDocument" ("userId", url, "fileKey", "originalName", "createdAt") VALUES (%s, %s, %s, %s, NOW())',
            (user_id, new_cv_url, new_path, new_path.split("/")[-1])
        )
        conn.commit()
        print("✅ Registro en EmployeeDocument insertado")

        cur.execute("DELETE FROM pending_users WHERE email = %s", (user_email,))
        conn.commit()
        print("✅ Registro en pending_users eliminado")

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
