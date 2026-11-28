FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[otel]"
USER 10001
ENV PORT=8080 SANDBOX_MUX_CONFIG=/etc/sandbox-mux/config.yaml
EXPOSE 8080
CMD ["python", "-m", "sandbox_mux.main"]
