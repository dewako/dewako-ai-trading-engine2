# Three stages from one base. `production` (the default, last stage) contains the application only: no tests, no test tools.
# `test` adds the test suite; docker-compose.test.yml builds it. Dependencies are installed with the same pins CI tests against
# (constraints-test.txt), so the image runs the versions that were tested. Base image by tag; see docs/GITHUB_CI_SECURITY_REVIEW.md.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt constraints-test.txt ./
RUN pip install -r requirements.txt -c constraints-test.txt
RUN useradd --system --uid 10001 dewako && mkdir /state && chown dewako /state

FROM base AS test
COPY dewako ./dewako
COPY tests ./tests
USER dewako

FROM base AS production
COPY dewako ./dewako
USER dewako
EXPOSE 8000
# Liveness only (the process answers). Readiness, including whether trading protection works, is GET /health.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).status == 200 else 1)"]
CMD ["uvicorn", "dewako.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
