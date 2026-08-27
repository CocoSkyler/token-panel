FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY tokenpanel_cloud.py huawei_push.py ./

# 数据目录挂持久盘
RUN mkdir -p /app/data
VOLUME /app/data

EXPOSE 8080
CMD ["python", "tokenpanel_cloud.py"]
