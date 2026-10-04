from __future__ import annotations
import json, os, sqlite3, hashlib, time
from pathlib import Path
from ..domain import Event, Evidence, Artifact, Run

class EvidenceStore:
    def __init__(self, root: str|Path="artifacts"):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.db_path=self.root/"patchpilot.sqlite3"; self.jsonl=self.root/"events.jsonl"
        self.db=sqlite3.connect(self.db_path,check_same_thread=False); self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS evidence(evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL);'''); self.db.commit()
    def save_run(self, run:Run):
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)",(run.run_id,json.dumps(run.to_dict(),ensure_ascii=False),time.time())); self.db.commit()
    def event(self,event:Event):
        payload=json.dumps(event.to_dict(),ensure_ascii=False); self.db.execute("INSERT INTO events VALUES (?,?,?,?)",(event.event_id,event.run_id,event.ts,payload)); self.db.commit()
        with self.jsonl.open("a",encoding="utf-8") as f: f.write(payload+"\n")
    def add_evidence(self,run_id:str,evidence:Evidence):
        self.db.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?)",(evidence.evidence_id,run_id,json.dumps(evidence.to_dict(),ensure_ascii=False))); self.db.commit()
    def add_artifact(self,artifact:Artifact):
        self.db.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?)",(artifact.artifact_id,artifact.run_id,json.dumps(artifact.to_dict(),ensure_ascii=False))); self.db.commit()
    def get_run(self,run_id):
        row=self.db.execute("SELECT payload FROM runs WHERE run_id=?",(run_id,)).fetchone(); return json.loads(row[0]) if row else None
    def list_runs(self,limit=100): return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM runs ORDER BY updated_at DESC LIMIT ?",(limit,))]
    def list_events(self,run_id): return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM events WHERE run_id=? ORDER BY ts",(run_id,))]
    def list_evidence(self,run_id): return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM evidence WHERE run_id=?",(run_id,))]
    def list_artifacts(self,run_id): return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM artifacts WHERE run_id=?",(run_id,))]
    def write_artifact(self,run_id,kind,content:bytes,filename:str,metadata=None):
        folder=self.root/run_id; folder.mkdir(exist_ok=True); path=folder/filename; path.write_bytes(content); digest=hashlib.sha256(content).hexdigest(); aid=f"art_{digest[:12]}"
        a=Artifact(aid,kind,str(path),digest,len(content),run_id,metadata or {}); self.add_artifact(a); return a
    def close(self): self.db.close()
