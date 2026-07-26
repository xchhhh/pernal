#!/bin/bash
# VPS 端执行：等应用容器健康 → cp 脚本进容器 → 重建索引
set -e
for i in $(seq 1 30); do
  st=$(docker inspect -f '{{.State.Health.Status}}' portfolio-app-1 2>/dev/null || echo none)
  [ "$st" = "healthy" ] && break
  sleep 3
done
docker inspect -f '{{.State.Health.Status}}' portfolio-app-1
docker cp /tmp/_reindex_in_container.py portfolio-app-1:/tmp/reindex.py
docker cp /tmp/eval_run.py portfolio-app-1:/tmp/eval_run.py
docker exec portfolio-app-1 python /tmp/reindex.py 2>&1 | tail -15
