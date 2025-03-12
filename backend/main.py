from fastapi import FastAPI
from backend.auth import router as auth_router
from app.routers import cv_confirm, cv_upload, cv_processing, files, file_processing, integration, token_utils, users, webhooks

app = FastAPI()

# Rutas administrativas
app.include_router(auth_router, prefix="/auth", tags=["admin"])

# Rutas públicas
app.include_router(cv_confirm.router)
app.include_router(cv_upload.router)
app.include_router(cv_processing.router)
app.include_router(files.router)
app.include_router(file_processing.router)
app.include_router(integration.router)
app.include_router(token_utils.router)
app.include_router(users.router)
app.include_router(webhooks.router)

@app.on_event("startup")
def list_routes():
    for route in app.routes:
        print(f"✅ Ruta cargada: {route.path}")
