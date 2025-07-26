############################################################
# app/routers/proposal.py
# ----------------------------------------------------------
# Gestión de propuestas (postulaciones) y su ciclo de vida.
# Versión refactorizada - 26-jul-2025
############################################################

from __future__ import annotations

import os
import time
import logging
from typing import Optional, Set, Dict

import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from app.database import engine

# Importaciones centralizadas para la comunicación
from app.email_utils import (
    send_proposal_to_employer,
    send_cancellation_warning,
    send_admin_alert,
)

load_dotenv()

# ───────────────────── Configuración Global ──────────────────────
SECRET_KEY: str = os.getenv("SECRET_KEY", "")
ALGORITHM: str = os.getenv("ALGORITHM", "HS256")
AUTO_DELAY: int = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))  # segundos

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# ───────────────────────────  Auth y DB  ─────────────────────────
def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o requerido")

def db() -> psycopg2.extensions.connection:
    conn = engine.raw_connection()
    conn.autocommit = False
    return conn

# ─────────────────── Lógica Principal de Envío (Deliver) ───────────────────
def deliver(proposal_id: int, sleep_first: bool) -> None:
    """
    Procesa y envía una única propuesta al empleador.
    Toda la lógica de email se delega a email_utils.
    """
    if sleep_first:
        logger.info(f"⏳ Esperando {AUTO_DELAY}s para procesar propuesta {proposal_id}")
        time.sleep(AUTO_DELAY)

    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor()

        # 1. Validar estado de la propuesta
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id = %s", (proposal_id,))
        proposal_data = cur.fetchone()
        if not proposal_data:
            logger.warning(f"Propuesta {proposal_id} no encontrada al intentar enviarla.")
            return
        
        status, job_id, applicant_id = proposal_data
        
        if (sleep_first and status != "waiting") or (not sleep_first and status != "sending"):
            logger.info(f"Envío de propuesta {proposal_id} omitido. Estado actual: {status}")
            return

        # 2. Recolectar toda la información necesaria
        cur.execute('SELECT title, label, "contactEmail", "contactPhone", "userId" FROM "Job" WHERE id = %s', (job_id,))
        job_title, job_label, contact_email, contact_phone, owner_id = cur.fetchone()

        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id = %s', (applicant_id,))
        applicant_name, applicant_email, cv_url = cur.fetchone()

        cur.execute('SELECT name, email, phone FROM "User" WHERE id = %s', (owner_id,))
        employer_data = cur.fetchone() or ("", "", "")
        employer_name, employer_email, employer_phone = employer_data

        # Priorizar email de contacto del Job, si no, usar el del dueño del Job
        final_contact_email = contact_email or employer_email

        # 3. Validar que exista un email de destino
        if not final_contact_email:
            error_note = "Sin email de contacto del empleador."
            cur.execute(
                "UPDATE proposals SET status='error_email', notes=%s, cancelled_at=NOW() WHERE id=%s",
                (error_note, proposal_id)
            )
            conn.commit()
            logger.error(f"❗ Propuesta {proposal_id} falló: {error_note}")
            send_admin_alert(
                subject="Fallo en envío de Propuesta (Sin Email)",
                details=f"La propuesta ID {proposal_id} para la oferta '{job_title}' (ID {job_id}) no pudo ser enviada porque no se encontró un email de contacto para el empleador."
            )
            return

        # 4. Construir contexto y enviar email
        context = {
            "applicant_name": applicant_name,
            "applicant_email": applicant_email,
            "job_title": job_title,
            "employer_name": employer_name,
            "cv_url": cv_url or "",
        }
        
        send_proposal_to_employer(final_contact_email, context)
        
        # (Opcional) Notificar por WhatsApp
        final_contact_phone = contact_phone or employer_phone
        if final_contact_phone:
            logger.info(f"📲 (Simulado) WhatsApp a {final_contact_phone}: Nueva propuesta para «{job_title}».")

        # 5. Actualizar estado a 'sent'
        cur.execute("UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s", (proposal_id,))
        conn.commit()
        logger.info(f"✅ Propuesta {proposal_id} enviada exitosamente a {final_contact_email}.")

    except Exception as e:
        if conn: conn.rollback()
        logger.exception(f"Error crítico al procesar la propuesta {proposal_id}: {e}")
        
        # Marcar la propuesta con error en la BD
        try:
            conn_err = db(); cur_err = conn_err.cursor()
            cur_err.execute("UPDATE proposals SET status='error_send', notes=%s WHERE id=%s", (str(e)[:250], proposal_id))
            conn_err.commit()
        except Exception as db_err:
            logger.error(f"Fallo al intentar marcar la propuesta {proposal_id} como errónea: {db_err}")
        finally:
            if 'cur_err' in locals() and cur_err: cur_err.close()
            if 'conn_err' in locals() and conn_err: conn_err.close()

        # Enviar alerta de administrador
        send_admin_alert(
            subject="Fallo Crítico en Envío de Propuesta",
            details=f"La función deliver() falló para la propuesta ID {proposal_id}.\nError: {e}"
        )
    finally:
        if cur: cur.close()
        if conn: conn.close()

# ───────────────────────── Endpoints de la API ─────────────────────────────

@router.post("/create")
def create(data: dict, bg: BackgroundTasks):
    """Crea una nueva propuesta, la agenda si es automática y envía aviso de cancelación."""
    job_id = data.get("job_id")
    applicant_id = data.get("applicant_id")
    if not job_id or not applicant_id:
        raise HTTPException(status_code=400, detail="Faltan campos requeridos: job_id, applicant_id")

    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor()

        # 1. Validar y manejar postulaciones existentes
        cur.execute("SELECT id, status FROM proposals WHERE job_id = %s AND applicant_id = %s", (job_id, applicant_id))
        existing = cur.fetchone()
        if existing:
            if existing[1] == "cancelled":
                cur.execute("DELETE FROM proposals WHERE id = %s", (existing[0],))
                logger.info(f"🗑️ Propuesta cancelada previa {existing[0]} eliminada para permitir nueva postulación.")
            else:
                raise HTTPException(status_code=409, detail="Ya has postulado a este empleo.")

        # 2. Obtener datos y crear la nueva propuesta
        cur.execute('SELECT label FROM "Job" WHERE id = %s', (job_id,))
        label = (cur.fetchone() or ["manual"])[0] or "manual"
        
        status = "waiting" if label == "automatic" else "pending"
        cur.execute(
            "INSERT INTO proposals (job_id, applicant_id, label, status, created_at) VALUES (%s, %s, %s, %s, NOW()) RETURNING id",
            (job_id, applicant_id, label, status)
        )
        proposal_id = cur.fetchone()[0]
        conn.commit()
        logger.info(f"🆕 Propuesta {proposal_id} creada con estado '{status}'.")

        # 3. Agendar entrega si es automática
        if label == "automatic":
            bg.add_task(deliver, proposal_id, True)

        # 4. Enviar aviso de cancelación al candidato
        cur.execute('SELECT title FROM "Job" WHERE id = %s', (job_id,))
        job_title = cur.fetchone()[0]
        cur.execute('SELECT name, email FROM "User" WHERE id = %s', (applicant_id,))
        applicant_name, applicant_email = cur.fetchone()
        
        if applicant_email:
            context = {"applicant_name": applicant_name, "job_title": job_title}
            bg.add_task(send_cancellation_warning, applicant_email, context)

        return {"proposal_id": proposal_id}

    except HTTPException:
        raise
    except Exception as e:
        if conn: conn.rollback()
        logger.exception("Error al crear la propuesta.")
        raise HTTPException(status_code=500, detail="Error interno al crear la propuesta.")
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.patch("/{proposal_id}/send", dependencies=[Depends(get_current_admin)])
def send_manual(proposal_id: int, bg: BackgroundTasks):
    """Fuerza el envío de una propuesta manual que está en estado 'pending'."""
    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE proposals SET status = 'sending' WHERE id = %s AND status = 'pending' RETURNING id",
            (proposal_id,)
        )
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Propuesta no encontrada o no está en estado 'pending'.")
        conn.commit()
        logger.info(f"Propuesta {proposal_id} marcada para envío manual inmediato.")
    finally:
        if cur: cur.close()
        if conn: conn.close()
    
    bg.add_task(deliver, proposal_id, sleep_first=False)
    return {"message": "Propuesta encolada para envío inmediato."}

@router.post("/cancel")
def cancel(data: dict):
    """Permite a un usuario cancelar su propia postulación si está a tiempo."""
    proposal_id = data.get("proposal_id")
    if not proposal_id:
        raise HTTPException(status_code=400, detail="proposal_id requerido")

    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id = %s FOR UPDATE", (proposal_id,))
        st = (cur.fetchone() or [None])[0]
        if st is None:
            raise HTTPException(status_code=404, detail="La propuesta no existe.")
        if st not in ("waiting", "pending"):
            raise HTTPException(status_code=400, detail=f"No se puede cancelar una propuesta en estado '{st}'.")
        
        cur.execute("UPDATE proposals SET status='cancelled', cancelled_at=NOW() WHERE id=%s", (proposal_id,))
        conn.commit()
        logger.info(f"🚫 Propuesta {proposal_id} cancelada por el usuario.")
        return {"message": "Postulación cancelada exitosamente."}
    finally:
        if cur: cur.close()
        if conn: conn.close()

# Se mantienen los endpoints de admin para DELETE y GET sin cambios significativos
@router.delete("/{pid}", dependencies=[Depends(get_current_admin)])
def delete_cancelled(pid: int):
    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("DELETE FROM proposals WHERE id = %s AND status = 'cancelled' RETURNING id", (pid,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Propuesta no encontrada o no está en estado 'cancelled'.")
        conn.commit()
        logger.info(f"🗑️ Propuesta cancelada {pid} eliminada permanentemente por un admin.")
        return {"message": "Propuesta eliminada."}
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT
              p.id, p.label, p.status, p.notes,
              p.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS created_at,
              p.sent_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS sent_at,
              p.cancelled_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS cancelled_at,
              j.id AS job_id,
              j.title AS job_title,
              u.id AS applicant_id,
              u.name AS applicant_name,
              u.email AS applicant_email,
              COALESCE(j."contactEmail", emp.email) AS job_contact_email
            FROM proposals p
            JOIN "Job" j ON p.job_id = j.id
            JOIN "User" u ON p.applicant_id = u.id
            LEFT JOIN "User" emp ON j."userId" = emp.id
            ORDER BY p.created_at DESC
        """)
        return {"proposals": cur.fetchall()}
    finally:
        if cur: cur.close()
        if conn: conn.close()
