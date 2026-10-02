BASE_URL ?= http://localhost:8000

.PHONY: up down logs test burst

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api

test:
	docker compose up -d db
	TEST_DATABASE_URL=postgresql://seats:seats@127.0.0.1:55432/seats python3 -m pytest -q

burst:
	./burst.sh $(BASE_URL)
