# app/routers/proposal.py

import os
import time
import logging
from datetime import datetime

from dotenv import load_dotenv
from fastapi import (
    APIRouter, HTTPException, Depends,
    BackgroundTasks, Request, status
)
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
import psycopg2

# --------------------------------------------------
# Carga de .env y configuración
# --------------------------------------------------
load_dotenv()
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"
)
ALGORITHM = os.getenv("ALGORITHM", "HS256")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Router y security
router = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")


def get_current_admin(token: str = Depends(oauth2)):
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if not payload.get("sub"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o expirado")
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o expirado")
    return payload["sub"]


def get_db_connection():
    try:
        return psycopg2.connect(
            dbname=os.getenv("DBNAME"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST"),
            port=int(os.getenv("DB_PORT", 5432)),
            sslmode="require"
        )
    except Exception as e:
        logger.error(f"DB connection error: {e}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error en la conexión a la BD")


def send_proposal_email(employer_email: str, subject: str, body: str, attachment_url: str = None) -> bool:
    # ... tu implementación de email aquí (sin cambios) ...
    return True


def send_whatsapp_message(phone: str, message: str) -> bool:
    # ... tu implementación de WhatsApp aquí (sin cambios) ...
    return True


def process_auto_proposal(proposal_id: int):
    """Tarea fondo para propuestas automáticas."""
    logger.info(f"Procesando propuesta automática {proposal_id}")
    time.sleep(300)
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id = %s", (proposal_id,))
        row = cur.fetchone()
        if not row or row[0] != "waiting":
            return
        _, job_id, applicant_id = row

        # Datos del job y usuarios
        cur.execute('SELECT title, "userId" FROM public."Job" WHERE id = %s', (job_id,))
        job_title, employer_id = cur.fetchone()
        cur.execute('SELECT name, email, "cvUrl" FROM public."User" WHERE id = %s', (applicant_id,))
        applicant_name, applicant_email, cv_url = cur.fetchone()
        cur.execute('SELECT name, email, phone FROM public."User" WHERE id = %s', (employer_id,))
        employer_name, employer_email, employer_phone = cur.fetchone()

        subject = f"Nueva propuesta para tu oferta: {job_title}"
        body = (
            f"Hola {employer_name},\n\n"
            f"El postulante {applicant_name} ha aplicado a tu oferta «{job_title}».\n"
            f"Contactalo en: {applicant_email}\n"
            f"CV: {cv_url}\n\n"
            "Saludos,\nEquipo FAP Mendoza"
        )
        send_proposal_email(employer_email, subject, body, attachment_url=cv_url)
        if employer_phone:
            send_whatsapp_message(employer_phone, f"Tienes nueva propuesta para «{job_title}».")

        cur.execute("UPDATE proposals SET status = 'sent', sent_at = NOW() WHERE id = %s", (proposal_id,))
        conn.commit()
        logger.info(f"Propuesta {proposal_id} marcada como sent")
    except Exception as e:
        logger.error(f"Error en process_auto_proposal: {e}")
    finally:
        cur.close()
        conn.close()


@router.post("/create", status_code=201)
def create_proposal(payload: dict, background_tasks: BackgroundTasks):
    """Crea propuesta manual o automática (sin duplicados)."""
    job_id = payload.get("job_id")
    applicant_id = payload.get("applicant_id")
    label = payload.get("label")
    if not job_id or not applicant_id or label not in ("automatic", "manual"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Faltan campos obligatorios o label inválido")

    status_ = "waiting" if label == "automatic" else "pending"
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO proposals (job_id, applicant_id, label, status)
            SELECT %s, %s, %s, %s
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals
               WHERE job_id=%s AND applicant_id=%s
            )
            RETURNING id
        """, (job_id, applicant_id, label, status_, job_id, applicant_id))
        row = cur.fetchone()
        conn.commit()
        if not row:
            return {"message": "Ya existe una propuesta para este usuario y oferta"}
        pid = row[0]
        if label == "automatic":
            background_tasks.add_task(process_auto_proposal, pid)
        return {"message": "Propuesta creada", "proposal_id": pid}
    except Exception:
        conn.rollback()
        logger.error("Error al crear propuesta", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno")
    finally:
        cur.close()
        conn.close()


@router.patch("/{proposal_id}/send", dependencies=[Depends(get_current_admin)])
def send_manual_proposal(proposal_id: int):
    """Envía inmediatamente una propuesta en status 'pending'."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id=%s", (proposal_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Propuesta no encontrada")
        status_, job_id, applicant_id = row
        if status_ != "pending":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Estado no es 'pending'")

        # Reutiliza lógica de envío
        process_auto_proposal(proposal_id)
        return {"message": "Propuesta enviada correctamente"}
    except HTTPException:
        raise
    except Exception:
        logger.error("Error al enviar manual", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno")
    finally:
        cur.close()
        conn.close()


@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    """Lista todas las propuestas (admin)."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT
              p.id,
              p.label,
              p.status,
              p.created_at,
              p.sent_at,
              p.notes,
              j.id       AS job_id,
              j.title    AS job_title,
              j.label    AS job_label,
              j.source   AS proposal_source,
              p.applicant_id,
              ua.name    AS applicant_name,
              ua.email   AS applicant_email
            FROM proposals p
            JOIN public."Job" j  ON p.job_id      = j.id
            JOIN public."User" ua ON p.applicant_id = ua.id
            ORDER BY p.created_at DESC
        """)
        cols = [d[0] for d in cur.description]
        data = [dict(zip(cols, row)) for row in cur.fetchall()]
        return {"proposals": data}
    except Exception:
        logger.error("Error al listar propuestas", exc_info=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error interno")
    finally:
        cur.close()
        conn.close()
