
all:
	make api

build:
	docker build -t ghcr.io/opensanctions/yente:latest .

shell: build
	docker compose run --rm app bash

stop:
	docker compose down

services:
	docker compose -f docker-compose.yml up --remove-orphans -d index

api: build services
	docker compose up --remove-orphans app

test:
	poetry run pytest --cov-report html --cov-report term --cov=yente -v tests

typecheck:
	poetry run mypy --strict yente

check: typecheck test