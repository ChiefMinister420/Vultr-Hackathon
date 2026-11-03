FROM python:3.12-alpine
RUN adduser -D -u 10001 runner
WORKDIR /app
COPY sandbox/analyze.py /app/analyze.py
USER 10001:10001
ENTRYPOINT ["python", "/app/analyze.py"]
