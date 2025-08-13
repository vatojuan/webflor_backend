# app/routers/proposal.py
############################################################
# Gestión de propuestas (postulaciones) y su ciclo de vida.
# Versión robustecida contra diferencias de esquema – 13-ago-2025
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

# ─────────────────── Helpers tolerantes de esquema ───────────────────

def _fetch_job(cur, job_id: int) -> dict | None:
    """Obtiene *todas* las columnas de Job y resuelve claves snake/camel en Python.
    Evita hacer referencia a columnas inexistentes en SQL (previene errores como contactPhone)."""
    cur.execute('SELECT * FROM "Job" WHERE id = %s', (job_id,))
    row = cur.fetchone()
    return row


def _job_contact_email(job: dict, employer_email: str = "") -> str:
    return (job.get("contact_email")
            or job.get("contactEmail")
            or employer_email
            or "")


def _job_contact_phone(job: dict, employer_phone: str = "") -> str:
    return (job.get("contact_phone")
            or job.get("contactPhone")
            or employer_phone
            or "")


def _fetch_user(cur, user_id: int) -> dict | None:
    cur.execute('SELECT * FROM "User" WHERE id = %s', (user_id,))
    return cur.fetchone()


# ─────────────────── Lógica Principal de Envío (Deliver) ───────────────────
def deliver(proposal_id: int, sleep_first: bool) -> None:
    """
    Procesa y envía una única propuesta al empleador.
    Toda la lógica de email se delega a email_utils.
    Tolerante a esquemas snake_case y camelCase en Job/User.
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

        # 2) Info de la oferta (SIN referenciar columnas inexistentes)
        job_info = _fetch_job(cur, job_id)
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

        job_title = job_info.get('title') or ''
        job_label = job_info.get('label') or 'manual'
        owner_id  = job_info.get('user_id') or job_info.get('userId')

        # 3) Info del postulante (tolerante al esquema)
        applicant_info = _fetch_user(cur, applicant_id)
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

        applicant_name  = applicant_info.get('name') or ''
        applicant_email = applicant_info.get('email') or ''
        cv_url          = applicant_info.get('cv_url') or applicant_info.get('cvUrl') or ''

        # 4) Info del empleador dueño de la oferta
        employer_name = employer_email = employer_phone = ""
        if owner_id:
            employer_data = _fetch_user(cur, owner_id)
            if employer_data:
                employer_name  = employer_data.get('name')  or ""
                employer_email = employer_data.get('email') or ""
                employer_phone = employer_data.get('phone') or ""

        # Email/teléfono final de contacto (con fallback a owner)
        final_contact_email = _job_contact_email(job_info, employer_email)
        final_contact_phone = _job_contact_phone(job_info, employer_phone)

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
                details=(
                    f"La propuesta ID {proposal_id} para la oferta '{job_title}' (ID {job_id}) "
                    f"no pudo enviarse por falta de email de contacto."
                )
            )
            return

        # 6) Construir contexto y enviar email
        context = {
            "applicant_name":  applicant_name,
            "applicant_email": applicant_email,
            "job_title":       job_title,
            "employer_name":   employer_name,
            "cv_url":          cv_url,
        }

        send_proposal_to_employer(final_contact_email, context)

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

        # Determinar etiqueta para estado inicial (no referenciamos columnas inexistentes)
        job_info = _fetch_job(cur, job_id)
        label = (job_info.get('label') if job_info else None) or "manual"

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

        if job_info:
            job_title = job_info.get('title') or ''
        else:
            job_title = ''
        applicant_info = _fetch_user(cur, applicant_id)

        if applicant_info and applicant_info.get('email'):
            context = {"applicant_name": applicant_info.get('name') or '', "job_title": job_title}
            bg.add_task(send_cancellation_warning, applicant_info['email'], context)

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

        # Traemos datos básicos y dejamos el cálculo de contacto en Python para evitar columnas inexistentes.
        cur.execute("""
            SELECT
              p.id, p.label, p.status, p.notes,
              p.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS created_at,
              p.sent_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS sent_at,
              p.cancelled_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS cancelled_at,
              j.*,                       -- ← todas las columnas de Job
              u.id   AS applicant_id,
              u.name AS applicant_name,
              u.email AS applicant_email,
              emp.email AS employer_email,
              emp.phone AS employer_phone
            FROM proposals p
            JOIN "Job" j ON p.job_id = j.id
            JOIN "User" u ON p.applicant_id = u.id
            LEFT JOIN "User" emp ON (j."userId" = emp.id OR j.user_id = emp.id)
            ORDER BY p.created_at DESC
        """)
        rows = cur.fetchall() or []

        # Normalizamos el contacto en Python (sin romper si falta contactEmail/contact_email)
        result = []
        for r in rows:
            job_contact_email = _job_contact_email(r, r.get('employer_email') or '')
            job_contact_phone = _job_contact_phone(r, r.get('employer_phone') or '')
            result.append({
                'id': r['id'],
                'label': r.get('label'),
                'status': r.get('status'),
                'notes': r.get('notes'),
                'created_at': r.get('created_at'),
                'sent_at': r.get('sent_at'),
                'cancelled_at': r.get('cancelled_at'),
                'job_id': r.get('id_1') if 'id_1' in r else r.get('job_id') or r.get('id'),  # compat para aliasing
                'job_title': r.get('title'),
                'applicant_id': r.get('applicant_id'),
                'applicant_name': r.get('applicant_name'),
                'applicant_email': r.get('applicant_email'),
                'job_contact_email': job_contact_email,
                'job_contact_phone': job_contact_phone,
            })
        return {"proposals": result}
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
