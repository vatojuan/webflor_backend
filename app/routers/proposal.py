# app/routers/proposal.py
############################################################
# Gestión de propuestas (postulaciones) y su ciclo de vida.
# Versión robustecida contra diferencias de esquema - 09-ago-2025
############################################################

from __future__ import annotations

import os
import time
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
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
        sub = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
        if not sub:
            raise ValueError("Token inválido")
        return sub
    except (JWTError, ValueError):
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
    Soporta esquemas camelCase y snake_case en Job/User.
    """
    if sleep_first:
        logger.info(f"⏳ Esperando {AUTO_DELAY}s para procesar propuesta {proposal_id}")
        time.sleep(AUTO_DELAY)

    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # 1) Validar estado de la propuesta
        cur.execute(
            "SELECT status, job_id, applicant_id FROM proposals WHERE id = %s",
            (proposal_id,)
        )
        proposal_data = cur.fetchone()
        if not proposal_data:
            logger.warning(f"Propuesta {proposal_id} no encontrada al intentar enviarla.")
            return
        
        status = proposal_data['status']
        job_id = proposal_data['job_id']
        applicant_id = proposal_data['applicant_id']
        
        if (sleep_first and status != "waiting") or (not sleep_first and status != "sending"):
            logger.info(f"Envío de propuesta {proposal_id} omitido. Estado actual: {status}")
            return

        # 2) Info de la oferta (tolerante al esquema)
        cur.execute(
            '''
            SELECT
                title,
                label,
                contact_email,
                COALESCE("contactPhone", contact_phone) AS contact_phone,
                COALESCE("userId", user_id)           AS owner_id
            FROM "Job"
            WHERE id = %s
            ''',
            (job_id,)
        )
        job_info = cur.fetchone()
        if not job_info:
            note = f"Oferta {job_id} no encontrada."
            cur2 = conn.cursor()
            cur2.execute("UPDATE proposals SET status='error_send', notes=%s WHERE id=%s", (note, proposal_id))
            conn.commit()
            logger.error(f"❗ Propuesta {proposal_id} falló: {note}")
            send_admin_alert(
                subject="Fallo en envío de Propuesta (Job inexistente)",
                details=f"La propuesta ID {proposal_id} no encontró la oferta ID {job_id}."
            )
            return

        job_title      = job_info['title']
        job_label      = job_info['label']
        contact_email  = job_info['contact_email']
        contact_phone  = job_info['contact_phone']
        owner_id       = job_info['owner_id']

        # 3) Info del postulante (tolerante al esquema)
        cur.execute(
            '''
            SELECT
                name,
                email,
                COALESCE("cvUrl", cv_url) AS cv_url
            FROM "User"
            WHERE id = %s
            ''',
            (applicant_id,)
        )
        applicant_info = cur.fetchone()
        if not applicant_info:
            note = f"Postulante {applicant_id} no encontrado."
            cur2 = conn.cursor()
            cur2.execute("UPDATE proposals SET status='error_send', notes=%s WHERE id=%s", (note, proposal_id))
            conn.commit()
            logger.error(f"❗ Propuesta {proposal_id} falló: {note}")
            send_admin_alert(
                subject="Fallo en envío de Propuesta (Usuario inexistente)",
                details=f"La propuesta ID {proposal_id} no encontró al usuario ID {applicant_id}."
            )
            return

        applicant_name  = applicant_info['name']
        applicant_email = applicant_info['email']
        cv_url          = applicant_info['cv_url']

        # 4) Info del empleador dueño de la oferta
        employer_name = employer_email = employer_phone = ""
        if owner_id:
            cur.execute(
                'SELECT name, email, phone FROM "User" WHERE id = %s',
                (owner_id,)
            )
            employer_data = cur.fetchone()
            if employer_data:
                employer_name  = employer_data.get('name')  or ""
                employer_email = employer_data.get('email') or ""
                employer_phone = employer_data.get('phone') or ""

        final_contact_email = contact_email or employer_email

        # 5) Validar email de destino
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
                details=f"La propuesta ID {proposal_id} para la oferta '{job_title}' (ID {job_id}) no pudo enviarse por falta de email de contacto."
            )
            return

        # 6) Construir contexto y enviar email
        context = {
            "applicant_name":  applicant_name,
            "applicant_email": applicant_email,
            "job_title":       job_title,
            "employer_name":   employer_name,
            "cv_url":          cv_url or "",
        }
        
        send_proposal_to_employer(final_contact_email, context)
        
        final_contact_phone = contact_phone or employer_phone
        if final_contact_phone:
            logger.info(f"📲 (Simulado) WhatsApp a {final_contact_phone}: Nueva propuesta para «{job_title}».")
        else:
            logger.info("No hay teléfono de contacto para WhatsApp (ok).")

        # 7) Marcar como enviada
        cur.execute("UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s", (proposal_id,))
        conn.commit()
        logger.info(f"✅ Propuesta {proposal_id} enviada exitosamente a {final_contact_email}.")

    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception(f"Error crítico al procesar la propuesta {proposal_id}: {e}")
        
        # Intento de marcar error en la propuesta
        try:
            conn_err = db(); cur_err = conn_err.cursor()
            cur_err.execute(
                "UPDATE proposals SET status='error_send', notes=%s WHERE id=%s",
                (str(e)[:250], proposal_id)
            )
            conn_err.commit()
        except Exception as db_err:
            logger.error(f"Fallo al marcar la propuesta {proposal_id} como errónea: {db_err}")
        finally:
            try:
                cur_err.close()
            except Exception:
                pass
            try:
                conn_err.close()
            except Exception:
                pass

        send_admin_alert(
            subject="Fallo Crítico en Envío de Propuesta",
            details=f"La función deliver() falló para la propuesta ID {proposal_id}.\nError: {e}"
        )
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

# ───────────────────────── Endpoints de la API ─────────────────────────────

@router.post("/create", dependencies=[Depends(get_current_admin)])
def create(data: dict, bg: BackgroundTasks):
    job_id = data.get("job_id")
    applicant_id = data.get("applicant_id")
    if not job_id or not applicant_id:
        raise HTTPException(status_code=400, detail="Faltan campos requeridos: job_id, applicant_id")

    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Evitar duplicados. Si existió cancelada, se elimina para permitir repostular.
        cur.execute("SELECT id, status FROM proposals WHERE job_id = %s AND applicant_id = %s", (job_id, applicant_id))
        existing = cur.fetchone()
        if existing:
            if existing['status'] == "cancelled":
                cur.execute("DELETE FROM proposals WHERE id = %s", (existing['id'],))
                logger.info(f"🗑️ Propuesta cancelada previa {existing['id']} eliminada.")
            else:
                raise HTTPException(status_code=409, detail="Ya has postulado a este empleo.")

        # Determinar etiqueta para estado inicial
        cur.execute('SELECT label FROM "Job" WHERE id = %s', (job_id,))
        label = (cur.fetchone() or {"label": "manual"})['label'] or "manual"
        
        status = "waiting" if label == "automatic" else "pending"
        cur.execute(
            "INSERT INTO proposals (job_id, applicant_id, label, status, created_at) VALUES (%s, %s, %s, %s, NOW()) RETURNING id",
            (job_id, applicant_id, label, status)
        )
        proposal_id = cur.fetchone()['id']
        conn.commit()
        logger.info(f"🆕 Propuesta {proposal_id} creada con estado '{status}' (label={label}).")

        # Envío automático (con demora) o advertencia de cancelación
        if label == "automatic":
            bg.add_task(deliver, proposal_id, True)

        cur.execute('SELECT title FROM "Job" WHERE id = %s', (job_id,))
        job_title = cur.fetchone()['title']
        cur.execute('SELECT name, email FROM "User" WHERE id = %s', (applicant_id,))
        user_info = cur.fetchone()
        
        if user_info and user_info['email']:
            context = {"applicant_name": user_info['name'], "job_title": job_title}
            bg.add_task(send_cancellation_warning, user_info['email'], context)

        return {"proposal_id": proposal_id}

    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("Error al crear la propuesta.")
        raise HTTPException(status_code=500, detail="Error interno al crear la propuesta.")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

@router.patch("/{proposal_id}/send", dependencies=[Depends(get_current_admin)])
def send_manual(proposal_id: int, bg: BackgroundTasks):
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
        logger.info(f"📨 Propuesta {proposal_id} marcada para envío manual inmediato.")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
    
    bg.add_task(deliver, proposal_id, sleep_first=False)
    return {"message": "Propuesta encolada para envío inmediato."}

@router.post("/cancel", dependencies=[Depends(get_current_admin)])
def cancel(data: dict):
    proposal_id = data.get("proposal_id")
    if not proposal_id:
        raise HTTPException(status_code=400, detail="proposal_id requerido")

    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT status FROM proposals WHERE id = %s FOR UPDATE", (proposal_id,))
        st = (cur.fetchone() or {}).get('status')
        if st is None:
            raise HTTPException(status_code=404, detail="La propuesta no existe.")
        if st not in ("waiting", "pending"):
            raise HTTPException(status_code=400, detail=f"No se puede cancelar una propuesta en estado '{st}'.")
        
        cur.execute("UPDATE proposals SET status='cancelled', cancelled_at=NOW() WHERE id=%s", (proposal_id,))
        conn.commit()
        logger.info(f"🚫 Propuesta {proposal_id} cancelada por el usuario.")
        return {"message": "Postulación cancelada exitosamente."}
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

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
        logger.info(f"🗑️ Propuesta cancelada {pid} eliminada por un admin.")
        return {"message": "Propuesta eliminada."}
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

@router.get("/", dependencies=[Depends(get_current_admin)], summary="Listar todas las propuestas")
def list_proposals():
    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # Corrección: j.contact_email (snake_case) + fallback al email del owner
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
              COALESCE(j.contact_email, emp.email) AS job_contact_email
            FROM proposals p
            JOIN "Job" j ON p.job_id = j.id
            JOIN "User" u ON p.applicant_id = u.id
            LEFT JOIN "User" emp ON j."userId" = emp.id
            ORDER BY p.created_at DESC
        """)
        proposals = cur.fetchall()
        return {"proposals": proposals}
    except Exception as e:
        logger.exception("Error al listar las propuestas.")
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {e}")
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
