"""Full offline upgrade. Only manifest-listed program files are replaced."""
import argparse,hashlib,json,os,shutil,subprocess,sys,uuid,zipfile
from pathlib import Path
from contextlib import ExitStack,contextmanager
from datetime import datetime

SKIP={'backups','temp','__pycache__','cache','.venv'}
LOCKS={'.workspace.lock','.run.lock'}
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def files(root):
    for base,dirs,names in os.walk(root,followlinks=False):
        base=Path(base)
        for d in dirs:
            if (base/d).is_symlink() or (base/d).is_junction():raise ValueError('目录包含链接，请先核对：'+str(base/d))
        dirs[:]=[d for d in dirs if d not in SKIP]
        for name in names:
            p=base/name
            if p.is_symlink():raise ValueError('文件包含链接：'+str(p))
            if name not in LOCKS:yield p

@contextmanager
def lock(path):
    import msvcrt
    f=path.open('a+b')
    try:
        if f.seek(0,2)==0:f.write(b'0');f.flush()
        f.seek(0)
        try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise ValueError('程序或账号仍在运行，请正常退出后再更新。') from None
        try:yield
        finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
    finally:f.close()

def idle(root):
    query=r'''$ErrorActionPreference='Stop'; $r=[IO.Path]::GetFullPath($env:FULL_UPGRADE_ROOT).TrimEnd('\')+'\';
$busy=@(Get-CimInstance Win32_Process | Where-Object {$_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -match '(account_manager|workspace|workspace_worker|run_account|study_window|login_window|pending_quiz_worker)\.py' -and (($_.ExecutablePath -and $_.ExecutablePath.StartsWith($r,[StringComparison]::OrdinalIgnoreCase)) -or ($_.CommandLine.Replace('/','\').IndexOf($r,[StringComparison]::OrdinalIgnoreCase) -ge 0))}); if($busy.Count){exit 2}; exit 0'''
    r=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',query],env={**os.environ,'FULL_UPGRADE_ROOT':str(root)},capture_output=True,timeout=40,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if r.returncode==2:raise ValueError('工作台或账号窗口仍在运行，请正常退出后再安装。')
    if r.returncode:raise ValueError('无法核对运行状态，尚未覆盖程序。')

def replace(src,dst):
    dst.parent.mkdir(parents=True,exist_ok=True)
    tmp=dst.with_name('.upgrade-'+uuid.uuid4().hex+'.tmp')
    try:shutil.copyfile(src,tmp);tmp.replace(dst)
    finally:tmp.unlink(missing_ok=True)

def perform(root,payload,check=idle,fail_after=None):
    root,payload=Path(root).resolve(),Path(payload).resolve()
    if not ((root/'account_manager.py').is_file() and (root/'app/main.py').is_file()):
        raise ValueError('请选择旧程序的根目录，应含 account_manager.py 和 app 文件夹。')
    if payload.is_relative_to(root):raise ValueError('更新源不能放在旧程序目录内。')
    manifest=json.loads((payload.parent/'full-manifest.json').read_text('utf-8'))
    for name,digest in manifest.items():
        rel=Path(name)
        if rel.is_absolute() or '..' in rel.parts or rel.parts[0] not in {'python','app'} and len(rel.parts)!=1:raise ValueError('更新路径无效。')
        if sha(payload/rel)!=digest:raise ValueError('更新文件校验失败：'+name)
    check(root)
    with ExitStack() as stack:
        stack.enter_context(lock(root/'.workspace.lock'))
        stack.enter_context(lock(root/'.run.lock'))
        for directory in sorted((root/'accounts').glob('*')):
            if directory.is_dir():stack.enter_context(lock(directory/'.run.lock'))
        existing=list(files(root))
        existing=list(dict.fromkeys([*existing,*[root/n for n in manifest if (root/n).is_file()]]))
        free=shutil.disk_usage(root).free
        required=sum(p.stat().st_size for p in existing)+sum((payload/n).stat().st_size for n in manifest)+100*1024**2
        if free<required:raise ValueError('磁盘空间不足以完成备份和覆盖，请先腾出空间。')
        backup=root/'backups'/('full-upgrade-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
        backup.mkdir(parents=True)
        original={p.relative_to(root).as_posix():sha(p) for p in existing}
        archive=backup/'before-upgrade.zip'
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
            for p in existing:z.write(p,p.relative_to(root).as_posix())
        with zipfile.ZipFile(archive) as z:
            if z.testzip():raise ValueError('备份校验失败，尚未覆盖程序。')
            for name,digest in original.items():
                if hashlib.sha256(z.read(name)).hexdigest()!=digest:raise ValueError('备份内容不完整。')
        info={'root':str(root),'original':original,'program_files':list(manifest),'new_hashes':manifest,'status':'backup_verified'}
        (backup/'restore-index.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),'utf-8')
        changed=[]
        try:
            for name,digest in manifest.items():
                # This is the sole configuration file shipped in the clean payload.
                if name=='app/config.ini' and (root/name).exists():continue
                changed.append(name)
                replace(payload/name,root/name)
                if sha(root/name)!=digest:raise ValueError('写入校验失败：'+name)
                if fail_after and len(changed)>=fail_after:raise OSError('synthetic failure')
            # Preserve every old non-program file byte-for-byte.
            for name,digest in original.items():
                if name not in changed and sha(root/name)!=digest:raise ValueError('资料保留核对失败：'+name)
            info['status']='completed';info['changed']=changed
            (backup/'restore-index.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),'utf-8')
            (root/'full-upgrade-last.json').write_text(json.dumps({'backup':str(backup),'revision':'1.7.7-full-rounds-20261030'},ensure_ascii=False),'utf-8')
        except Exception:
            with zipfile.ZipFile(archive) as z:
                for name in reversed(changed):
                    target=root/name
                    if name in original:target.write_bytes(z.read(name))
                    else:target.unlink(missing_ok=True)
            info['status']='rolled_back';(backup/'restore-index.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),'utf-8')
            raise
        return {'ok':True,'backup':str(backup),'message':'全量升级成功。账号、设置和记录已保留。请使用原快捷方式打开。'}

def restore(root,backup,check=idle):
    root,backup=Path(root).resolve(),Path(backup).resolve()
    if not backup.is_relative_to(root/'backups'):raise ValueError('备份目录无效。')
    info=json.loads((backup/'restore-index.json').read_text('utf-8'))
    check(root)
    with ExitStack() as stack:
        stack.enter_context(lock(root/'.workspace.lock'));stack.enter_context(lock(root/'.run.lock'))
        for d in (root/'accounts').glob('*'):
            if d.is_dir():stack.enter_context(lock(d/'.run.lock'))
        with zipfile.ZipFile(backup/'before-upgrade.zip') as z:
            for name in info['changed']:
                if name in info['original'] and hashlib.sha256(z.read(name)).hexdigest()!=info['original'][name]:raise ValueError('备份文件校验失败。')
                if sha(root/name)!=info['new_hashes'][name]:raise ValueError('程序升级后又有变化，需先核对：'+name)
            for name in info['changed']:
                if name in info['original']:(root/name).write_bytes(z.read(name))
                else:(root/name).unlink()
    return {'ok':True,'message':'已恢复升级前程序；升级后产生的账号资料继续保留。'}

def main():
    p=argparse.ArgumentParser();p.add_argument('--root',required=True,type=Path);p.add_argument('--payload',type=Path);p.add_argument('--restore',type=Path);p.add_argument('--rollback',action='store_true');p.add_argument('--gui',action='store_true');a=p.parse_args()
    try:
        if a.rollback:
            a.restore=Path(json.loads((a.root/'full-upgrade-last.json').read_text('utf-8'))['backup'])
        r=restore(a.root,a.restore) if a.restore else perform(a.root,a.payload)
    except Exception as e:r={'ok':False,'message':str(e)}
    if a.gui:
        import tkinter as tk
        from tkinter import messagebox
        w=tk.Tk();w.withdraw();(messagebox.showinfo if r['ok'] else messagebox.showerror)('全量升级',r['message']);w.destroy()
    print(json.dumps(r,ensure_ascii=False));return 0 if r['ok'] else 1
if __name__=='__main__':raise SystemExit(main())
