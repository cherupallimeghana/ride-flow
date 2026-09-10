.PHONY: up down logs migrate test lint fmt

up:
	docker compose up --build

down:
	docker compose down -v

logs:
	docker compose logs -f api outbox-worker celery-worker

migrate:
	docker compose run --rm api alembic upgrade head

test:
	cd backend && python -m pytest tests/ -v --cov=app --cov-report=term-missing

lint:
	cd backend && ruff check app tests

fmt:
	cd backend && ruff check app tests --fix
