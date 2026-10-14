"""Generate local owner configuration. Existing files are never overwritten."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import base64
ROOT = Path(__file__).resolve().parents[1]
def generate(root: Path, runtime: str) -> None:
    paths = [".env", "app/.env.local", "deploy/.env.api", "deploy/.env.worker", "deploy/.env.scheduler", "deploy/.env.web", "deploy/.env.agent", "deploy/hermes/engagemate-agent/.env"]
    if any((root / p).exists() for p in paths):
        raise ValueError("Configuration exists; move it aside explicitly to generate a new installation")
    dbpass = secrets.token_hex(24)
    values = {"DATABASE_URL": f"postgresql://engagemate:{dbpass}@127.0.0.1:5433/engagemate", "INTERNAL_API_KEY": secrets.token_urlsafe(48), "AGENT_API_KEY": secrets.token_urlsafe(48), "SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="), "OWNER_ACCESS_TOKEN": secrets.token_urlsafe(48), "HERMES_AGENT_API_KEY": secrets.token_urlsafe(48), "ENGAGEMATE_EXECUTION_MCP_KEY": secrets.token_urlsafe(48)}
    values["API_SERVER_KEY"] = values["HERMES_AGENT_API_KEY"]
    for name in paths:
        target = root / name
        template = root / (name + ".example")
        if name == ".env":
            content = f"ENGAGEMATE_DB_PASSWORD={dbpass}\nNEXT_PUBLIC_APP_URL=http://localhost:3030\n"
        else:
            content = template.read_text()
            lines = []
            for line in content.splitlines():
                key = line.split("=",1)[0]
                if key in values: line = f"{key}={values[key]}"
                if name == "deploy/.env.api" and key == "DATABASE_URL": line = f"DATABASE_URL=postgresql://engagemate:{dbpass}@db:5432/engagemate"
                if key == "INTELLIGENCE_PROVIDER": line = "INTELLIGENCE_PROVIDER=hermes_agent"
                if key == "HERMES_AGENT_URL": line = "HERMES_AGENT_URL=http://127.0.0.1:8657/v1/chat/completions"
                if key == "INSTAGRAM_EXECUTION_MODE": line = "INSTAGRAM_EXECUTION_MODE=disabled"
                if key == "IPROYAL_MODE": line = "IPROYAL_MODE=disabled"
                lines.append(line)
            content = "\n".join(lines) + "\n"
        target.parent.mkdir(parents=True,exist_ok=True)
        with open(target, "x", opener=lambda p,flags:os.open(p,flags,0o600)) as out: out.write(content)
    print(f"Created private configuration for your {runtime} runtime. No keys were printed.")
    print("Read OWNER_ACCESS_TOKEN from app/.env.local privately and enter it at http://localhost:3030/setup.")
    print("Complete docs/self-hosting.md before enabling Instagram execution.")
def main():
    parser=argparse.ArgumentParser();parser.add_argument("--runtime",choices=["codex","hermes"],required=True)
    args=parser.parse_args()
    try: generate(ROOT,args.runtime)
    except ValueError as e: parser.exit(1,str(e)+"\n")
    subprocess.run([sys.executable,str(ROOT/"scripts/package_extension.py")],cwd=ROOT,check=True)
    downloads=ROOT/"app/public/downloads";downloads.mkdir(parents=True,exist_ok=True)
    for archive in (ROOT/"dist").glob("engagemate-extension-*.zip"): shutil.copy2(archive,downloads/archive.name)
if __name__ == "__main__": main()
