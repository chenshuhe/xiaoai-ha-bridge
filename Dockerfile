FROM python:3.12-slim

WORKDIR /app

# 国内用户可取消注释下一行加速 pip 安装
# RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py .
COPY web/ web/
RUN mkdir -p config logs

EXPOSE 47521

CMD ["python", "-u", "bridge.py"]
