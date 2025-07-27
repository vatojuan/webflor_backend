# app/routers/admin_users.py
import os
import json
import psycopg2
import re
import io
import datetime
import logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from dotenv import load_dotenv
from google.cloud import storage
from PyPDF2 import PdfReader
from app.utils.auth_utils import get_current_admin
from app.services.embedding import generate_file_embedding, get_db_connection

load_dotenv()
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["admin_users"])

# Configurar Google Cloud Storage
try:
    service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
    storage_client = storage.Client.from_service_account_info(service_account_info)
    BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
except (json.JSONDecodeError, TypeError):
    logger.error("Credenciales de Google Cloud no configuradas correctamente.")
    storage_client = None
    BUCKET_NAME = None


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
                "id": u[0], "email": u[1], "name": u[2],
                "phone": u[3], "description": u[4], "files": []
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
    Actualiza el nombre, teléfono y descripción del usuario y regenera el embedding de la descripción.
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

        from app.services.embedding import update_user_embedding
        update_user_embedding(user_id)

        return {"message": "Usuario actualizado y embedding de descripción modificado"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- FUNCIÓN CORREGIDA ---
@router.delete("/{user_id}")
def delete_user(user_id: str, current_admin: str = Depends(get_current_admin)):
    """
    Elimina un usuario y todos sus datos asociados de forma segura y en el orden correcto.
    """
    conn = None
    cur = None
    logger.info(f"Iniciando proceso de eliminación para el usuario ID: {user_id}")
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Obtener todos los 'fileKey' de los documentos asociados al usuario.
        cur.execute('SELECT "fileKey" FROM "EmployeeDocument" WHERE "userId" = %s', (user_id,))
        files_to_delete = cur.fetchall()
        file_keys = [f[0] for f in files_to_delete if f[0]]
        
        if file_keys:
            logger.info(f"Se encontraron {len(file_keys)} archivos para eliminar.")
            
            # 2. Eliminar archivos de Google Cloud Storage
            if storage_client and BUCKET_NAME:
                bucket = storage_client.bucket(BUCKET_NAME)
                for key in file_keys:
                    try:
                        blob = bucket.blob(key)
                        blob.delete()
                        logger.info(f"Archivo eliminado de GCS: {key}")
                    except Exception as gcs_error:
                        logger.error(f"No se pudo eliminar el archivo {key} de GCS: {gcs_error}")
            
            # 3. Eliminar los embeddings asociados a esos fileKeys
            # Usamos 'ANY' para una eliminación eficiente de múltiples registros.
            cur.execute('DELETE FROM "FileEmbedding" WHERE "fileKey" = ANY(%s)', (file_keys,))
            logger.info(f"Embeddings eliminados para las claves: {file_keys}")

        # 4. Eliminar los registros de 'EmployeeDocument'
        cur.execute('DELETE FROM "EmployeeDocument" WHERE "userId" = %s', (user_id,))
        logger.info(f"Registros de EmployeeDocument eliminados para el usuario {user_id}.")

        # 5. Finalmente, eliminar el usuario de la tabla 'User'
        # Esto solo funcionará si no hay OTRAS dependencias (como proposals, matches, etc.)
        # Si las hay, se necesita ON DELETE CASCADE como discutimos.
        cur.execute('DELETE FROM "User" WHERE id = %s', (user_id,))
        logger.info(f"Registro del usuario {user_id} eliminado de la tabla User.")
        
        conn.commit()
        logger.info(f"Proceso de eliminación completado exitosamente para el usuario {user_id}.")
        return {"message": "Usuario y todos sus datos asociados han sido eliminados."}

    except psycopg2.errors.ForeignKeyViolation as fk_error:
        if conn: conn.rollback()
        logger.error(f"Error de clave externa al eliminar usuario {user_id}: {fk_error}")
        raise HTTPException(status_code=409, detail="No se puede eliminar el usuario porque tiene postulaciones u otra actividad registrada. Elimine esas dependencias primero.")
    
    except Exception as e:
        if conn: conn.rollback()
        logger.exception(f"Error inesperado al eliminar el usuario {user_id}.")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.post("/{user_id}/files")
def upload_user_file(user_id: str, file: UploadFile = File(...), current_admin: str = Depends(get_current_admin)):
    """
    Sube un archivo para un usuario y genera su embedding.
    """
    try:
        file_bytes = file.file.read()
        safe_filename = sanitize_filename(file.filename)
        file_key = f"user-files/{user_id}/{safe_filename}"
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(file_key)
        blob.upload_from_string(file_bytes, content_type=file.content_type)
        file_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{file_key}"
        
        text_content = extract_text_from_pdf(file_bytes)
        if not text_content:
            raise HTTPException(status_code=400, detail="No se pudo extraer texto del archivo para generar embedding")
        
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
    Elimina un archivo específico de un usuario.
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

# =================================================================
# === NUEVA RUTA AÑADIDA PARA GENERAR URL DE DESCARGA SEGURA ===
# =================================================================
@router.get("/files/{file_id}/signed-url")
def get_signed_url_for_file(file_id: int, current_admin: str = Depends(get_current_admin)):
    """
    Genera una URL firmada (temporal y segura) para descargar un archivo.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Buscar el 'fileKey' del archivo en la base de datos
        cur.execute('SELECT "fileKey" FROM "EmployeeDocument" WHERE id = %s', (file_id,))
        result = cur.fetchone()
        cur.close()
        conn.close()

        if not result:
            raise HTTPException(status_code=404, detail="Archivo no encontrado en la base de datos.")

        file_key = result[0]
        
        # 2. Generar la URL firmada desde Google Cloud Storage
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(file_key)

        # La URL expirará en 15 minutos
        expiration_time = datetime.timedelta(minutes=15)
        
        signed_url = blob.generate_signed_url(
            version="v4",
            expiration=expiration_time,
            method="GET",
        )

        return {"url": signed_url}

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {e}")
