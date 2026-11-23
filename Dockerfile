# An exact version, so a rebuild cannot pull in another Python or Debian
# release unnoticed. Move it on deliberately, for security updates.
FROM python:3.11.13-slim-bookworm

WORKDIR /app

# Install Python dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY . .

# Runtime data/log/asset directories (usually mounted as volumes), owned by
# the unprivileged user the app runs as: nothing in it needs root. data/ is
# that user's alone: it holds the database and its backups.
RUN useradd --create-home --uid 10001 outreach \
    && mkdir -p data logs assets \
    && chmod 700 data \
    && chown -R outreach:outreach /app
USER outreach

# Bind to all interfaces inside the container so the host can reach it.
ENV DASHBOARD_HOST=0.0.0.0
EXPOSE 5000

# The app stamps its records by its own clock. This is for what still reads the
# host's, the log lines among them: a container otherwise runs on UTC.
ENV TZ=Europe/Rome

# docker-compose.yml runs the image read-only: no bytecode files to write.
ENV PYTHONDONTWRITEBYTECODE=1

# Default: launch the monitoring dashboard. Start the agent from the dashboard
# UI (same process), or override the command to run it standalone, e.g.:
#   docker run --env-file .env suppleco-agent python main.py run --continuous
CMD ["python", "main.py", "dashboard"]
