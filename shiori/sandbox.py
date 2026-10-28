"""Explicit local Docker isolation. Missing isolation never falls back to host."""
from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import uuid
from .filesystem import is_link

# Docker Hub library/python OCI index, verified 2026-10-20. Execute the immutable
# digest, even if someone subsequently retags a different local image.
IMAGE_DIGEST = 'sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1'
IMAGE = 'docker.io/library/python:3.12-slim@' + IMAGE_DIGEST


def docker_prefix():
    executable = shutil.which('docker')
    if not executable:
        raise ValueError('未安装 Docker。请安装并启动本机 Docker 的 Linux 容器引擎，再准备沙箱镜像；未运行本机命令。')
    endpoint = 'npipe:////./pipe/docker_engine' if os.name == 'nt' else 'unix:///var/run/docker.sock'
    return [executable, '--host', endpoint]


def availability():
    try:
        prefix = docker_prefix()
        result = subprocess.run(prefix+['info','--format','{{.OSType}}'],capture_output=True,text=True,
            timeout=4,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode or result.stdout.strip()!='linux':
            return {'available':False,'message':'本机 Docker Linux 引擎未就绪，请启动 Docker；不会连接远程引擎或降级为本机执行。'}
        result = subprocess.run(prefix+['image','inspect',IMAGE],capture_output=True,timeout=4,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode:
            return {'available':False,'message':f'沙箱镜像尚未准备。请在本机运行 docker pull {IMAGE}；软件不会自动下载或更换镜像。'}
        try:
            metadata=json.loads(result.stdout)[0]
            digests=metadata.get('RepoDigests') or []
            verified=any(value.rsplit('@',1)[-1]==IMAGE_DIGEST and value.split('@',1)[0] in
                         ('python','library/python','docker.io/library/python') for value in digests if isinstance(value,str))
        except (ValueError,TypeError,KeyError,IndexError):verified=False
        if not verified:
            return {'available':False,'message':f'本地镜像摘要与已核验的官方镜像不符，请运行 docker pull {IMAGE}；未执行代码。'}
        return {'available':True,'image':IMAGE,'digest_verified':True,'message':'本机 Docker Linux 引擎与固定摘要镜像就绪。沙箱默认断网；工作目录为可写映射，里面的文件可被脚本增删。'}
    except (ValueError,OSError,subprocess.TimeoutExpired) as exc:
        return {'available':False,'message':str(exc) if isinstance(exc,ValueError) else 'Docker 检测超时或不可用，请检查本机引擎。'}


def prepare(root: Path, history: Path, name: str, value: str):
    status=availability()
    if not status['available']: raise ValueError(status['message'])
    root=root.resolve()
    if ',' in str(root): raise ValueError('Docker 沙箱工作目录不能含逗号，请另选专用文件夹')
    # Do not hand a pre-existing host junction/symlink to a container file share.
    count=0
    for path in root.rglob('*'):
        count+=1
        if count>20000: raise ValueError('沙箱工作目录超过2万个条目，请选择更小的专用目录')
        if is_link(path):
            raise ValueError('沙箱工作目录含链接或目录联接，请移除后重试')
    identity='shiori-'+uuid.uuid4().hex
    uid=os.getuid() if hasattr(os,'getuid') and os.getuid()!=0 else 65534
    gid=os.getgid() if hasattr(os,'getgid') and os.getgid()!=0 else 65534
    command=docker_prefix()+['run','--rm','--pull=never','--name',identity,'--network','none',
        '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
        '--pids-limit','64','--memory','512m','--cpus','1','--user',f'{uid}:{gid}',
        '--tmpfs','/tmp:rw,noexec,nosuid,size=64m','--env','PYTHONDONTWRITEBYTECODE=1',
        '--mount',f'type=bind,source={root},target=/workspace','--workdir','/workspace']
    if name=='execute_python':
        history.mkdir(parents=True,exist_ok=True)
        script=history/(identity+'.py')
        script.write_text(value,'utf-8')
        if ',' in str(script): raise ValueError('沙箱脚本位置不能含逗号')
        command+=['--mount',f'type=bind,source={script.resolve()},target=/run/approved.py,readonly',
                  IMAGE,'python','-I','-B','/run/approved.py']
    else:
        command += [IMAGE,'/bin/sh','-c',value]
    # Deliberately do not pass the application's environment/credentials through.
    return command, identity


def stop(identity):
    if not identity.startswith('shiori-') or len(identity)!=len('shiori-')+32:
        raise ValueError('沙箱实例标识无效')
    result=subprocess.run(docker_prefix()+['rm','--force',identity],capture_output=True,timeout=10,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        check=subprocess.run(docker_prefix()+['ps','-a','--filter',f'name=^/{identity}$','--format','{{.ID}}'],capture_output=True,timeout=5,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if check.returncode or check.stdout.strip():
            raise RuntimeError('无法确认沙箱已经停止，请在 Docker 中检查该 Shiori 容器，勿重复执行任务')
