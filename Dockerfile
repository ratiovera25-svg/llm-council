FROM node:20-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

# Copy the whole project (pyproject.toml is in root)
COPY pyproject.toml uv.lock* ./
COPY backend/ ./backend/

# Install dependencies from project root
RUN uv sync

# Copy built frontend
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# Create data directory
RUN mkdir -p /app/data/conversations

EXPOSE 8000

# Run from project root (not backend dir)
CMD ["sh", "-c", "uv run uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
