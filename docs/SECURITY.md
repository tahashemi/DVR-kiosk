# DVR Kiosk Security & Hardening Guide

Security is a first-class priority in DVR Kiosk. This document outlines the security architecture, threat model, credential management, and reverse proxy guidelines.

---

## 1. Security Architecture & Threat Model

```
                    Internet / LAN
                          │
                          ▼ (HTTPS :443)
            ┌───────────────────────────┐
            │ pfSense / HAProxy / Nginx │  <-- TLS Termination & WAF
            └─────────────┬─────────────┘
                          │ (Plain HTTP :80)
                          ▼
            ┌───────────────────────────┐
            │  DVR Kiosk SBC (Port 80)  │  <-- fail2ban & Token Auth
            └───────────────────────────┘
```

1. **Lightweight Edge Design**:
   - The SBC runs plain HTTP on port 80, eliminating CPU-heavy TLS encryption overhead on embedded ARM processors.
   - All TLS termination is offloaded to your network firewall or reverse proxy (e.g., pfSense HAProxy, Nginx, Traefik).
2. **Reverse-Proxy Awareness**:
   - `dvr_control.py` inspects `X-Forwarded-Proto` and `X-Forwarded-For` headers. When upstream TLS is detected, session cookies automatically enforce `Secure` and `SameSite=Lax` attributes.

---

## 2. Authentication & Session Management

- **Cryptographic Password Hashing**: Passwords are saved with SHA-256 and secure random salts in `/etc/dvr-kiosk/auth_config.json`.
- **Session Tokens**: Logged-in sessions generate random 256-bit cryptographically secure session tokens stored with timestamped expiry.
- **Fail2ban Protection**: `fail2ban` automatically monitors failed login attempts and SSH brute force attacks, banning abusive IPs at the firewall layer.

---

## 3. Zero-Credential Leak Rules & Best Practices

1. **Never Commit Secrets**:
   - Git tracked files must NEVER contain real passwords, tokens, private keys, or internal camera IP addresses.
   - Use `dvr_config.py.example` or `.env.example` templates for version control.
2. **Local Credential Storage**:
   - Live DVR configurations are stored locally on the target device in `/etc/dvr-kiosk/dvr_config.json` or `/root/dvr_config.py` with `chmod 600` permissions.
3. **Pre-Push Sanitization**:
   - Always run pre-commit or pre-push verification to audit for leaked tokens or IP patterns before publishing upstream.

---

## 4. Recommended Reverse Proxy Configurations

### pfSense / HAProxy Example
- **Frontend**: Bind `WAN:443` or `LAN:443`, select SSL Offloading certificate.
- **Backend**: Point to SBC IP address `http://192.168.1.100:80`.
- **Headers**: Check `Add X-Forwarded-Proto` and `Add X-Forwarded-For`.

### Nginx Example
```nginx
server {
    listen 443 ssl http2;
    server_name kiosk.example.local;

    ssl_certificate /etc/letsencrypt/live/kiosk/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/kiosk/privkey.pem;

    location / {
        proxy_pass http://192.168.1.100:80;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
```
