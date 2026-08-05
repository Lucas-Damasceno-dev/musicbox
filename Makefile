# MusicBox — Makefile de desenvolvimento
# Comandos não-interativos: rodam com CI=true e sem prompts.

PORT ?= 8080
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: venv dev test

# Cria o ambiente virtual se não existir
venv:
	@if [ ! -d $(VENV) ]; then \
		echo "Criando ambiente virtual em $(VENV)..."; \
		python3 -m venv $(VENV); \
	fi

# Sobe o servidor de desenvolvimento: instala deps, verifica ffmpeg e roda uvicorn
dev: venv
	$(PIP) install --quiet -r requirements.txt
	@if command -v ffmpeg >/dev/null 2>&1; then \
		echo "ffmpeg encontrado: OK"; \
	else \
		echo "AVISO: ffmpeg não encontrado no PATH. Conversões de formato (ex.: mp3) podem falhar. Instale ffmpeg para continuar."; \
	fi
	CI=true $(VENV)/bin/uvicorn app.main:app --host 0.0.0.0 --port $(PORT)

# Roda a suíte de testes
test: venv
	$(PIP) install --quiet -r requirements-dev.txt
	CI=true $(PY) -m pytest
