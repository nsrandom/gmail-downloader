"""Every path the pipelines runner reads or writes."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

CONFIG_DIR = BASE_DIR / "configs"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "pipelines_config.yaml"
DEFAULT_ENV_PATH = BASE_DIR / ".env"

# Shared with attachments_downloader.py: the OAuth client file and the
# read-only token live here, and both runners use the same ones.
STATE_DIR = BASE_DIR / "state"
CREDENTIALS_PATH = STATE_DIR / "credentials.json"
TOKEN_PATH = STATE_DIR / "token.json"

# Everything this runner owns sits one level down, so a pipeline here and a
# pipeline in the attachments config can share a name without one clobbering
# the other's state file.
PIPELINE_STATE_DIR = STATE_DIR / "pipelines"
LOG_PATH = STATE_DIR / "pipelines.log"
RUN_SUMMARY_PATH = PIPELINE_STATE_DIR / "last_run.json"
DRY_RUN_SUMMARY_PATH = PIPELINE_STATE_DIR / "last_run.dry.json"

FIXTURE_DIR = BASE_DIR / "tests" / "fixtures"
