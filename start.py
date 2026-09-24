"""Local launcher: migrate, serve loopback, open browser. Ctrl+C stops it."""
import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description='启动危险驾驶监测平台')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口必须为1至65535。')
    os.chdir(Path(__file__).resolve().parent)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        import django
    except ImportError:
        print('请先安装依赖：python -m pip install -r requirements.txt')
        return 1
    django.setup()
    from django.core.management import call_command
    from django.conf import settings
    call_command('migrate', interactive=False, verbosity=0)
    call_command('check')
    url = f'http://127.0.0.1:{args.port}/'
    print(f'访问地址：{url}', flush=True)
    if settings.DEBUG:
        print('首次使用请在网页创建管理账户。按 Ctrl+C 停止服务。', flush=True)
    else:
        print('生产模式：请先使用 createsuperuser 创建账户，并通过本机 HTTPS 代理访问。', flush=True)
    if not args.no_browser:
        timer = threading.Timer(1.5, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    if settings.DEBUG:
        call_command('runserver', f'127.0.0.1:{args.port}', use_reloader=False)
    else:
        from waitress import serve
        from config.wsgi import application
        serve(application, host='127.0.0.1', port=args.port, threads=4,
              trusted_proxy='127.0.0.1', trusted_proxy_headers='x-forwarded-proto',
              max_request_body_size=6*1024*1024)
    return 0

if __name__ == '__main__':
    sys.exit(main())
