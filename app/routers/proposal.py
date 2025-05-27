############################################################
# app/routers/proposal.py
# ----------------------------------------------------------
# Envío de propuestas por e-mail / WhatsApp + gestión estado
# Versión depurada – junio 2025
############################################################

from __future__ import annotations

import os
import time
import logging
import smtplib
import dns.resolver          # requiere python-dns
from email.message import EmailMessage
from datetime import timedelta
from typing import Tuple, Optional

import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from app.database import engine   # SQLAlchemy engine para raw_connection

load_dotenv()

# ───────────────────────── Configuración ──────────────────────────
SECRET_KEY   = os.getenv("SECRET_KEY", "")
ALGORITHM    = os.getenv("ALGORITHM", "HS256")
AUTO_DELAY   = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))  # segundos
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "20"))          # segundos

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

router        = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")


# ───────────────────────── Auth & DB helpers ──────────────────────
def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(status_code=401, detail="Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub") or ""
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido")


def db_connection() -> psycopg2.extensions.connection:
    """Conexión cruda (autocommit=False) usando SQLAlchemy engine.raw_connection()."""
    conn = engine.raw_connection()
    conn.autocommit = False
    return conn


# ───────────────────────── SMTP helpers ───────────────────────────
def _smtp_cfg() -> Tuple[str,int,str,str]:
    return (
        os.getenv("SMTP_SERVER", ""),
        int(os.getenv("SMTP_PORT", "587")),
        os.getenv("SMTP_USER", ""),
        os.getenv("SMTP_PASS", ""),
    )


def _check_mx(address: str) -> None:
    """Loguea advertencia si el dominio no tiene MX, pero no aborta."""
    domain = address.split("@")[-1]
    try:
        dns.resolver.resolve(domain, "MX")
    except Exception as e:
        logger.warning(f"⚠️ Dominio sin MX ({domain}): {e}")


def send_mail(dest: str, subj: str, body: str, cv: Optional[str] = None) -> None:
    """
    Envía correo (STARTTLS o SSL implícito).
    Lanza excepción en caso de fallo para marcar error_email.
    """
    host, port, user, pwd = _smtp_cfg()
    if not all([host, port, user, pwd]):
        raise RuntimeError("Configuración SMTP incompleta")
    if not dest:
        raise ValueError("Destino de e-mail vacío")

    _check_mx(dest)  # solo logging

    msg = EmailMessage()
    msg["From"]    = user
    msg["To"]      = dest
    msg["Subject"] = subj
    content = body + (f"\n\nCV: {cv}" if cv else "")
    msg.set_content(content)

    logger.info(f"📤 Conectando a SMTP {host}:{port} para enviar a {dest}…")
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT)
    else:
        smtp = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()

    smtp.login(user, pwd)
    smtp.send_message(msg)
    smtp.quit()
    logger.info("✉️ Mail enviado correctamente")


def send_whatsapp(phone: Optional[str], txt: str) -> None:
    if phone:
        logger.info(f"📲 WhatsApp → {phone}: {txt}")


# ─────────────────────────── Lógica de entrega ─────────────────────
def deliver(pid: int, sleep_first: bool) -> None:
    """
    • sleep_first=True → espera AUTO_DELAY y envía automáticamente.
    • sleep_first=False → envía inmediatamente (envío manual).
    """
    if sleep_first:
        logger.info(f"⏳ task {pid}: durmiendo {AUTO_DELAY}s antes de enviar")
        time.sleep(AUTO_DELAY)

    conn = cur = None
    try:
        conn = db_connection()
        cur = conn.cursor()

        # 1) Estado actual de la propuesta
        cur.execute(
            "SELECT status, job_id, applicant_id FROM proposals WHERE id=%s",
            (pid,)
        )
        row = cur.fetchone()
        if not row:
            logger.warning(f"Propuesta {pid} no existe")
            return
        status, job_id, applicant_id = row

        if sleep_first and status != "waiting":
            logger.info(f"Propuesta {pid} no está waiting (estado={status}), omito")
            return
        if not sleep_first and status != "pending":
            logger.error(f"Propuesta {pid} no está pending (estado={status})")
            raise HTTPException(status_code=400, detail="Solo proposals en pending")

        # 2) Carga datos de la oferta desde tabla jobs
        cur.execute(
            "SELECT id, title, source, \"userId\", contact_email, contact_phone "
            "FROM jobs WHERE id=%s",
            (job_id,)
        )
        jrow = cur.fetchone()
        if not jrow:
            logger.error(f"Job {job_id} no hallado")
            return
        job_id_, title, source, owner_id, contact_email, contact_phone = jrow
        logger.debug(f"Job carga: id={job_id_} title={title!r} source={source!r} "
                     f"userId={owner_id} contact_email={contact_email!r}")

        # 3) Carga datos del postulante
        cur.execute(
            'SELECT name, email, "cvUrl" FROM "User" WHERE id=%s',
            (applicant_id,)
        )
        cand = cur.fetchone()
        if not cand:
            logger.error(f"User {applicant_id} no hallado")
            return
        cand_name, cand_email, cand_cv = cand

        # 4) Determina destino: primero contact_email, si no existe fallback a owner.user
        dest_mail  = contact_email
        dest_phone = contact_phone
        if not dest_mail:
            cur.execute(
                'SELECT email, phone FROM "User" WHERE id=%s',
                (owner_id,)
            )
            owner = cur.fetchone()
            if owner:
                dest_mail, dest_phone = owner

        logger.debug(f"Destino final → email={dest_mail!r}, phone={dest_phone!r}")

        # 5) Validación e-mail
        if not dest_mail:
            msg = "Sin e-mail de contacto"
            cur.execute(
                "UPDATE proposals SET status='error_email', cancelled_at=NOW(), notes=%s WHERE id=%s",
                (msg, pid)
            )
            conn.commit()
            logger.warning(f"Propuesta {pid} marcada error_email: {msg}")
            return

        # 6) Intento envío e-mail
        subj = f"Nueva propuesta – {title}"
        body = (
            f"Hola,\n\n"
            f"{cand_name} se postuló a «{title}».\n"
            f"Mail candidato: {cand_email}\n"
        )
        try:
            send_mail(dest_mail, subj, body, cand_cv)
        except Exception as exc:
            err = str(exc)
            cur.execute(
                "UPDATE proposals SET status='error_email', cancelled_at=NOW(), notes=%s WHERE id=%s",
                (f"SMTP error: {err}", pid)
            )
            conn.commit()
            logger.error(f"SMTP error al enviar propuesta {pid}: {err}")
            return

        # 7) Envío WhatsApp (opcional)
        send_whatsapp(dest_phone, f"Nueva propuesta para «{title}».")

        # 8) Marca como enviada
        cur.execute(
            "UPDATE proposals SET status='sent', sent_at=NOW() WHERE id=%s",
            (pid,)
        )
        conn.commit()
        logger.info(f"✅ Propuesta {pid} enviada correctamente")

    except Exception:
        if conn:
            conn.rollback()
        logger.exception("deliver error")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ───────────────────────── End-points principales ────────────────────

@router.post("/create")
def create_proposal(data: dict, bg: BackgroundTasks):
    job_id       = data.get("job_id")
    applicant_id = data.get("applicant_id")
    label        = data.get("label", "automatic")
    if not (job_id and applicant_id):
        raise HTTPException(status_code=400, detail="Faltan campos obligatorios")

    conn = cur = None
    try:
        conn = db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT %s, %s, %s, %s, NOW()
            WHERE NOT EXISTS (
              SELECT 1 FROM proposals WHERE job_id=%s AND applicant_id=%s
            )
            RETURNING id
            """,
            (
                job_id,
                applicant_id,
                label,
                "waiting" if label == "automatic" else "pending",
                job_id,
                applicant_id,
            )
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return {"message": "Ya existe una propuesta"}
        pid = row[0]
        logger.info(f"🆕 Propuesta {pid} creada ({label})")
        if label == "automatic":
            bg.add_task(deliver, pid, True)
        return {"proposal_id": pid}

    except Exception:
        if conn:
            conn.rollback()
        logger.exception("create error")
        raise HTTPException(status_code=500, detail="Error interno")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.post("/cancel")
def cancel_proposal(data: dict):
    pid = data.get("proposal_id")
    if not pid:
        raise HTTPException(status_code=400, detail="proposal_id requerido")

    conn = cur = None
    try:
        conn = db_connection()
        cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No existe propuesta")
        if row[0] not in ("waiting", "pending"):
            raise HTTPException(status_code=400, detail="Estado no cancelable")

        cur.execute(
            "UPDATE proposals SET status='cancelled', cancelled_at=NOW() WHERE id=%s",
            (pid,)
        )
        conn.commit()
        logger.info(f"🚫 Propuesta {pid} cancelada")
        return {"message": "cancelada"}

    except HTTPException:
        raise
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("cancel error")
        raise HTTPException(status_code=500, detail="Error interno")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.patch("/{pid}/send", dependencies=[Depends(get_current_admin)])
def send_manual(pid: int):
    deliver(pid, False)
    return {"message": "enviada"}


@router.delete("/{pid}", dependencies=[Depends(get_current_admin)])
def delete_proposal(pid: int):
    conn = cur = None
    try:
        conn = db_connection()
        cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id=%s", (pid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No existe propuesta")
        if row[0] != "cancelled":
            raise HTTPException(status_code=400, detail="Solo canceladas pueden borrarse")

        cur.execute("DELETE FROM proposals WHERE id=%s", (pid,))
        conn.commit()
        logger.info(f"🗑️ Propuesta {pid} eliminada")
        return {"message": "eliminada"}

    except HTTPException:
        raise
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("delete error")
        raise HTTPException(status_code=500, detail="Error interno")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn = cur = None
    try:
        conn = db_connection()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
              p.id,
              p.label,
              p.status,
              p.created_at  AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS created_at,
              p.sent_at     AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS sent_at,
              p.cancelled_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS cancelled_at,
              p.notes,
              j.id    AS job_id,
              j.title AS job_title,
              j.source AS proposal_source,
              j.contact_email    AS job_contact_email,
              j.contact_phone    AS job_contact_phone,
              u.id    AS applicant_id,
              u.name  AS applicant_name,
              u.email AS applicant_email
            FROM proposals p
            JOIN jobs      j ON p.job_id      = j.id
            JOIN "User"    u ON p.applicant_id = u.id
            ORDER BY p.created_at DESC
            """
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        logger.debug(f"Fetched {len(rows)} proposals")
        return {"proposals": [dict(zip(cols, r)) for r in rows]}

    except Exception as exc:
        logger.exception(f"list_proposals error: {exc}")
        raise HTTPException(status_code=500, detail="Error interno")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
