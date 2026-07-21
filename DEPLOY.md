# Deploying KidBucks (Streamlit Community Cloud + Neon Postgres)

KidBucks runs on **SQLite locally** (no setup) and on **Postgres automatically
whenever a `DATABASE_URL` is set**. Hosting = push to GitHub, point Streamlit
Community Cloud at it, and give it your Neon connection string as a secret.

## 1. Prepare the Neon database (one-time)

Seed your Neon database with the tables (and optionally demo data). From this
folder, with your Neon connection string:

```powershell
$env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require"
python seed.py        # creates tables + a demo family, OR:
python -c "import db; db.init_db()"   # creates empty tables only
```

`seed.py` **wipes and reloads** the demo family — run it only if you want the
demo data. For a clean start, use the `init_db()` one-liner and register your
real family from the app's Register screen.

## 2. Push to GitHub

```powershell
git init
git add .
git commit -m "KidBucks"
# create an empty repo on github.com, then:
git remote add origin https://github.com/<you>/kidbucks.git
git push -u origin main
```

`.gitignore` already excludes `.streamlit/secrets.toml`, `.env`, and the local
`data/` DB — so **your Neon password is never committed**.

## 3. Deploy on Streamlit Community Cloud

1. Go to https://share.streamlit.io → **New app** → pick your repo.
2. **Main file path:** `Home.py`.
3. **Advanced → Secrets:** paste one line (this is what connects it to Neon):

   ```toml
   DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require"
   ```

4. Deploy. Streamlit installs `requirements.txt` (incl. `psycopg`) and runs the app.

## Notes

- **Data persistence:** because the app uses Neon (Postgres), data survives
  restarts/redeploys. (SQLite on a free host would NOT — it lives on ephemeral disk.)
- **Security:** family-scale auth (hashed PINs/passwords). Registration is open —
  anyone with the URL can create a family. Fine for a family app; not hardened for
  hostile traffic. **Rotate your Neon password** if it has been shared anywhere.
- **Local dev** still uses SQLite automatically (no `DATABASE_URL` → `data/kidbucks.db`).
