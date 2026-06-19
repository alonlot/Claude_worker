#!/usr/bin/env bash
#
# setup_server.sh — one-shot setup for the Jira Claude Worker on a Linux/macOS server.
#
# What it does (idempotent — safe to re-run):
#   1. Checks prerequisites: git, python3, pip, venv  (docker is optional).
#   2. Creates a Python virtualenv (.venv) and installs dependencies.
#   3. Creates config.yaml from config.example.yaml if it does not exist.
#   4. Generates a strong auth.session_secret on first setup.
#   5. Initializes the SQLite database.
#   6. (Optional) Builds the Docker agent image for isolated runs.
#   7. Runs the test suite to confirm the install is healthy.
#   8. Prints the exact next steps to start serving.
#
# Usage:
#   bash setup_server.sh                # base setup (subprocess execution mode)
#   bash setup_server.sh --with-docker  # also build the Docker agent image
#   WITH_DOCKER=1 bash setup_server.sh  # same, via env var
#
# This script does NOT install system packages or Docker itself (that needs your
# distro's package manager and root). If a prerequisite is missing it tells you
# the command to install it, then exits.

set -euo pipefail

# --- pretty output ---------------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; NC=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; NC=""
fi
info()  { printf "%s\n" "${BOLD}==>${NC} $*"; }
ok()    { printf "%s\n" "${GREEN}OK${NC}  $*"; }
warn()  { printf "%s\n" "${YELLOW}!!${NC}  $*"; }
die()   { printf "%s\n" "${RED}ERROR${NC} $*" >&2; exit 1; }

# Always operate from the directory this script lives in (the repo root).
cd "$(dirname "$0")"
REPO_DIR="$(pwd)"
info "Setting up Jira Claude Worker in ${REPO_DIR}"

# --- parse flags -----------------------------------------------------------
WITH_DOCKER="${WITH_DOCKER:-0}"
for arg in "$@"; do
  case "$arg" in
    --with-docker) WITH_DOCKER=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown argument: $arg (try --help)" ;;
  esac
done

# --- 1. prerequisites ------------------------------------------------------
info "Checking prerequisites"
command -v git >/dev/null 2>&1 || die "git not found. Install it (e.g. 'sudo apt-get install -y git')."
ok "git: $(git --version)"

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
[ -n "$PY" ] || die "python3 not found. Install it (e.g. 'sudo apt-get install -y python3 python3-venv python3-pip')."
PY_VERSION="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
ok "python: $PY_VERSION ($PY)"
# Require Python 3.10+ (dataclass/typing features used in the app).
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)' \
  || die "Python 3.10+ required, found $PY_VERSION."

# venv module must be available to create the virtualenv.
"$PY" -c 'import venv' 2>/dev/null || die "python venv module missing. Install it (e.g. 'sudo apt-get install -y python3-venv')."

# --- 2. virtualenv + dependencies -----------------------------------------
info "Creating virtualenv (.venv) and installing dependencies"
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  ok "created .venv"
else
  ok ".venv already exists"
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
# requirements-dev.txt pulls in runtime deps (-r requirements.txt) plus pytest.
python -m pip install --quiet -r requirements-dev.txt
ok "dependencies installed"

# --- 3. config.yaml --------------------------------------------------------
info "Preparing config.yaml"
FRESH_CONFIG=0
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  FRESH_CONFIG=1
  ok "created config.yaml from config.example.yaml"
else
  ok "config.yaml already exists (left unchanged)"
fi

# --- 4. session secret (only on a fresh config) ----------------------------
if [ "$FRESH_CONFIG" -eq 1 ]; then
  info "Generating a strong auth.session_secret"
  SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
  # Replace the placeholder from the example file.
  python - "$SECRET" <<'PYEOF'
import re, sys
secret = sys.argv[1]
path = "config.yaml"
text = open(path, encoding="utf-8").read()
text = re.sub(r'(?m)^(\s*session_secret:\s*).*$', r'\g<1>' + secret, text, count=1)
open(path, "w", encoding="utf-8").write(text)
PYEOF
  ok "session_secret set"
else
  warn "Existing config.yaml kept — make sure auth.session_secret is a long random string."
fi

# --- 5. database -----------------------------------------------------------
info "Initializing the database"
python -m app init-db
ok "database ready"

# --- 6. Docker agent image (optional) -------------------------------------
if [ "$WITH_DOCKER" -eq 1 ]; then
  info "Building the Docker agent image (isolated run mode)"
  if ! command -v docker >/dev/null 2>&1; then
    warn "docker is not installed — skipping image build."
    warn "Install Docker Engine, then run: docker build -t claude-worker-agent:latest ./docker"
  elif ! docker info >/dev/null 2>&1; then
    warn "docker is installed but the daemon is not reachable (need sudo or the docker group?)."
    warn "Start Docker, then run: docker build -t claude-worker-agent:latest ./docker"
  else
    docker build -t claude-worker-agent:latest ./docker
    ok "built image claude-worker-agent:latest"
    warn "To USE Docker mode, set in config.yaml:  docker.enabled: true,  claude.command: claude,"
    warn "and set claude.api_key (the container cannot use a host Claude login)."
  fi
else
  ok "Skipping Docker image (subprocess execution mode). Re-run with --with-docker to build it."
fi

# --- 7. test suite ---------------------------------------------------------
info "Running the test suite"
if python -m pytest -q; then
  ok "all tests passed"
else
  die "tests failed — fix the above before serving."
fi

# --- 8. next steps ---------------------------------------------------------
HOST="$(python -c 'from app.config import load_config; c=load_config(); print(c.app.host)' 2>/dev/null || echo 127.0.0.1)"
PORT="$(python -c 'from app.config import load_config; c=load_config(); print(c.app.port)' 2>/dev/null || echo 8000)"
PROVIDER="$(python -c 'from app.config import load_config; c=load_config(); print(c.auth.provider)' 2>/dev/null || echo proxy_header)"

cat <<EOF

${GREEN}${BOLD}Setup complete.${NC}

Next steps:
  1. Activate the environment:    ${BOLD}source .venv/bin/activate${NC}
  2. Review ${BOLD}config.yaml${NC} (Jira/Git/Claude defaults, auth, host/port).
EOF

if [ "$PROVIDER" = "login_page" ]; then
  cat <<EOF
  3. Create a login account:      ${BOLD}python -m app add-user admin --admin${NC}
  4. Start the server:            ${BOLD}python -m app serve${NC}
EOF
else
  cat <<EOF
  3. Auth provider is '${PROVIDER}'. For built-in login instead, set
     auth.provider: login_page in config.yaml and run:
                                  ${BOLD}python -m app add-user admin --admin${NC}
  4. Start the server:            ${BOLD}python -m app serve${NC}
EOF
fi

cat <<EOF
  5. Open:                        ${BOLD}http://${HOST}:${PORT}${NC}  (front with your reverse proxy / TLS)

Tip: from Settings -> Connection Tests, use ${BOLD}Test Jira / Test Git / Test Claude${NC}
     and (if Docker mode is on) ${BOLD}Check Docker run${NC} to verify everything end to end.
EOF
