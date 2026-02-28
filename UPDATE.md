# Update Log

## Admin Password Functionality

**Date:** 2026-02-28
**Branch:** `cursor/admin-password-functionality-5d53`

### Overview

Added admin password protection for the interactive CLI session. The admin can set, change, and remove a password that gates access to the interactive mode on startup.

### What Changed

**File:** `issue_finder/interactive.py`

- **Password storage:** Passwords are SHA-256 hashed with a random 16-byte salt before being written to `~/.issue_finder/admin_password` (file permission `0600`).
- **Startup authentication:** When a password is set, the user must enter it before the interactive session begins. Three incorrect attempts lock out access.
- **New commands:**
  - `password` — Show password management help and current status.
  - `password set` — Set a new admin password (prompts interactively; minimum 4 characters, confirmation required).
  - `password change` — Change the existing password (requires current password first).
  - `password remove` — Remove password protection (requires current password).
  - `password status` — Check whether a password is currently set.
  - `set password` — Alias for `password set`.
  - `update password` — Alias for `password change`.
  - `unset password` — Alias for `password remove`.
- **Settings display:** The `settings` command now shows password status.
- **Banner:** The startup banner now shows whether password protection is enabled.
- **Help:** The `help` command includes the new Admin Password section.

### Security Details

- Passwords are never stored in plaintext; only a salted SHA-256 hash is persisted.
- Each password save generates a fresh random salt via `secrets.token_hex(16)`.
- Verification uses `secrets.compare_digest` to prevent timing attacks.
- The password file is created with `0600` permissions (owner read/write only).
- `getpass.getpass` is used for all password prompts so input is not echoed to the terminal.

### How to Use

```
# Start interactive mode
python -m issue_finder -i

# Set a password (inside the interactive session)
issue-finder> password set

# Change the password
issue-finder> password change

# Remove the password
issue-finder> password remove

# Check status
issue-finder> password status
```

On subsequent launches with a password set, you will be prompted:

```
Admin password (1/3): ****
```

Three incorrect attempts will deny access.
