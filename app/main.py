from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

# Importar routers públicos (clientes)
from app.routers import (
    auth as public_auth,
    cv_confirm, cv_upload, cv_processing, files,
    file_processing, integration, token_utils, users, webhooks
)

# Importar router administrativo (login admin) desde backend
from backend.auth import router as admin_router

# Importar el router de carga masiva de CVs
from app.routers import cv_admin_upload

# Importar el router de ofertas de empleo para creación (job.py)
from app.routers import job

# Importar el router de ofertas de empleo para gestión (job_admin.py)
from app.routers import job_admin

# Importar el router para la gestión de usuarios (nuevos endpoints)
from app.routers import admin_users

# Importar el router de propuestas
from app.routers import proposal

SECRET_KEY = "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"
ALGORITHM  = "HS256"

app = FastAPI(
    proxy_headers=True,      # para respetar X‑Forwarded‑Proto
    redirect_slashes=False,  # <— ¡evita el 307 a /api/proposals/!
    docs_url="/docs",
    redoc_url="/redoc",
    root_path="/"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://fapmendoza.online",
        "https://webfloradmin-vatojuans-projects.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# (opcional) logging de cabeceras
@app.middleware("http")
async def _log(request: Request, call_next):
    print("📥", request.method, request.url.path,
          "x-forwarded-proto=", request.headers.get("x-forwarded-proto"),
          "Host=", request.headers.get("host"))
    resp = await call_next(request)
    print("📤", resp.status_code)
    return resp

# Routers públicos
for r in (
    public_auth.router,
    cv_confirm.router,
    cv_upload.router,
    cv_processing.router,
    files.router,
    file_processing.router,
    integration.router,
    users.router,
    webhooks.router
):
    app.include_router(r)

# Login admin
app.include_router(admin_router, prefix="/auth", tags=["admin"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if not payload.get("sub"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido o expirado")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido o expirado")
    return payload["sub"]

# Routers protegidos
app.include_router(cv_admin_upload.router, tags=["cv_admin"], dependencies=[Depends(get_current_admin)])
app.include_router(job.router,            prefix="/api/job", tags=["job"])
app.include_router(job_admin.router,      prefix="/api/job", tags=["job_admin"])
app.include_router(admin_users.router)
app.include_router(proposal.router)  # ahora /api/proposals y sin redirect

@app.get("/admin/protected", tags=["admin"])
def admin_protected(current_admin: str = Depends(get_current_admin)):
    return {"message": f"Bienvenido, {current_admin}"}

@app.get("/")
def home():
    return {"ok": True, "message": "API viva y en HTTPS"}

@app.on_event("startup")
def list_routes():
    for r in app.routes:
        print("✅ Ruta cargada:", r.path)
