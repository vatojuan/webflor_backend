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

# Configuración de JWT (debe coincidir con la usada en backend/auth.py)
SECRET_KEY = "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"
ALGORITHM = "HS256"

app = FastAPI(
    proxy_headers=True,
    root_path="/",  # útil si usás subdominios o rutas base
    docs_url="/docs",
    redoc_url="/redoc"
)

# Middleware CORS
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

# Middleware para loguear headers y detectar posibles redirecciones
@app.middleware("http")
async def log_request_headers(request: Request, call_next):
    print("📥 Incoming request:", request.method, request.url.path)
    print("🔍 x-forwarded-proto:", request.headers.get("x-forwarded-proto"))
    print("🔍 Host:", request.headers.get("host"))
    print("🔍 Authorization:", request.headers.get("authorization"))
    response = await call_next(request)
    print("📤 Response status:", response.status_code)
    return response

# Registrar routers públicos
app.include_router(public_auth.router)
app.include_router(cv_confirm.router)
app.include_router(cv_upload.router)
app.include_router(cv_processing.router)
app.include_router(files.router)
app.include_router(file_processing.router)
app.include_router(integration.router)
app.include_router(users.router)
app.include_router(webhooks.router)

# Registrar el router administrativo para login
app.include_router(admin_router, prefix="/auth", tags=["admin"])

# Configurar OAuth2 para endpoints protegidos
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token no proporcionado"
        )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido o sesión expirada"
            )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o sesión expirada"
        )
    return username

# Registrar el router de carga masiva de CVs (ruta: /admin_upload)
app.include_router(
    cv_admin_upload.router,
    tags=["cv_admin"],
    dependencies=[Depends(get_current_admin)]
)

# Registrar el router de ofertas de empleo para creación (job.py)
app.include_router(
    job.router,
    prefix="/api/job",
    tags=["job"]
)

# Registrar el router de ofertas de empleo para gestión (job_admin.py)
app.include_router(
    job_admin.router,
    prefix="/api/job",  # Usamos el mismo prefijo para que el endpoint sea /api/job/admin_offers
    tags=["job_admin"]
)

# Registrar el router para la gestión de usuarios
app.include_router(admin_users.router)

# Registrar el router de propuestas
app.include_router(proposal.router)

@app.get("/admin/protected", tags=["admin"])
def admin_protected(current_admin: str = Depends(get_current_admin)):
    return {"message": f"Ruta protegida para administradores, bienvenido {current_admin}"}

@app.get("/")
def home():
    return {"ok": True, "message": "Hello from FastAPI"}

@app.on_event("startup")
def list_routes():
    for route in app.routes:
        print(f"✅ Ruta cargada: {route.path}")
