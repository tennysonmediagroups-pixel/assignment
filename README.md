# Assignment Service (Vercel-ready)

FastAPI backend + static frontend. Runs on Vercel and locally.

```
main.py            FastAPI app (Vercel detects it automatically)
public/            index.html, skeleton.js  -> served by Vercel's CDN
requirements.txt   Python dependencies
vercel.json        security headers
.env.example       environment variables
```

## Why storage changed
Vercel Functions have a read-only, temporary filesystem, so `data/*.json` and `uploads/` cannot persist there.
- **Data** (users, requests) -> **Upstash Redis** (Vercel Marketplace)
- **Files** -> **Vercel Blob, private store** (only the owning student and the admin can download)

## Deploy
1. Push this folder to GitHub (`.env*`, `data/`, `uploads/` are git-ignored).
2. Import the repo at vercel.com/new. Vercel detects FastAPI; no build settings needed.
3. **Storage tab -> Create**:
   - **Upstash Redis** (connect to the project; adds `KV_REST_API_URL` / `KV_REST_API_TOKEN`).
   - **Blob**, access **Private** (adds `BLOB_STORE_ID`). Access mode cannot be changed later.
4. **Settings -> Environment Variables**, add for Production (and Preview if used):
   - `ADMIN_EMAIL`
   - `ADMIN_PASSWORD` (long and unique)
   - `SESSION_SECRET` (32+ random chars: `python -c "import secrets; print(secrets.token_hex(32))"`)
5. Redeploy. Check `https://<your-app>.vercel.app/api/health` -> `{"db":"redis","files":"blob",...}`.

The app refuses to start on Vercel if the three variables are missing.

## Local development
```
pip install -r requirements.txt
cp .env.example .env.local     # fill in values (optional)
python main.py                 # http://127.0.0.1:8000
```
Without Redis/Blob variables it stores data in `./data` and `./uploads`. If `ADMIN_PASSWORD` is unset, a temporary one is printed at startup.
To test with the real services: `vercel link && vercel env pull .env.local`, then `vercel dev`.

## Limits and notes
- **Uploads are limited to 4 MB per file.** Vercel rejects request bodies over 4.5 MB. The page uploads files one at a time. For larger files you would need Blob client uploads (JavaScript SDK).
- Allowed file types: pdf, doc(x), ppt(x), xls(x), jpg, png, heic, webp, zip, txt. Downloads are always sent as attachments.
- Login is throttled: 10 failed attempts per IP per 15 minutes.
- Sessions are signed HttpOnly cookies (Secure on Vercel).
- Redis is fine for a prototype. Requests are stored one record per key field, but there is no locking, so two admins editing the same request at the same instant could overwrite each other.
- Use separate Redis/Blob stores for Production and Preview so previews can't touch real data.
