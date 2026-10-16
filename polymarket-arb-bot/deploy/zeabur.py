"""Deploy or update polyarb on Zeabur with one command.

    export ZEABUR_TOKEN=...                      # Zeabur -> Settings -> Developer -> API keys
    python deploy/zeabur.py --region hkg1        # shared Hong Kong region
    python deploy/zeabur.py --server <server-id> # your own Zeabur dedicated server

Re-running is safe: the project and service are reused by name, variables are only added when
missing (a POLYARB_MODE=live you set in the Zeabur dashboard is never reverted), then the current
code is uploaded and the script waits for /healthz and prints the go-live checklist the server
reports. Live secrets (POLYMARKET_PRIVATE_KEY, ...) never pass through this script: paste them
into the Zeabur dashboard. Needs Node.js for the official Zeabur CLI (npx), which does the upload.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import secrets
import shutil
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.zeabur.com/graphql"
CLI = "zeabur@0.22.2"
ROOT = Path(__file__).resolve().parent.parent
TOP_SKIP = {"tests", "research", "data", "deploy"}
ANY_SKIP = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules"}
SKIP_FILES = {".env", "KILL"}
DEFAULT_VARS = {"POLYARB_MODE": "paper", "POLYARB_DATA_DIR": "/data", "PORT": "8080"}
FINAL = {"RUNNING", "FAILED", "CRASHED"}
SETTLING = {"REMOVED", "CANCELED", "UNKNOWN"}  # briefly reported while a new deployment is being scheduled


def gql(token: str, query: str, **variables):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "polyarb-deploy"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        out = json.loads(exc.read() or b"{}")
    if out.get("errors") or "data" not in out:
        raise RuntimeError((out.get("errors") or [{"message": str(out)}])[0]["message"])
    return out["data"]


def suffix(n: int = 6) -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def find_or_create_project(token: str, name: str, region: str) -> tuple[str, str]:
    projects = gql(token, "{ projects { edges { node { _id name region { id } environments { _id name } } } } }")
    for edge in projects["projects"]["edges"]:
        p = edge["node"]
        if p["name"] == name and (p["region"] or {}).get("id") == region:
            print(f"沿用專案 {name} ({p['_id']})")
            break
    else:
        p = gql(token, "mutation($n:String,$r:String){ createProject(name:$n, region:$r){ _id environments { _id name } } }", n=name, r=region)["createProject"]
        print(f"已建立專案 {name} ({p['_id']}) 於 {region}")
    envs = p["environments"]
    env = next((e for e in envs if e["name"] == "production"), envs[0])
    return p["_id"], env["_id"]


def find_or_create_service(token: str, project: str, name: str) -> str:
    services = gql(token, "query($p:ObjectID!){ project(_id:$p){ services { _id name } } }", p=project)["project"]["services"]
    for s in services:
        if s["name"] == name:
            print(f"沿用服務 {name} ({s['_id']})")
            return s["_id"]
    sid = gql(token, "mutation($p:ObjectID!,$n:String!){ createService(projectID:$p, template: GIT, name:$n){ _id } }", p=project, n=name)["createService"]["_id"]
    print(f"已建立服務 {name} ({sid})")
    return sid


def ensure_variables(token: str, service: str, env: str, wanted: dict[str, str], overrides: dict[str, str]) -> None:
    existing = {v["key"] for v in gql(token, "query($s:ObjectID!,$e:ObjectID!){ service(_id:$s){ variables(environmentID:$e){ key } } }", s=service, e=env)["service"]["variables"]}
    for key, value in {**wanted, **overrides}.items():
        if key in existing and key not in overrides:
            continue
        if key in existing:
            gql(token, "mutation($s:ObjectID!,$e:ObjectID!,$k:String!,$v:String!){ updateSingleEnvironmentVariable(serviceID:$s, environmentID:$e, oldKey:$k, newKey:$k, value:$v){ key } }", s=service, e=env, k=key, v=value)
        else:
            gql(token, "mutation($s:ObjectID!,$e:ObjectID!,$k:String!,$v:String!){ createEnvironmentVariable(serviceID:$s, environmentID:$e, key:$k, value:$v){ key } }", s=service, e=env, k=key, v=value)
        shown = "******" if "PASSWORD" in key or "KEY" in key else value
        print(f"  變數 {key}={shown}")


def read_variable(token: str, service: str, env: str, key: str) -> str | None:
    rows = gql(token, "query($s:ObjectID!,$e:ObjectID!){ service(_id:$s){ variables(environmentID:$e){ key value } } }", s=service, e=env)["service"]["variables"]
    return next((v["value"] for v in rows if v["key"] == key), None)


def stage_source(dest: Path) -> int:
    """Copy what git would ship (tracked + untracked-but-not-ignored), minus tests/research/data."""
    try:
        listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        listed = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()]
    count = 0
    for rel in listed:
        parts = Path(rel).parts
        if not parts or parts[0] in TOP_SKIP or ANY_SKIP & set(parts) or Path(rel).name in SKIP_FILES:
            continue
        src = ROOT / rel
        if not src.is_file():
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / rel)
        count += 1
    return count


def upload(token: str, service: str, env: str) -> None:
    npx = shutil.which("npx")
    if npx is None:
        raise SystemExit("找不到 npx：請先安裝 Node.js 18+（Zeabur 官方 CLI 需要）")
    with tempfile.TemporaryDirectory(prefix="polyarb-deploy-") as tmp:
        n = stage_source(Path(tmp))
        print(f"上傳 {n} 個檔案並建置映像（約 2–3 分鐘）…")
        cmd = [npx, "-y", CLI, "deploy", "--service-id", service, "--environment-id", env, "-i=false", "--auto_check_update=false"]
        if subprocess.run(cmd, cwd=tmp, env={**os.environ, "ZEABUR_TOKEN": token}).returncode != 0:
            raise SystemExit("Zeabur CLI 上傳失敗（見上方訊息）")


def wait_for_deployment(token: str, service: str, env: str, since: float, timeout_s: float = 1200) -> str:
    query = "query($s:ObjectID!,$e:ObjectID!){ deployments(serviceID:$s, environmentID:$e, perPage:1){ edges { node { _id status createdAt } } } }"
    last, settling_since = None, None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        edges = gql(token, query, s=service, e=env)["deployments"]["edges"]
        if edges:
            node = edges[0]["node"]
            created = calendar.timegm(time.strptime(node["createdAt"][:19], "%Y-%m-%dT%H:%M:%S"))
            if created >= since - 120:
                status = node["status"]
                if status != last:
                    print(f"  部署 {node['_id']}: {status}")
                    last = status
                if status in FINAL:
                    return status
                if status in SETTLING:
                    settling_since = settling_since or time.time()
                    if time.time() - settling_since > 120:
                        return status
                else:
                    settling_since = None
        time.sleep(5)
    return last or "UNKNOWN"


def ensure_health_check(token: str, service: str, env: str) -> None:
    for _ in range(60):
        ports = gql(token, "query($s:ObjectID!,$e:ObjectID!){ service(_id:$s){ ports(environmentID:$e){ id } } }", s=service, e=env)["service"]["ports"] or []
        if any(p["id"] == "web" for p in ports):
            gql(token, 'mutation($s:ObjectID!){ updateServiceHealthCheckV2(serviceID:$s, healthCheck:{type: HTTP, port:"web", http:{path:"/healthz"}}) }', s=service)
            return
        time.sleep(5)
    print("  （未偵測到 web 連接埠，略過健康檢查設定）")


def ensure_domain(token: str, service: str, env: str, prefix: str) -> str:
    domains = gql(token, "query($s:ObjectID!,$e:ObjectID!){ service(_id:$s){ domains(environmentID:$e){ domain } } }", s=service, e=env)["service"]["domains"]
    if domains:
        return domains[0]["domain"]
    for _ in range(5):
        try:
            return gql(
                token,
                'mutation($s:ObjectID!,$e:ObjectID!,$d:String!){ addDomain(serviceID:$s, environmentID:$e, domain:$d, isGenerated:true, portName:"web"){ domain } }',
                s=service, e=env, d=f"{prefix}-{suffix()}",
            )["addDomain"]["domain"]
        except RuntimeError as exc:
            last = exc
    raise SystemExit(f"無法建立網域：{last}")


def report(url: str, password: str | None) -> None:
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=15) as resp:
                health = json.loads(resp.read())
            if health.get("phase") == "running":
                break
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass
        time.sleep(10)
    else:
        print("  （10 分鐘內未見 running，請到 Zeabur 查看執行日誌）")
        return
    print(f"  引擎運行中：mode={health['mode']} version={health['version']}")
    if not password:
        return
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    login = urllib.request.Request(f"{url}/api/login", data=json.dumps({"password": password}).encode(), headers={"Content-Type": "application/json"})
    try:
        opener.open(login, timeout=15)
        state = json.loads(opener.open(f"{url}/api/state", timeout=30).read())
    except urllib.error.URLError as exc:
        print(f"  （無法登入讀取轉實盤清單：{exc}；若剛改過密碼，請在 Zeabur 重新啟動服務）")
        return
    geo = state.get("geo") or {}
    print(f"  地區檢查：{geo.get('country', '?')} blocked={geo.get('blocked')}")
    for item in state.get("readiness", []):
        print(f"    {'✓' if item['ok'] else '✗'} {item['id']}{' ' + item['detail'] if item.get('detail') else ''}")
    if state.get("live_blocked"):
        print(f"  實盤被拒原因：{', '.join(state['live_blocked'])}（系統維持紙盤）")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--region", help="Zeabur shared region id, e.g. hkg1 (Hong Kong), cgk1 (Jakarta)")
    where.add_argument("--server", help="Zeabur dedicated server id")
    ap.add_argument("--project", default="polyarb")
    ap.add_argument("--service", default="polyarb")
    ap.add_argument("--cpu", type=int, default=800, help="CPU limit in millicores (default 800)")
    ap.add_argument("--memory", type=int, default=1536, help="memory limit in MiB (default 1536)")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="set/overwrite a variable (repeatable)")
    ap.add_argument("--no-upload", action="store_true", help="only (re)configure; skip the code upload")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    token = os.environ.get("ZEABUR_TOKEN", "").strip()
    if not token:
        raise SystemExit("請先設定 ZEABUR_TOKEN 環境變數")
    overrides = dict(item.split("=", 1) for item in args.env)
    if any(k.startswith("POLYMARKET_") for k in overrides):
        raise SystemExit("錢包相關的機密請直接在 Zeabur 控制台輸入，不要經過指令列")
    region = args.region or f"server-{args.server}"

    project, env = find_or_create_project(token, args.project, region)
    service = find_or_create_service(token, project, args.service)
    wanted = dict(DEFAULT_VARS)
    password = read_variable(token, service, env, "POLYARB_DASHBOARD_PASSWORD")
    generated = password is None
    if generated:
        password = secrets.token_urlsafe(15)
        wanted["POLYARB_DASHBOARD_PASSWORD"] = password
    ensure_variables(token, service, env, wanted, overrides)
    password = overrides.get("POLYARB_DASHBOARD_PASSWORD", password)

    volumes = gql(token, "query($s:ObjectID!,$e:ObjectID!){ service(_id:$s){ volumes(environmentID:$e){ dir } } }", s=service, e=env)["service"]["volumes"]
    if not any(v["dir"] == "/data" for v in volumes):
        gql(token, 'mutation($s:ObjectID!){ mountVolume(serviceID:$s, id:"data", dir:"/data") }', s=service)
        print("  已掛載永久磁碟 /data")
    gql(token, "mutation($s:ObjectID!,$c:Int,$m:Int){ updateServiceResourceLimit(serviceID:$s, resourceLimit:{cpu:$c, memory:$m}) }", s=service, c=args.cpu, m=args.memory)
    print(f"  資源上限 {args.cpu}m CPU / {args.memory} MiB")

    if not args.no_upload:
        started = time.time()
        upload(token, service, env)
        status = wait_for_deployment(token, service, env, started)
        if status != "RUNNING":
            raise SystemExit(f"部署狀態 {status}：請到 Zeabur 查看建置日誌")
    ensure_health_check(token, service, env)
    url = "https://" + ensure_domain(token, service, env, args.service)
    print(f"\n控制台：{url}")
    print("密碼：" + (password if generated else "（沿用 Zeabur 變數 POLYARB_DASHBOARD_PASSWORD）"))
    report(url, password)
    return 0


if __name__ == "__main__":
    sys.exit(main())
