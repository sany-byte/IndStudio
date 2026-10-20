FROM python:3.12-slim
WORKDIR /app
COPY app.py prompt_builder.py mcp_server.py fal_provider.py telegram_review_bot.py background_removal.py ./
COPY web ./web
COPY inputs ./inputs
COPY work/prompt_model.json ./work/prompt_model.json
COPY outputs/prompt_pilot ./outputs/prompt_pilot
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
CMD ["python", "app.py", "--host", "0.0.0.0"]
