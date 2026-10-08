FROM python:3.12-slim
WORKDIR /srv/assistant
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
RUN useradd --uid 10001 --create-home assistant && mkdir /data && chown assistant:assistant /data
ENV PLUME_DATABASE_PATH=/data/plume.sqlite3
ENV PLUME_TEAM_CONFIG_PATH=/data/team.json
USER assistant
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
