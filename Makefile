.PHONY: help install dev-db dev-db-stop migrate migrations run test lint format \
        check superuser shell docker-build docker-up docker-down docker-logs \
        keygen backup

# Windows developers get .venv/Scripts; everyone else gets .venv/bin.
VENV_BIN := $(if $(wildcard .venv/Scripts/python.exe),.venv/Scripts,.venv/bin)
PY := $(VENV_BIN)/python

help:
	@echo "Setup"
	@echo "  install       Create .venv and install dev dependencies"
	@echo "  keygen        Print fresh values for the two secrets in .env"
	@echo "  dev-db        Start only Postgres, for local development"
	@echo ""
	@echo "Development"
	@echo "  run           Run the development server"
	@echo "  migrate       Apply migrations"
	@echo "  migrations    Generate migrations for model changes"
	@echo "  superuser     Create the break-glass superuser"
	@echo "  shell         Django shell"
	@echo ""
	@echo "Quality"
	@echo "  test          Run the test suite (needs Postgres running)"
	@echo "  lint          Ruff lint"
	@echo "  format        Ruff format, writing changes"
	@echo "  check         Everything CI runs"
	@echo ""
	@echo "Deployment"
	@echo "  (first install: sh scripts/bootstrap.sh --help)"
	@echo "  docker-build  Build the application image"
	@echo "  docker-up     Start the full stack"
	@echo "  docker-down   Stop the stack"
	@echo "  docker-logs   Follow logs"
	@echo "  backup        Run a database backup inside the running stack"

install:
	python -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements/dev.txt
	@echo ""
	@echo "Next: cp .env.example .env, then 'make keygen' and paste the values in."

keygen:
	@$(PY) -c "import secrets; print('DJANGO_SECRET_KEY=' + secrets.token_urlsafe(64))"
	@$(PY) -c "import base64, os; print('BCTRACKER_MASTER_KEY=' + base64.b64encode(os.urandom(32)).decode())"
	@echo ""
	@echo "Back up BCTRACKER_MASTER_KEY offline and separately from your database"
	@echo "and document backups. Losing it makes every stored document unreadable."

# Postgres only, published to localhost. The app runs on the host during
# development for fast reloads. The -f pair is required: the base compose file
# keeps the database off any published port, which is correct for production but
# unreachable from a host-side app or test run.
dev-db:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db

dev-db-stop:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml stop db

migrate:
	$(PY) manage.py migrate

migrations:
	$(PY) manage.py makemigrations

run:
	$(PY) manage.py runserver 0.0.0.0:8000

superuser:
	$(PY) manage.py createsuperuser

shell:
	$(PY) manage.py shell

test:
	$(PY) -m pytest -v

lint:
	$(VENV_BIN)/ruff check .

format:
	$(VENV_BIN)/ruff format .
	$(VENV_BIN)/ruff check --fix .

# Mirrors the CI job, so a green 'make check' means a green pipeline.
check: lint
	$(VENV_BIN)/ruff format --check .
	$(PY) manage.py makemigrations --check --dry-run
	$(PY) -m pytest -q

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

# In the cron service, not web: the backups volume is mounted there and only
# there, and BACKUP_ROOT is set there and only there. Run this in web and it writes
# an encrypted copy of the whole database into the container's own filesystem,
# where it survives until the next rebuild and is on no volume anybody replicates.
backup:
	docker compose exec cron python manage.py backup_database
