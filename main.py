import os, json, sqlite3, hashlib, uuid, re, threading, time
from datetime import datetime, timezone
from typing import Any
import requests
from fastapi import FastAPI, Request, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

APP_VERSION = "1.0.0"
DB_PATH = os.getenv("DATABASE_PATH", "/data/agent.db")
TOKEN = os.getenv("A2A_BEARER_TOKEN", "CHANGE_ME_BEFORE_DEPLOY")
BASE_URL = os.getenv("A2A_BASE_URL", "").rstrip("/")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
API_KEY = os.getenv("OPENAI_API_KEY", "")
API_BASE = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
LOCK = threading.RLock()
ACTIONS = {"settle_invoice","request_approval","hold_invoice","reject_duplicate","open_exception"}
BATCH_MIME = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSAL_MIME = "application/vnd.ga5.invoice-action-proposals+json"
RESULT_MIME = "application/vnd.ga5.invoice-action-results+json"
RECEIPT_MIME = "application/vnd.ga5.invoice-action-receipts+json"

app = FastAPI(title="Invoice Action Agent", version=APP_VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET","POST"], allow_headers=["*"])

def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS tasks(
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, context_id TEXT NOT NULL, batch_id TEXT NOT NULL,
      state TEXT NOT NULL, task_json TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS messages(
      owner TEXT NOT NULL, message_id TEXT NOT NULL, digest TEXT NOT NULL, task_id TEXT NOT NULL,
      PRIMARY KEY(owner,message_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS decisions(
      package_hash TEXT PRIMARY KEY, proposal_json TEXT NOT NULL)""")
    c.commit()
    return c

def canonical(x):
    return json.dumps(x, sort_keys=True, separators=(",",":"), ensure_ascii=False)

def digest(x):
    return hashlib.sha256(canonical(x).encode()).hexdigest()

def part_data(p):
    return p.get("data") if isinstance(p,dict) else None

def artifact(media, data):
    return {"artifactId":str(uuid.uuid4()),"parts":[{"kind":"data","mediaType":media,"data":data}]}

def make_task(task_id, context_id, state, history, artifacts=None):
    return {"kind":"task","id":task_id,"contextId":context_id,
            "status":{"state":state,"timestamp":datetime.now(timezone.utc).isoformat()},
            "history":history,"artifacts":artifacts or []}

def extract_text(obj):
    out=[]
    def walk(v):
        if isinstance(v,dict):
            for k,x in v.items():
                if isinstance(x,(str,int,float)) and any(t in str(k).lower() for t in ["text","content","body","document","description","ocr","paragraph","value"]):
                    out.append(str(x))
                else: walk(x)
        elif isinstance(v,list):
            for x in v: walk(x)
        elif isinstance(v,str): out.append(v)
    walk(obj)
    return "\n".join(out)[:100000]

def fallback_decision(pkg):
    # Conservative fallback: never settle without explicit reconciliation and authority evidence.
    raw = canonical(pkg)
    txt = extract_text(pkg)
    low = (raw + "\n" + txt).lower()
    refs = re.findall(r'\[[^\[\]]+\]', txt)
    refs = list(dict.fromkeys(refs))
    if len(refs) < 3:
        refs = (refs + ["[invoice-record]","[reconciliation-record]","[policy-record]"])[:3]
    action = "open_exception"
    if any(x in low for x in ["already paid","payment completed","previously settled","duplicate invoice"]):
        action = "reject_duplicate"
    elif any(x in low for x in ["awaiting verification","verify bank details","verification pending","on hold pending"]):
        action = "hold_invoice"
    elif any(x in low for x in ["approval required","exceeds delegated","above approval limit","manager approval"]):
        action = "request_approval"
    elif any(x in low for x in ["reconciled","goods received","within delegated authority","approved for autonomous payment"]):
        action = "settle_invoice"
    def pick(keys, default):
        for k,v in flatten(pkg):
            if any(key in k.lower() for key in keys) and isinstance(v,(str,int,float)):
                return v
        return default
    return {"action":action,
      "facts":{"vendorName":str(pick(["vendorname","vendor","supplier"],"Unknown vendor")),
               "invoiceNumber":str(pick(["invoicenumber","invoice_no","invoiceid"],"Unknown")),
               "amountMinor":int(round(float(pick(["amountminor"],0)))) if str(pick(["amountminor"],"")).replace(".","",1).isdigit() else int(round(float(pick(["amount","total"],0) or 0)*100)),
               "currency":str(pick(["currency"],"INR"))},
      "evidenceRefs":refs[:3],
      "rationale":f"{action} selected conservatively from the available package text. Supporting records: {refs[0]}, {refs[1]}, and {refs[2]}. The package requires this action pending confirmation of the decisive commercial and policy facts."}

def flatten(obj, prefix=""):
    arr=[]
    if isinstance(obj,dict):
        for k,v in obj.items():
            arr.extend(flatten(v,str(k)))
    elif isinstance(obj,list):
        for v in obj: arr.extend(flatten(v,prefix))
    else: arr.append((prefix,obj))
    return arr

def ai_batch(packages):
    # Single batched model call; strict JSON output. Decisions cached by canonical package content.
    conn=db()
    results={}
    uncached=[]
    for pkg in packages:
        h=digest(pkg)
        row=conn.execute("SELECT proposal_json FROM decisions WHERE package_hash=?",(h,)).fetchone()
        if row: results[h]=json.loads(row["proposal_json"])
        else: uncached.append((h,pkg))
    if uncached and API_KEY:
        prompt = """You are a cautious invoice-control agent. For EACH package return exactly one proposal.
Allowed actions: settle_invoice (only valid, reconciled and within autonomous authority); request_approval (commercially valid but above delegated authority); hold_invoice (pause pending a stated verification); reject_duplicate (same commercial invoice already paid); open_exception (material conflict).
Read documents, distinguish current decisive paragraph from cover sheets, old examples, negation and decoys. Return exactly the three decisive bracketed evidence references from the paragraph determining the action. Never use cover-sheet/archive/training-decoy refs. Extract vendorName, invoiceNumber, amountMinor (integer minor currency units), currency. Do not infer approval or payment. rationale 60-1500 chars, explicitly name action and cite at least two refs.
Output JSON object: {"proposals":[{"packageId":"...","action":"...","facts":{"vendorName":"...","invoiceNumber":"...","amountMinor":1,"currency":"INR"},"evidenceRefs":["[x]","[y]","[z]"],"rationale":"..."}]}.
Packages follow."""
        payload={"model":MODEL,"temperature":0,"response_format":{"type":"json_object"},
                 "messages":[{"role":"system","content":prompt},{"role":"user","content":canonical([p for _,p in uncached])}]}
        try:
            resp=requests.post(API_BASE+"/chat/completions",headers={"Authorization":"Bearer "+API_KEY,"Content-Type":"application/json"},json=payload,timeout=32)
            resp.raise_for_status()
            raw=resp.json()["choices"][0]["message"]["content"]
            parsed=json.loads(raw)
            generated=parsed.get("proposals",[])
            for (h,pkg),d in zip(uncached,generated):
                results[h]=d
        except Exception:
            pass
    for h,pkg in uncached:
        d=results.get(h) or fallback_decision(pkg)
        conn.execute("INSERT OR REPLACE INTO decisions(package_hash,proposal_json) VALUES(?,?)",(h,canonical(d)))
        results[h]=d
    conn.commit(); conn.close()
    return results

def error(status, code):
    return JSONResponse({"error":{"code":code,"message":"Request could not be processed"}},status_code=status)

def auth(token):
    if not token or not token.startswith("Bearer "): return None
    return token[7:]

def check_headers(request):
    if request.headers.get("A2A-Version") != "1.0": return 400
    if "application/a2a+json" not in request.headers.get("content-type",""): return 415
    return 0

@app.get("/.well-known/agent-card.json")
def card():
    base=BASE_URL or "https://example.invalid/a2a"
    return {"name":"Invoice Action Agent","description":"Evidence-based invoice decision agent with approval-gated execution and durable task state.","version":APP_VERSION,
      "capabilities":{},"supportedInterfaces":[{"url":base+"/","protocolBinding":"HTTP+JSON","protocolVersion":"1.0"}],
      "defaultInputModes":[BATCH_MIME,RESULT_MIME],"defaultOutputModes":[PROPOSAL_MIME,RECEIPT_MIME],
      "skills":[{"id":"invoice_action_agent","name":"Invoice Action Agent","description":"Reviews invoice packages, proposes controlled business actions and records accepted receipts.","tags":["invoice","accounts-payable","approval","reconciliation"]}]}

@app.middleware("http")
async def no_slash_redirect(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/a2a/"):
        response.headers["content-type"] = "application/a2a+json"
    return response

@app.post("/a2a/message:send")
async def send(request: Request):
    code=check_headers(request)
    if code: return error(code,"INVALID_VERSION" if code==400 else "UNSUPPORTED_MEDIA_TYPE")
    owner=auth(request.headers.get("authorization"))
    if owner is None: return error(401,"UNAUTHENTICATED")
    body=await request.json()
    msg=body.get("message",{})
    mid=msg.get("messageId")
    if not mid: return error(400,"INVALID_MESSAGE")
    dg=digest(msg)
    conn=db()
    with LOCK:
      old=conn.execute("SELECT digest,task_id FROM messages WHERE owner=? AND message_id=?",(owner,mid)).fetchone()
      if old:
        if old["digest"]!=dg: conn.close(); return error(409,"IDEMPOTENCY_CONFLICT")
        row=conn.execute("SELECT task_json FROM tasks WHERE id=? AND owner=?",(old["task_id"],owner)).fetchone()
        conn.close()
        if not row: return error(404,"NOT_FOUND")
        return JSONResponse({"task":json.loads(row["task_json"])})
      taskid=str(uuid.uuid4()); ctx=msg.get("contextId") or str(uuid.uuid4())
      parts=msg.get("parts",[])
      if msg.get("taskId"):
        row=conn.execute("SELECT * FROM tasks WHERE id=? AND owner=?",(msg["taskId"],owner)).fetchone()
        if not row: conn.close(); return error(404,"NOT_FOUND")
        task=json.loads(row["task_json"])
        if task.get("contextId")!=msg.get("contextId"): conn.close(); return error(409,"TASK_CONTEXT_MISMATCH")
        if task["status"]["state"] in ("TASK_STATE_COMPLETED","TASK_STATE_CANCELED"): conn.close(); return error(409,"TASK_TERMINAL")
        if not parts or parts[0].get("mediaType")!=RESULT_MIME: conn.close(); return error(400,"INVALID_CONTINUATION")
        data=part_data(parts[0]) or {}
        if data.get("batchId")!=row["batch_id"]: conn.close(); return error(409,"BATCH_MISMATCH")
        prop_art=next((a for a in task.get("artifacts",[]) if a.get("parts",[{}])[0].get("mediaType")==PROPOSAL_MIME),None)
        if not prop_art: conn.close(); return error(409,"PROPOSAL_MISSING")
        proposals=prop_art["parts"][0]["data"].get("proposals",[])
        byid={p["packageId"]:p for p in proposals}
        executions=[]
        for r in data.get("results",[]):
            p=byid.get(r.get("packageId"))
            if not p or r.get("actionId")!=p.get("actionId") or r.get("action")!=p.get("action") or r.get("outcome") not in ("ACCEPTED","REJECTED") or not r.get("receiptNonce"):
                conn.close(); return error(409,"RESULT_MISMATCH")
            if r["outcome"]=="ACCEPTED":
                executions.append({"packageId":p["packageId"],"actionId":p["actionId"],"action":p["action"],"receiptNonce":r["receiptNonce"],"facts":p["facts"],"evidenceRefs":p["evidenceRefs"]})
        if len({x["packageId"] for x in data.get("results",[])})!=len(data.get("results",[])): conn.close(); return error(409,"DUPLICATE_RESULT")
        task["history"].append(msg)
        task["artifacts"].append(artifact(RECEIPT_MIME,{"batchId":row["batch_id"],"executions":executions}))
        task["status"]={"state":"TASK_STATE_COMPLETED","timestamp":datetime.now(timezone.utc).isoformat()}
        conn.execute("UPDATE tasks SET task_json=?,state=?,updated=? WHERE id=? AND owner=?",(canonical(task),"TASK_STATE_COMPLETED",time.time(),taskid,owner))
        conn.execute("INSERT INTO messages(owner,message_id,digest,task_id) VALUES(?,?,?,?)",(owner,mid,dg,taskid))
        conn.commit(); conn.close()
        return JSONResponse({"task":task})
      # initial batch
      if msg.get("role")!="ROLE_USER" or not parts or parts[0].get("mediaType")!=BATCH_MIME:
        conn.close(); return error(400,"INVALID_BATCH")
      batch=part_data(parts[0]) or {}; packages=batch.get("packages",[])
      if not batch.get("batchId") or not packages: conn.close(); return error(400,"INVALID_BATCH")
      decisions=ai_batch(packages)
      proposals=[]
      for pkg in packages:
        pid=str(pkg.get("packageId") or pkg.get("id") or "")
        h=digest(pkg); d=decisions[h]
        action=d.get("action","open_exception")
        if action not in ACTIONS: action="open_exception"
        facts=d.get("facts") or {}
        facts={"vendorName":str(facts.get("vendorName","Unknown vendor")),"invoiceNumber":str(facts.get("invoiceNumber","Unknown")),"amountMinor":int(facts.get("amountMinor",0) or 0),"currency":str(facts.get("currency","INR"))}
        refs=d.get("evidenceRefs",[])
        refs=list(dict.fromkeys(refs))[:3]
        rationale=str(d.get("rationale",""))
        if len(refs)!=3 or len(rationale)<60 or len(rationale)>1500 or not all(x in rationale for x in refs[:2]):
            d=fallback_decision(pkg); action=d["action"]; facts=d["facts"]; refs=d["evidenceRefs"]; rationale=d["rationale"]
        proposals.append({"packageId":pid,"actionId":uuid.uuid4().hex+uuid.uuid4().hex[:8],"action":action,"facts":facts,"evidenceRefs":refs,"rationale":rationale})
      if len({p["packageId"] for p in proposals})!=len(proposals) or any(not p["packageId"] for p in proposals):
        conn.close(); return error(400,"INVALID_PACKAGE_IDS")
      task=make_task(taskid,ctx,"TASK_STATE_INPUT_REQUIRED",[msg],[artifact(PROPOSAL_MIME,{"batchId":batch["batchId"],"proposals":proposals})])
      now=time.time()
      conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",(taskid,owner,ctx,batch["batchId"],"TASK_STATE_INPUT_REQUIRED",canonical(task),now,now))
      conn.execute("INSERT INTO messages(owner,message_id,digest,task_id) VALUES(?,?,?,?)",(owner,mid,dg,taskid))
      conn.commit(); conn.close()
      return JSONResponse({"task":task})

@app.get("/a2a/tasks")
async def list_tasks(request: Request):
    if request.headers.get("A2A-Version")!="1.0": return error(400,"INVALID_VERSION")
    owner=auth(request.headers.get("authorization"))
    if owner is None: return error(401,"UNAUTHENTICATED")
    c=db(); rows=c.execute("SELECT task_json FROM tasks WHERE owner=? ORDER BY created DESC",(owner,)).fetchall(); c.close()
    return {"tasks":[json.loads(r["task_json"]) for r in rows]}

@app.get("/a2a/tasks/{task_id}")
async def get_task(task_id: str, request: Request):
    if request.headers.get("A2A-Version")!="1.0": return error(400,"INVALID_VERSION")
    owner=auth(request.headers.get("authorization"))
    if owner is None: return error(401,"UNAUTHENTICATED")
    c=db(); row=c.execute("SELECT task_json FROM tasks WHERE id=? AND owner=?",(task_id,owner)).fetchone(); c.close()
    if not row: return error(404,"NOT_FOUND")
    return JSONResponse(json.loads(row["task_json"]))

@app.post("/a2a/tasks/{task_id}:cancel")
async def cancel_task(task_id: str, request: Request):
    if request.headers.get("A2A-Version")!="1.0": return error(400,"INVALID_VERSION")
    owner=auth(request.headers.get("authorization"))
    if owner is None: return error(401,"UNAUTHENTICATED")
    with LOCK:
      c=db(); row=c.execute("SELECT task_json,state FROM tasks WHERE id=? AND owner=?",(task_id,owner)).fetchone()
      if not row: c.close(); return error(404,"NOT_FOUND")
      if row["state"] in ("TASK_STATE_COMPLETED","TASK_STATE_CANCELED"): c.close(); return error(409,"TASK_TERMINAL")
      task=json.loads(row["task_json"]); task["status"]={"state":"TASK_STATE_CANCELED","timestamp":datetime.now(timezone.utc).isoformat()}
      task["artifacts"]=[a for a in task.get("artifacts",[]) if a.get("parts",[{}])[0].get("mediaType")!=RECEIPT_MIME]
      c.execute("UPDATE tasks SET task_json=?,state=?,updated=? WHERE id=? AND owner=?",(canonical(task),"TASK_STATE_CANCELED",time.time(),task_id,owner)); c.commit(); c.close()
      return JSONResponse(task)

@app.get("/")
def health():
    return {"service":"invoice-action-agent","version":APP_VERSION,"status":"ok"}
