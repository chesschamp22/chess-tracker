FROM python:3.12-slim

WORKDIR /app

# Copy app files
COPY server.py .
COPY chess-tracker.html .

# Expose port (Render/Railway inject $PORT at runtime)
EXPOSE 8765

CMD ["python3", "server.py"]
