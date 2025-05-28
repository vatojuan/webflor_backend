# app/routers/email_db_admin.py
import os, io, re, mimetypes, psycopg2, smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import List, Optional

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query, BackgroundTasks
from fastapi.encoders import jsonable_encoder
from fastapi.security import OAuth2PasswordBearer
from dotenv import load_dotenv
from jose import JWTError, jwt
from PyPDF2 import PdfReader
from docx import Document
from openai import OpenAI

# ──────────────────────────── Config ────────────────────────────
load_dotenv()

router = APIRouter(
    prefix="/api/admin/emails",   # todas las rutas cuelgan de aquí
    tags=["email_db"]
)

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")

SMTP_HOST  = os.getenv("SMTP_HOST")
SMTP_PORT  = int(os.getenv("SMTP_PORT", 587))
SMTP_USER  = os.getenv("SMTP_USER")
SMTP_PASS  = os.getenv("SMTP_PASS")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client  = OpenAI(api_key=OPENAI_API_KEY)

oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2)):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])["sub"]
    except (JWTError, KeyError):
        raise HTTPException(401, "Token inválido o expirado")

def db():
    return psycopg2.connect(
        dbname=os.getenv("DBNAME", "postgres"),
        user=os.getenv("USER"),
        password=os.getenv("PASSWORD"),
        host=os.getenv("HOST"),
        port=5432,
        sslmode="require",
    )

EMAIL_RE  = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE  = re.compile(r"\+?\d[\d\s\-]{8,}")

# ───────────────────── Funciones auxiliares ─────────────────────
def pdf_to_text(b: bytes) -> str:
    return " ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(b)).pages)

def docx_to_text(b: bytes) -> str:
    doc = Document(io.BytesIO(b))
    return "\n".join(p.text for p in doc.paragraphs)

def txt_to_text(b: bytes) -> str:
    return b.decode(errors="ignore")

def extract_email(text: str) -> Optional[str]:
    match = EMAIL_RE.search(text)
    return match.group(0).lower() if match else None

def extract_phone(text: str) -> Optional[str]:
    match = PHONE_RE.search(text)
    return match.group(0) if match else None

def extract_name(text: str) -> Optional[str]:
    prompt = [
        {"role": "system", "content": "Eres un experto en análisis de CVs."},
        {"role": "user",
         "content": ("Extrae ÚNICAMENTE el nombre completo del candidato del siguiente texto. "
                     "No incluyas títulos. Si no encuentras un nombre responde 'No encontrado'.\n\n"
                     f"{text[:1000]}")}
    ]
    resp  = openai_client.chat.completions.create(model="gpt-4-turbo", messages=prompt, max_tokens=10)
    name  = resp.choices[0].message.content.strip()
    return None if name.lower() == "no encontrado" else name or None

# ──────────────────────── Endpoints API ─────────────────────────

@router.post("/upload", dependencies=[Depends(get_current_admin)])
async def upload_files(files: List[UploadFile] = File(...)):
    """
    Subida masiva de CV / docs → email_contacts
    """
    results = []
    for f in files:
        logs = [f"Procesando {f.filename}"]
        try:
            raw  = await f.read()
            mime, _ = mimetypes.guess_type(f.filename)

            if mime == "application/pdf" or f.filename.lower().endswith(".pdf"):
                text = pdf_to_text(raw);  logs.append("PDF → texto")
            elif (mime and "word" in mime) or f.filename.lower().endswith(".docx"):
                text = docx_to_text(raw); logs.append("DOCX → texto")
            else:
                text = txt_to_text(raw);  logs.append("Texto plano")

            email = extract_email(text)
            if not email:
                raise Exception("E-mail no encontrado")
            logs.append(f"E-mail: {email}")

            name  = extract_name(text) or email.split("@")[0].replace(".", " ").replace("_", " ").title()
            phone = extract_phone(text)

            conn, cur = db(), None
            try:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO email_contacts (email, name, phone, source, source_file)
                    VALUES (%s,%s,%s,'file',%s)
                    ON CONFLICT (email) DO UPDATE SET
                      name         = EXCLUDED.name,
                      phone        = EXCLUDED.phone,
                      source       = 'file',
                      source_file  = EXCLUDED.source_file,
                      imported_at  = NOW()
                    """,
                    (email, name, phone, f.filename)
                )
                conn.commit()
            finally:
                if cur: cur.close()
                conn.close()

            logs.append("Guardado en BD")
            results.append({"file": f.filename, "email": email, "status": "success", "logs": logs})

        except Exception as e:
            logs.append(f"Error: {e}")
            results.append({"file": f.filename, "status": "error", "logs": logs})

    return {"results": results}


@router.post("/manual", dependencies=[Depends(get_current_admin)])
async def add_manual(payload: dict):
    """
    Alta manual de contacto
    """
    email = payload.get("email", "").lower().strip()
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "E-mail inválido")

    conn, cur = db(), None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO email_contacts (email, name, phone, source, notes)
            VALUES (%s,%s,%s,'manual',%s)
            ON CONFLICT (email) DO UPDATE SET
              name        = COALESCE(EXCLUDED.name ,email_contacts.name ),
              phone       = COALESCE(EXCLUDED.phone,email_contacts.phone),
              notes       = COALESCE(EXCLUDED.notes,email_contacts.notes),
              imported_at = NOW()
            """,
            (
                email,
                payload.get("name")  or "",
                payload.get("phone") or None,
                payload.get("notes") or None,
            )
        )
        conn.commit()
    finally:
        if cur: cur.close()
        conn.close()

    return {"ok": True, "email": email}


@router.get("", dependencies=[Depends(get_current_admin)])
def list_emails(
    search: Optional[str] = Query(None),
    page: int            = Query(1,  ge=1),
    page_size: int       = Query(25, ge=1, le=200),
):
    offset = (page - 1) * page_size
    conn, cur = db(), None
    try:
        cur = conn.cursor()
        if search:
            q = f"%{search.lower()}%"
            cur.execute(
                """
                SELECT id,email,name,phone,source,imported_at,valid,notes
                FROM email_contacts
                WHERE LOWER(email) LIKE %s OR LOWER(name) LIKE %s
                ORDER BY imported_at DESC
                LIMIT %s OFFSET %s
                """,
                (q, q, page_size, offset)
            )
        else:
            cur.execute(
                """
                SELECT id,email,name,phone,source,imported_at,valid,notes
                FROM email_contacts
                ORDER BY imported_at DESC
                LIMIT %s OFFSET %s
                """,
                (page_size, offset)
            )

        rows  = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) FROM email_contacts")
        total = cur.fetchone()[0]
    finally:
        if cur: cur.close()
        conn.close()

    return {"total": total, "items": jsonable_encoder(rows)}


@router.put("/{contact_id}", dependencies=[Depends(get_current_admin)])
def update_contact(contact_id: int, payload: dict):
    fields = ["name", "phone", "notes", "valid"]
    sets   = [f"{f} = %s" for f in fields if f in payload]
    if not sets:
        return {"ok": True}

    values = [payload[f] for f in fields if f in payload] + [contact_id]
    conn, cur = db(), None
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE email_contacts SET {', '.join(sets)} WHERE id = %s", values)
        conn.commit()
    finally:
        if cur: cur.close()
        conn.close()

    return {"ok": True}


@router.delete("/{contact_id}", dependencies=[Depends(get_current_admin)])
def delete_contact(contact_id: int):
    conn, cur = db(), None
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM email_contacts WHERE id = %s", (contact_id,))
        conn.commit()
    finally:
        if cur: cur.close()
        conn.close()
    return {"ok": True}


# ─────────────── Envío masivo (cola con BG tasks) ───────────────
def _send_email(subject: str, body: str, to_: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = to_
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


@router.post("/send_bulk", dependencies=[Depends(get_current_admin)])
def send_bulk(data: dict, bg: BackgroundTasks):
    subject = data.get("subject")
    body    = data.get("body")
    ids     = data.get("ids")       # lista opcional de IDs

    if not subject or not body:
        raise HTTPException(400, "subject y body requeridos")

    conn, cur = db(), None
    try:
        cur = conn.cursor()
        if ids:
            cur.execute("SELECT email FROM email_contacts WHERE id = ANY(%s)", (ids,))
        else:
            cur.execute("SELECT email FROM email_contacts")
        recipients = [r[0] for r in cur.fetchall()]
    finally:
        if cur: cur.close()
        conn.close()

    for email in recipients:
        bg.add_task(_send_email, subject, body, email)

    return {"queued": len(recipients)}
