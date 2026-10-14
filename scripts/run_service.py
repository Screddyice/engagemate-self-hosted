"""Run owner services natively with generated, scoped environment files."""
import argparse
import os
from pathlib import Path
import sys
from dotenv import dotenv_values
ROOT=Path(__file__).resolve().parents[1]
COMMANDS={"api":["-m","uvicorn","main:app","--app-dir","api","--host","127.0.0.1","--port","13100"],
 "worker":["-m","engagemate.worker.main"],"scheduler":["-m","engagemate.scheduler.main"],
 "codex":["-m","uvicorn","engagemate.intelligence.codex_gateway:app","--host","127.0.0.1","--port","8657","--no-access-log"]}
def main():
 parser=argparse.ArgumentParser();parser.add_argument("service",choices=COMMANDS);parser.add_argument("--port",type=int);args=parser.parse_args()
 if args.port is not None:
  if args.service not in {"api","codex"} or not 1024<=args.port<=65535: parser.error("--port requires api/codex and a port from 1024 to 65535")
  COMMANDS[args.service][COMMANDS[args.service].index("--port")+1]=str(args.port)
 config=dict(dotenv_values(ROOT/"deploy/.env.api"))
 config["DATABASE_URL"]=dotenv_values(ROOT/"app/.env.local")["DATABASE_URL"]
 if args.service=="worker": config.update(dotenv_values(ROOT/"deploy/.env.worker"))
 if args.service=="codex": config=dict(dotenv_values(ROOT/"deploy/.env.agent"))
 env=dict(os.environ);env.update({k:v for k,v in config.items() if v is not None});env["PYTHONPATH"]=str(ROOT/"api")
 os.chdir(ROOT);os.execvpe(sys.executable,[sys.executable,*COMMANDS[args.service]],env)
if __name__=="__main__":main()
