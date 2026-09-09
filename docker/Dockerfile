FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY bootstrap/ bootstrap/
COPY tracker/ tracker/
COPY remediator/ remediator/
COPY scanner/ scanner/
COPY shared/ shared/

# Every package is copied before this: pip install reads pyproject.toml's
# package list and fails on one that is not there yet.
RUN pip install --no-cache-dir .

ENV DB_PATH=/data/state.db \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "tracker.app:app", "--host", "0.0.0.0", "--port", "8000"]
