# Mobile Deploy Guide

## GitHub
1. Create private GitHub repo: `godmode-video-bot-v3`.
2. Upload all files from this folder.
3. Do not upload `.env` with real keys.

## Render
1. New Web Service.
2. Select GitHub repo.
3. Environment/Language: Docker.
4. Add env variables from `.env.example`.
5. Deploy.
6. After deploy, set `WEBHOOK_URL=https://your-app.onrender.com` and redeploy.

## UPI Gateway
Set callback/webhook URL:

```text
https://your-app.onrender.com/webhook/payment
```

## Testing order
1. /start
2. /balance
3. /recharge
4. payment auto-credit
5. mini audit
6. small edit video

Note: Render free 512MB may still fail for video rendering. Use VPS 4GB/8GB for real render.
