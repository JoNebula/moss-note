.PHONY: setup doctor start test check docker-build

setup:
	./scripts/setup.sh

doctor:
	./scripts/doctor.sh

start:
	./scripts/start.sh

test:
	.venv/bin/pytest -q

check: test
	.venv/bin/python -m py_compile app/*.py
	node --check static/app.js
	for script in scripts/*.sh; do bash -n "$$script"; done

docker-build:
	docker build -t moss-note:0.2.0 .
