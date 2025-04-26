import os
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

# Routers públicos
from app.routers import (
    auth as public_auth,
    cv_confirm, cv_upload, cv_processing,
    files, file_processing, integration,
    users, webhooks
)
# Auth Admin
from backend.auth import router as admin_router
# Routers de administración
from app.routers import cv_admin_upload, job, job_admin, admin_users, proposal

# Configuración desde ENV
SECRET_KEY = os.getenv("SECRET_KEY", "clave_por_defecto")
ALGORITHM = os.getenv("ALGORITHM", "HS256")

app = FastAPI(
    proxy_headers=True,
    redirect_slashes=False,
    docs_url="/docs",
    redoc_url="/redoc",
    root_path="/"
)

# CORS: toma FRONTEND_ORIGINS como CSV y permite solo esos orígenes
origins = os.getenv("FRONTEND_ORIGINS", "").split(",")
if not origins or origins == [""]:
    origins = ["*"]  # fallback si no definiste la var

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Logging de cada request
@app.middleware("http")
async def log_request(request: Request, call_next):
    print("📥", request.method, request.url.path,
          "proto=", request.headers.get("x-forwarded-proto"),
          "host=", request.headers.get("host"))
    resp = await call_next(request)
    print("📤", resp.status_code)
    return resp

# Incluimos routers públicos
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

# Login / Auth Admin
app.include_router(admin_router, prefix="/auth", tags=["admin"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token inválido o expirado")
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token inválido o expirado")
    return sub

# Routers que requieren admin
app.include_router(
    cv_admin_upload.router,
    tags=["cv_admin"],
    dependencies=[Depends(get_current_admin)],
)
app.include_router(job.router, prefix="/api/job", tags=["job"])
app.include_router(job_admin.router, prefix="/api/job", tags=["job_admin"])
app.include_router(admin_users.router)
app.include_router(proposal.router)  # /api/proposals

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
