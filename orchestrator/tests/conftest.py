"""Los tests nunca deben escribir en los datos reales de Sergio: antes cada
pasada dejaba usuarios "_test_integration_*"/"smoketest_*" en su users.db, y
fue el origen de la confusion con los datos del 2026-09-25. Se redirigen
DATA_DIR y OUTPUT_DIR a una carpeta temporal ANTES de que ningun modulo haga
`from paths import DATA_DIR`. MODELS_DIR se deja tal cual: los tests
live/slow necesitan los modelos de verdad (y solo los leen)."""

import shutil
import tempfile
from pathlib import Path

import paths

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="chati_tests_"))
paths.DATA_DIR = _TEST_ROOT / "data"
paths.OUTPUT_DIR = _TEST_ROOT / "outputs"
paths.DATA_DIR.mkdir(parents=True)
paths.OUTPUT_DIR.mkdir(parents=True)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)

# TestClient llama al servidor "testserver": para los tests cuenta como local
# (security.LocalOnlyMiddleware rechaza cualquier otro Host)
import security  # noqa: E402

security.LOCAL_HOSTS.add("testserver")
