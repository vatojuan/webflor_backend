# app/routers/training.py
import os
import json
import uuid
import datetime
import psycopg2
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, status
from google.cloud import storage
from google.oauth2 import service_account

# --- Importaciones de la aplicación ---
from app.utils.auth_utils import get_current_admin, get_current_active_user, UserInDB
from app.routers.auth import get_db_connection


# ===============================================================
# =============== CONFIGURACIÓN DE SERVICIOS EXTERNOS ===========
# ===============================================================

storage_client: Optional[storage.Client] = None
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
credentials: Optional[service_account.Credentials] = None

try:
    credentials_json_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if credentials_json_str:
        service_account_info = json.loads(credentials_json_str)
        credentials = service_account.Credentials.from_service_account_info(service_account_info)
        storage_client = storage.Client(credentials=credentials)
    else:
        print("ADVERTENCIA: La variable de entorno GOOGLE_APPLICATION_CREDENTIALS_JSON no está definida.")
except (json.JSONDecodeError, TypeError, ValueError) as e:
    print(f"Error CRÍTICO al procesar las credenciales JSON: {e}")


# ===============================================================
# ===================== INICIALIZACIÓN ROUTER ===================
# ===============================================================

router = APIRouter(prefix="/training", tags=["Formación"])


# ===============================================================
# ======================== FUNCIONES ÚTILES =====================
# ===============================================================

def sanitize_filename(filename: str) -> str:
    filename = filename.replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_.-]", "", filename)


def _signed_url(blob_name: str, minutes: int = 60) -> Optional[str]:
    if not storage_client or not blob_name or not credentials:
        return None
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_name)
        return blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(minutes=minutes),
            method="GET",
            credentials=credentials,
        )
    except Exception as e:
        print(f"Error al generar URL firmada para '{blob_name}': {e}")
        return None


def generate_signed_url(blob_name: str) -> Optional[str]:
    return _signed_url(blob_name, minutes=60)


def delete_blob_from_gcs(blob_name: str):
    if not storage_client or not blob_name:
        return
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_name)
        if blob.exists():
            blob.delete()
    except Exception as e:
        print(f"Error al intentar eliminar el archivo {blob_name} de GCS: {e}")


# ===============================================================
# ================== ENDPOINTS PARA ADMINISTRADORES =============
# ===============================================================

@router.post("/courses", status_code=status.HTTP_201_CREATED, summary="Crear un nuevo curso")
def create_course(
    title: str = Form(...),
    description: str = Form(...),
    image: UploadFile = File(None),
    current_admin: UserInDB = Depends(get_current_admin),
):
    if not storage_client:
        raise HTTPException(status_code=503, detail="Servicio de almacenamiento no configurado correctamente.")

    image_blob_name = None
    if image:
        safe_filename = sanitize_filename(image.filename)
        image_blob_name = f"course-images/{uuid.uuid4()}-{safe_filename}"
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(image_blob_name)
        blob.upload_from_file(image.file, content_type=image.content_type)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO "Course" (title, description, "imageUrl", "createdBy") VALUES (%s, %s, %s, %s) RETURNING id',
            (title, description, image_blob_name, current_admin.id),
        )
        course_id = cur.fetchone()[0]
        conn.commit()
        return {"message": "Curso creado exitosamente", "courseId": course_id}
    finally:
        cur.close()
        conn.close()


@router.post("/courses/{course_id}/lessons", summary="Añadir una lección a un curso")
def upload_lesson(
    course_id: uuid.UUID,
    title: str = Form(...),
    order_index: int = Form(...),
    video: UploadFile = File(...),
    current_admin: UserInDB = Depends(get_current_admin),
):
    if not storage_client:
        raise HTTPException(status_code=500, detail="El servicio de almacenamiento no está configurado.")

    safe_filename = sanitize_filename(video.filename)
    video_blob_name = f"course-videos/{course_id}/{uuid.uuid4()}-{safe_filename}"
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(video_blob_name)
    blob.upload_from_file(video.file, content_type=video.content_type)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO "Lesson" ("courseId", title, "videoUrl", "orderIndex") VALUES (%s, %s, %s, %s)',
            (str(course_id), title, video_blob_name, order_index),
        )
        conn.commit()
        return {"message": "Lección añadida exitosamente"}
    finally:
        cur.close()
        conn.close()

# ------------- resto de endpoints de administración omitidos (sin cambios) -------------


# ===============================================================
# ===================== ENDPOINTS PARA USUARIOS =================
# ===============================================================

@router.get("/courses", summary="Listar cursos para un usuario")
def list_all_courses(current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = '''
            SELECT c.id, c.title, c.description, c."imageUrl",
                   (e.id IS NOT NULL) as "isEnrolled",
                   COALESCE(e.progress, 0) as progress
            FROM "Course" c
            LEFT JOIN "Enrollment" e
                ON c.id = e."courseId" AND e."userId" = %s
            ORDER BY c."createdAt" DESC;
        '''
        cur.execute(query, (current_user.id,))
        courses = []
        for r in cur.fetchall():
            courses.append({
                "id": r[0],
                "title": r[1],
                "description": r[2],
                "imageUrl": _signed_url(r[3], minutes=60) if r[3] else None,
                "isEnrolled": r[4],
                "progress": r[5],
            })
        return courses
    finally:
        cur.close()
        conn.close()


@router.get("/courses/{course_id}/details", summary="Ver el detalle de un curso")
def get_course_details_for_user(course_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Datos básicos del curso
        cur.execute('SELECT id, title, description FROM "Course" WHERE id = %s', (str(course_id),))
        course_data = cur.fetchone()
        if not course_data:
            raise HTTPException(status_code=404, detail="Curso no encontrado")

        course_details = {
            "id": course_data[0],
            "title": course_data[1],
            "description": course_data[2],
        }

        # Progreso del usuario en este curso
        cur.execute(
            'SELECT progress FROM "Enrollment" WHERE "courseId" = %s AND "userId" = %s',
            (str(course_id), current_user.id),
        )
        course_details["progress"] = (cur.fetchone() or [0])[0]

        # Lecciones + estado de cada una
        query = '''
            SELECT l.id, l.title, l."orderIndex", l."videoUrl",
                   (lp.id IS NOT NULL) as "isCompleted"
            FROM "Lesson" l
            LEFT JOIN "Enrollment" e
                   ON l."courseId" = e."courseId" AND e."userId" = %s
            LEFT JOIN "LessonProgress" lp
                   ON l.id = lp."lessonId" AND e.id = lp."enrollmentId"
            WHERE l."courseId" = %s
            ORDER BY l."orderIndex" ASC;
        '''
        cur.execute(query, (current_user.id, str(course_id)))
        lessons = []
        for r in cur.fetchall():
            lessons.append({
                "id": r[0],
                "title": r[1],
                "orderIndex": r[2],
                "videoUrl": _signed_url(r[3], minutes=60) if r[3] else None,
                "isCompleted": r[4],
            })

        course_details["lessons"] = lessons
        return course_details
    finally:
        cur.close()
        conn.close()


@router.post("/enroll/{course_id}", summary="Inscribirse a un curso")
def enroll_in_course(course_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO "Enrollment" ("userId", "courseId") VALUES (%s, %s)',
            (current_user.id, str(course_id)),
        )
        conn.commit()
        return {"message": "Inscripción exitosa"}
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=400, detail="Ya estás inscrito en este curso.")
    finally:
        cur.close()
        conn.close()


@router.delete("/unenroll/{course_id}", summary="Desinscribirse de un curso")
def unenroll_from_course(course_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            'DELETE FROM "Enrollment" WHERE "userId" = %s AND "courseId" = %s',
            (current_user.id, str(course_id)),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="No estás inscrito en este curso.")
        conn.commit()
        return {"message": "Te has desinscrito del curso exitosamente"}
    finally:
        cur.close()
        conn.close()


@router.post("/lessons/{lesson_id}/complete", summary="Marcar una lección como completada")
def complete_lesson(lesson_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            '''
            SELECT e.id, e."courseId"
            FROM "Enrollment" e
            JOIN "Lesson" l ON e."courseId" = l."courseId"
            WHERE e."userId" = %s AND l.id = %s
            ''',
            (current_user.id, str(lesson_id)),
        )
        enrollment_data = cur.fetchone()
        if not enrollment_data:
            raise HTTPException(status_code=403, detail="No estás inscrito en el curso de esta lección.")

        enrollment_id, course_id = enrollment_data

        cur.execute(
            'INSERT INTO "LessonProgress" ("enrollmentId", "lessonId") VALUES (%s, %s) ON CONFLICT DO NOTHING',
            (enrollment_id, str(lesson_id)),
        )

        cur.execute('SELECT COUNT(id) FROM "Lesson" WHERE "courseId" = %s', (course_id,))
        total_lessons = cur.fetchone()[0]

        cur.execute('SELECT COUNT(id) FROM "LessonProgress" WHERE "enrollmentId" = %s', (enrollment_id,))
        completed_lessons = cur.fetchone()[0]

        progress = int((completed_lessons / total_lessons) * 100) if total_lessons > 0 else 0
        cur.execute('UPDATE "Enrollment" SET progress = %s WHERE id = %s', (progress, enrollment_id))
        conn.commit()

        return {"message": "Progreso actualizado", "newProgress": progress}
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error al actualizar el progreso: {e}")
    finally:
        cur.close()
        conn.close()
