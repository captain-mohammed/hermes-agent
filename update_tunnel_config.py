"""Read tunnel URLs from cloudflared logs and write dashboard.public_url + CORS.

Invoked from start-stash.bat after tunnels come up. Parses the log files
directly so no shell quoting is needed.
"""
import io
import os
import re
import yaml

CONFIGS = [
    r"C:/Users/angel/.hermes/config.yaml",
    r"C:/Users/angel/AppData/Local/hermes/config.yaml",
]
ENV_PATH = r"C:/Users/angel/AppData/Local/hermes/.env"
LOG_9119 = r"G:/LLMAPI/cf-9119.log"
LOG_8642 = r"G:/LLMAPI/cf-8642.log"
PASSWORD_HASH = "scrypt$16384$8$1$fALRNv5szu6xd5DwsEjavQ==$Kmc60mA5weaZLT/dA8wj/ti2ZkQdAk50HCfE95Hk0OQ="
API_KEY = "ea98a07e53a4a62b3e8d72aaabc1da6e4f3bfd2ba29daea78c04f1d2f6af0510"

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def read_tunnel_url(log_path: str) -> str:
    """Return the MOST RECENT tunnel URL from a cloudflared log.

    Cloudflared appends to the logfile across runs, so the file can contain
    URLs from earlier sessions; the active tunnel is the last one written.
    """
    try:
        with io.open(log_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except FileNotFoundError:
        return ""
    matches = _URL_RE.findall(content)
    return matches[-1] if matches else ""


def main() -> int:
    dash_url = read_tunnel_url(LOG_9119)
    api_url = read_tunnel_url(LOG_8642)
    if not dash_url:
        print("WARN: no dashboard tunnel URL found in", LOG_9119)
    if not api_url:
        print("WARN: no API tunnel URL found in", LOG_8642)

    origins = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://stash-os.vercel.app",
        "*",
    ]
    if dash_url:
        origins.append(dash_url)
    if api_url:
        origins.append(api_url)
    # De-dupe preserving order
    seen = set()
    origins = [o for o in origins if not (o in seen or seen.add(o))]

    for path in CONFIGS:
        with io.open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        cfg.setdefault("dashboard", {})
        if dash_url:
            cfg["dashboard"]["public_url"] = dash_url
        cfg["dashboard"].setdefault("basic_auth", {})
        cfg["dashboard"]["basic_auth"]["username"] = "admin"
        cfg["dashboard"]["basic_auth"]["password_hash"] = PASSWORD_HASH
        extra = cfg.setdefault("platforms", {}).setdefault("api_server", {}).setdefault("extra", {})
        extra["cors_origins"] = origins
        extra["key"] = API_KEY
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        print("updated", path)

    try:
        with io.open(ENV_PATH, encoding="utf-8") as f:
            env = f.read()
        env = re.sub(
            r"(?m)^API_SERVER_CORS_ORIGINS=.*$",
            "API_SERVER_CORS_ORIGINS=" + ",".join(origins),
            env,
        )
        with io.open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write(env)
        print("updated", ENV_PATH)
    except FileNotFoundError:
        print("skipped (no AppData .env)")

    print("dashboard_url =", dash_url or "(none)")
    print("api_url =", api_url or "(none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())