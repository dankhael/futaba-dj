# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Install system dependencies (FFmpeg is required)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code to the container
COPY . .

# The command to run your bot
CMD ["python", "bot.py"]
