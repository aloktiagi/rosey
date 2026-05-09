FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so Docker layer caching reuses this when
# only Python sources change.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Source files. Update this list when adding modules. Avoids COPY * which
# would also copy .env, .venv, scheduler.db, etc.
COPY agent.py alexa_handler.py app.py channels.py gate.py household.py \
     memory_tool.py paths.py reminder_format.py reminders.py roster.py \
     scheduler.py server.py summary.py telegram_bot.py tools.py transcribe.py \
     whatsapp_handler.py ./

EXPOSE 8080

# Multi-channel HTTP entrypoint. server.py exposes a Quart ASGI app
# (`asgi_app`) that hosts:
#   POST /telegram   — Telegram webhook (dispatches to PTB)
#   POST /alexa      — Alexa skill webhook (dispatches to alexa_handler)
#   GET  /health     — Fly health check
#
# We run via hypercorn so we can host both routes under one port. The
# previous `python -m telegram_bot` polling/webhook entrypoint is still
# usable for local dev (no Alexa support there).
#
# `--access-logfile - --error-logfile -` send logs to stdout so they
# fold into `fly logs`.
CMD ["hypercorn", "server:asgi_app", \
     "--bind", "0.0.0.0:8080", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
