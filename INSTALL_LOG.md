# Install Log

Date: 2025-09-22T19:17:04+08:00

## System
- OS: Debian GNU/Linux 12 (bookworm)
- Kernel: 6.1.0-39-cloud-amd64

## APT packages installed
- mariadb-server, mariadb-client
- python3, python3-venv, python3-pip, python3-dev, python3.11-dev
- build-essential, libssl-dev, libffi-dev, libxml2-dev, libxslt1-dev, zlib1g-dev

## MySQL/MariaDB
- Service: MariaDB
- DB: danmuapi
- User: danmuapi
- Host: 127.0.0.1:3306
- Note: root uses unix_socket auth; app uses dedicated user

## Python
- Python: Python 3.11.2
- Virtualenv: /root/misaka_danmu_server_plus/.venv_run

### Pip freeze
```
aiomysql==0.2.0
annotated-types==0.7.0
anyio==4.10.0
APScheduler==3.11.0
asyncpg==0.30.0
bcrypt==4.0.1
beautifulsoup4==4.13.5
certifi==2025.8.3
cffi==2.0.0
chardet==5.2.0
click==8.3.0
cryptography==46.0.1
ecdsa==0.19.1
fastapi==0.117.1
gmssl==3.2.2
greenlet==3.2.4
h11==0.16.0
httpcore==1.0.9
httptools==0.6.4
httpx==0.28.1
idna==3.10
Levenshtein==0.27.1
lxml==6.0.2
opencc-python-reimplemented==0.1.7
passlib==1.7.4
protobuf==3.20.3
pyasn1==0.6.1
pycparser==2.23
pycryptodome==3.23.0
pycryptodomex==3.23.0
pydantic==2.11.9
pydantic-settings==2.10.1
pydantic_core==2.33.2
PyMySQL==1.1.2
python-dotenv==1.1.1
python-jose==3.5.0
python-Levenshtein==0.27.1
python-multipart==0.0.20
PyYAML==6.0.2
RapidFuzz==3.14.1
rsa==4.9.1
six==1.17.0
sniffio==1.3.1
socksio==1.0.0
soupsieve==2.8
SQLAlchemy==2.0.43
starlette==0.48.0
thefuzz==0.22.1
typing-inspection==0.4.1
typing_extensions==4.15.0
tzlocal==5.3.1
uvicorn==0.36.0
uvloop==0.21.0
watchfiles==1.1.0
websockets==15.0.1
```
