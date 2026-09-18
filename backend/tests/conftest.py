import os

# Default to memory backend for hermetic test suites when no external DATABASE_URL is provided
if not os.environ.get("DATABASE_URL"):
    os.environ.setdefault("DATA_BACKEND", "memory")

os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key-at-least-32-characters-long")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
