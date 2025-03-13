import io
import random
import string
import re
import os
import json
import uuid
import psycopg2
from fastapi import APIRouter, HTTPException, UploadFile, File
from dotenv import load_dotenv
from google.cloud import storage
from PyPDF2 import PdfReader
from openai import OpenAI
from app.email_utils import send_credentials_email
from pgvector.psycopg2 import register_vector
import bcrypt

load_dotenv()

# Configuración de Google Cloud Storage y OpenAI (igual que en los otros endpoints)
service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
storage_client = storage.Client.from_service_account_info(service_account_info)
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST"),
            port=5432,
            sslmode="require"
        )
        register_vector(conn)
        return conn
    except Exception as e:
        raise Exception(f"Error en la conexión a la base de datos: {e}")

def generate_secure_password(length=12):
    plain_password = "".join(random.choice(string.ascii_letters + string.digits + "!@#$%^&*()") for _ in range(length))
    hashed = bcrypt.hashpw(plain_password.encode('utf-8'), bcrypt.gensalt())
    return plain_password, hashed.decode('utf-8')

def extract_text_from_pdf(pdf_bytes):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = " ".join([page.extract_text() or "" for page in reader.pages])
        return text.strip()
    except Exception as e:
        raise Exception(f"Error extrayendo texto del PDF: {e}")

def extract_email(text):
    emails = re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    return emails[0] if emails else None

def extract_phone(text):
    phones = re.findall(r"\+?\d[\d\s\-]{8,}", text)
    return phones[0] if phones else None

def extract_name(text):
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
    return None if name_from_cv.lower() == "no encontrado" or not name_from_cv else name_from_cv

def sanitize_filename(filename: str) -> str:
    filename = filename.replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_.-]", "", filename)

router = APIRouter(prefix="/cv", tags=["cv_admin"])

@router.post("/admin_upload")
async def admin_upload_cv(files: list[UploadFile] = File(...)):
    results = []
    for file in files:
        logs = []
        logs.append(f"Procesando archivo: {file.filename}")
        try:
            file_bytes = await file.read()
            logs.append(f"Archivo leído, tamaño: {len(file_bytes)} bytes")
            safe_filename = sanitize_filename(file.filename)
            logs.append(f"Nombre sanitizado: {safe_filename}")
            
            # Subir el archivo directamente a "employee-documents"
            blob_path = f"employee-documents/{safe_filename}"
            bucket = storage_client.bucket(BUCKET_NAME)
            blob = bucket.blob(blob_path)
            blob.upload_from_string(file_bytes, content_type=file.content_type)
            new_cv_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{blob_path}"
            logs.append(f"Archivo subido a GCS: {new_cv_url}")
            
            # Extraer datos del CV
            text_content = extract_text_from_pdf(file_bytes)
            logs.append("Texto extraído del CV")
            if not text_content:
                raise Exception("No se pudo extraer texto del CV")
            
            user_email = extract_email(text_content)
            if not user_email:
                raise Exception("No se encontró un email válido en el CV")
            user_email = user_email.lower()
            logs.append(f"Email extraído: {user_email}")
            
            phone_number = extract_phone(text_content)
            logs.append(f"Teléfono extraído: {phone_number}")
            
            name_from_cv = extract_name(text_content)
            if not name_from_cv:
                name_from_cv = user_email.split("@")[0]
                logs.append("Nombre no encontrado, usando parte del email")
            else:
                logs.append(f"Nombre extraído: {name_from_cv}")
            
            # Generar descripción automática
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
            logs.append("Descripción generada")
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
                logs.append("Descripción ampliada con más detalles")
            
            # Generar embeddings
            embedding_response = client.embeddings.create(
                model="text-embedding-ada-002",
                input=text_content
            )
            embedding_cv = embedding_response.data[0].embedding
            logs.append("Embedding del CV generado")
            embedding_response_desc = client.embeddings.create(
                model="text-embedding-ada-002",
                input=description
            )
            embedding_desc = embedding_response_desc.data[0].embedding
            logs.append("Embedding de la descripción generado")
            
            # Generar contraseña segura
            plain_password, hashed_password = generate_secure_password()
            logs.append("Contraseña generada y hasheada")
            
            # Insertar o actualizar el usuario en la base de datos
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                'INSERT INTO "User" (email, name, role, description, phone, password, confirmed, "cvUrl", embedding) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s) '
                'ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description, phone = EXCLUDED.phone, '
                'password = EXCLUDED.password, confirmed = TRUE, "cvUrl" = EXCLUDED."cvUrl", embedding = EXCLUDED.embedding RETURNING id',
                (user_email, name_from_cv, "empleado", description, phone_number, hashed_password, new_cv_url, embedding_desc)
            )
            user_id = cur.fetchone()[0]
            conn.commit()
            logs.append(f"Usuario insertado/actualizado con ID: {user_id}")
            
            # Registrar el documento en EmployeeDocument
            cur.execute(
                'INSERT INTO "EmployeeDocument" ("userId", url, "fileKey", "originalName", "createdAt") VALUES (%s, %s, %s, %s, NOW())',
                (user_id, new_cv_url, blob_path, safe_filename)
            )
            conn.commit()
            logs.append("Registro en EmployeeDocument insertado")
            cur.close()
            conn.close()
            
            # Enviar email con credenciales
            send_credentials_email(user_email, user_email, plain_password)
            logs.append("Credenciales enviadas por email")
            
            results.append({
                "file": file.filename,
                "email": user_email,
                "status": "success",
                "message": "Cuenta creada y credenciales enviadas.",
                "logs": logs
            })
        except Exception as e:
            logs.append(f"Error: {str(e)}")
            results.append({
                "file": file.filename,
                "status": "error",
                "message": str(e),
                "logs": logs
            })
    
    return {"results": results}
