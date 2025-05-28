############################################################
# app/routers/proposal.py
# ----------------------------------------------------------
# Envío de propuestas por e-mail / WhatsApp + gestión estado
# Versión final corregida – mayo 2025
############################################################

from __future__ import annotations

import os
import time
import logging
import smtplib
import dns.resolver
from email.message import EmailMessage
from datetime import datetime, timedelta
from typing import Tuple, Optional, Set, Dict

import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from app.database import engine  # SQLAlchemy engine

load_dotenv()

# ───────────────────────── Configuración ──────────────────────────
SECRET_KEY   = os.getenv("SECRET_KEY", "")
ALGORITHM    = os.getenv("ALGORITHM", "HS256")
AUTO_DELAY   = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "20"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s"
)
logger = logging.getLogger(__name__)

router        = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")


# ───────────────────────── Auth & DB helpers ──────────────────────
def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token inválido")


def db() -> psycopg2.extensions.connection:
    conn = engine.raw_connection()
    conn.autocommit = False
    return conn


# ───────────────────────── SMTP helpers ───────────────────────────
def _smtp_cfg() -> Tuple[str, int, str, str]:
    return (
        os.getenv("SMTP_HOST", ""),
        int(os.getenv("SMTP_PORT", "587")),
        os.getenv("SMTP_USER", ""),
        os.getenv("SMTP_PASS", ""),
    )


def _check_mx(address: str) -> None:
    domain = address.split("@")[-1]
    try:
        dns.resolver.resolve(domain, "MX")
    except Exception as e:
        logger.warning(f"⚠️  Dominio sin MX ({domain}): {e}")


def send_mail(dest: str, subj: str, body: str,
              cv: Optional[str] = None) -> None:
    host, port, user, pwd = _smtp_cfg()
    if not all([host, port, user, pwd]):
        raise RuntimeError("Variables SMTP* incompletas")
    if not dest:
        raise ValueError("Destino vacío")

    _check_mx(dest)

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, dest, subj
    msg.set_content(body + (f"\n\nCV: {cv}" if cv else ""))

    logger.info(f"📤  Conectando a SMTP {host}:{port} …")
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
    logger.info(f"✉️  Mail enviado correctamente a {dest}")


def send_whatsapp(phone: Optional[str], txt: str) -> None:
    if phone:
        logger.info(f"📲 WhatsApp → {phone}: {txt}")


# ───────────────────────── Utilidades tabla Job ───────────────────
def job_columns(cur) -> Set[str]:
    cur.execute("""
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema='public'
           AND table_name='Job';
    """)
    return {row[0] for row in cur.fetchall()}


def debug_dump_job(job_id: int, job: dict) -> None:
    logger.debug("Job %d dump → %s", job_id,
                 ", ".join(f"{k}={v!r}" for k, v in job.items()))


# ─────────────────── Reemplazo de placeholders ───────────────────
def apply_template(tpl: Dict[str, str], context: Dict[str, str]) -> Tuple[str, str]:
    """
    Dada una plantilla con 'subject' y 'body' y un contexto de claves->valores,
    reemplaza {{key}} por su valor.
    """
    subj = tpl.get("subject", "")
    body = tpl.get("body", "")
    for key, val in context.items():
        subj = subj.replace(f"{{{{{key}}}}}", val)
        body = body.replace(f"{{{{{key}}}}}", val)
    return subj, body


# ─────────────────────────── Lógica de entrega ─────────────────────
def deliver(pid: int, sleep_first: bool) -> None:
    if sleep_first:
        logger.info("⏳ task %d: sleep %s", pid, timedelta(seconds=AUTO_DELAY))
        time.sleep(AUTO_DELAY)

    conn = cur = None
    try:
        conn = db()
        cur  = conn.cursor()

        # 1) Estado actual
        cur.execute("SELECT status, job_id, applicant_id, created_at FROM proposals WHERE id = %s", (pid,))
        row = cur.fetchone()
        if not row:
            logger.warning("Propuesta %d no existe", pid)
            return
        status, job_id, applicant_id, created_at = row

        if sleep_first and status != "waiting":
            logger.info("Propuesta %d dejó 'waiting' (%s)", pid, status)
            return
        if not sleep_first and status != "pending":
            raise HTTPException(400, "Solo proposals en pending")

        # 2) Cargar Job
        cur.execute('SELECT * FROM "Job" WHERE id = %s', (job_id,))
        jrow = cur.fetchone()
        if not jrow:
            logger.error("Job %d no hallado", job_id)
            return
        job = dict(zip([d[0] for d in cur.description], jrow))
        debug_dump_job(job_id, job)

        title         = job.get("title")
        source        = job.get("source")
        owner_id      = job.get("user_id") or job.get("userId")
        contact_email = job.get("contact_email") or job.get("contactEmail")
        contact_phone = job.get("contact_phone") or job.get("contactPhone")

        # 3) Cargar postulante
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id = %s', (applicant_id,))
        a_name, a_mail, cv_url = cur.fetchone()

        # 4) Cargar empleador
        cur.execute('SELECT name FROM "User" WHERE id = %s', (owner_id,))
        owner_row = cur.fetchone()
        employer_name = owner_row[0] if owner_row else ""

        # 5) Construir contexto de reemplazo
        context = {
            "applicant_name": a_name,
            "job_title":      title,
            "employer_name":  employer_name,
            "cv_url":         cv_url or "",
            "created_at":     created_at.strftime("%Y-%m-%d %H:%M:%S"),
        }

        # 6) Obtener plantilla predeterminada según label
        cur.execute("""
            SELECT subject, body
              FROM templates
             WHERE type = %s AND is_default = TRUE
             LIMIT 1
        """, (job.get("label") or "manual",))
        tpl_row = cur.fetchone()
        if tpl_row:
            tpl = {"subject": tpl_row[0], "body": tpl_row[1]}
            subj, body = apply_template(tpl, context)
        else:
            # Fallback legacy
            subj = f"Nueva propuesta – {title}"
            body = (
                f"Hola,\n\n"
                f"{a_name} se postuló a «{title}».\n"
                f"Mail candidato: {a_mail}\n"
            )

        # 7) Validar e-mail
        if not contact_email:
            cur.execute("""
                UPDATE proposals
                   SET status='error_email',
                       cancelled_at = NOW(),
                       notes = 'Sin e-mail de contacto'
                 WHERE id = %s
            """, (pid,))
            conn.commit()
            logger.warning("❗ propuesta sin e-mail, marcada error_email")
            return

        # 8) Envío de correo
        try:
            send_mail(contact_email, subj, body, cv_url)
        except Exception as exc:
            cur.execute("""
                UPDATE proposals
                   SET status='error_email',
                       cancelled_at = NOW(),
                       notes = %s
                 WHERE id = %s
            """, (f"SMTP: {exc}", pid))
            conn.commit()
            return

        # 9) WhatsApp y marcar como enviada
        send_whatsapp(contact_phone, f"Nueva propuesta para «{title}».")
        cur.execute(
            "UPDATE proposals SET status='sent', sent_at = NOW() WHERE id = %s",
            (pid,)
        )
        conn.commit()
        logger.info("✅ propuesta %d enviada", pid)

    except Exception:
        if conn:
            conn.rollback()
        logger.exception("deliver error for proposal %d", pid)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ───────────────────────────── End-points ────────────────────────

@router.post("/create")
def create(data: dict, bg: BackgroundTasks):
    job_id       = data.get("job_id")
    applicant_id = data.get("applicant_id")

    if not (job_id and applicant_id):
        raise HTTPException(400, "Faltan campos")

    conn = cur = None
    try:
        conn = db()
        cur  = conn.cursor()

        # Leer label de la oferta
        cur.execute('SELECT label FROM "Job" WHERE id = %s', (job_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Oferta no existe")
        label = row[0] or "manual"

        # Insertar propuesta con ese label
        cur.execute("""
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            SELECT %s, %s, %s, %s, NOW()
             WHERE NOT EXISTS (
               SELECT 1 FROM proposals WHERE job_id = %s AND applicant_id = %s
             )
            RETURNING id
        """, (
            job_id, applicant_id, label,
            "waiting" if label == "automatic" else "pending",
            job_id, applicant_id
        ))
        row2 = cur.fetchone()
        if not row2:
            conn.commit()
            return {"message": "Ya existe una propuesta"}

        pid = row2[0]
        conn.commit()
        logger.info("🆕 propuesta %d creada (%s)", pid, label)

        if label == "automatic":
            bg.add_task(deliver, pid, True)

        return {"proposal_id": pid}

    except Exception:
        if conn:
            conn.rollback()
        logger.exception("create error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.post("/cancel")
def cancel(data: dict):
    pid = data.get("proposal_id")
    if not pid:
        raise HTTPException(400, "proposal_id requerido")

    conn = cur = None
    try:
        conn = db()
        cur  = conn.cursor()

        cur.execute("SELECT status FROM proposals WHERE id = %s", (pid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "No existe propuesta")
        if row[0] not in ("waiting", "pending"):
            raise HTTPException(400, "Estado no cancelable")

        cur.execute(
            "UPDATE proposals SET status='cancelled', cancelled_at = NOW() WHERE id = %s",
            (pid,)
        )
        conn.commit()
        logger.info("🚫 propuesta %d cancelada", pid)
        return {"message": "cancelada"}

    except HTTPException:
        raise
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("cancel error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.patch("/{pid}/send", dependencies=[Depends(get_current_admin)])
def send_manual(pid: int):
    """
    Envío manual inmediato de una propuesta que esté en 'pending'.
    """
    deliver(pid, sleep_first=False)
    return {"message": "enviada"}


@router.delete("/{pid}", dependencies=[Depends(get_current_admin)])
def delete_cancelled(pid: int):
    conn = cur = None
    try:
        conn = db()
        cur  = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id = %s", (pid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "No existe")
        if row[0] != "cancelled":
            raise HTTPException(400, "Solo canceladas")

        cur.execute("DELETE FROM proposals WHERE id = %s", (pid,))
        conn.commit()
        logger.info("🗑️  propuesta %d eliminada", pid)
        return {"message": "eliminada"}

    except HTTPException:
        raise
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("delete error")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn = cur = None
    try:
        conn = db()
        cur  = conn.cursor()

        cols      = job_columns(cur)
        email_col = ("contact_email"    if "contact_email"    in cols
                     else "\"contactEmail\"" if "contactEmail" in cols
                     else None)
        phone_col = ("contact_phone"    if "contact_phone"    in cols
                     else "\"contactPhone\"" if "contactPhone" in cols
                     else None)

        email_expr = (
            f"COALESCE(j.{email_col}) AS job_contact_email"
            if email_col else "NULL AS job_contact_email"
        )
        phone_expr = (
            f"COALESCE(j.{phone_col}) AS job_contact_phone"
            if phone_col else "NULL AS job_contact_phone"
        )

        sql = f"""
            SELECT
              p.id,
              p.label,
              p.status,
              p.created_at   AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS created_at,
              p.sent_at      AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS sent_at,
              p.cancelled_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS cancelled_at,
              p.notes,
              j.title        AS job_title,
              j.source       AS proposal_source,
              {email_expr},
              {phone_expr},
              u.name         AS applicant_name,
              u.email        AS applicant_email
            FROM proposals p
            JOIN "Job"   j ON p.job_id      = j.id
            JOIN "User"  u ON p.applicant_id = u.id
            ORDER BY p.created_at DESC
        """
        logger.debug("list_proposals SQL → %s", sql)
        cur.execute(sql)

        col_names = [d[0] for d in cur.description]
        items     = [dict(zip(col_names, row)) for row in cur.fetchall()]
        return {"proposals": items}

    except Exception as exc:
        logger.exception("list_proposals error: %s", exc)
        raise HTTPException(500, "Error interno al listar propuestas")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
