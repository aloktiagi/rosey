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
COPY agent.py app.py channels.py gate.py household.py memory_tool.py \
     paths.py reminder_format.py reminders.py roster.py scheduler.py \
     summary.py telegram_bot.py tools.py transcribe.py ./

EXPOSE 8080

# Single-tenant entrypoint. telegram_bot.py auto-switches to webhook mode
# when TELEGRAM_WEBHOOK_URL is set; otherwise it long-polls. Either works
# on Fly — webhook is more efficient and is what the fly.toml below assumes.
#
# We do NOT use the Flask `app.py` entrypoint anymore — it predates the
# trigger gating, fuzzy gate, ack lookup, and per-chat lock that all live
# in telegram_bot.py.
CMD ["python", "-m", "telegram_bot"]
