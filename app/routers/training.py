import os
import json
import uuid
import psycopg2
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, status
from typing import List
from google.cloud import storage

# --- Importaciones de la aplicación ---
from app.utils.auth_utils import get_current_admin, get_current_active_user, UserInDB
from app.routers.auth import get_db_connection

# --- Configuración de Servicios Externos ---
try:
    # Carga de credenciales de Google Cloud Storage
    service_account_info = json.loads(os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON"))
    storage_client = storage.Client.from_service_account_info(service_account_info)
    BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
except (json.JSONDecodeError, TypeError):
    storage_client = None
    BUCKET_NAME = None

# --- Inicialización del Router ---
router = APIRouter(prefix="/training", tags=["Formación"])


# --- Funciones Auxiliares ---
def delete_blob_from_gcs(blob_url: str):
    """
    Elimina un archivo de Google Cloud Storage a partir de su URL pública.
    Si falla, imprime un error pero no detiene la ejecución.
    """
    if not storage_client or not blob_url or not blob_url.startswith(f"https://storage.googleapis.com/{BUCKET_NAME}/"):
        return
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob_name = blob_url.replace(f"https://storage.googleapis.com/{BUCKET_NAME}/", "")
        blob = bucket.blob(blob_name)
        if blob.exists():
            blob.delete()
    except Exception as e:
        print(f"Error al intentar eliminar el archivo {blob_url} de GCS: {e}")


# ===============================================================
# ================== ENDPOINTS PARA ADMINISTRADORES ==============
# ===============================================================

@router.post("/courses", status_code=status.HTTP_201_CREATED, summary="Crear un nuevo curso")
def create_course(
    title: str = Form(...),
    description: str = Form(...),
    image: UploadFile = File(None),
    current_admin: UserInDB = Depends(get_current_admin)
):
    """Crea un nuevo curso. Requiere permisos de administrador."""
    image_url = None
    if image and storage_client:
        image_blob_name = f"course-images/{uuid.uuid4()}-{image.filename}"
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(image_blob_name)
        blob.upload_from_file(image.file, content_type=image.content_type)
        image_url = blob.public_url

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO "Course" (title, description, "imageUrl", "createdBy") VALUES (%s, %s, %s, %s) RETURNING id',
            (title, description, image_url, current_admin.id)
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
    current_admin: UserInDB = Depends(get_current_admin)
):
    """Sube un video como una lección para un curso existente."""
    if not storage_client:
        raise HTTPException(status_code=500, detail="El servicio de almacenamiento no está configurado.")

    video_blob_name = f"course-videos/{course_id}/{uuid.uuid4()}-{video.filename}"
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(video_blob_name)
    blob.upload_from_file(video.file, content_type=video.content_type)
    video_url = blob.public_url

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO "Lesson" ("courseId", title, "videoUrl", "orderIndex") VALUES (%s, %s, %s, %s)',
            (str(course_id), title, video_url, order_index)
        )
        conn.commit()
        return {"message": "Lección añadida exitosamente", "videoUrl": video_url}
    finally:
        cur.close()
        conn.close()

@router.get("/admin/courses", summary="Listar todos los cursos para el panel de admin")
def admin_list_courses(current_admin: UserInDB = Depends(get_current_admin)):
    """Devuelve una lista de todos los cursos con el número de estudiantes inscritos."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = """
        SELECT c.id, c.title, c.description, c."imageUrl", COUNT(e.id) as student_count
        FROM "Course" c
        LEFT JOIN "Enrollment" e ON c.id = e."courseId"
        GROUP BY c.id
        ORDER BY c."createdAt" DESC;
        """
        cur.execute(query)
        return [
            {"id": r[0], "title": r[1], "description": r[2], "imageUrl": r[3], "studentCount": r[4]}
            for r in cur.fetchall()
        ]
    finally:
        cur.close()
        conn.close()

@router.get("/admin/courses/{course_id}/enrollments", summary="Ver inscripciones de un curso")
def admin_get_enrollments(course_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    """Devuelve los detalles de los usuarios inscritos en un curso específico."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = """
        SELECT u.name, u.email, e.progress FROM "Enrollment" e
        JOIN "User" u ON e."userId" = u.id
        WHERE e."courseId" = %s ORDER BY u.name;
        """
        cur.execute(query, (str(course_id),))
        return [{"name": r[0], "email": r[1], "progress": r[2]} for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

@router.get("/admin/courses/{course_id}/lessons", summary="Ver lecciones de un curso")
def admin_get_lessons(course_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    """Devuelve la lista de lecciones de un curso específico."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = 'SELECT id, title, "orderIndex", "videoUrl" FROM "Lesson" WHERE "courseId" = %s ORDER BY "orderIndex" ASC'
        cur.execute(query, (str(course_id),))
        return [{"id": r[0], "title": r[1], "orderIndex": r[2], "videoUrl": r[3]} for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

@router.delete("/admin/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un curso")
def admin_delete_course(course_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    """Elimina un curso, sus lecciones, inscripciones y todos los archivos asociados en GCS."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('SELECT "imageUrl" FROM "Course" WHERE id = %s', (str(course_id),))
        image_url_to_delete = (cur.fetchone() or [None])[0]

        cur.execute('SELECT "videoUrl" FROM "Lesson" WHERE "courseId" = %s', (str(course_id),))
        video_urls_to_delete = [row[0] for row in cur.fetchall()]

        cur.execute('DELETE FROM "Course" WHERE id = %s', (str(course_id),))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Curso no encontrado")
        conn.commit()

        if image_url_to_delete:
            delete_blob_from_gcs(image_url_to_delete)
        for video_url in video_urls_to_delete:
            delete_blob_from_gcs(video_url)
        return
    except Exception as e:
        if conn: conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error al eliminar el curso: {e}")
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ===============================================================
# ===================== ENDPOINTS PARA USUARIOS =================
# ===============================================================

@router.get("/courses", summary="Listar cursos para un usuario")
def list_all_courses(current_user: UserInDB = Depends(get_current_active_user)):
    """Devuelve todos los cursos, indicando si el usuario actual está inscrito y su progreso."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = """
        SELECT c.id, c.title, c.description, c."imageUrl", e.id IS NOT NULL as "isEnrolled", COALESCE(e.progress, 0) as progress
        FROM "Course" c
        LEFT JOIN "Enrollment" e ON c.id = e."courseId" AND e."userId" = %s
        ORDER BY c."createdAt" DESC;
        """
        cur.execute(query, (current_user.id,))
        return [{"id": r[0], "title": r[1], "description": r[2], "imageUrl": r[3], "isEnrolled": r[4], "progress": r[5]} for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

@router.get("/courses/{course_id}/details", summary="Ver el detalle de un curso")
def get_course_details_for_user(course_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    """Obtiene los detalles de un curso, su lista de lecciones y marca cuáles ha completado el usuario."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('SELECT id, title, description FROM "Course" WHERE id = %s', (str(course_id),))
        course_data = cur.fetchone()
        if not course_data:
            raise HTTPException(status_code=404, detail="Curso no encontrado")
        
        course_details = {"id": course_data[0], "title": course_data[1], "description": course_data[2]}

        query = """
        SELECT l.id, l.title, l."orderIndex", l."videoUrl", (lp.id IS NOT NULL) as "isCompleted"
        FROM "Lesson" l
        LEFT JOIN "Enrollment" e ON l."courseId" = e."courseId" AND e."userId" = %s
        LEFT JOIN "LessonProgress" lp ON l.id = lp."lessonId" AND e.id = lp."enrollmentId"
        WHERE l."courseId" = %s ORDER BY l."orderIndex" ASC;
        """
        cur.execute(query, (current_user.id, str(course_id)))
        course_details["lessons"] = [{"id": r[0], "title": r[1], "orderIndex": r[2], "videoUrl": r[3], "isCompleted": r[4]} for r in cur.fetchall()]
        return course_details
    finally:
        cur.close()
        conn.close()

@router.post("/enroll/{course_id}", summary="Inscribirse a un curso")
def enroll_in_course(course_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    """Inscribe al usuario actual en un curso."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('INSERT INTO "Enrollment" ("userId", "courseId") VALUES (%s, %s)', (current_user.id, str(course_id)))
        conn.commit()
        return {"message": "Inscripción exitosa"}
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=400, detail="Ya estás inscrito en este curso.")
    finally:
        cur.close()
        conn.close()

@router.post("/lessons/{lesson_id}/complete", summary="Marcar una lección como completada")
def complete_lesson(lesson_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    """Marca una lección como completada y recalcula el progreso del curso."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT e.id, e."courseId" FROM "Enrollment" e
            JOIN "Lesson" l ON e."courseId" = l."courseId"
            WHERE e."userId" = %s AND l.id = %s
        """, (current_user.id, str(lesson_id)))
        enrollment_data = cur.fetchone()
        if not enrollment_data:
            raise HTTPException(status_code=403, detail="No estás inscrito en el curso de esta lección.")
        
        enrollment_id, course_id = enrollment_data

        cur.execute('INSERT INTO "LessonProgress" ("enrollmentId", "lessonId") VALUES (%s, %s) ON CONFLICT DO NOTHING', (enrollment_id, str(lesson_id)))
        
        cur.execute('SELECT COUNT(id) FROM "Lesson" WHERE "courseId" = %s', (course_id,))
        total_lessons = cur.fetchone()[0]
        
        cur.execute('SELECT COUNT(id) FROM "LessonProgress" WHERE "enrollmentId" = %s', (enrollment_id,))
        completed_lessons = cur.fetchone()[0]

        progress = int((completed_lessons / total_lessons) * 100) if total_lessons > 0 else 0

        cur.execute('UPDATE "Enrollment" SET progress = %s WHERE id = %s', (progress, enrollment_id))
        
        conn.commit()
        return {"message": "Progreso actualizado", "newProgress": progress}
    except Exception as e:
        if conn: conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error al actualizar el progreso: {e}")
    finally:
        cur.close()
        conn.close()
