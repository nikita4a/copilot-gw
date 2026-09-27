FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir aiohttp

COPY . .

EXPOSE 8787

CMD ["python", "gateway.py"]
