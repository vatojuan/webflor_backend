# app/routers/email_db_admin.py
import os, io, re, json, mimetypes, psycopg2
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from dotenv import load_dotenv
from PyPDF2 import PdfReader
from jose import jwt, JWTError
from docx import Document  # pip install python-docx
from datetime import datetime

load_dotenv()
router = APIRouter(tags=["email_db"])

SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")

def get_current_admin(token: str = Depends(lambda: None)):
    # Re-utiliza el mismo esquema de tu proyecto
    from fastapi.security import OAuth2PasswordBearer
    oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
    token = Depends(oauth2_scheme)
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

# ---------- Utilidades de extracción ----------
EMAIL_RE  = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE  = re.compile(r"\+?\d[\d\s\-]{8,}")

def pdf_to_text(b: bytes) -> str:
    return " ".join([p.extract_text() or "" for p in PdfReader(io.BytesIO(b)).pages])

def docx_to_text(b: bytes) -> str:
    tmp = io.BytesIO(b)
    doc = Document(tmp)
    return "\n".join([p.text for p in doc.paragraphs])

def txt_to_text(b: bytes) -> str:
    return b.decode(errors="ignore")

def extract_contact(text: str):
    email = next(iter(EMAIL_RE.findall(text)), None)
    phone = next(iter(PHONE_RE.findall(text)), None)
    if not email:
        return None, None, None
    name_guess = (
        email.split("@")[0]
        .replace(".", " ")
        .replace("_", " ")
        .title()
    )
    return email.lower(), name_guess, phone

# ---------- ENDPOINTS ----------
@router.post("/admin_emails_upload", dependencies=[Depends(get_current_admin)])
async def admin_emails_upload(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        logs = [f"Procesando {f.filename}"]
        try:
            raw = await f.read()
            mime, _ = mimetypes.guess_type(f.filename)
            text = ""
            if mime == "application/pdf" or f.filename.lower().endswith(".pdf"):
                text = pdf_to_text(raw)
                logs.append("PDF → texto extraído")
            elif mime in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword") or f.filename.lower().endswith(".docx"):
                text = docx_to_text(raw)
                logs.append("DOCX → texto extraído")
            else:
                text = txt_to_text(raw)
                logs.append("TXT/otro → texto extraído")

            email, name, phone = extract_contact(text)
            if not email:
                raise Exception("No se encontró un e-mail válido")

            # Inserta si no existe
            conn = db(); cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO email_contacts (email, name, phone, source, source_file)
                VALUES (%s, %s, %s, 'file', %s)
                ON CONFLICT (email) DO NOTHING
                """,
                (email, name, phone, f.filename)
            )
            conn.commit(); cur.close(); conn.close()
            logs.append("Guardado en BD")

            results.append({"file": f.filename, "email": email, "status": "success", "logs": logs})
        except Exception as e:
            logs.append(f"Error: {e}")
            results.append({"file": f.filename, "status": "error", "logs": logs})
    return {"results": results}

@router.post("/admin_emails_manual", dependencies=[Depends(get_current_admin)])
async def admin_emails_manual(payload: dict):
    email = payload.get("email", "").lower().strip()
    name  = payload.get("name")
    phone = payload.get("phone")
    notes = payload.get("notes")
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "E-mail inválido")
    conn = db(); cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO email_contacts (email, name, phone, source, notes)
        VALUES (%s, %s, %s, 'manual', %s)
        ON CONFLICT (email) DO UPDATE
          SET name=COALESCE(EXCLUDED.name,email_contacts.name),
              phone=COALESCE(EXCLUDED.phone,email_contacts.phone),
              notes=COALESCE(EXCLUDED.notes,email_contacts.notes),
              imported_at=%s
        """,
        (email, name, phone, notes, datetime.utcnow())
    )
    conn.commit(); cur.close(); conn.close()
    return {"ok": True, "email": email}
