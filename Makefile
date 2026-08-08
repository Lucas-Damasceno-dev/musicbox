# MusicBox — Makefile de desenvolvimento
# Comandos não-interativos: rodam com CI=true e sem prompts.

PORT ?= 8080
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Marcador de dependências de desenvolvimento: evita reinstalar a cada execução
# (o `test`/`lint`/etc. só reinstalam quando requirements-dev.txt mudar).
DEV_STAMP := .requirements-dev.stamp

.PHONY: venv install install-dev dev test test-coverage lint format clean

# Cria o ambiente virtual se não existir
venv:
	@if [ ! -d $(VENV) ]; then \
		echo "Criando ambiente virtual em $(VENV)..."; \
		python3 -m venv $(VENV); \
	fi

# Instala as dependências de produção (setup único)
install: venv
	$(PIP) install --quiet -r requirements.txt

# Instala as dependências de desenvolvimento (inclui as de produção);
# só reinstala quando requirements-dev.txt mudar (pip é rápido quando já instalado).
install-dev: venv $(DEV_STAMP)

$(DEV_STAMP): requirements-dev.txt
	$(PIP) install --quiet -r requirements-dev.txt
	@touch $(DEV_STAMP)

# Sobe o servidor de desenvolvimento: instala deps, verifica ffmpeg e roda uvicorn
dev: install
	@if command -v ffmpeg >/dev/null 2>&1; then \
		echo "ffmpeg encontrado: OK"; \
	else \
		echo "AVISO: ffmpeg não encontrado no PATH. Conversões de formato (ex.: mp3) podem falhar. Instale ffmpeg para continuar."; \
	fi
	CI=true $(VENV)/bin/uvicorn app.main:app --host 0.0.0.0 --port $(PORT)

# Roda a suíte de testes
test: venv install-dev
	CI=true $(PY) -m pytest

# Roda a suíte com cobertura (alvo: >= 60%)
test-coverage: venv install-dev
	$(PY) -m pytest --cov=app --cov-report=term-missing

# Lint estático (ruff)
lint: venv install-dev
	$(VENV)/bin/ruff check .

# Formata o código (ruff format)
format: venv install-dev
	$(VENV)/bin/ruff format .

# Limpa artefatos gerados (caches, cobertura e o marcador de deps)
clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov $(DEV_STAMP)
	find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
