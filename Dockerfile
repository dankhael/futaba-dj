# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Install system dependencies (FFmpeg is required)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime to solve YouTube's signature/n challenges
# (yt-dlp wiki/EJS); without it every format is dropped and extraction
# fails with "The page needs to be reloaded". Deno is the runtime yt-dlp
# recommends; the solver script itself comes from the yt-dlp[default] extra.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code to the container
COPY . .

# The command to run your bot
CMD ["python", "bot.py"]
