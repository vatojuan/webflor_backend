import os
import json
import uuid
import datetime
import psycopg2
import re
from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, status
from typing import List
from google.cloud import storage
from google.oauth2 import service_account

# --- Importaciones de la aplicación ---
from app.utils.auth_utils import get_current_admin, get_current_active_user, UserInDB
from app.routers.auth import get_db_connection

# --- Configuración de Servicios Externos ---
# Se inicializan las variables para que existan en el ámbito del módulo.
storage_client = None
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
credentials = None

try:
    # --- MÉTODO CONSISTENTE CON TU PROYECTO ---
    # Se carga la información de la cuenta de servicio desde la variable de entorno JSON.
    credentials_json_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if credentials_json_str:
        service_account_info = json.loads(credentials_json_str)
        # Se crea un objeto de credenciales explícito que usaremos para firmar.
        credentials = service_account.Credentials.from_service_account_info(service_account_info)
        storage_client = storage.Client(credentials=credentials)
    else:
        print("ADVERTENCIA: La variable de entorno GOOGLE_APPLICATION_CREDENTIALS_JSON no está definida.")

except (json.JSONDecodeError, TypeError, ValueError) as e:
    print(f"Error CRÍTICO al procesar las credenciales JSON: {e}")


# --- Inicialización del Router de FastAPI ---
router = APIRouter(prefix="/training", tags=["Formación"])


# --- Funciones Auxiliares ---
def sanitize_filename(filename: str) -> str:
    """Limpia un nombre de archivo para que sea seguro para la URL."""
    filename = filename.replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_.-]", "", filename)

def generate_signed_url(blob_name: str) -> str:
    """
    Genera una URL firmada a partir de la RUTA RELATIVA (blob_name) del archivo,
    usando explícitamente las credenciales cargadas.
    """
    if not storage_client or not blob_name or not credentials:
        print(f"No se puede generar URL firmada. Cliente: {storage_client is not None}, Blob: {blob_name}, Creds: {credentials is not None}")
        return None
    try:
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_name)
        
        signed_url = blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(hours=1),
            method="GET",
        )
        
        cache_buster = f"&t={int(datetime.datetime.now().timestamp())}"
        return signed_url + cache_buster
    except Exception as e:
        print(f"Error detallado al generar URL firmada para '{blob_name}': {e}")
        return None # Devolver None en caso de error para evitar URLs rotas.

def delete_blob_from_gcs(blob_name: str):
    """Elimina un archivo de GCS a partir de su RUTA RELATIVA (blob_name)."""
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
# ================== ENDPOINTS PARA ADMINISTRADORES ==============
# ===============================================================

@router.post("/courses", status_code=status.HTTP_201_CREATED, summary="Crear un nuevo curso")
def create_course(
    title: str = Form(...),
    description: str = Form(...),
    image: UploadFile = File(None),
    current_admin: UserInDB = Depends(get_current_admin)
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
            (title, description, image_blob_name, current_admin.id)
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
            (str(course_id), title, video_blob_name, order_index)
        )
        conn.commit()
        return {"message": "Lección añadida exitosamente"}
    finally:
        cur.close()
        conn.close()

@router.get("/admin/courses", summary="Listar todos los cursos para el panel de admin")
def admin_list_courses(current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = 'SELECT c.id, c.title, c.description, c."imageUrl", COUNT(e.id) as student_count FROM "Course" c LEFT JOIN "Enrollment" e ON c.id = e."courseId" GROUP BY c.id ORDER BY c."createdAt" DESC;'
        cur.execute(query)
        return [{"id": r[0], "title": r[1], "description": r[2], "imageUrl": r[3], "studentCount": r[4]} for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

@router.get("/admin/courses/{course_id}/enrollments", summary="Ver inscripciones de un curso")
def admin_get_enrollments(course_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = 'SELECT u.name, u.email, e.progress FROM "Enrollment" e JOIN "User" u ON e."userId" = u.id WHERE e."courseId" = %s ORDER BY u.name;'
        cur.execute(query, (str(course_id),))
        return [{"name": r[0], "email": r[1], "progress": r[2]} for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

@router.get("/admin/courses/{course_id}/lessons", summary="Ver lecciones de un curso")
def admin_get_lessons(course_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
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
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('SELECT "imageUrl" FROM "Course" WHERE id = %s', (str(course_id),))
        image_blob_to_delete = (cur.fetchone() or [None])[0]
        cur.execute('SELECT "videoUrl" FROM "Lesson" WHERE "courseId" = %s', (str(course_id),))
        video_blobs_to_delete = [row[0] for row in cur.fetchall()]
        cur.execute('DELETE FROM "Course" WHERE id = %s', (str(course_id),))
        if cur.rowcount == 0: raise HTTPException(status_code=404, detail="Curso no encontrado")
        conn.commit()
        if image_blob_to_delete: delete_blob_from_gcs(image_blob_to_delete)
        for video_blob in video_blobs_to_delete: delete_blob_from_gcs(video_blob)
        return
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.put("/admin/lessons/{lesson_id}", summary="Editar una lección")
def admin_edit_lesson(lesson_id: uuid.UUID, title: str = Form(...), order_index: int = Form(...), current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('UPDATE "Lesson" SET title = %s, "orderIndex" = %s WHERE id = %s', (title, order_index, str(lesson_id)))
        if cur.rowcount == 0: raise HTTPException(status_code=404, detail="Lección no encontrada")
        conn.commit()
        return {"message": "Lección actualizada con éxito"}
    finally:
        cur.close()
        conn.close()

@router.delete("/admin/lessons/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar una lección")
def admin_delete_lesson(lesson_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('SELECT "videoUrl" FROM "Lesson" WHERE id = %s', (str(lesson_id),))
        video_blob_to_delete = (cur.fetchone() or [None])[0]
        cur.execute('DELETE FROM "Lesson" WHERE id = %s', (str(lesson_id),))
        if cur.rowcount == 0: raise HTTPException(status_code=404, detail="Lección no encontrada")
        conn.commit()
        if video_blob_to_delete: delete_blob_from_gcs(video_blob_to_delete)
        return
    finally:
        cur.close()
        conn.close()

@router.get("/admin/lessons/{lesson_id}/signed-url", summary="Obtener URL de descarga de video")
def admin_get_lesson_download_url(lesson_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('SELECT "videoUrl" FROM "Lesson" WHERE id = %s', (str(lesson_id),))
        video_url = (cur.fetchone() or [None])[0]
        if not video_url: raise HTTPException(status_code=404, detail="Video no encontrado")
        if not storage_client: raise HTTPException(status_code=500, detail="Servicio de almacenamiento no configurado.")
        bucket = storage_client.bucket(BUCKET_NAME)
        blob_name = video_url
        blob = bucket.blob(blob_name)
        signed_url = blob.generate_signed_url(version="v4", expiration=datetime.timedelta(minutes=15), method="GET")
        return {"url": signed_url}
    finally:
        cur.close()
        conn.close()


# ===============================================================
# ===================== ENDPOINTS PARA USUARIOS =================
# ===============================================================

@router.get("/courses", summary="Listar cursos para un usuario")
def list_all_courses(current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = 'SELECT c.id, c.title, c.description, c."imageUrl", e.id IS NOT NULL as "isEnrolled", COALESCE(e.progress, 0) as progress FROM "Course" c LEFT JOIN "Enrollment" e ON c.id = e."courseId" AND e."userId" = %s ORDER BY c."createdAt" DESC;'
        cur.execute(query, (current_user.id,))
        courses = []
        for r in cur.fetchall():
            courses.append({
                "id": r[0], "title": r[1], "description": r[2],
                "imageUrl": generate_signed_url(r[3]) if r[3] else None,
                "isEnrolled": r[4], "progress": r[5]
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
        cur.execute('SELECT id, title, description FROM "Course" WHERE id = %s', (str(course_id),))
        course_data = cur.fetchone()
        if not course_data: raise HTTPException(status_code=404, detail="Curso no encontrado")
        course_details = {"id": course_data[0], "title": course_data[1], "description": course_data[2]}
        query = 'SELECT l.id, l.title, l."orderIndex", l."videoUrl", (lp.id IS NOT NULL) as "isCompleted" FROM "Lesson" l LEFT JOIN "Enrollment" e ON l."courseId" = e."courseId" AND e."userId" = %s LEFT JOIN "LessonProgress" lp ON l.id = lp."lessonId" AND e.id = lp."enrollmentId" WHERE l."courseId" = %s ORDER BY l."orderIndex" ASC;'
        cur.execute(query, (current_user.id, str(course_id)))
        lessons = []
        for r in cur.fetchall():
            lessons.append({
                "id": r[0], "title": r[1], "orderIndex": r[2],
                "videoUrl": generate_signed_url(r[3]),
                "isCompleted": r[4]
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
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('SELECT e.id, e."courseId" FROM "Enrollment" e JOIN "Lesson" l ON e."courseId" = l."courseId" WHERE e."userId" = %s AND l.id = %s', (current_user.id, str(lesson_id)))
        enrollment_data = cur.fetchone()
        if not enrollment_data: raise HTTPException(status_code=403, detail="No estás inscrito en el curso de esta lección.")
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
