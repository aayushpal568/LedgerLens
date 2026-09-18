import os
import pytest

# Default to memory backend for hermetic test suites when no external DATABASE_URL is provided
if not os.environ.get("DATABASE_URL"):
    os.environ.setdefault("DATA_BACKEND", "memory")

os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key-at-least-32-characters-long")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")

# Hermetic paid AI isolation: Ensure no accidental calls to paid APIs during normal tests
if not os.environ.get("ENABLE_LIVE_PAID_AI_TESTS"):
    os.environ["FAL_KEY"] = ""
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ["FAL_API_KEY"] = ""


def pytest_configure(config):
    config.addinivalue_line("markers", "live_ai: mark test as requiring live external paid AI services (opt-in)")
