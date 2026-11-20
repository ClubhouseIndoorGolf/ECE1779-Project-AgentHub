# Share the connection pool, while keeping sessions scoped to each operation.
# 共用连接池，但每次操作使用独立会话。

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import database_url, positive_number

# Bound lock waits and statements independently of the worker lease.
# 分别限制锁等待和语句执行时长，不依赖 worker 租约结束。
lock_ms = max(1, int(positive_number("DB_LOCK_TIMEOUT_MS", "5000")))
statement_ms = max(1, int(positive_number("DB_STATEMENT_TIMEOUT_MS", "15000")))
engine = create_engine(
    database_url(),
    pool_pre_ping=True,
    connect_args={
        "options": f"-c timezone=UTC -c lock_timeout={lock_ms} -c statement_timeout={statement_ms}"
    },
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# Release the request session even when a handler raises an exception.
# 即使接口抛出异常，也释放请求使用的数据库会话。
def get_session():
    with SessionLocal() as session:
        yield session
