FROM python:3.13.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/autohistory
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home app
COPY app ./app
COPY migrations ./migrations
COPY assets ./assets
RUN mkdir -p /var/lib/autohistory/reports /var/lib/autohistory/deletions && chown -R app:app /var/lib/autohistory
USER app
CMD ["python", "-m", "app.bot"]
