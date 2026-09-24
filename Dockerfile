# Start from an official, lightweight Python base image
# "slim" = a smaller variant than the default (fewer preinstalled OS packages)
FROM python:3.11-slim

# Sets the working directory inside the container — every command below
# runs relative to this path
WORKDIR /app

# Copy just the requirements file first, not the whole app yet.
# This is a deliberate ordering trick: Docker caches each step, and this
# layer only gets rebuilt when requirements.txt itself changes — not every
# time you edit app.py. Speeds up rebuilds a lot once your app grows.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest of your actual app code
COPY . .

# Documents which port this container listens on (doesn't actually
# publish it — that happens at "docker run" time — but it's good practice)
EXPOSE 5000

# The command that runs when the container starts
CMD ["python", "app.py"]