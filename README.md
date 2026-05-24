# Notion Task Health Reporter

Queries one or more Notion databases and emails a formatted task health report via Microsoft 365 (Graph API). Designed to be run on a schedule (cron / Task Scheduler).

## What it reports

| Section | Description |
|---|---|
| No Due Date | Open tasks with no due date set |
| No Owner | Open tasks with no owner/assignee |
| No Reviewer | Open tasks with no reviewer (where the field exists) |
| Overdue | Tasks past their due date and not Done |
| Maximum Slippage | Top 10 tasks ranked by how many days their due date has shifted (parsed from Due Date History) |

Each task row shows: name (linked to Notion), status, priority, team tags, and a description preview.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure secrets

```bash
cp .env.example .env
```

Fill in `.env` with secrets only:

```env
NOTION_TOKEN=secret_...
NOTION_DATABASE_ID=<32-char database ID>
NOTION_DATABASE_2_ID=<32-char database ID>

AZURE_TENANT_ID=...
AZURE_CLIENT_ID=...
AZURE_CLIENT_SECRET=...
```

### 3. Configure app settings

```bash
cp config.json.example config.json
```

Fill in `config.json`:

```json
{
  "sender_email": "sender@yourdomain.com",
  "recipient_emails": [
    "recipient1@yourdomain.com",
    "recipient2@yourdomain.com"
  ]
}
```

Add as many recipients as needed. `config.json` is gitignored.

**Finding a database ID:** Open the database in Notion, copy the URL. The ID is the 32-character string *before* the `?` — not the `v=` view parameter.

### 3. Azure app registration (one-time)

1. Go to [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID → App registrations → New registration**
2. Note the **Tenant ID** and **Client ID** from the Overview page
3. Under **Certificates & secrets**, create a new client secret and copy the value immediately
4. Under **API permissions**, add `Mail.Send` (Application permission under Microsoft Graph), then click **Grant admin consent**

### 4. Share databases with your Notion integration

For each database: open it in Notion → **⋯ → Connections** → connect your integration.

### 5. Run

```bash
python main.py
```

## Adding a database

No Python changes needed — everything is driven by `databases.json` and `.env`.

**1. Add the ID to `.env`:**
```
NOTION_DATABASE_3_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**2. Add an entry to `databases.json`:**
```json
{
  "env_var": "NOTION_DATABASE_3_ID",
  "fields": {
    "title":       "Task name",
    "due_date":    "Due date",
    "status":      "Status",
    "done_value":  "Done",
    "owner":       "Assignee",
    "reviewer":    null,
    "overdue":     "Overdue",
    "history":     "Due Date History",
    "description": "Description",
    "priority":    "Priority",
    "team":        "Team",
    "project":     "Project",
    "parent_task": "Parent task"
  }
}
```

Set any field to `null` to skip that section (e.g. `"reviewer": null` omits No Reviewer).
Property names are case-sensitive — verify them in Notion via the column header → Edit property.

## Scheduling

**Linux/macOS (cron) — run weekdays at 9 AM:**
```
0 9 * * 1-5 /usr/bin/python3 /path/to/main.py
```

**Windows Task Scheduler:**
- Action: `python.exe`
- Arguments: `C:/path/to/main.py`

## Project structure

```
.
├── main.py            # Entry point — loops over databases, sends emails
├── notion_client.py   # Notion API queries, pagination, field extraction
├── mailer.py          # HTML email builder + Microsoft Graph API sender
├── requirements.txt
├── .env               # Secrets (gitignored)
└── .env.example       # Template
```
