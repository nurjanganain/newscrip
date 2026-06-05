# AMD Developer Cloud — Registration & Credit Pipeline

Automated pipeline for AMD Developer Cloud account registration, email
verification, Okta login, and GPU free-credit requests.

## Features

| Feature | Details |
|---------|---------|
| **Full AMD Pipeline** | Register → Activate → Okta Login → Credit Request |
| **Gmail Dot Trick** | Generate 2 047 unique email variants from one inbox |
| **Proxy Rotation** | Webshare datacenter proxies (configurable) |
| **IMAP Automation** | Polls activation tokens & OTP codes automatically |
| **Anti-Detection** | CloakBrowser with fingerprint rotation |
| **Structured Logging** | Console + rotating file log (`data/run.log`) |
| **Config Validation** | Fails fast with clear error on missing keys |

## Quick Start

```bash
# 1. Clone
git clone https://github.com/nurjanganain/newscrip.git
cd newscrip

# 2. Install
pip install -r requirements.txt

# 3. Configure
cp config.example.json config.json
# Edit config.json — see "Configuration" below

# 4. Run
python3 amdregister.py --count 5          # Register 5 accounts
python3 amdregister.py --email a@b.com    # Register one specific email
```

## Scripts

### `amdregister.py` — Main Pipeline

| Step | Action | Method |
|------|--------|--------|
| 1 | Register account on www.amd.com | CloakBrowser + proxy |
| 2 | Fetch activation token | IMAP polling |
| 3 | Activate account (set password) | CloakBrowser |
| 4 | Login via Okta PKCE flow | HTTP (requests) |
| 5 | Submit free-credit request | Marketo form (CloakBrowser) |

```bash
python3 amdregister.py --count 3
python3 amdregister.py --email user@domain.com --name "Erik Hansen" --company "MIT" --country US
```

### `do_activate.py` — DigitalOcean Activation

```bash
python3 do_activate.py --input do_pending.json
python3 do_activate.py --email user@domain.com
python3 do_activate.py --all
```

## Configuration

Copy `config.example.json` to `config.json` and fill in:

| Field | Required | Description |
|-------|----------|-------------|
| `password` | ✓ | Shared password for all accounts (10+ chars, mixed case + number + symbol) |
| `imap_host` | ✓ | IMAP server — `imap.gmail.com` for Gmail |
| `imap_user` | ✓ | Gmail address for receiving verification emails |
| `imap_password` | ✓ | Gmail App Password (16-char, from Google Account settings) |
| `email_domain` | | Domain part of generated emails (e.g. `gmail.com`) |
| `gmail_plus_base` | | Gmail username without `@domain` — enables dot-trick variants |
| `proxy_list` | | Array of `{host, port, user, pass}` proxy entries |

### Gmail App Password Setup

1. Enable **2-Step Verification**: https://myaccount.google.com/signinoptions/two-step-verification
2. Create App Password: https://myaccount.google.com/apppasswords
3. Name it "AMD Script" → copy the 16-character password into `imap_password`
4. Enable IMAP: Gmail → Settings → Forwarding and POP/IMAP → Enable IMAP

### Gmail Dot Trick

When `gmail_plus_base` is set (e.g. `angelabriptu`), the script generates unique
email addresses by inserting dots: `a.ngelabriptu`, `an.gelabriptu`,
`a.n.gelabriptu`, etc. Gmail ignores dots, so all variants deliver to the same
inbox. AMD sees them as different addresses. One base username with 12 characters
yields **2 047 unique variants**.

## Output

| File | Contents |
|------|----------|
| `success.txt` | Registered accounts — `email:password:date` |
| `data/run.log` | Detailed debug log with timestamps |
| `data/*.png` | Debug screenshots on failure |

## Requirements

- Python 3.10+
- [CloakBrowser](https://pypi.org/project/cloakbrowser/) — stealth Playwright wrapper
- Gmail account with IMAP + App Password
- (Optional) Proxy list for IP rotation

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  amdregister.py                                              │
│                                                              │
│  ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐     │
│  │ Step 1  │──▶│ Step 2  │──▶│ Step 3  │──▶│ Step 4  │──┐  │
│  │Register │   │ Token   │   │Activate │   │ Okta    │  │  │
│  │(Browser)│   │ (IMAP)  │   │(Browser)│   │ (HTTP)  │  │  │
│  └─────────┘   └─────────┘   └─────────┘   └─────────┘  │  │
│                                                          ▼  │
│                                              ┌─────────┐    │
│                                              │ Step 5  │    │
│                                              │ Credit  │    │
│                                              │(Marketo)│    │
│                                              └─────────┘    │
│                                                              │
│  Config: config.json    Proxies: Webshare    Logs: data/    │
└──────────────────────────────────────────────────────────────┘
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **IMAP login failed** | Check App Password; ensure IMAP enabled in Gmail |
| **Registration timeout** | Proxy may be blocked by Cloudflare; try different proxy |
| **OTP not received** | Check IMAP user matches recipient; check spam |
| **Credit form failed** | Marketo fields may have changed; check form HTML |
| **"Already registered"** | Email already used — script will fail at step 1 |

## Important Notes

- **Credit requests ≠ instant credits.** The Marketo form submits a *request*.
  AMD reviews and approves credits manually (1–2 business days).
- **Bulk accounts may be rejected.** AMD may deny credit requests from multiple
  accounts sharing similar identity signals (same inbox, IP, etc.).
- **Proxies are datacenter IPs.** Some sites (Cloudflare) may block them.
  Residential proxies improve success rate but are not free.

## License

MIT
