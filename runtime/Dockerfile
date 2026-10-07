FROM python:3.12-slim
RUN groupadd --gid 65534 runtime 2>/dev/null || true
WORKDIR /tmp
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
CMD ["python", "--version"]
