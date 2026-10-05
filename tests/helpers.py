"""Shared test setup: a temp data/log dir, dev auth, and a service on a fake backend.
Import this before anything under app/ so the settings pick the env up."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="laya_poc_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["DEV_AUTH_BYPASS"] = "true"
os.environ["APP_ENV"] = "dev"
os.environ["LAYA_DEFAULT_MODEL"] = "auto"

from app.backends.fake import FakeBackend  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.registry.bindings import BindingStore  # noqa: E402
from app.registry.loader import load_registry  # noqa: E402
from app.services.classify import ClassifyService  # noqa: E402
from app.services.decision_log import DecisionLog  # noqa: E402

TENANT = "77245"
AUTH = {"Authorization": f"Bearer {TENANT}"}
REGISTRY = load_registry()


def make_service(backend: FakeBackend) -> ClassifyService:
    settings = get_settings()
    return ClassifyService(backend, REGISTRY, BindingStore(settings.data_dir), settings,
                           DecisionLog(settings.log_dir, settings.log_text))
