from __future__ import annotations
import subprocess, time, os, shutil, json
from pathlib import PurePosixPath
from dataclasses import dataclass
@dataclass
class CommandResult:
    command:list[str]; returncode:int; stdout:str; stderr:str; duration_sec:float; timed_out:bool=False
    @property
    def ok(self): return self.returncode==0 and not self.timed_out
class Harness:
    ALLOW={'python','python3','pytest','git','rg','cat','find','ruff'}
    def __init__(self,root='.',timeout=60,unsafe_local=False,image='patchpilot/python:3.12',network=False,pythonpath=()):
        self.root=os.path.abspath(root); self.timeout=timeout; self.unsafe_local=unsafe_local; self.image=image; self.network=network
        self.docker=shutil.which('docker') is not None
        self.pythonpath=[self._rel(p) for p in pythonpath or ()]
    @staticmethod
    def _rel(p):
        q=PurePosixPath(str(p))
        if q.is_absolute() or '..' in q.parts or not q.parts: raise ValueError(f'pythonpath entry must be a relative path inside the workspace: {p}')
        return str(q)
    def _pythonpath(self,base,deps):
        return ':'.join(([f'{base}/.deps'] if deps else [])+[f'{base}/{p}' for p in self.pythonpath])
    def docker_ready(self):
        if not self.docker: return False
        try: return subprocess.run(['docker','info'],capture_output=True,timeout=4).returncode == 0
        except Exception: return False
    def _docker_image_ready(self):
        try: return subprocess.run(['docker','image','inspect',self.image],capture_output=True,timeout=4).returncode == 0
        except Exception: return False
    def run(self,command):
        parts=command if isinstance(command,list) else command.split()
        if not parts or os.path.basename(parts[0]) not in self.ALLOW: return CommandResult(parts,126,'',f'command not allowed: {parts[0] if parts else ""}',0)
        start=time.time()
        if not self.unsafe_local:
            if not self.docker_ready(): return CommandResult(parts,125,'','docker daemon unavailable; use --unsafe-local only for development',0)
            if not self._docker_image_ready(): return CommandResult(parts,125,'',f'docker image unavailable: {self.image}; build with docker build -t {self.image} -f docker/patchpilot.Dockerfile .',0)
            pp=self._pythonpath('/workspace',os.path.isdir(os.path.join(self.root,'.deps')))
            deps=['-e',f'PYTHONPATH={pp}'] if pp else []
            command=['docker','run','--rm','--network','bridge' if self.network else 'none','--cap-drop=ALL','--security-opt=no-new-privileges','--cpus','1.0','--memory','768m','--pids-limit','256','--read-only','--tmpfs','/tmp:rw,noexec,nosuid,size=256m','-e','HOME=/tmp','-e','PIP_CACHE_DIR=/tmp/pip']+deps+['-v',f'{self.root}:/workspace:rw','-w','/workspace',self.image]+parts
        else: command=parts
        try:
            env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME'}}
            env.update({'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'})
            if self.unsafe_local and self.pythonpath: env['PYTHONPATH']=self._pythonpath(self.root,os.path.isdir(os.path.join(self.root,'.deps')))
            p=subprocess.run(command,cwd=self.root,text=True,capture_output=True,timeout=self.timeout,env=env)
            return CommandResult(command,p.returncode,p.stdout[-1_000_000:],p.stderr[-1_000_000:],time.time()-start)
        except subprocess.TimeoutExpired as e:return CommandResult(parts,124,e.stdout or '',e.stderr or '',time.time()-start,True)
