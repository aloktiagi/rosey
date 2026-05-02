FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY agent.py app.py channels.py household.py memory_tool.py paths.py reminder_format.py reminders.py roster.py summary.py telegram_bot.py tools.py transcribe.py ./

EXPOSE 8080

# --workers 1 so APScheduler doesn't double-fire. One household, low traffic.
# --timeout 120 because Claude tool loops can take a few seconds.
CMD ["gunicorn", "--bind", "[::]:8080", "--workers", "1", "--timeout", "120", "app:app"]
