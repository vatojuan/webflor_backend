# app/routers/admin_users.py
import os
import json
import psycopg2
import re
import io
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from dotenv import load_dotenv
from google.cloud import storage
from PyPDF2 import PdfReader
from app.utils.auth_utils import get_current_admin
from app.services.embedding import generate_file_embedding, get_db_connection  # Usamos el get_db_connection de embedding.py

load_dotenv()

router = APIRouter(prefix="/admin/users", tags=["admin_users"])

# Configurar Google Cloud Storage
service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
storage_client = storage.Client.from_service_account_info(service_account_info)
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")

def sanitize_filename(filename: str) -> str:
    filename = filename.replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_.-]", "", filename)

def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = " ".join([page.extract_text() or "" for page in reader.pages])
        return text.strip()
    except Exception as e:
        raise Exception(f"Error extrayendo texto del PDF: {e}")

@router.get("")
def list_users(current_admin: str = Depends(get_current_admin)):
    """
    Lista todos los usuarios con sus datos básicos y archivos subidos.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT id, email, name, phone, description FROM "User"')
        users = cur.fetchall()
        users_list = []
        for u in users:
            user_obj = {
                "id": u[0],
                "email": u[1],
                "name": u[2],
                "phone": u[3],
                "description": u[4],
                "files": []
            }
            cur.execute('SELECT id, url, "originalName" FROM "EmployeeDocument" WHERE "userId" = %s', (u[0],))
            files = cur.fetchall()
            files_list = [{"id": f[0], "url": f[1], "filename": f[2]} for f in files]
            user_obj["files"] = files_list
            users_list.append(user_obj)
        cur.close()
        conn.close()
        return {"users": users_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/{user_id}")
def update_user(user_id: str, data: dict, current_admin: str = Depends(get_current_admin)):
    """
    Actualiza el nombre, teléfono y descripción del usuario.
    Se espera que data incluya: { "name": "...", "phone": "...", "description": "..." }
    """
    try:
        name = data.get("name")
        phone = data.get("phone")
        description = data.get("description")
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'UPDATE "User" SET name = %s, phone = %s, description = %s WHERE id = %s',
            (name, phone, description, user_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        # (Opcional) Podés llamar a update_user_embedding(user_id) si querés actualizar el embedding del usuario
        return {"message": "Usuario actualizado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{user_id}")
def delete_user(user_id: str, current_admin: str = Depends(get_current_admin)):
    """
    Elimina el usuario y, además, elimina:
      - Sus archivos almacenados en GCS
      - Registros en EmployeeDocument
      - Embeddings en file_embeddings
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT "fileKey", url FROM "EmployeeDocument" WHERE "userId" = %s', (user_id,))
        files = cur.fetchall()
        bucket = storage_client.bucket(BUCKET_NAME)
        for f in files:
            file_key = f[0]
            blob = bucket.blob(file_key)
            blob.delete()
        cur.execute('DELETE FROM "EmployeeDocument" WHERE "userId" = %s', (user_id,))
        cur.execute('DELETE FROM file_embeddings WHERE user_id = %s', (user_id,))
        cur.execute('DELETE FROM "User" WHERE id = %s', (user_id,))
        conn.commit()
        cur.close()
        conn.close()
        return {"message": "Usuario y sus datos eliminados"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{user_id}/files")
def upload_user_file(user_id: str, file: UploadFile = File(...), current_admin: str = Depends(get_current_admin)):
    """
    Sube un archivo para el usuario, extrae el texto y genera el embedding del contenido,
    registrándolo en la tabla "FileEmbedding". Además, registra el archivo en "EmployeeDocument".
    """
    try:
        file_bytes = file.file.read()
        safe_filename = sanitize_filename(file.filename)
        file_key = f"user-files/{user_id}/{safe_filename}"
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(file_key)
        blob.upload_from_string(file_bytes, content_type=file.content_type)
        file_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{file_key}"
        # Extraer texto del archivo (se asume PDF)
        text_content = extract_text_from_pdf(file_bytes)
        if not text_content:
            raise HTTPException(status_code=400, detail="No se pudo extraer texto del archivo para generar embedding")
        # Generar embedding del contenido usando la función del servicio
        embedding_file = generate_file_embedding(text_content)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO "EmployeeDocument" ("userId", url, "fileKey", "originalName", "createdAt") VALUES (%s, %s, %s, %s, NOW()) RETURNING id',
            (user_id, file_url, file_key, safe_filename)
        )
        file_id = cur.fetchone()[0]
        conn.commit()
        cur.execute(
            'INSERT INTO "FileEmbedding" ("fileKey", embedding, "createdAt") VALUES (%s, %s::vector, NOW()) '
            'ON CONFLICT ("fileKey") DO UPDATE SET embedding = EXCLUDED.embedding, "createdAt" = NOW()',
            (file_key, embedding_file)
        )
        conn.commit()
        cur.execute('SELECT id, url, "originalName" FROM "EmployeeDocument" WHERE "userId" = %s', (user_id,))
        files = cur.fetchall()
        files_list = [{"id": f[0], "url": f[1], "filename": f[2]} for f in files]
        cur.close()
        conn.close()
        return {"message": "Archivo subido y embedding generado", "files": files_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{user_id}/files/{file_id}")
def delete_user_file(user_id: str, file_id: str, current_admin: str = Depends(get_current_admin)):
    """
    Elimina un archivo específico:
      - Borra el archivo en GCS.
      - Elimina el registro en EmployeeDocument.
      - Elimina el embedding asociado en FileEmbedding.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT "fileKey" FROM "EmployeeDocument" WHERE id = %s AND "userId" = %s', (file_id, user_id))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Archivo no encontrado")
        file_key = result[0]
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(file_key)
        blob.delete()
        cur.execute('DELETE FROM "EmployeeDocument" WHERE id = %s', (file_id,))
        conn.commit()
        cur.execute('DELETE FROM "FileEmbedding" WHERE "fileKey" = %s', (file_key,))
        conn.commit()
        cur.execute('SELECT id, url, "originalName" FROM "EmployeeDocument" WHERE "userId" = %s', (user_id,))
        files = cur.fetchall()
        files_list = [{"id": f[0], "url": f[1], "filename": f[2]} for f in files]
        cur.close()
        conn.close()
        return {"message": "Archivo y su embedding eliminados", "files": files_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
