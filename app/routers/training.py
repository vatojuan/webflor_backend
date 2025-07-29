import os
import json
import uuid
import psycopg2
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form
from typing import List
from google.cloud import storage

# --- Importaciones Corregidas ---
# Reutiliza tus utilidades existentes y el modelo UserInDB que ya definimos
from app.utils.auth_utils import get_current_admin, get_current_active_user, UserInDB
from app.routers.auth import get_db_connection

# --- Configuración de Google Cloud Storage ---
try:
    service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
    storage_client = storage.Client.from_service_account_info(service_account_info)
    BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
except (json.JSONDecodeError, TypeError):
    storage_client = None
    BUCKET_NAME = None

router = APIRouter(prefix="/training", tags=["Formación"])

# ===============================================================
# ================== ENDPOINTS PARA ADMINISTRADORES ==============
# ===============================================================

@router.post("/courses", status_code=201)
def create_course(
    title: str = Form(...),
    description: str = Form(...),
    image: UploadFile = File(None),
    # Usamos el modelo UserInDB para el tipado
    current_admin: UserInDB = Depends(get_current_admin)
):
    """Crea un nuevo curso. Solo para administradores."""
    image_url = None
    if image and storage_client:
        image_blob_name = f"course-images/{uuid.uuid4()}-{image.filename}"
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(image_blob_name)
        blob.upload_from_file(image.file, content_type=image.content_type)
        image_url = blob.public_url

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO "Course" (title, description, "imageUrl", "createdBy") VALUES (%s, %s, %s, %s) RETURNING id',
        (title, description, image_url, current_admin.id)
    )
    course_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "Curso creado exitosamente", "courseId": course_id}

@router.post("/courses/{course_id}/lessons")
def upload_lesson(
    course_id: uuid.UUID,
    title: str = Form(...),
    order_index: int = Form(...),
    video: UploadFile = File(...),
    current_admin: UserInDB = Depends(get_current_admin)
):
    """Sube un video como una lección para un curso existente."""
    if not storage_client:
        raise HTTPException(status_code=500, detail="Google Cloud Storage no está configurado.")

    video_blob_name = f"course-videos/{course_id}/{uuid.uuid4()}-{video.filename}"
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(video_blob_name)
    blob.upload_from_file(video.file, content_type=video.content_type)
    video_url = blob.public_url

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO "Lesson" ("courseId", title, "videoUrl", "orderIndex") VALUES (%s, %s, %s, %s)',
        (str(course_id), title, video_url, order_index)
    )
    conn.commit()
    cur.close()
    conn.close()
    return {"message": "Lección añadida exitosamente", "videoUrl": video_url}

# ===============================================================
# ===================== ENDPOINTS PARA USUARIOS =================
# ===============================================================

@router.get("/courses")
def list_all_courses(current_user: UserInDB = Depends(get_current_active_user)):
    """
    Devuelve todos los cursos. Para cada curso, indica si el usuario actual
    está inscrito y cuál es su progreso.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    query = """
    SELECT c.id, c.title, c.description, c."imageUrl",
           e.id IS NOT NULL as "isEnrolled",
           COALESCE(e.progress, 0) as progress
    FROM "Course" c
    LEFT JOIN "Enrollment" e ON c.id = e."courseId" AND e."userId" = %s
    ORDER BY c."createdAt" DESC;
    """
    cur.execute(query, (current_user.id,))
    courses = [
        {
            "id": row[0], "title": row[1], "description": row[2], "imageUrl": row[3],
            "isEnrolled": row[4], "progress": row[5]
        } for row in cur.fetchall()
    ]
    cur.close()
    conn.close()
    return courses

@router.post("/enroll/{course_id}")
def enroll_in_course(course_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    """Inscribe al usuario actual en un curso."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO "Enrollment" ("userId", "courseId") VALUES (%s, %s)',
            (current_user.id, str(course_id))
        )
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=400, detail="Ya estás inscrito en este curso.")
    finally:
        cur.close()
        conn.close()
    return {"message": "Inscripción exitosa"}
