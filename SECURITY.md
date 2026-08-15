# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in nanoinfra, please report it by:

1. **DO NOT** open a public GitHub issue
2. Create a private security advisory on GitHub (https://github.com/nanoinfraorg/nanoinfra/security/advisories) or contact the maintainer directly: Alberto Ferrer <albertof@barrahome.org>
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We aim to respond to security reports within 48 hours.

## Security Best Practices

### 1. API Key Management

**CRITICAL**: Never commit API keys to version control.

```bash
# ✅ Best: Use environment variable references in config (never writes the key to disk)
# In ~/.nanoinfra/config.json:
#   "apiKey": "${ANTHROPIC_API_KEY}"
# Then supply the key at runtime via env var or Docker secret.

# ✅ Good: Store in config file with restricted permissions
chmod 600 ~/.nanoinfra/config.json

# ❌ Bad: Hardcoding keys in code or committing them
```

**Recommendations:**
- **Prefer environment variable references** (`${VAR}`) in config — the config file stores the `${VAR}` placeholder, and the plaintext value only exists in memory at runtime. See [Configuration: Environment Variables for Secrets](https://docs.nanoinfra.org/configuration/#environment-variables-for-secrets) for details.
- When plaintext keys are stored in `~/.nanoinfra/config.json`, set file permissions to `0600` (`chmod 600`)
- Consider using an OS keyring/credential manager for production deployments
- Rotate API keys regularly
- Use separate API keys for development and production

### 2. Channel Access Control

**IMPORTANT**: Always configure `allowFrom` lists for production use.

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["123456789", "987654321"]
    },
    "whatsapp": {
      "enabled": true,
      "allowFrom": ["1234567890"]
    }
  }
}
```

**Security Notes:**
- In `v0.1.4.post3` and earlier, an empty `allowFrom` allowed all users. Since `v0.1.4.post4`, empty `allowFrom` denies all access by default — set `["*"]` to explicitly allow everyone.
- Get your Telegram user ID from `@userinfobot`
- Use WhatsApp sender IDs as full phone numbers with country code and no leading `+`
- Review access logs regularly for unauthorized access attempts

**Reachability, not authority:** `allowFrom` (`nanoinfra/channels/base.py:217`) and the pairing store (`nanoinfra/pairing/store.py`) decide who can reach the bot. They do not decide who can approve a privileged action. Operators put teammates in `allowFrom` so that those teammates can send messages to the bot. The pairing store adds an approved sender at runtime, after the bot delivers a code in chat. An approval decision for a privileged action must read neither list. Do not call `is_allowed()` or `is_approved()` to authorize an action.

### 3. Shell Command Execution

The `exec` tool runs shell commands. The tool refuses a small set of literal destructive patterns (`nanoinfra/agent/tools/shell.py`). That check is not a security boundary. A model that writes shell can express the same effect in a form that the pattern list does not hold. Treat the check as a guard against an accident. Then apply these controls:

- ✅ **Enable the bwrap sandbox** (`"tools.exec.sandbox": "bwrap"`) for kernel-level isolation (Linux only)
- ✅ Review all tool usage in agent logs
- ✅ Understand what commands the agent is running
- ✅ Use a dedicated user account with limited privileges
- ✅ Never run nanoinfra as root
- ❌ Don't disable security checks
- ❌ Don't run on systems with sensitive data without careful review

**Exec sandbox (bwrap):**

On Linux, set `"tools.exec.sandbox": "bwrap"` to wrap every shell command in a [bubblewrap](https://github.com/containers/bubblewrap) sandbox. This uses Linux kernel namespaces to restrict what the process can see:

- Workspace directory → **read-write** (agent works normally)
- Media directory → **read-only** (can read uploaded attachments)
- System directories (`/usr`, `/bin`, `/lib`) → **read-only** (commands still work)
- Config files and API keys (`~/.nanoinfra/config.json`) → **hidden** (masked by tmpfs)

**The sandbox does not cover remote execution.** bwrap confines the gateway process. bwrap does not constrain remote execution. The command `ssh prod-db 'systemctl stop postgres'` meets every bwrap constraint. The sandbox creates no network namespace, so a command inside the sandbox still reaches the network.

The `execute_on_server` tool opens its connection from the gateway process itself. `wrap_command` (`nanoinfra/agent/tools/sandbox.py`) never wraps that connection. A workspace bind, a masked config directory, and a read-only `/usr` do not limit a host that the agent reaches over SSH. The strongest control in this document does not cover the strongest available action.

Requires `bwrap` installed (`apt install bubblewrap`). Pre-installed in the official Docker image. **Not available on macOS or Windows** — bubblewrap depends on Linux kernel namespaces.

Enabling the sandbox also automatically activates `restrictToWorkspace` for file tools.

**Patterns the `exec` tool refuses** (`nanoinfra/agent/tools/shell.py`):
- Recursive removal (`rm -r`, `rm -rf`, `rm -fr`) against any target, not only `/`
- Fork bombs
- Filesystem format commands (`mkfs`, `diskpart`, standalone `format`)
- Raw disk writes (`dd if=`, redirects to `/dev/sd*`)
- System power commands (`shutdown`, `reboot`, `poweroff`)
- Direct writes to nanoinfra internal state files

The list holds literal patterns. Another spelling of the same command passes the check. The list applies to the local `exec` tool only. `execute_on_server` does not consult it. See [Known Limitations](#known-limitations).

### 4. File System Access

File operations have path traversal protection, but:

- ✅ Enable `restrictToWorkspace` or the bwrap sandbox to confine file access
- ✅ Run nanoinfra with a dedicated user account
- ✅ Use filesystem permissions to protect sensitive directories
- ✅ Regularly audit file operations in logs
- ❌ Don't give unrestricted access to sensitive files

### 5. Network Security

**API Calls:**
- All external API calls use HTTPS by default
- Timeouts are configured to prevent hanging requests
- The OpenAI-compatible API server must set `api.api_key` when binding to `0.0.0.0` or `::`; otherwise startup fails to prevent unauthenticated network access
- Consider using a firewall to restrict outbound connections if needed

**WhatsApp:**
- Keep the neonize session database under `~/.nanoinfra/whatsapp-auth` secure (mode 0700).
- Use `nanoinfra channels login whatsapp --force` to remove and recreate the local session database when rotating linked devices.

### 6. Dependency Security

**Critical**: Keep dependencies updated!

```bash
# Check for vulnerable dependencies
pip install pip-audit
pip-audit

# Update to latest secure versions
pip install --upgrade nanoinfra
```

**Important Notes:**
- Keep `litellm` updated to the latest version for security fixes
- Run `pip-audit` regularly after enabling the channels used in production; their manifest-declared dependencies are installed into the same environment
- Subscribe to security advisories for nanoinfra and its dependencies

### 7. Production Deployment

For production use:

1. **Isolate the Environment**
   ```bash
   # Run in a container or VM
   docker run --rm -it python:3.11
   pip install nanoinfra
   ```

2. **Use a Dedicated User**
   ```bash
   sudo useradd -m -s /bin/bash nanoinfra
   sudo -u nanoinfra nanoinfra gateway
   ```

3. **Set Proper Permissions**
   ```bash
   chmod 700 ~/.nanoinfra
   chmod 600 ~/.nanoinfra/config.json
   chmod 700 ~/.nanoinfra/whatsapp-auth
   ```

4. **Enable Logging**
   ```bash
   # Configure log monitoring
   tail -f ~/.nanoinfra/logs/nanoinfra.log
   ```

5. **Use Rate Limiting**
   - Configure rate limits on your API providers
   - Monitor usage for anomalies
   - Set spending limits on LLM APIs

6. **Regular Updates**
   ```bash
   # Check for updates weekly
   pip install --upgrade nanoinfra
   ```

### 8. Development vs Production

**Development:**
- Use separate API keys
- Test with non-sensitive data
- Enable verbose logging
- Use a test Telegram bot

**Production:**
- Use dedicated API keys with spending limits
- Restrict file system access
- Monitor the application log, because nanoinfra ships no audit log yet (see [Known Limitations](#known-limitations))
- Regular security reviews
- Monitor for unusual activity

### 9. Data Privacy

- **Logs may contain sensitive information** - secure log files appropriately
- **LLM providers see your prompts** - review their privacy policies
- **Chat history is stored locally** - protect the `~/.nanoinfra` directory
- **API keys are in plain text** - use OS keyring for production

### 10. Incident Response

If you suspect a security breach:

1. **Immediately revoke compromised API keys**
2. **Review logs for unauthorized access**
   ```bash
   grep "Access denied" ~/.nanoinfra/logs/nanoinfra.log
   ```
3. **Check for unexpected file modifications**
4. **Rotate all credentials**
5. **Update to latest version**
6. **Report the incident** to maintainers

## Security Features

### Built-in Security Controls

✅ **Input Validation**
- Path traversal protection on file operations
- Input length limits on HTTP requests
- Remote execution targets validated by `nanoinfra/servers/network_guard.py`, which blocks loopback, link-local, cloud metadata, and the unspecified address. The guard allows RFC1918 on purpose. The `ssm` provider dials no address and relies on IAM instead

✅ **Authentication**
- Allow-list based access control — in `v0.1.4.post3` and earlier empty `allowFrom` allowed all; since `v0.1.4.post4` it denies all (`["*"]` explicitly allows all)
- Failed authentication attempt logging
- The allow-list and the pairing store control reachability only. They authorize no action. See [Channel Access Control](#2-channel-access-control)

✅ **Resource Protection**
- Command execution timeouts (60s default)
- Output truncation (10KB limit)
- HTTP request timeouts (10-30s)

✅ **Secure Communication**
- HTTPS for all external API calls
- TLS for Telegram API
- WhatsApp session secrets stay in the local session database

## Known Limitations

⚠️ **Current Security Limitations:**

1. **No Rate Limiting** - Users can send unlimited messages (add your own if needed)
2. **Plain Text Config** - API keys stored in plain text in `config.json` (prefer `${VAR}` env references when possible, or use keyring for production)
3. **No Session Management** - No automatic session expiry
4. **Command Pattern Match Is Not A Boundary** - The `exec` tool refuses a small set of literal destructive patterns (`nanoinfra/agent/tools/shell.py`). This is not a boundary against a model that composes shell. `rm -rf /`, fork bombs, and `mkfs.*` are all expressible in forms that a matcher does not catch. The list applies to the local `exec` tool only, and `execute_on_server` does not consult it. Enable the bwrap sandbox for kernel-level isolation of local commands on Linux.
5. **No Audit Trail** - Security event logs are limited. nanoinfra writes the decision that a capability gate would make into the application log (`nanoinfra/agent/tools/capabilities.py`). nanoinfra enforces no gate decision today. No append-only audit store exists yet, and no retention setting exists for one.
6. **No Gate On Remote Execution** - `execute_on_server` is the highest-consequence tool in the system. The tool defaults to `dry_run=true`, and its description tells the model to get an explicit user confirmation first. That default is a model-visible convention, not an enforced control. Neither bwrap nor the `exec` pattern list applies to this path. The network guard blocks loopback, link-local, and metadata targets only, and it allows RFC1918 on purpose.

## Security Checklist

Before deploying nanoinfra:

- [ ] API keys stored securely (not in code)
- [ ] Config file permissions set to 0600
- [ ] `allowFrom` lists configured for all channels
- [ ] Running as non-root user
- [ ] Exec sandbox enabled (`"tools.exec.sandbox": "bwrap"`) on Linux deployments — this covers local commands only
- [ ] File system permissions properly restricted
- [ ] Dependencies updated to latest secure versions
- [ ] Logs monitored for security events
- [ ] Rate limits configured on API providers
- [ ] Backup and disaster recovery plan in place
- [ ] Security review of custom skills/tools
- [ ] `gates.unattended` reviewed for every unattended context (cron automations, sustained goals, subagents)
- [ ] Standing grants scoped to non-production hosts where possible
- [ ] Audit retention set for the gate records

nanoinfra does not enforce the capability gate yet. The gate proposal (nanoinfraorg/nanoinfra#2) defines `gates.unattended`, the standing grants, and the audit records. Review the last three items when your release ships those settings.

## Updates

**Last Updated**: 2026-08-14

For the latest security updates and announcements, check:
- GitHub Security Advisories: https://github.com/nanoinfraorg/nanoinfra/security/advisories
- Release Notes: https://github.com/nanoinfraorg/nanoinfra/releases

## License

See LICENSE file for details.
