# app/routers/training.py
import os
import json
import uuid
import datetime
import psycopg2
import re
from typing import Optional

from fastapi import (
    APIRouter, Depends, HTTPException, File, UploadFile,
    Form, status
)
from google.cloud import storage
from google.oauth2 import service_account

# --- App imports ------------------------------------------------------------
from app.utils.auth_utils import (
    get_current_admin,
    get_current_active_user,
    UserInDB,
)
from app.routers.auth import get_db_connection

# ============================================================================
#                        CONFIGURACIÓN GCS
# ============================================================================
storage_client: Optional[storage.Client] = None
BUCKET_NAME = os.getenv("GOOGLE_STORAGE_BUCKET")
credentials: Optional[service_account.Credentials] = None

try:
    credentials_json_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if credentials_json_str:
        info = json.loads(credentials_json_str)
        credentials = service_account.Credentials.from_service_account_info(info)
        storage_client = storage.Client(credentials=credentials)
    else:
        print("⚠️  Falta la var GOOGLE_APPLICATION_CREDENTIALS_JSON.")
except (json.JSONDecodeError, ValueError) as e:
    print(f"❌ Error cargando credenciales GCS: {e}")

# ============================================================================
#                               ROUTER
# ============================================================================
router = APIRouter(prefix="/training", tags=["Formación"])

# ============================================================================
#                            UTILIDADES
# ============================================================================
def sanitize_filename(filename: str) -> str:
    """Reemplaza espacios y quita caracteres peligrosos."""
    filename = filename.replace(" ", "_")
    return re.sub(r"[^a-zA-Z0-9_.-]", "", filename)


def _signed_url(blob_name: str, minutes: int = 60) -> Optional[str]:
    """Genera URL firmada V4 sin modificar query-string."""
    if not (storage_client and credentials and blob_name):
        return None
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(blob_name)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(minutes=minutes),
        method="GET",
        credentials=credentials,
    )


def delete_blob_from_gcs(blob_name: str):
    if not (storage_client and blob_name):
        return
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(blob_name)
    try:
        if blob.exists():
            blob.delete()
    except Exception as e:
        print(f"❌ No se pudo borrar {blob_name}: {e}")

# ============================================================================
#                    ENDPOINTS PARA ADMINISTRADORES
# ============================================================================
@router.post("/courses", status_code=status.HTTP_201_CREATED)
def create_course(
    title: str = Form(...),
    description: str = Form(...),
    image: UploadFile = File(None),
    current_admin: UserInDB = Depends(get_current_admin),
):
    if not storage_client:
        raise HTTPException(503, "GCS no configurado.")

    image_blob = None
    if image:
        safe = sanitize_filename(image.filename)
        image_blob = f"course-images/{uuid.uuid4()}-{safe}"
        blob = storage_client.bucket(BUCKET_NAME).blob(image_blob)
        blob.upload_from_file(image.file, content_type=image.content_type)

    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO "Course"(title,description,"imageUrl","createdBy")'
            ' VALUES (%s,%s,%s,%s) RETURNING id',
            (title, description, image_blob, current_admin.id),
        )
        course_id = cur.fetchone()[0]
        conn.commit()
        return {"message": "Curso creado", "courseId": course_id}
    finally:
        cur.close(); conn.close()


@router.post("/courses/{course_id}/lessons")
def upload_lesson(
    course_id: uuid.UUID,
    title: str = Form(...),
    order_index: int = Form(...),
    video: UploadFile = File(...),
    current_admin: UserInDB = Depends(get_current_admin),
):
    if not storage_client:
        raise HTTPException(500, "GCS no configurado.")

    safe = sanitize_filename(video.filename)
    video_blob = f"course-videos/{course_id}/{uuid.uuid4()}-{safe}"
    blob = storage_client.bucket(BUCKET_NAME).blob(video_blob)
    blob.upload_from_file(video.file, content_type=video.content_type)

    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO "Lesson"("courseId",title,"videoUrl","orderIndex") '
            'VALUES(%s,%s,%s,%s)',
            (str(course_id), title, video_blob, order_index),
        )
        conn.commit()
        return {"message": "Lección creada"}
    finally:
        cur.close(); conn.close()


@router.get("/admin/courses")
def admin_list_courses(current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            'SELECT c.id,c.title,c.description,c."imageUrl",'
            ' COUNT(e.id) FROM "Course" c '
            'LEFT JOIN "Enrollment" e ON c.id=e."courseId" '
            'GROUP BY c.id ORDER BY c."createdAt" DESC'
        )
        return [
            {
                "id": i,
                "title": t,
                "description": d,
                "imageUrl": u,
                "studentCount": n,
            }
            for i, t, d, u, n in cur.fetchall()
        ]
    finally:
        cur.close(); conn.close()


@router.get("/admin/courses/{course_id}/enrollments")
def admin_get_enrollments(course_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            'SELECT u.name,u.email,e.progress '
            'FROM "Enrollment" e JOIN "User" u ON e."userId"=u.id '
            'WHERE e."courseId"=%s ORDER BY u.name',
            (str(course_id),),
        )
        return [{"name": n, "email": e, "progress": p} for n, e, p in cur.fetchall()]
    finally:
        cur.close(); conn.close()


@router.get("/admin/courses/{course_id}/lessons")
def admin_get_lessons(course_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            'SELECT id,title,"orderIndex","videoUrl" '
            'FROM "Lesson" WHERE "courseId"=%s ORDER BY "orderIndex"',
            (str(course_id),),
        )
        return [
            {"id": i, "title": t, "orderIndex": o, "videoUrl": v}
            for i, t, o, v in cur.fetchall()
        ]
    finally:
        cur.close(); conn.close()


@router.delete("/admin/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_course(course_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute('SELECT "imageUrl" FROM "Course" WHERE id=%s', (str(course_id),))
        img_blob = (cur.fetchone() or [None])[0]

        cur.execute('SELECT "videoUrl" FROM "Lesson" WHERE "courseId"=%s', (str(course_id),))
        vid_blobs = [row[0] for row in cur.fetchall()]

        cur.execute('DELETE FROM "Course" WHERE id=%s', (str(course_id),))
        if cur.rowcount == 0:
            raise HTTPException(404, "Curso no encontrado")
        conn.commit()

        if img_blob:
            delete_blob_from_gcs(img_blob)
        for b in vid_blobs:
            delete_blob_from_gcs(b)
    finally:
        cur.close(); conn.close()


@router.put("/admin/lessons/{lesson_id}")
def admin_edit_lesson(
    lesson_id: uuid.UUID,
    title: str = Form(...),
    order_index: int = Form(...),
    current_admin: UserInDB = Depends(get_current_admin),
):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            'UPDATE "Lesson" SET title=%s,"orderIndex"=%s WHERE id=%s',
            (title, order_index, str(lesson_id)),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Lección no encontrada")
        conn.commit()
        return {"message": "Lección actualizada"}
    finally:
        cur.close(); conn.close()


@router.delete("/admin/lessons/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_lesson(lesson_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute('SELECT "videoUrl" FROM "Lesson" WHERE id=%s', (str(lesson_id),))
        blob_name = (cur.fetchone() or [None])[0]

        cur.execute('DELETE FROM "Lesson" WHERE id=%s', (str(lesson_id),))
        if cur.rowcount == 0:
            raise HTTPException(404, "Lección no encontrada")
        conn.commit()

        if blob_name:
            delete_blob_from_gcs(blob_name)
    finally:
        cur.close(); conn.close()


@router.get("/admin/lessons/{lesson_id}/signed-url")
def admin_get_lesson_download_url(lesson_id: uuid.UUID, current_admin: UserInDB = Depends(get_current_admin)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute('SELECT "videoUrl" FROM "Lesson" WHERE id=%s', (str(lesson_id),))
        blob_name = (cur.fetchone() or [None])[0]
        if not blob_name:
            raise HTTPException(404, "Video no encontrado")
        url = _signed_url(blob_name, minutes=15)
        if not url:
            raise HTTPException(500, "No se pudo generar URL")
        return {"url": url}
    finally:
        cur.close(); conn.close()

# ============================================================================
#                       ENDPOINTS PARA USUARIOS
# ============================================================================
@router.get("/courses")
def list_all_courses(current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            'SELECT c.id,c.title,c.description,c."imageUrl",'
            ' (e.id IS NOT NULL),COALESCE(e.progress,0) '
            'FROM "Course" c LEFT JOIN "Enrollment" e '
            'ON c.id=e."courseId" AND e."userId"=%s '
            'ORDER BY c."createdAt" DESC',
            (current_user.id,),
        )
        resp = []
        for r in cur.fetchall():
            resp.append(
                {
                    "id": r[0],
                    "title": r[1],
                    "description": r[2],
                    "imageUrl": _signed_url(r[3]) if r[3] else None,
                    "isEnrolled": r[4],
                    "progress": r[5],
                }
            )
        return resp
    finally:
        cur.close(); conn.close()


@router.get("/courses/{course_id}/details")
def get_course_details_for_user(course_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute('SELECT id,title,description FROM "Course" WHERE id=%s', (str(course_id),))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Curso no encontrado")

        details = {"id": row[0], "title": row[1], "description": row[2]}

        cur.execute(
            'SELECT progress FROM "Enrollment" WHERE "courseId"=%s AND "userId"=%s',
            (str(course_id), current_user.id),
        )
        details["progress"] = (cur.fetchone() or [0])[0]

        cur.execute(
            '''
            SELECT l.id,l.title,l."orderIndex",l."videoUrl",
                   (lp.id IS NOT NULL)
            FROM "Lesson" l
            LEFT JOIN "Enrollment" e
              ON l."courseId"=e."courseId" AND e."userId"=%s
            LEFT JOIN "LessonProgress" lp
              ON l.id=lp."lessonId" AND e.id=lp."enrollmentId"
            WHERE l."courseId"=%s
            ORDER BY l."orderIndex"
            ''',
            (current_user.id, str(course_id)),
        )
        details["lessons"] = [
            {
                "id": i,
                "title": t,
                "orderIndex": o,
                "videoUrl": _signed_url(v) if v else None,
                "isCompleted": c,
            }
            for i, t, o, v, c in cur.fetchall()
        ]
        return details
    finally:
        cur.close(); conn.close()


@router.post("/enroll/{course_id}")
def enroll_in_course(course_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            'INSERT INTO "Enrollment"("userId","courseId") VALUES(%s,%s)',
            (current_user.id, str(course_id)),
        )
        conn.commit()
        return {"message": "Inscripción exitosa"}
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(400, "Ya inscrito")
    finally:
        cur.close(); conn.close()


@router.delete("/unenroll/{course_id}")
def unenroll_from_course(course_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            'DELETE FROM "Enrollment" WHERE "userId"=%s AND "courseId"=%s',
            (current_user.id, str(course_id)),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "No estabas inscrito")
        conn.commit()
        return {"message": "Desinscripto"}
    finally:
        cur.close(); conn.close()


@router.post("/lessons/{lesson_id}/complete")
def complete_lesson(lesson_id: uuid.UUID, current_user: UserInDB = Depends(get_current_active_user)):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute(
            '''
            SELECT e.id,e."courseId" FROM "Enrollment" e
            JOIN "Lesson" l ON l."courseId"=e."courseId"
            WHERE e."userId"=%s AND l.id=%s
            ''',
            (current_user.id, str(lesson_id)),
        )
        data = cur.fetchone()
        if not data:
            raise HTTPException(403, "No inscrito")

        enroll_id, course_id = data
        cur.execute(
            'INSERT INTO "LessonProgress"("enrollmentId","lessonId") '
            'VALUES(%s,%s) ON CONFLICT DO NOTHING',
            (enroll_id, str(lesson_id)),
        )

        cur.execute('SELECT COUNT(*) FROM "Lesson" WHERE "courseId"=%s', (course_id,))
        total = cur.fetchone()[0]
        cur.execute('SELECT COUNT(*) FROM "LessonProgress" WHERE "enrollmentId"=%s', (enroll_id,))
        done = cur.fetchone()[0]
        progress = int(done / total * 100) if total else 0

        cur.execute('UPDATE "Enrollment" SET progress=%s WHERE id=%s', (progress, enroll_id))
        conn.commit()
        return {"message": "Progreso actualizado", "newProgress": progress}
    finally:
        cur.close(); conn.close()
