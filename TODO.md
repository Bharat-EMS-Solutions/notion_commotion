# Actionable Messages — Production Setup Checklist

These steps are needed before the daily digest emails can be sent with
interactive hour-logging cards inside Outlook.

## 1. Make Flask server publicly reachable
The Outlook client POSTs directly to `/log-hours-action` when a user submits
the card, so the server must be reachable from the internet (or at minimum
from Microsoft's servers).

Options:
- Deploy to Azure App Service / any VPS
- Use a reverse proxy (nginx) with a public IP
- Temporary tunnel for testing: `ngrok http 5000`

Set `APP_BASE_URL` in `.env` to the public HTTPS URL once done.

## 2. Register the Actionable Message provider
1. Go to https://outlook.office.com/connectors/oam/publish
2. Sign in as tenant admin
3. Click **New Provider**
4. Set **Target URL** to `https://<your-domain>/log-hours-action`
5. Set **Scope** to "Organization" (covers all users on the tenant)
6. Copy the **Originator ID** (a GUID) — set it as `ACTIONABLE_MSG_ORIGINATOR`
   in `.env`
7. Approve the provider in the Microsoft 365 admin center under
   Settings → Integrated apps (or the connector approval workflow)

## 3. Enable JWT validation in production
Set `LOG_HOURS_DEV=false` in `.env` (or remove the variable entirely).
The `/log-hours-action` endpoint will then validate the Microsoft-signed
Bearer JWT on every submission.

The JWT `upn` claim gives the submitting user's email for the CSV log.
The `sub` claim gives their OID if needed.

## 4. Set up the daily cron job
```
# Run at 8:00 AM IST on weekdays
30 2 * * 1-5 /usr/bin/python3 /path/to/daily_digest.py
```
(8:00 IST = 02:30 UTC)
