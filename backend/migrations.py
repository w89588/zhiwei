"""Ordered application migrations; engine upgrades are managed outside the application."""
from dataclasses import dataclass
import re
import time


class MigrationError(RuntimeError):
    pass


class CompatibilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: object
    retry_safe: bool = False


def check_engine_version(engine, version):
    match = re.match(r'^(\d+)\.(\d+)', version)
    supported = {'mysql': ((8, 0), (10, 0)), 'mongodb': ((6, 0), (9, 0))}
    minimum, maximum = supported[engine]
    if not match or 'mariadb' in version.lower() or not minimum <= tuple(map(int, match.groups())) < maximum:
        raise CompatibilityError(f'{engine} 版本 {version[:60]} 不在支持范围：{minimum[0]}.{minimum[1]} 至 {maximum[0]}.0（不含）')
    return version


def validate_history(applied, migrations):
    expected = [m.version for m in migrations]
    if expected != list(range(1, len(expected) + 1)):
        raise MigrationError('迁移定义必须从 1 开始连续递增')
    if sorted(applied) != expected[:len(applied)]:
        raise MigrationError('数据库结构版本高于当前程序或迁移记录不连续，请使用匹配的镜像')


def run_sql_migrations(con, history, runs, migrations):
    from sqlalchemy import select
    history.create(con, checkfirst=True)
    runs.create(con, checkfirst=True)
    applied = list(con.execute(select(history.c.version)).scalars())
    validate_history(applied, migrations)
    durable = con.dialect.name != 'sqlite'
    if durable:
        con.commit()
    for migration in migrations:
        if migration.version in applied:
            continue
        previous = con.execute(select(runs).where(runs.c.version == migration.version)).mappings().first()
        if previous and previous['state'] in {'running', 'failed'} and not migration.retry_safe:
            raise MigrationError(f'SQL 迁移 {migration.version} 上次未完成，需要检查数据库后修复')
        values = dict(name=migration.name, state='running', started_at=int(time.time()), finished_at=0)
        if previous:
            con.execute(runs.update().where(runs.c.version == migration.version).values(**values))
        else:
            con.execute(runs.insert().values(version=migration.version, **values))
        if durable:
            con.commit()
        try:
            migration.apply(con)
            con.execute(history.insert().values(version=migration.version, applied_at=int(time.time())))
            con.execute(runs.update().where(runs.c.version == migration.version).values(state='done', finished_at=int(time.time())))
            if durable:
                con.commit()
        except Exception:
            if durable:
                con.rollback()
                con.execute(runs.update().where(runs.c.version == migration.version).values(state='failed', finished_at=int(time.time())))
                con.commit()
            raise MigrationError(f'SQL 迁移 {migration.version}（{migration.name}）失败；未启动服务') from None
    return len(migrations)
