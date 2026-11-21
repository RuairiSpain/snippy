# Until foundry-finops is on PyPI, build from the monorepo root instead:
#   docker build -f foundry-finops-cowork/Dockerfile .  (and add: COPY foundry-finops /sdk ; RUN pip install /sdk)
FROM python:3.12-slim
WORKDIR /app
COPY mcp_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mcp_server ./mcp_server
ENV HOST=0.0.0.0 PORT=8000
EXPOSE 8000
USER 1000
CMD ["python", "-m", "mcp_server.app"]
