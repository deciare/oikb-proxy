FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    httpx \
    pyyaml \
    starlette \
    uvicorn

COPY proxy.py /app/proxy.py

ENV OIKB_PROXY_API_KEY=""

EXPOSE 8080

ENTRYPOINT ["python", "/app/proxy.py"]
CMD ["--config", "/data/config.yaml", "--host", "0.0.0.0", "--port", "8080"]
