############################################################
# app/routers/proposal.py
# ----------------------------------------------------------
# Envío de propuestas por e-mail / WhatsApp + gestión de estado
# Versión profesional – 28-may-2025 (modificado 05-jun-2025)
############################################################

from __future__ import annotations

import os, time, logging, smtplib, dns.resolver
from email.message import EmailMessage
from datetime import datetime, timedelta
from typing import Tuple, Optional, Set, Dict

import psycopg2
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from app.database import engine               # SQLAlchemy engine

load_dotenv()

# ───────────────────── Configuración global ──────────────────────
SECRET_KEY   = os.getenv("SECRET_KEY", "")
ALGORITHM    = os.getenv("ALGORITHM", "HS256")
AUTO_DELAY   = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))   # seg
SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "20"))           # seg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s"
)
logger = logging.getLogger(__name__)

router        = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# ───────────────────────────  Auth y DB  ─────────────────────────
def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    if not token:
        raise HTTPException(401, "Token requerido")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub", "")
    except JWTError:
        raise HTTPException(401, "Token inválido")

def db() -> psycopg2.extensions.connection:
    conn = engine.raw_connection()
    conn.autocommit = False
    return conn

# ───────────────────────── SMTP helpers ──────────────────────────
def _smtp_cfg() -> Tuple[str, int, str, str]:
    return (
        os.getenv("SMTP_HOST", ""),
        int(os.getenv("SMTP_PORT", "587")),
        os.getenv("SMTP_USER", ""),
        os.getenv("SMTP_PASS", ""),
    )

def _check_mx(address: str) -> None:
    """Sólo loguea un warning si el dominio no tiene MX."""
    try:
        dns.resolver.resolve(address.split("@")[-1], "MX")
    except Exception as e:
        logger.warning("⚠️  dominio sin MX %s: %s", address, e)

def send_mail(dest: str, subj: str, body: str, cv: Optional[str] = None) -> None:
    host, port, user, pwd = _smtp_cfg()
    if not all([host, port, user, pwd]):
        raise RuntimeError("Variables SMTP* incompletas")
    if not dest:
        raise ValueError("Destino vacío")

    _check_mx(dest)

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, dest, subj
    msg.set_content(body + (f"\n\nCV: {cv}" if cv else ""))

    smtp = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT) if port == 465 \
        else smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    if port != 465:
        smtp.ehlo(); smtp.starttls(); smtp.ehlo()
    smtp.login(user, pwd)
    smtp.send_message(msg)
    smtp.quit()
    logger.info("✉️  mail enviado a %s", dest)

def send_whatsapp(phone: Optional[str], txt: str) -> None:
    if phone:
        logger.info("📲 WhatsApp → %s: %s", phone, txt)

# ───────────────────────── Aviso de cancelación ───────────────────
def send_cancel_warning_email(dest: str, job_title: str) -> None:
    """
    Envía al candidato un mail avisando que tiene 5 minutos para cancelar
    su postulación si fue un error.
    """
    subj = "Postulación recibida en FAP Mendoza"
    body = (
        f"Hola,\n\n"
        f"Has postulado a «{job_title}».\n\n"
        f"Si fue un error o cambiaste de idea, tenés *5 minutos* para cancelar "
        f"la postulación desde tu perfil. Después de ese tiempo, ya no podrá cancelarse.\n\n"
        f"Gracias por usar FAP Mendoza.\n"
        f"— Equipo FAP Mendoza"
    )
    try:
        send_mail(dest, subj, body)
        logger.info("✉️  aviso de cancelación enviado a %s", dest)
    except Exception:
        logger.exception("❌ Error enviando aviso de cancelación a %s", dest)

# ─────────────────── Utilidades de tablas ────────────────────────
def job_columns(cur) -> Set[str]:
    cur.execute("""
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema='public' AND table_name='Job';
    """)
    return {r[0] for r in cur.fetchall()}

def debug_dump_job(job_id: int, job: dict) -> None:
    logger.debug("Job %d dump → %s",
                 job_id, ", ".join(f"{k}={v!r}" for k, v in job.items()))

def apply_template(tpl: Dict[str, str], ctx: Dict[str, str]) -> Tuple[str, str]:
    subj, body = tpl.get("subject", ""), tpl.get("body", "")
    for k, v in ctx.items():
        subj = subj.replace(f"{{{{{k}}}}}", v)
        body = body.replace(f"{{{{{k}}}}}", v)
    return subj, body

# ─────────────────────────── Deliver ─────────────────────────────
def deliver(pid: int, sleep_first: bool) -> None:
    """Envía una propuesta y actualiza su estado."""
    if sleep_first:
        logger.info("⏳ task %d: sleep %ds", pid, AUTO_DELAY)
        time.sleep(AUTO_DELAY)

    conn = cur = None
    try:
        conn = db(); cur = conn.cursor()

        cur.execute("""SELECT status, job_id, applicant_id, created_at
                         FROM proposals WHERE id = %s""", (pid,))
        row = cur.fetchone()
        if not row:
            logger.warning("Propuesta %d no existe", pid)
            return
        status, job_id, applicant_id, created_at = row

        if sleep_first and status != "waiting":
            return
        if not sleep_first and status != "sending":
            raise HTTPException(400, "Estado no válido para enviar")

        # ── Datos del Job
        cur.execute('SELECT * FROM "Job" WHERE id = %s', (job_id,))
        job = dict(zip([d[0] for d in cur.description], cur.fetchone()))
        title    = job.get("title")
        label    = job.get("label") or "manual"
        c_email  = job.get("contact_email")  or job.get("contactEmail")
        c_phone  = job.get("contact_phone")  or job.get("contactPhone")
        owner_id = job.get("user_id")        or job.get("userId")

        # ── Postulante
        cur.execute('SELECT name, email, "cvUrl" FROM "User" WHERE id = %s',
                    (applicant_id,))
        a_name, a_mail, cv_url = cur.fetchone()

        # ── Empleador
        cur.execute('SELECT name, email, phone FROM "User" WHERE id = %s',
                    (owner_id,))
        emp_name, emp_mail, emp_phone = cur.fetchone() or ("", "", "")
        if not c_email:  c_email  = emp_mail
        if not c_phone:  c_phone  = emp_phone

        ctx = dict(
            applicant_name = a_name,
            job_title      = title,
            employer_name  = emp_name,
            cv_url         = cv_url or "",
            created_at     = created_at.strftime("%Y-%m-%d %H:%M:%S"),
        )

        # ── Plantilla predeterminada
        cur.execute("""SELECT subject, body
                         FROM proposal_templates
                        WHERE type = %s AND is_default = TRUE
                        LIMIT 1""", (label,))
        tpl = cur.fetchone()
        if tpl:
            subj, body = apply_template({"subject": tpl[0], "body": tpl[1]}, ctx)
        else:  # fallback legacy
            subj = f"Nueva propuesta – {title}"
            body = f"Hola,\n\n{a_name} se postuló a «{title}».\nMail candidato: {a_mail}\n"

        if not c_email:
            cur.execute("""UPDATE proposals
                             SET status='error_email',
                                 cancelled_at = NOW(),
                                 notes='Sin contacto'
                           WHERE id = %s""", (pid,))
            conn.commit()
            logger.warning("❗ propuesta %d sin e-mail de destino", pid)
            return

        send_mail(c_email, subj, body, cv_url)
        send_whatsapp(c_phone, f"Nueva propuesta para «{title}».")
        cur.execute("""UPDATE proposals
                         SET status='sent', sent_at = NOW()
                       WHERE id = %s""", (pid,))
        conn.commit()
        logger.info("✅ propuesta %d enviada", pid)

    except Exception:
        if conn: conn.rollback()
        logger.exception("deliver error pid=%d", pid)
    finally:
        if cur:  cur.close()
        if conn: conn.close()

# ───────────────────────── End-points ─────────────────────────────

@router.post("/create")
def create(data: dict, bg: BackgroundTasks):
    job_id       = data.get("job_id")
    applicant_id = data.get("applicant_id")
    if not job_id or not applicant_id:
        raise HTTPException(400, "Faltan campos requeridos")

    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor()

        # 1) Determinar label del job
        cur.execute('SELECT label FROM "Job" WHERE id = %s', (job_id,))
        label = (cur.fetchone() or ["manual"])[0] or "manual"

        # 2) Chequear si ya existe propuesta para este job y postulante
        cur.execute("""
            SELECT id, status
              FROM proposals
             WHERE job_id = %s AND applicant_id = %s
        """, (job_id, applicant_id))
        existing = cur.fetchone()

        if existing:
            pid_existing, st = existing
            if st == "cancelled":
                # Borrar la propuesta cancelada para permitir una nueva
                cur.execute("DELETE FROM proposals WHERE id = %s", (pid_existing,))
                conn.commit()
                logger.info("🗑️  propuesta %d eliminada (cancelada previa)", pid_existing)
            else:
                # Si existe y no está cancelada => no permitir duplicado
                conn.commit()
                raise HTTPException(400, "Ya has postulado a este empleo")

        # 3) Insertar nueva propuesta
        cur.execute("""
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            VALUES (%s, %s, %s, %s, NOW())
            RETURNING id
        """, (
            job_id,
            applicant_id,
            label,
            "waiting" if label == "automatic" else "pending"
        ))
        row = cur.fetchone()
        if not row:
            conn.commit()
            raise HTTPException(500, "No se pudo crear la propuesta")
        pid = row[0]
        conn.commit()
        logger.info("🆕 propuesta %d creada (%s)", pid, label)

        # 4) Si es automática, programar entrega
        if label == "automatic":
            bg.add_task(deliver, pid, True)

        # 5) Enviar aviso de posibilidad de cancelar
        cur.execute('SELECT title FROM "Job" WHERE id = %s', (job_id,))
        job_title = cur.fetchone()[0]
        cur.execute('SELECT email FROM "User" WHERE id = %s', (applicant_id,))
        applicant_email = cur.fetchone()[0]
        bg.add_task(send_cancel_warning_email, applicant_email, job_title)

        return {"proposal_id": pid}

    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.patch("/{pid}/send", dependencies=[Depends(get_current_admin)])
def send_manual(pid: int):
    """Cambia de 'pending' → 'sending' y envía al instante."""
    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("""UPDATE proposals
                         SET status = 'sending'
                       WHERE id = %s AND status = 'pending'
                       RETURNING id""", (pid,))
        if not cur.fetchone():
            raise HTTPException(400, "Estado no es pending")
        conn.commit()
    finally:
        if cur: cur.close()
        if conn: conn.close()

    deliver(pid, sleep_first=False)
    return {"message": "enviada"}

@router.post("/cancel")
def cancel(data: dict):
    pid = data.get("proposal_id")
    if not pid:
        raise HTTPException(400, "proposal_id requerido")

    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id = %s", (pid,))
        st = (cur.fetchone() or [None])[0]
        if st is None:
            raise HTTPException(404, "No existe propuesta")
        if st not in ("waiting", "pending"):
            raise HTTPException(400, "Estado no cancelable")
        cur.execute("""UPDATE proposals
                         SET status='cancelled',
                             cancelled_at = NOW()
                       WHERE id = %s""", (pid,))
        conn.commit()
        logger.info("🚫 propuesta %d cancelada", pid)
        return {"message": "cancelada"}
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.delete("/{pid}", dependencies=[Depends(get_current_admin)])
def delete_cancelled(pid: int):
    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM proposals WHERE id = %s", (pid,))
        st = (cur.fetchone() or [None])[0]
        if st is None:
            raise HTTPException(404, "No existe")
        if st != "cancelled":
            raise HTTPException(400, "Solo canceladas")
        cur.execute("DELETE FROM proposals WHERE id = %s", (pid,))
        conn.commit()
        logger.info("🗑️  propuesta %d eliminada", pid)
        return {"message": "eliminada"}
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.get("/", dependencies=[Depends(get_current_admin)])
def list_proposals():
    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor()
        cols = job_columns(cur)
        email_c = "contact_email" if "contact_email" in cols \
                  else "\"contactEmail\"" if "contactEmail" in cols else None
        phone_c = "contact_phone" if "contact_phone" in cols \
                  else "\"contactPhone\"" if "contactPhone" in cols else None
        email_expr = f"COALESCE(j.{email_c})" if email_c else "NULL"
        phone_expr = f"COALESCE(j.{phone_c})" if phone_c else "NULL"

        cur.execute(f"""
            SELECT
              p.id, p.label, p.status,
              p.created_at AT TIME ZONE 'UTC'
                           AT TIME ZONE 'America/Argentina/Buenos_Aires' AS created_at,
              p.sent_at    AT TIME ZONE 'UTC'
                           AT TIME ZONE 'America/Argentina/Buenos_Aires' AS sent_at,
              p.cancelled_at AT TIME ZONE 'UTC'
                           AT TIME ZONE 'America/Argentina/Buenos_Aires' AS cancelled_at,
              p.notes,
              j.title  AS job_title,
              j.source AS proposal_source,
              {email_expr} AS job_contact_email,
              {phone_expr} AS job_contact_phone,
              u.name   AS applicant_name,
              u.email  AS applicant_email
              FROM proposals p
              JOIN "Job"  j ON p.job_id       = j.id
              JOIN "User" u ON p.applicant_id = u.id
             ORDER BY p.created_at DESC
        """)
        cols = [d[0] for d in cur.description]
        return {"proposals": [dict(zip(cols, r)) for r in cur.fetchall()]}
    finally:
        if cur: cur.close()
        if conn: conn.close()
