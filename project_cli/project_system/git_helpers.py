from pathlib import Path
import subprocess

def changed_files(root):
    try:
        r=subprocess.run(['git','diff','--name-only','HEAD'],cwd=root,text=True,capture_output=True,check=False)
        if r.returncode!=0: return []
        return [x for x in r.stdout.splitlines() if x.strip()]
    except Exception: return []
