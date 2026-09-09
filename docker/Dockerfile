FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY bootstrap/ bootstrap/
COPY control_plane/ control_plane/
COPY remediator/ remediator/
COPY scanner/ scanner/
COPY shared/ shared/

# Dependencies are declared once, in pyproject.toml — there is no
# requirements.txt to keep in step with it.
RUN pip install --no-cache-dir .

ENV DB_PATH=/data/state.db \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "control_plane.app:app", "--host", "0.0.0.0", "--port", "8000"]
