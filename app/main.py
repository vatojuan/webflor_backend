# main.py

import os
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

# Routers públicos
from app.routers import (
    auth as public_auth,
    cv_confirm,
    cv_upload,
    cv_processing,
    files,
    file_processing,
    integration,
    users,
    webhooks,
)
# Auth Admin (login)
from backend.auth import router as admin_router
# Routers de administración
from app.routers import (
    cv_admin_upload,
    job,
    job_admin,
    admin_users,
    proposal,
)
# Router de matchings
from app.routers.matchings_admin import router as matchings_admin_router
# Router de configuración
from app.routers.admin_config import router as admin_config_router

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")

app = FastAPI(
    proxy_headers=True,
    redirect_slashes=False,
    docs_url="/docs",
    redoc_url="/redoc",
    root_path="/",
)

# CORS
origins = os.getenv("FRONTEND_ORIGINS", "").split(",")
if not origins or origins == [""]:
    origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Logging middleware
@app.middleware("http")
async def log_request(request: Request, call_next):
    print("📥", request.method, request.url.path,
          "proto=", request.headers.get("x-forwarded-proto"),
          "host=", request.headers.get("host"))
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
    webhooks.router,
):
    app.include_router(r)

# Auth admin-login
app.include_router(admin_router, prefix="/auth", tags=["admin"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(401, "Token inválido o expirado")
    except JWTError:
        raise HTTPException(401, "Token inválido o expirado")
    return sub

# Routers protegidos
app.include_router(
    cv_admin_upload.router,
    tags=["cv_admin"],
    dependencies=[Depends(get_current_admin)],
)

# job.router NO tiene prefix interno → lo ponemos aquí
app.include_router(
    job.router,
    prefix="/api/job",
    tags=["job"],
)

# job_admin.router YA incluye prefix="/api/job" internamente → sin volver a ponerlo
app.include_router(
    job_admin.router,
    tags=["job_admin"],
)

app.include_router(
    admin_users.router,
    tags=["admin_users"],
)

app.include_router(
    proposal.router,
    tags=["proposals"],
)

# matchings_admin_router ya define prefix="/api/admin" internamente
app.include_router(
    matchings_admin_router,
    tags=["matchings"],
    dependencies=[Depends(get_current_admin)],
)

# admin_config_router ya define prefix="/api/admin/config" internamente
app.include_router(
    admin_config_router,
    tags=["admin_config"],
    dependencies=[Depends(get_current_admin)],
)

# endpoints adicionales
@app.get("/admin/protected", tags=["admin"])
def admin_protected(user=Depends(get_current_admin)):
    return {"message": f"Bienvenido, {user}"}

@app.get("/")
def home():
    return {"ok": True, "message": "API viva y en HTTPS"}

@app.on_event("startup")
def list_routes():
    for route in app.routes:
        print("✅ Ruta cargada:", route.path)
