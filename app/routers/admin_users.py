# backend/routers/admin_users.py
import os
import json
import psycopg2
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.security import OAuth2PasswordBearer
from dotenv import load_dotenv
from google.cloud import storage

from main import get_current_admin  # Asegurate de que la función get_current_admin esté definida en main.py

load_dotenv()

router = APIRouter(prefix="/admin/users", tags=["admin_users"])

# Función para obtener conexión a la BD (utilizada en otros endpoints también)
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
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la conexión a la base de datos: {e}")

# Configurar Google Cloud Storage
service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
storage_client = storage.Client.from_service_account_info(service_account_info)
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")

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
            # Consultar archivos asociados al usuario en EmployeeDocument
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
        # Obtener los archivos asociados al usuario
        cur.execute('SELECT "fileKey", url FROM "EmployeeDocument" WHERE "userId" = %s', (user_id,))
        files = cur.fetchall()
        bucket = storage_client.bucket(BUCKET_NAME)
        for f in files:
            file_key = f[0]
            blob = bucket.blob(file_key)
            blob.delete()  # Elimina el archivo de Google Cloud Storage
        # Eliminar registros en EmployeeDocument
        cur.execute('DELETE FROM "EmployeeDocument" WHERE "userId" = %s', (user_id,))
        # Eliminar embeddings asociados (tabla file_embeddings)
        cur.execute('DELETE FROM file_embeddings WHERE user_id = %s', (user_id,))
        # Eliminar el usuario
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
    Sube un archivo para el usuario y lo registra en EmployeeDocument.
    """
    try:
        file_bytes = file.file.read()
        safe_filename = file.filename.replace(" ", "_")
        # Definir la ruta del archivo en GCS
        file_key = f"user-files/{user_id}/{safe_filename}"
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(file_key)
        blob.upload_from_string(file_bytes, content_type=file.content_type)
        file_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{file_key}"
        # Registrar el archivo en la base de datos
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO "EmployeeDocument" ("userId", url, "fileKey", "originalName", "createdAt") VALUES (%s, %s, %s, %s, NOW()) RETURNING id',
            (user_id, file_url, file_key, safe_filename)
        )
        file_id = cur.fetchone()[0]
        conn.commit()
        # Obtener archivos actualizados del usuario
        cur.execute('SELECT id, url, "originalName" FROM "EmployeeDocument" WHERE "userId" = %s', (user_id,))
        files = cur.fetchall()
        files_list = [{"id": f[0], "url": f[1], "filename": f[2]} for f in files]
        cur.close()
        conn.close()
        return {"message": "Archivo subido", "files": files_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{user_id}/files/{file_id}")
def delete_user_file(user_id: str, file_id: str, current_admin: str = Depends(get_current_admin)):
    """
    Elimina un archivo específico:
      - Se borra el archivo en GCS.
      - Se elimina el registro en EmployeeDocument.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Obtener el registro del archivo
        cur.execute('SELECT "fileKey" FROM "EmployeeDocument" WHERE id = %s AND "userId" = %s', (file_id, user_id))
        result = cur.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Archivo no encontrado")
        file_key = result[0]
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(file_key)
        blob.delete()
        # Eliminar el registro del archivo
        cur.execute('DELETE FROM "EmployeeDocument" WHERE id = %s', (file_id,))
        conn.commit()
        # Obtener archivos actualizados del usuario
        cur.execute('SELECT id, url, "originalName" FROM "EmployeeDocument" WHERE "userId" = %s', (user_id,))
        files = cur.fetchall()
        files_list = [{"id": f[0], "url": f[1], "filename": f[2]} for f in files]
        cur.close()
        conn.close()
        return {"message": "Archivo eliminado", "files": files_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
