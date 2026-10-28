"""Generate a reviewed HTTPS proxy configuration without exposing an endpoint."""
import re
from urllib.parse import urlsplit


def https_guide(public_url, port):
    parsed=urlsplit(str(public_url))
    host=parsed.hostname or ''
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.path.strip('/')
            or parsed.query or parsed.fragment or not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?',host)
            or '.' not in host or parsed.port not in (None,443) or host in ('localhost',)):
        raise ValueError('请填写可解析到运行电脑的HTTPS域名，例如 https://bot.example.com；不要填写路径、IP或密钥')
    if type(port) is not int or not 1<=port<=65535: raise ValueError('后台端口无效')
    return {'public_url':'https://'+host,'allowed_hosts':[host],
        'caddyfile':f'{host} {{\n    reverse_proxy 127.0.0.1:{port}\n}}\n',
        'steps':['先将自己的域名解析到代理主机，并按网络环境配置证书验证所需端口。',
            '从 https://caddyserver.com/docs/install 安装Caddy，保存上方内容为Caddyfile，在运行电脑上执行 caddy run --config Caddyfile。',
            '在登录与远程管理中填写相同HTTPS入口并保存；重启Shiori后通过域名登录。',
            '默认要求Caddy位于本机，保留Host并设置X-Forwarded-Proto=https；容器部署需将固定代理IP填入代理高级设置。不要将后端HTTP端口直接暴露到公网。'],
        'verified':False,'message':'这里只生成部署配置，不代表域名、证书或公网连通性已经验证。'}
