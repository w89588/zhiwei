"""Create a new deployment configuration without printing or replacing secrets."""
import argparse
from pathlib import Path
import secrets


def initialize(destination):
    template = (Path(__file__).resolve().parents[1] / '.env.example').read_text('utf-8')
    for name in ('MYSQL_PASSWORD', 'MYSQL_ROOT_PASSWORD', 'MONGO_PASSWORD', 'REDIS_PASSWORD', 'MASTER_KEY', 'ADMIN_PASSWORD'):
        template = template.replace('CHANGE_' + name, secrets.token_urlsafe(32))
    with Path(destination).open('x', encoding='utf-8', newline='') as target:
        target.write(template)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.env'))
    args = parser.parse_args()
    initialize(args.output)
    print(f'配置已生成：{args.output}；请按部署环境修改连接地址与允许访问的 Host。')
