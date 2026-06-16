#!/bin/bash
set -e
docker stop godmode-video-bot-v3 || true
docker rm godmode-video-bot-v3 || true
docker build -t godmode-video-bot-v3 .
docker run -d --name godmode-video-bot-v3 --restart unless-stopped --env-file .env -p 8080:8080 -v $(pwd)/data:/data -v $(pwd)/downloads:/app/downloads godmode-video-bot-v3
