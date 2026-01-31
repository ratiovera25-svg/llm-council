FROM node:20-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy backend and install dependencies
COPY backend/ ./backend/
WORKDIR /app/backend
RUN uv sync

# Copy built frontend
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

# Create data directory for conversations
RUN mkdir -p /app/data/conversations

WORKDIR /app/backend

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
