import os
import sys

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-for-pytest-only")
os.environ.setdefault("DB_NAME", "whatsapp_ai_bot_test")
# CRM tests use a REAL MySQL/MariaDB (never your production DB: name must end in _test).
os.environ.setdefault("DB_PORT", os.environ.get("TEST_DB_PORT", "3307"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
