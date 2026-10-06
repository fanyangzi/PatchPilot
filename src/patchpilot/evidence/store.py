from __future__ import annotations
import json, hashlib, sqlite3, threading, time
from pathlib import Path
from ..domain import Event, Evidence, Artifact, Run
from ..domain.models import compute_event_hash


# ``PatchPilot`` creates one store for the API and another one for the
# orchestrator.  Both connections can be used by different request threads,
# so a lock attached only to an instance is not enough to serialize writes to
# the same SQLite file.  Keep one re-entrant lock per resolved database path;
# this also makes the read-modify-write event hash chain atomic when multiple
# ``EvidenceStore`` instances share an artifact root in one process.
_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


class EvidenceStore:
    def __init__(self, root: str|Path="artifacts"):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.db_path=self.root/"patchpilot.sqlite3"; self.jsonl=self.root/"events.jsonl"
        lock_key=str(self.db_path.resolve())
        with _STORE_LOCKS_GUARD:
            self._lock=_STORE_LOCKS.setdefault(lock_key, threading.RLock())
        # ``check_same_thread=False`` is intentional: FastAPI may dispatch
        # handlers to different worker threads.  Every operation below is
        # protected by ``self._lock``.
        self.db=sqlite3.connect(self.db_path,check_same_thread=False,timeout=30.0)
        self.db.row_factory=sqlite3.Row
        with self._lock:
            self.db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence(evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL);''')
            self.db.commit()

    def save_run(self, run:Run):
        payload=json.dumps(run.to_dict(),ensure_ascii=False)
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)",(run.run_id,payload,time.time()))
            self.db.commit()

    def event(self,event:Event):
        # Reading the previous hash and inserting the new event must happen
        # under one lock/transaction.  Otherwise two concurrent requests can
        # both observe the same predecessor and fork the chain.
        with self._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                prev_hash = self._get_last_event_hash_unlocked(event.run_id)
                event.prev_hash = prev_hash
                event.event_hash = compute_event_hash(event, prev_hash)
                payload=json.dumps(event.to_dict(),ensure_ascii=False)
                self.db.execute("INSERT INTO events VALUES (?,?,?,?)",(event.event_id,event.run_id,event.ts,payload))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            # Keep the append serialized with the database commit so the JSONL
            # audit stream follows the same insertion order as SQLite.
            with self.jsonl.open("a",encoding="utf-8") as f:
                f.write(payload+"\n")

    def get_last_event_hash(self, run_id:str) -> str|None:
        """获取指定 run 的最后一个事件的哈希"""
        with self._lock:
            return self._get_last_event_hash_unlocked(run_id)

    def _get_last_event_hash_unlocked(self, run_id:str) -> str|None:
        # SQLite's implicit rowid is the serialized append sequence.  It is
        # the authoritative order for the hash chain: event timestamps are
        # captured before a worker acquires the lock and may therefore be out
        # of order under concurrent requests.
        row = self.db.execute("SELECT payload FROM events WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,)).fetchone()
        if not row:
            return None
        event_data = json.loads(row["payload"])
        return event_data.get('event_hash')

    def add_evidence(self,run_id:str,evidence:Evidence):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?)",(evidence.evidence_id,run_id,json.dumps(evidence.to_dict(),ensure_ascii=False)))
            self.db.commit()

    def add_artifact(self,artifact:Artifact):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?)",(artifact.artifact_id,artifact.run_id,json.dumps(artifact.to_dict(),ensure_ascii=False)))
            self.db.commit()

    def get_run(self,run_id):
        with self._lock:
            row=self.db.execute("SELECT payload FROM runs WHERE run_id=?",(run_id,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def list_runs(self,limit=100):
        with self._lock:
            rows=self.db.execute("SELECT payload FROM runs ORDER BY updated_at DESC LIMIT ?",(limit,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def list_events(self,run_id):
        with self._lock:
            rows=self.db.execute("SELECT payload FROM events WHERE run_id=? ORDER BY rowid",(run_id,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def list_evidence(self,run_id):
        with self._lock:
            rows=self.db.execute("SELECT payload FROM evidence WHERE run_id=?",(run_id,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def list_artifacts(self,run_id):
        with self._lock:
            rows=self.db.execute("SELECT payload FROM artifacts WHERE run_id=?",(run_id,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def write_artifact(self,run_id,kind,content:bytes,filename:str,metadata=None):
        folder=self.root/run_id
        with self._lock:
            folder.mkdir(exist_ok=True)
            path=folder/filename
            path.write_bytes(content)
        digest=hashlib.sha256(content).hexdigest(); aid=f"art_{digest[:12]}"
        a=Artifact(aid,kind,str(path),digest,len(content),run_id,metadata or {}); self.add_artifact(a); return a

    def close(self):
        with self._lock:
            self.db.close()
