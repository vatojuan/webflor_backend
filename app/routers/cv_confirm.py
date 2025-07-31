import io
import random
import string
import re
import os
import json
import uuid
import psycopg2
import time # Importar la librería time
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks, UploadFile, File, Form
from dotenv import load_dotenv
from google.cloud import storage
from PyPDF2 import PdfReader
import openai # Importar openai para manejar sus excepciones específicas
from app.email_utils import send_credentials_email
from pgvector.psycopg2 import register_vector
import bcrypt
import urllib.parse

load_dotenv()

# Configuración de Google Cloud Storage
# Asegúrate de que la variable de entorno está correctamente configurada.
service_account_info_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not service_account_info_str:
    raise ValueError("La variable de entorno GOOGLE_APPLICATION_CREDENTIALS_JSON no está configurada.")
service_account_info = json.loads(service_account_info_str)
storage_client = storage.Client.from_service_account_info(service_account_info)
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")

# Configuración de OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = openai.OpenAI(api_key=OPENAI_API_KEY)

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

router = APIRouter(prefix="/api/cv", tags=["cv"])

def extract_text_from_pdf(pdf_bytes):
    """Extrae el texto completo de un PDF dado en bytes."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = " ".join([page.extract_text() or "" for page in reader.pages])
        return text.strip()
    except Exception as e:
        raise Exception(f"Error extrayendo texto del PDF: {e}")

# --- FUNCIÓN DE TELÉFONO MEJORADA Y MÁS PRECISA ---
def extract_phone(text):
    """
    Extrae un número de teléfono de forma precisa, utilizando un enfoque de múltiples pasos:
    1. Búsqueda amplia de candidatos potenciales.
    2. Aplicación de filtros agresivos para descartar falsos positivos (fechas, CUITs, etc.).
    3. Selección del candidato más probable basado en un sistema de puntuación.
    """
    # 1. BÚSQUEDA AMPLIA DE CANDIDATOS
    # Busca secuencias que contengan dígitos, espacios y caracteres comunes de teléfono.
    # Se buscan secuencias de entre 8 y 25 caracteres para ser flexibles.
    potential_candidates = re.findall(r'[\d\s\-\(\)\+]{8,25}', text)
    
    # Añade búsquedas cerca de palabras clave para darles prioridad.
    keyword_pattern = re.compile(r'(?:tel(?:éfono)?|cel(?:ular)?|whatsapp|contacto|m[óo]vil)[\s:.]*([+\d\s\-\(\)]{8,20})', re.IGNORECASE)
    potential_candidates.extend(keyword_pattern.findall(text))

    valid_phones = []

    # 2. FILTRADO AGRESIVO DE CANDIDATOS
    for candidate in potential_candidates:
        cleaned_candidate = candidate.strip()
        digits_only = re.sub(r'\D', '', cleaned_candidate)

        # --- Filtros de descarte ---

        # Filtro 1: Longitud de dígitos. Un teléfono válido en Argentina tiene entre 8 y 13 dígitos.
        if not (8 <= len(digits_only) <= 13):
            continue

        # Filtro 2: Descartar si es un CUIT/CUIL obvio (11 dígitos y prefijo conocido).
        if len(digits_only) == 11 and digits_only.startswith(('20', '23', '24', '27', '30', '33', '34')):
            continue
        
        # Filtro 3: Descartar si parece un rango de años (ej: "2015 - 2020", "2015 a 2020").
        if re.search(r'\b(19|20)\d{2}\b\s*[-–aAtoTO\s]+\s*\b(19|20)\d{2}\b', cleaned_candidate):
            continue
        
        # Filtro 4: Descartar si contiene palabras clave de descarte como "actualidad", "presente", etc.
        if re.search(r'\b(actualidad|presente|hoy|fecha|nacimiento)\b', cleaned_candidate, re.IGNORECASE):
            continue

        # Filtro 5: Descartar si está cerca de palabras como DNI, Legajo, etc.
        # Se busca en una ventana de 20 caracteres alrededor del candidato.
        pos = text.find(cleaned_candidate)
        if pos != -1:
            context = text[max(0, pos-20):pos+len(cleaned_candidate)+20]
            if re.search(r'\b(DNI|CUIT|CUIL|Legajo|Matr[íi]cula)\b', context, re.IGNORECASE):
                continue

        # Filtro 6: Descartar si tiene demasiados separadores (más de 4), es poco probable que sea un teléfono.
        if len(re.findall(r'[\s\-]', cleaned_candidate)) > 4:
            continue

        # Si pasa todos los filtros, se considera un teléfono válido.
        valid_phones.append(cleaned_candidate)

    if not valid_phones:
        return None

    # 3. SELECCIÓN DEL MEJOR CANDIDATO
    # Se da una puntuación más alta a los números con una longitud más típica (10-13 dígitos).
    def score(p):
        digits = len(re.sub(r'\D', '', p))
        if 10 <= digits <= 13:
            return 100 + digits  # Máxima prioridad
        return digits # Menor prioridad para números más cortos

    best_phone = max(set(valid_phones), key=score)
    
    return best_phone.strip()


# --- FUNCIÓN DE NOMBRE PROFESIONAL ---
def extract_name(text):
    """
    Usa OpenAI para extraer el nombre completo con un prompt más robusto y filtros de validación.
    """
    name_prompt = [
        {"role": "system", "content": "Eres un analista de RR.HH. experto. Tu tarea es extraer el nombre y apellido del candidato del siguiente texto. El nombre suele ser lo primero y más destacado en el CV, a menudo en mayúsculas o en una fuente más grande. Ignora cualquier cargo, título profesional o email que pueda aparecer junto al nombre. Devuelve únicamente el nombre completo. Si no puedes identificar un nombre claro, responde 'No encontrado'."},
        {"role": "user", "content": f"A partir del siguiente CV, extrae solo el nombre completo del candidato.\n\nCV:\n{text[:2000]}"}
    ]
    name_response = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=name_prompt,
        max_tokens=25
    )
    name_from_cv = name_response.choices[0].message.content.strip().replace('"', '').replace("'", "")
    if ("no encontrado" in name_from_cv.lower() or 
        not name_from_cv or 
        len(name_from_cv.split()) < 2 or 
        "@" in name_from_cv or
        "CV" in name_from_cv.upper() or
        "CURRICULUM" in name_from_cv.upper()):
        return None
    return name_from_cv

def sanitize_filename(filename: str) -> str:
    """Reemplaza espacios por guiones bajos y elimina caracteres problemáticos."""
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^a-zA-Z0-9_.-]", "", filename)
    return filename

def run_regeneration_for_all_users():
    """
    Tarea en segundo plano para regenerar los perfiles de todos los usuarios,
    con manejo de errores de API y pausas para evitar rate limiting.
    """
    print("🚀 INICIANDO TAREA DE REGENERACIÓN DE PERFILES PARA TODOS LOS USUARIOS 🚀")
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT id, email, "cvUrl", name FROM "User"')
        users = cur.fetchall()
        print(f"👥 Se encontraron {len(users)} usuarios para procesar.")
        bucket = storage_client.bucket(BUCKET_NAME)

        for user_id, user_email, cv_url, current_name in users:
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
                
                new_phone = extract_phone(text_content)
                print(f"✅ Nuevo teléfono extraído: {new_phone}")

                # --- Bloque de llamadas a OpenAI con manejo de errores ---
                try:
                    new_name = extract_name(text_content)
                    if not new_name:
                        print("⚠️ OpenAI no encontró un nombre válido. Se mantiene el nombre actual o se genera desde el email.")
                        if current_name is None or "no encontrado" in current_name.lower() or "@" in current_name:
                            new_name = user_email.split("@")[0].replace(".", " ").replace("_", " ").title()
                        else:
                            new_name = current_name
                    print(f"✅ Nuevo nombre: {new_name}")

                    description_prompt = [
                        {"role": "system", "content": "Eres un analista de RR.HH. experto. Tu objetivo es crear un resumen profesional y atractivo basado exclusivamente en el CV. La longitud del resumen debe ser proporcional a la información útil del CV, sin rellenar y sin superar los 950 caracteres. Redacta en un tono profesional y directo."},
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

                except openai.APIStatusError as e:
                    if e.status_code == 429:
                        print("❌❌ ERROR CRÍTICO: Cuota de OpenAI excedida. Deteniendo la tarea de regeneración. ❌❌")
                        print("Por favor, revisa tu plan y facturación en platform.openai.com.")
                        # Detiene el bucle for completamente si la cuota se agota.
                        break 
                    else:
                        print(f"❌ ERROR de API de OpenAI procesando al usuario {user_id}: {e}. Saltando al siguiente usuario.")
                        continue # Salta al siguiente usuario si es otro tipo de error de API

                cur.execute(
                    'UPDATE "User" SET name = %s, description = %s, phone = %s, embedding = %s WHERE id = %s',
                    (new_name, description, new_phone, embedding_desc, user_id)
                )
                conn.commit()
                print(f"✅ Perfil del usuario {user_id} actualizado en la base de datos.")

                # Pausa para no sobrecargar la API de OpenAI
                print("⏳ Pausando por 2 segundos...")
                time.sleep(2)

            except Exception as e:
                print(f"❌ ERROR GENERAL procesando al usuario {user_id} ({user_email}): {e}")
                if conn: conn.rollback() 
    except Exception as e:
        print(f"❌❌ ERROR CRÍTICO durante la tarea de regeneración: {e}")
    finally:
        if cur: cur.close()
        if conn: conn.close()
        print("\n🏁 TAREA DE REGENERACIÓN DE PERFILES FINALIZADA 🏁")

@router.post("/regenerate-all-profiles/")
async def regenerate_all_profiles(background_tasks: BackgroundTasks):
    """
    Endpoint para administradores. Inicia la tarea de regeneración en segundo plano.
    """
    print("⚡️ Solicitud recibida para regenerar todos los perfiles. Añadiendo a tareas en segundo plano. ⚡️")
    background_tasks.add_task(run_regeneration_for_all_users)
    return {"message": "El proceso de regeneración de perfiles ha comenzado en segundo plano. Revisa los logs del servidor para ver el progreso."}

@router.get("/confirm/")
async def confirm_email(code: str = Query(...)):
    """
    Endpoint para confirmar el email de un nuevo usuario y procesar su CV.
    """
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
        
        phone_number = extract_phone(text_content)
        print(f"✅ Teléfono extraído: {phone_number}")
        
        # --- Bloque de llamadas a OpenAI con manejo de errores ---
        try:
            name_from_cv = extract_name(text_content)
            if not name_from_cv:
                print("⚠️ OpenAI no encontró el nombre en el CV, usando parte del email como referencia.")
                name_from_cv = user_email.split("@")[0].replace(".", " ").replace("_", " ").title()
            print(f"✅ Nombre extraído con OpenAI: {name_from_cv}")

            print("🧠 Iniciando generación de descripción profesional y adaptativa...")
            description_prompt = [
                {"role": "system", "content": "Eres un analista de RR.HH. experto. Tu objetivo es crear un resumen profesional y atractivo basado exclusivamente en el CV. La longitud del resumen debe ser proporcional a la información útil del CV, sin rellenar y sin superar los 950 caracteres. Redacta en un tono profesional y directo."},
                {"role": "user", "content": f"Analiza y resume el siguiente CV:\n\n---\n{text_content[:4000]}\n---"}
            ]
            description_response = client.chat.completions.create(
                model="gpt-4-turbo", messages=description_prompt, max_tokens=700, temperature=0.6, top_p=1,
                frequency_penalty=0.1, presence_penalty=0.1
            )
            description = description_response.choices[0].message.content.strip()
            print(f"✅ Descripción generada ({len(description)} caracteres).")

            embedding_response = client.embeddings.create(model="text-embedding-ada-002", input=text_content)
            embedding_cv = embedding_response.data[0].embedding
            print("✅ Embedding del CV generado exitosamente")

            embedding_response_desc = client.embeddings.create(model="text-embedding-ada-002", input=description)
            embedding_desc = embedding_response_desc.data[0].embedding
            print("✅ Embedding de la descripción generado exitosamente")

        except openai.APIStatusError as e:
            if e.status_code == 429:
                raise HTTPException(status_code=429, detail="La cuota de OpenAI ha sido excedida. No se pudo procesar el perfil. Por favor, contacta al administrador.")
            else:
                raise HTTPException(status_code=500, detail=f"Ocurrió un error con la API de OpenAI: {e}")

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
            'INSERT INTO "FileEmbedding" ("fileKey", embedding, "createdAt") VALUES (%s, %s::vector, NOW()) '
            'ON CONFLICT ("fileKey") DO UPDATE SET embedding = EXCLUDED.embedding, "createdAt" = NOW()',
            (new_path, embedding_cv)
        )
        conn.commit()
        print("✅ Embedding del CV almacenado en FileEmbedding")

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

    except HTTPException as http_exc:
        # Re-lanza las excepciones HTTP para que FastAPI las maneje
        raise http_exc
    except Exception as e:
        print(f"❌ Error confirmando cuenta: {e}")
        if conn and not conn.closed: conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error interno confirmando cuenta: {e}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
