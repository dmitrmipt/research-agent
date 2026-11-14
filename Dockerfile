FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -c "import pypdf, pypdfium2, markdown_it, PIL"
COPY *.py ./
COPY static ./static
CMD ["python", "app.py", "--bind", "0.0.0.0", "--data-dir", "/app/data", "--materials-dir", "/app/Materials"]
