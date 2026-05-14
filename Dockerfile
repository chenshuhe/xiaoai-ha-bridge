FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# 先安装基础依赖，再安装 miservice（其 setup.py 依赖 aiohttp/aiofiles）
RUN pip install --no-cache-dir aiohttp>=3.9.0 aiofiles>=23.0 pyyaml>=6.0 \
    fastapi>=0.110.0 "uvicorn[standard]>=0.27.0" python-multipart>=0.0.9 \
    && pip install --no-cache-dir --no-build-isolation miservice_fork>=2.9.0

COPY bridge.py .
COPY web/ web/

RUN mkdir -p config logs

EXPOSE 47521

CMD ["python", "-u", "bridge.py"]
