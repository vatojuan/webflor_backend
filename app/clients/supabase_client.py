import requests

SUPABASE_BASE_URL = "https://apnfioxjddccokgkljvd.supabase.co/rest/v1"
SUPABASE_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFwbmZpb3hqZGRjY29rZ2tsanZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MzkzMTkxODAsImV4cCI6MjA1NDg5NTE4MH0.BB8Zq2UQ4n9MuovSOiPnP0Aff6xLC2uUSfEcjYFvfGY"

HEADERS = {
    "apikey": SUPABASE_API_KEY,
    "Authorization": f"Bearer {SUPABASE_API_KEY}"
}

def get_users():
    url = f"{SUPABASE_BASE_URL}/users?select=*"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()

# Puedes crear funciones similares para otras tablas.
