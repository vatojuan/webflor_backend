############################################################
# app/routers/proposal.py
# ----------------------------------------------------------
# Envío de propuestas por e‑mail / WhatsApp + gestión estado
# Revisado y robustecido – mayo 2025
############################################################

from __future__ import annotations

import os
import time
import logging
import smtplib
from email.message import EmailMessage
from datetime import timedelta
from typing import Tuple, Optional, Set

import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from app.database import engine  # SQLAlchemy engine para conexiones crudas

load_dotenv()

# ────────────────────────────── Configuración ───────────────────────────────
SECRET_KEY  = os.getenv("SECRET_KEY", "")
ALGORITHM   = os.getenv("ALGORITHM", "HS256")
AUTO_DELAY  = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))  # 5 min por defecto

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

router        = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# ───────────────────────────── Auth & DB helpers ────────────────────────────

def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    """Devuelve el `sub` del token JWT o lanza 401."""
    if not token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token inválido")


def db() -> psycopg2.extensions.connection:
    """Conexión cruda (autocommit = False) a la BD configurada en SQLAlchemy."""
    conn = engine.raw_connection()
    conn.autocommit = False
    return conn

# ───────────────────────────── SMTP helpers ────────────────────────────────

def _smtp_cfg() -> Tuple[str, int, str, str]:
    """Lee configuración SMTP de variables de entorno."""
    return (
        os.getenv("SMTP_SERVER", ""),
        int(os.getenv("SMTP_PORT", "587")),
        os.getenv("SMTP_USER", ""),
        os.getenv("SMTP_PASS", ""),
    )


def send_mail(dest: str, subj: str, body: str, cv: Optional[str] = None) -> None:
    """Envío de correo resiliente.

    • Valida config / destino.
    • Soporta TLS (STARTTLS) o SSL implícito según puerto.
    • Lanza excepción para que el caller marque error_email si algo falla.
    """
    host, port, user, pwd = _smtp_cfg()

    if not all([host, port, user, pwd]):
        raise RuntimeError("Configuración SMTP incompleta – revise variables de entorno")
    if not dest:
        raise ValueError("Destino de e‑mail vacío")

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, dest, subj
    msg.set_content(body + (f"\n\nCV: {cv}" if cv else ""))

    logger.info(f"📤 Enviando mail → {dest} ({host}:{port})")

    try:
        if port == 465:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            smtp = smtplib.SMTP(host, port, timeout=20)
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()

        smtp.login(user, pwd)
        smtp.send_message(msg)
        smtp.quit()
        logger.info("✉️ Mail enviado correctamente")

    except Exception as exc:
        logger.exception(f"send_mail error: {exc}")
        raise


def send_whatsapp(phone: Optional[str], txt: str) -> None:
    if phone:
        logger.info(f"📲 WhatsApp → {phone}: {txt}")

# ───────────────────────────── Utilidades tabla Job ─────────────────────────

def job_columns(cur) -> Set[str]:
    cur.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema='public' AND table_name='Job'
        """
    )
    return {c[0] for c in cur.fetchall()}

# ─────────────────────────── Lógica de entrega (e‑mail) ─────────────────────

def deliver(pid: int, sleep_first: bool) -> None:
    """Entrega (automática o manual) de la propuesta *pid*."""
    if sleep_first:
        logger.info(f"⏳ task {pid}: sleep {timedelta(seconds=AUTO_DELAY)}")
        time.sleep(AUTO_DELAY)

    conn = cur = None
    try:
        conn = db(); cur = conn.cursor()

        # 1) Obtenemos estado actual
        cur.execute("SELECT status, job_id, applicant_id FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            logger.warning(f"Propuesta {pid} no existe"); return
        status, job_id, applicant_id = row
        if sleep_first and status != "waiting":
            logger.info(f"Propuesta {pid} dejó waiting ({status})"); return
        if (not sleep_first) and status != "pending":
            raise HTTPException(400, "Solo proposals en pending")

        # 2) Datos de la oferta (snake & camel)
        cur.execute('SELECT * FROM "Job" WHERE id=%s', (job_id,))
        jrow = cur.fetchone()
        if not jrow:
            logger.error(f"Job {job_id} no hallado"); return
        jcols = [d[0] for d in cur.description]
        job   = dict(zip(jcols, jrow))
        title         = job.get("title")
        source        = job.get("source")
        owner_id      = job.get("user_id") or job.get("userId")
        contact_email = job.get("contact_email") or job.get("contactEmail")
        contact_phone = job.get("contact_phone") or job.get("contactPhone")

        # 3) Datos del postulante
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id=%s', (applicant_id,))
        a_name, a_mail, cv_url = cur.fetchone()

        # 4) Determinar destino
        if source == "admin":
            dest_mail, dest_phone = contact_email, contact_phone
        else:
            cur.execute('SELECT email, phone FROM "User" WHERE id=%s', (owner_id,))
            dest_mail, dest_phone = cur.fetchone()

        # 5) Validación de e‑mail destino
        if not dest_mail:
            logger.warning("❗ Sin e‑mail destino → error_email")
            cur.execute(
                """
                UPDATE proposals
                   SET status='error_email',
                       cancelled_at = NOW(),
                       notes = 'Sin e‑mail de contacto'
                 WHERE id=%s
                """, (pid,))
            conn.commit(); return

        # 6) Envío correo
        subj = f"Nueva propuesta – {title}"
        body = f"Hola,\n\n{a_name} se postuló a «{title}».\nMail candidato: {a_mail}\n"
        try:
            send_mail(dest_mail, subj, body, cv_url)
        except Exception:
            cur.execute(
                """
                UPDATE proposals
                   SET status='error_email',
                       cancelled_at = NOW(),
                       notes = 'Fallo en el envío de e‑mail'
                 WHERE id=%s
                """, (pid,))
            conn.commit(); return

        # 7) WhatsApp opcional
        send_whatsapp(dest_phone, f"Nueva propuesta para «{title}».")

        # 8) Marcar enviada
        cur.execute("UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s", (pid,))
        conn.commit()
        logger.info(f"✅ propuesta {pid} → sent")

    except Exception:
        if conn: conn.rollback()
        logger.exception("deliver error")
    finally:
        if cur:  cur.close()
        if conn: conn.close()

# ───────────────────────────── End‑points principales ───────────────────────

@router.post("/create")
def create(data: dict, bg: BackgroundTasks):
    job_id, applicant_id = data.get("job_id"), data.get("applicant_id")
    label                = data.get("label", "automatic")
    if not (job_id and applicant_id):
        raise HTTPException(400, "Faltan campos")

    conn = cur = None
    try:
        conn = db(); cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT %s,%s,%s,%s,NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals WHERE job_id=%s AND applicant_id=%s
            )
            RETURNING id
            """,
            (job_id, applicant_id, label,
             "waiting" if label == "automatic" else "pending",
             job_id, applicant_id)
        )
        row = cur.fetchone()
        if not row:
            conn.commit();
            return {"message": "Ya existe una propuesta"}
        pid = row[0]
        conn.commit()
        logger.info(f"🆕 propuesta {pid} creada ({label})")

        if label == "automatic":
            bg.add_task(deliver, pid, True)
        return {"proposal_id": pid}
    except Exception:
        if conn: conn.rollback()
        logger.exception("create error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()


@router.post("/cancel")
def cancel(data: dict):
    pid = data.get("proposal_id")
    if not pid:
        raise HTTPException(400, "proposal_id requerido")

    conn = cur = None
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "No existe propuesta")
        if row[0] not in ("waiting", "pending"):
            raise HTTPException(400, "Estado no cancelable")

        cur.execute("UPDATE proposals SET status='cancelled', cancelled_at=NOW() WHERE id=%s", (pid,))
        conn.commit()
        logger.info(f"🚫 propuesta {pid} cancelada")
        return {"message": "cancelada"}
    except HTTPException:
        raise
    except Exception:
        if conn: conn.rollback()
        logger.exception("cancel error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()


@router.patch("/{pid}/send", dependencies=[Depends(get_current_admin)])
def send_manual(pid: int):
    deliver(pid, False)
    return {"message": "enviada"}


@router.delete("/{pid}", dependencies=[Depends(get_current_admin)])
def delete_cancelled(pid: int):
    conn = cur = None
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row: raise HTTPException(404, "No existe")
        if row[0] != "cancelled":
            raise HTTPException(400, "Solo canceladas")

        cur.execute("DELETE FROM proposals WHERE id=%s", (pid,))
        conn.commit()
        logger.info(f"🗑️ propuesta {pid} eliminada")
        return {"message": "eliminada"}
    except HTTPException:
        raise
    except Exception:
        if conn: conn.rollback()
        logger.exception("delete error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()


@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn = cur = None
    try:
        conn = db(); cur = conn.cursor()

        cols = job_columns(cur)
        email_col = "contact_email" if "contact_email" in cols else ("\"contactEmail\"" if "contactEmail" in cols else None)
        phone_col = "contact_phone" if "contact_phone" in cols else ("\"contactPhone\"" if "contactPhone" in cols else None)
        email_expr = f"COALESCE(j.{email_col}) AS job_contact_email" if email_col else "NULL AS job_contact_email"
        phone_expr = f"COALESCE(j.{phone_col}) AS job_contact_phone" if phone_col else "NULL AS job_contact_phone"

        cur.execute(f"""
            SELECT
              p.id, p.label, p.status,
              p.created_at  AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS created_at,
              p.sent_at     AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS sent_at,
              p.cancelled_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS cancelled_at,
              p.notes,
              j.title  AS job_title,
              j.source AS proposal_source,
              {email_expr},
              {phone_expr},
              u.name   AS applicant_name,
              u.email  AS applicant_email
            FROM proposals p
            JOIN "Job"  j ON p.job_id      = j.id
            JOIN "User" u ON p.applicant_id = u.id
            ORDER BY p.created_at DESC
        """)
        col_names = [d[0] for d in cur.description]
        return {"proposals": [dict(zip(col_names, r)) for r in cur.fetchall()]}

    except Exception:
        logger.exception("list error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()
