FROM python:3.12-alpine
RUN adduser -D -u 10002 proxy
WORKDIR /app
COPY proxy/proxy.py /app/proxy.py
USER 10002:10002
EXPOSE 3128
ENTRYPOINT ["python", "/app/proxy.py"]
