FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends docker.io && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/slipstream
COPY app /srv/slipstream/app
COPY static /srv/slipstream/static
COPY sandbox /srv/slipstream/sandbox
COPY examples /srv/slipstream/examples
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "app.server"]
