FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --system --uid 10001 dewako && mkdir /state && chown dewako /state
USER dewako
EXPOSE 8000
CMD ["uvicorn", "dewako.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
